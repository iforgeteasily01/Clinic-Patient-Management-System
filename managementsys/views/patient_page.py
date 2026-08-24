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


def _actor(request):
    return request.user if isinstance(request.user, AppUser) else None


def _release_beauticians(active_patient):
    """
    Frees every beautician tied to `active_patient` via a TreatmentSession,
    unless that beautician is still holding a *different* patient at status 4
    (in treatment). Call this on every path that ends a patient's involvement
    in a session — completed, cancelled, dismissed, or bulk-cleared — not just
    the "finish treatment" button, or the beautician is left marked busy with
    nothing left to finish it.
    """
    beautician_ids = set(
        TreatmentSession.objects.filter(active_patient=active_patient)
        .exclude(beautician__isnull=True)
        .values_list('beautician_id', flat=True)
    )
    for beautician_id in beautician_ids:
        still_busy = TreatmentSession.objects.filter(
            beautician_id=beautician_id,
            active_patient__status=4,
        ).exclude(active_patient=active_patient).exists()
        if not still_busy:
            Beauticians.objects.filter(id=beautician_id).update(available=True)


class PatientSearchView(APIView):
    def get(self, request):
        qs = Patient.objects.all()

        # Unified ?search=<value>&field=<name|patient_no|phone|address> format
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
            # Legacy per-field params
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
        # Scoped to the caller's own branch: clearing the queue is an end-of-day
        # action at one clinic, and a group-wide wipe from one reception desk
        # would delete another branch's live queue mid-shift.
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

        patient = active_patient.patient_no  # may be None for general appointments
        with transaction.atomic():
            session = TreatmentSession.objects.create(
                active_patient=active_patient,
                patient_no=patient,
                beautician=beautician,
                branch=write_branch(request, locked=True),
            )
            session.treatments.set(treatment_list)

            beautician.available = False
            beautician.save()

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
    """
    DELETE /api/treatment-session/<session_id>/treatment/<treatment_id>/
    Removes one treatment from a session's M2M list.
    Deletes the session entirely if it becomes empty.
    """

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
    """
    GET /api/patients/count/
    Returns the total number of patients. Lightweight — no data serialization.
    """

    def get(self, request):
        return Response({'total': Patient.objects.count()})


class PatientNextNoView(APIView):
    """
    GET /api/patients/next-no/?initial=J
    Returns the projected next patient_no for the given name initial.
    No DB write — preview only.
    """

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
    """
    GET /api/patients/sync/?since=<ISO-8601>&page=<n>&page_size=<n>

    Returns patients updated since the given timestamp, paginated.
    Omit `since` for a full sync (first-time setup).
    Default page_size=200, max=1000.
    """

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
