from ..models import Patient, ActivePatient, patientStatus, Treatment, Beauticians, TreatmentSession, AppUser, AuditLog
from django.http import HttpResponse
from django.db.models import Q
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from ..api.serializers import PatientSerializer, ActivePatientSerializer, TreatmentSerializer


def _actor(request):
    return request.user if isinstance(request.user, AppUser) else None


class PatientSearchView(APIView):
    def get(self, request):
        qs = Patient.objects.all()

        if no := request.GET.get('patient_no', '').strip():
            qs = qs.filter(patient_no__icontains=no)
        if name := request.GET.get('name', '').strip():
            qs = qs.filter(name__icontains=name)
        if phone := request.GET.get('phone', '').strip():
            qs = qs.filter(phone_number__icontains=phone)
        if address := request.GET.get('address', '').strip():
            qs = qs.filter(address__icontains=address)

        serializer = PatientSerializer(qs[:20], many=True)
        return Response(serializer.data)


class PatientCreateWithActiveView(APIView):
    def post(self, request):
        patient_serializer = PatientSerializer(data=request.data)
        if not patient_serializer.is_valid():
            return Response(patient_serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        patient = patient_serializer.save()

        consult_status = bool(request.data.get('consult_status', False))
        active_patient_data = {
            'patient_no': patient.patient_no,
            'status': 1 if consult_status else 3,
            'consult_status': consult_status,
        }
        active_serializer = ActivePatientSerializer(data=active_patient_data)
        if active_serializer.is_valid():
            active_serializer.save()
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
                    'active_patient': active_serializer.data
                },
                status=status.HTTP_201_CREATED,
            )

        patient.delete()
        return Response(active_serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ActivePatientClearView(APIView):
    def delete(self, request):
        deleted_count, _ = ActivePatient.objects.all().delete()
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
        patients = ActivePatient.objects.filter(status__in=[3, 4])
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
        treatments = Treatment.objects.filter(active=True)
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

        treatment_list = Treatment.objects.filter(id__in=treatment_ids, active=True)

        patient = active_patient.patient_no  # may be None for general appointments
        session = TreatmentSession.objects.create(
            active_patient=active_patient,
            patient_no=patient,
            beautician=beautician,
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

        sessions = TreatmentSession.objects.filter(active_patient=active_patient).select_related('beautician')
        seen = set()
        for session in sessions:
            if session.beautician_id and session.beautician_id not in seen:
                seen.add(session.beautician_id)
                session.beautician.available = True
                session.beautician.save(update_fields=['available'])

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
