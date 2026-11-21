import re

from ..models import Patient, ActivePatient, patientStatus, Treatment, Beauticians, TreatmentSession, AppUser, AuditLog, PatientNote
from django.http import HttpResponse
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from ..api.serializers import PatientSerializer, PatientSyncSerializer, ActivePatientSerializer, TreatmentSerializer
from ..services.branches import filter_by_branch, write_branch
from ..services import beautician_assignment


def _actor(request):
    return request.user if isinstance(request.user, AppUser) else None


def _release_beauticians(active_patient):
    beautician_assignment.release(active_patient)


class PatientSearchView(APIView):
    def get(self, request):
        qs = Patient.objects.all()

        search = request.GET.get('search', '').strip()
        field = request.GET.get('field', '').strip()
        if search and field:
            if field == 'name':
                qs = qs.filter(name__istartswith=search)
            elif field == 'patient_no':
                qs = qs.filter(patient_no__icontains=search)
            elif field == 'phone':
                qs = qs.filter(phone_number__icontains=search)
            elif field == 'address':
                qs = qs.filter(address__icontains=search)
            else:
                qs = qs.filter(
                    Q(name__istartswith=search) |
                    Q(patient_no__icontains=search) |
                    Q(phone_number__icontains=search)
                )
        else:
            if no := request.GET.get('patient_no', '').strip():
                qs = qs.filter(patient_no__icontains=no)
            if name := request.GET.get('name', '').strip():
                qs = qs.filter(name__istartswith=name)
            if phone := request.GET.get('phone', '').strip():
                qs = qs.filter(phone_number__icontains=phone)
            if address := request.GET.get('address', '').strip():
                qs = qs.filter(address__icontains=address)

        serializer = PatientSerializer(qs.order_by(
            'name', 'patient_no')[:100], many=True)
        return Response(serializer.data)


class PatientCreateWithActiveView(APIView):
    def post(self, request):
        patient_serializer = PatientSerializer(data=request.data)
        if not patient_serializer.is_valid():
            return Response(patient_serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        patient = patient_serializer.save()
        consult_status = bool(request.data.get('consult_status', False))

        active_patient = ActivePatient.objects.create(
            patient_no=patient,
            status=1 if consult_status else 3,
            consult_status=consult_status,
            branch=write_branch(request, locked=True),
        )

        AuditLog.objects.create(
            performed_by=_actor(request),
            action='CREATE',
            entity_type='Patient',
            entity_id=patient.patient_no,
            description=f'New patient registered: {patient.name} ({patient.patient_no})',
        )
        return Response(
            {
                'patient': patient_serializer.data,
                'active_patient': ActivePatientSerializer(active_patient).data,
            },
            status=status.HTTP_201_CREATED,
        )


class ActivePatientClearView(APIView):
    def delete(self, request):
        queue = filter_by_branch(
            ActivePatient.objects.all(), request, locked=True, include_null=False,
        )
        with transaction.atomic():
            for active_patient in queue.filter(status=4):
                _release_beauticians(active_patient)
            deleted_count, _ = queue.delete()
        AuditLog.objects.create(
            performed_by=_actor(request),
            action='DELETE',
            entity_type='ActivePatient',
            entity_id='all',
            description=f'All active patients cleared ({deleted_count} records deleted)',
        )
        return Response(
            {"deleted": deleted_count},
            status=status.HTTP_200_OK
        )


class AppointmentAddView(APIView):
    def post(self, request):
        patient_no = request.data.get('patient_no')
        if not patient_no:
            return Response(
                {"error": "patient_no is required."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            patient = Patient.objects.get(patient_no=patient_no)
        except Patient.DoesNotExist:
            return Response(
                {"error": f"Patient '{patient_no}' not found."},
                status=status.HTTP_404_NOT_FOUND
            )

        consult_status = bool(request.data.get('consult_status', False))

        active_patient = ActivePatient.objects.create(
            patient_no=patient,
            status=1 if consult_status else 3,
            consult_status=consult_status,
            branch=write_branch(request, locked=True),
        )

        visit_type = 'consultation' if consult_status else 'treatment'
        AuditLog.objects.create(
            performed_by=_actor(request),
            action='CREATE',
            entity_type='ActivePatient',
            entity_id=patient.patient_no,
            description=f'Appointment added for {patient.name} ({patient.patient_no}) – {visit_type}',
        )

        serializer = ActivePatientSerializer(active_patient)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class TreatmentQueueView(APIView):
    def get(self, request):
        patients = filter_by_branch(
            ActivePatient.objects.filter(status__in=[3, 4]),
            request, locked=True, include_null=False,
        )
        serializer = ActivePatientSerializer(patients, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class GeneralAppointmentCreateView(APIView):
    def post(self, request):
        guest_name = request.data.get('guest_name', '').strip()
        if not guest_name:
            return Response(
                {"error": "guest_name is required."},
                status=status.HTTP_400_BAD_REQUEST
            )

        consult_status = bool(request.data.get('consult_status', False))

        active_patient = ActivePatient.objects.create(
            patient_no=None,
            guest_name=guest_name,
            status=1 if consult_status else 3,
            consult_status=consult_status,
            branch=write_branch(request, locked=True),
        )

        visit_type = 'consultation' if consult_status else 'treatment'
        AuditLog.objects.create(
            performed_by=_actor(request),
            action='CREATE',
            entity_type='ActivePatient',
            entity_id=str(active_patient.id),
            description=f'General appointment created for {guest_name} – {visit_type}',
        )

        serializer = ActivePatientSerializer(active_patient)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class ActivePatientUpdateStatusView(APIView):
    def post(self, request):
        active_patient_id = request.data.get('active_patient_id')
        target_status = request.data.get('status')

        if active_patient_id is None or target_status is None:
            return Response(
                {"error": "active_patient_id and status are required."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            target_status = int(target_status)
        except (TypeError, ValueError):
            return Response(
                {"error": "status must be an integer."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            active_patient = ActivePatient.objects.get(id=active_patient_id)
        except ActivePatient.DoesNotExist:
            return Response(
                {"error": f"No active patient found with id '{active_patient_id}'."},
                status=status.HTTP_404_NOT_FOUND
            )

        if target_status in (0, 6):
            with transaction.atomic():
                if active_patient.status == 4:
                    _release_beauticians(active_patient)
                if active_patient.patient_no_id:
                    arrive_str = active_patient.visit_time.strftime(
                        '%H:%M') if active_patient.visit_time else 'unknown time'
                    PatientNote.objects.create(
                        patient_no=active_patient.patient_no,
                        date=timezone.now().date(),
                        content=f'Patient arrived at the clinic (check-in: {arrive_str}). Visit was ended early.',
                        author='System',
                    )
                AuditLog.objects.create(
                    performed_by=_actor(request),
                    action='STATUS_CHANGE',
                    entity_type='ActivePatient',
                    entity_id=str(active_patient.id),
                    description=f'ActivePatient #{active_patient.id} dismissed (status {target_status}) — visit note recorded',
                )
                active_patient.delete()
            return Response({'deleted': True}, status=status.HTTP_200_OK)

        with transaction.atomic():
            if active_patient.status == 4 and target_status != 4:
                _release_beauticians(active_patient)
            active_patient.status = target_status
            active_patient.save()

        AuditLog.objects.create(
            performed_by=_actor(request),
            action='STATUS_CHANGE',
            entity_type='ActivePatient',
            entity_id=str(active_patient.id),
            description=f'ActivePatient #{active_patient.id} status → {target_status}',
        )

        serializer = ActivePatientSerializer(active_patient)
        return Response(serializer.data, status=status.HTTP_200_OK)


class TreatmentListView(APIView):
    def get(self, request):
        from ..models import TreatmentCategory
        treatments = Treatment.objects.filter(active=True)
        if request.GET.get('for_beautician') == '1':
            hidden = TreatmentCategory.objects.filter(
                show_to_beautician=False
            ).values_list('name', flat=True)
            treatments = treatments.exclude(category__in=hidden)
        serializer = TreatmentSerializer(treatments, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class TreatmentSessionCreateView(APIView):
    def post(self, request):
        active_patient_id = request.data.get('active_patient_id')
        beautician_id = request.data.get('beautician')
        treatment_ids = request.data.get('treatment_ids', [])

        if not active_patient_id or not beautician_id:
            return Response(
                {"error": "active_patient_id and beautician are required."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            active_patient = ActivePatient.objects.get(id=active_patient_id)
        except ActivePatient.DoesNotExist:
            return Response(
                {"error": f"ActivePatient '{active_patient_id}' not found."},
                status=status.HTTP_404_NOT_FOUND
            )

        try:
            beautician = Beauticians.objects.get(id=beautician_id)
        except Beauticians.DoesNotExist:
            return Response(
                {"error": "Beautician not found."},
                status=status.HTTP_404_NOT_FOUND
            )

        treatment_list = Treatment.objects.filter(
            id__in=treatment_ids, active=True)

        patient = active_patient.patient_no
        branch = write_branch(request, locked=True)
        with transaction.atomic():
            session = TreatmentSession.objects.create(
                active_patient=active_patient,
                patient_no=patient,
                beautician=beautician,
                branch=branch,
            )
            session.treatments.set(treatment_list)

            beautician_assignment.assign(
                active_patient, beautician, actor=_actor(request), branch=branch,
            )

            active_patient.status = 4
            active_patient.save()

        label = patient.name if patient else active_patient.guest_name
        AuditLog.objects.create(
            performed_by=_actor(request),
            action='CREATE',
            entity_type='TreatmentSession',
            entity_id=str(session.id),
            description=f'Treatment session started for {label} with {beautician.beautician_name}',
        )

        return Response({"id": session.id}, status=status.HTTP_201_CREATED)


class BeauticianReassignView(APIView):
    def _payload(self, active_patient):
        rows = beautician_assignment.history(active_patient)
        current = next((a for a in reversed(rows) if a.ended_at is None), None)
        return {
            'active_patient_id': active_patient.id,
            'current_beautician_id': current.beautician_id if current else None,
            'current_beautician_name': (
                current.beautician.beautician_name if current and current.beautician else None
            ),
            'history': [
                {
                    'id': a.id,
                    'beautician_id': a.beautician_id,
                    'beautician_name': a.beautician.beautician_name if a.beautician else None,
                    'assigned_at': a.assigned_at,
                    'ended_at': a.ended_at,
                    'assigned_by': a.assigned_by.display_name if a.assigned_by else None,
                    'reason': a.reason,
                }
                for a in rows
            ],
        }

    def _visit(self, pk):
        try:
            return ActivePatient.objects.get(pk=pk)
        except ActivePatient.DoesNotExist:
            return None

    def get(self, request, pk):
        active_patient = self._visit(pk)
        if active_patient is None:
            return Response({'error': 'Visit not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(self._payload(active_patient))

    def post(self, request, pk):
        active_patient = self._visit(pk)
        if active_patient is None:
            return Response({'error': 'Visit not found.'}, status=status.HTTP_404_NOT_FOUND)
        if active_patient.status != 4:
            return Response(
                {'error': 'Only a patient currently in treatment can change beautician.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        beautician_id = request.data.get('beautician')
        try:
            beautician = Beauticians.objects.get(pk=beautician_id)
        except (Beauticians.DoesNotExist, ValueError, TypeError):
            return Response({'beautician': 'Beautician not found.'}, status=status.HTTP_400_BAD_REQUEST)

        reason = (request.data.get('reason') or '').strip()
        _, previous = beautician_assignment.assign(
            active_patient, beautician, actor=_actor(request), reason=reason,
        )

        if previous is not None:
            label = active_patient.patient_no.name if active_patient.patient_no_id else active_patient.guest_name
            old_name = previous.beautician.beautician_name if previous.beautician else '—'
            AuditLog.objects.create(
                performed_by=_actor(request),
                action='UPDATE',
                entity_type='ActivePatient',
                entity_id=str(active_patient.id),
                description=(
                    f'Beautician for {label} changed: {old_name} -> {beautician.beautician_name}'
                    + (f' ({reason})' if reason else '')
                ),
            )
        return Response(self._payload(active_patient), status=status.HTTP_200_OK)


class CompleteTreatmentView(APIView):
    def post(self, request):
        active_patient_id = request.data.get('active_patient_id')
        if not active_patient_id:
            return Response({"error": "active_patient_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            active_patient = ActivePatient.objects.get(id=active_patient_id)
        except ActivePatient.DoesNotExist:
            return Response({"error": f"ActivePatient '{active_patient_id}' not found."}, status=status.HTTP_404_NOT_FOUND)

        with transaction.atomic():
            _release_beauticians(active_patient)
            active_patient.status = 5
            active_patient.save()

        label = active_patient.patient_no.name if active_patient.patient_no_id else active_patient.guest_name
        AuditLog.objects.create(
            performed_by=_actor(request),
            action='STATUS_CHANGE',
            entity_type='ActivePatient',
            entity_id=str(active_patient.id),
            description=f'Treatment completed for {label}',
        )
        serializer = ActivePatientSerializer(active_patient)
        return Response(serializer.data, status=status.HTTP_200_OK)


class TreatmentRemoveView(APIView):
    def delete(self, request, session_id, treatment_id):
        try:
            session = TreatmentSession.objects.get(id=session_id)
        except TreatmentSession.DoesNotExist:
            return Response({'error': 'Session not found.'}, status=status.HTTP_404_NOT_FOUND)

        try:
            treatment = Treatment.objects.get(id=treatment_id)
        except Treatment.DoesNotExist:
            return Response({'error': 'Treatment not found.'}, status=status.HTTP_404_NOT_FOUND)

        session.treatments.remove(treatment)

        label = (
            session.patient_no.name if session.patient_no_id
            else (session.active_patient.guest_name if session.active_patient_id else str(session_id))
        )
        AuditLog.objects.create(
            performed_by=_actor(request),
            action='UPDATE',
            entity_type='TreatmentSession',
            entity_id=str(session.id),
            description=f'Removed {treatment.name} from session for {label}',
        )

        if session.treatments.count() == 0:
            active_patient = session.active_patient
            with transaction.atomic():
                session.delete()
                if active_patient is not None:
                    _release_beauticians(active_patient)

        return Response(status=status.HTTP_204_NO_CONTENT)


class PatientCountView(APIView):
    def get(self, request):
        return Response({'total': Patient.objects.count()})


class PatientNextNoView(APIView):
    def get(self, request):
        initial = request.GET.get('initial', '').strip().upper()
        if not initial or not initial.isalpha() or len(initial) != 1:
            return Response({'error': 'Provide a single letter as ?initial='}, status=400)
        candidates = Patient.objects.filter(
            patient_no__regex=rf'^{initial}\d+$'
        ).aggregate(last=Max('patient_no'))['last']
        new_number = 1
        if candidates:
            m = re.search(r'\d+$', candidates)
            if m:
                new_number = int(m.group()) + 1
        return Response({'patient_no': f'{initial}{new_number:06d}'})


class PatientSyncView(APIView):
    def get(self, request):
        synced_at = timezone.now()
        since_raw = request.GET.get('since')

        try:
            page = max(1, int(request.GET.get('page', 1)))
            page_size = min(
                max(1, int(request.GET.get('page_size', 200))), 1000)
        except (ValueError, TypeError):
            return Response(
                {'error': 'Invalid `page` or `page_size` value.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        qs = Patient.objects.all().order_by('updated_at', 'patient_no')

        if since_raw:
            try:
                since_dt = parse_datetime(since_raw)
                if since_dt is None:
                    return Response(
                        {'error': 'Invalid `since` value. Use ISO 8601 format, e.g. 2025-01-01T00:00:00Z.'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if timezone.is_naive(since_dt):
                    since_dt = timezone.make_aware(since_dt)
                qs = qs.filter(updated_at__gte=since_dt)
            except (ValueError, TypeError):
                return Response(
                    {'error': 'Invalid `since` value. Use ISO 8601 format, e.g. 2025-01-01T00:00:00Z.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        total_count = qs.count()
        start = (page - 1) * page_size
        end = start + page_size
        patients = PatientSyncSerializer(qs[start:end], many=True).data
        has_more = end < total_count

        return Response({
            'synced_at': synced_at,
            'count': len(patients),
            'total_count': total_count,
            'has_more': has_more,
            'next_page': page + 1 if has_more else None,
            'patients': patients,
        })
