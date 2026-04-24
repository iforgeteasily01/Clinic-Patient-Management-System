from ..models import Patient, ActivePatient, patientStatus
from django.http import HttpResponse
from django.db.models import Q
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from ..api.serializers import PatientSerializer, ActivePatientSerializer

class PatientSearchView(APIView):
    def get(self, request):
        query = request.GET.get('q', '')

        patients = Patient.objects.filter(
            Q(name__icontains=query) |
            Q(patient_no__icontains=query)
        )[:10]

        serializer = PatientSerializer(patients, many=True)
        return Response(serializer.data)


class PatientCreateWithActiveView(APIView):
    def post(self, request):
        patient_serializer = PatientSerializer(data=request.data)
        if not patient_serializer.is_valid():
            return Response(patient_serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        patient = patient_serializer.save()

        active_patient_data = {
            'patient_no': patient.patient_no,
            'status': 1,
            'consult_status': request.data.get('consult_status', False)
        }
        active_serializer = ActivePatientSerializer(data=active_patient_data)
        if active_serializer.is_valid():
            active_serializer.save()
            return Response(
                {
                    'patient': patient_serializer.data,
                    'active_patient': active_serializer.data
                },
                status=status.HTTP_201_CREATED,
            )

        patient.delete()
        return Response(active_serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ActivePatientUpdateStatusView(APIView):
    def post(self, request):
        patient_no = request.data.get('patient_no')
        target_status = request.data.get('status')

        if patient_no is None or target_status is None:
            return Response(
                {"error": "patient_no and status are required."},
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
            active_patient = ActivePatient.objects.filter(
                patient_no=patient_no
            ).latest('visit_time')
        except ActivePatient.DoesNotExist:
            return Response(
                {"error": f"No active patient found with patient_no '{patient_no}'."},
                status=status.HTTP_404_NOT_FOUND
            )

        active_patient.status = target_status
        active_patient.save()

        serializer = ActivePatientSerializer(active_patient)
        return Response(serializer.data, status=status.HTTP_200_OK)
