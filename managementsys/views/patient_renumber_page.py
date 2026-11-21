from rest_framework.response import Response
from rest_framework.views import APIView

from ..auth_backend import IsAppAuthenticated
from ..api.serializers import PatientSerializer
from ..models import Patient
from ..services.patient_renumber import (
    PatientRenumberError, normalize, preview, renumber,
)

RENUMBER_ROLES = ('superuser',)


class PatientRenumberView(APIView):
    permission_classes = [IsAppAuthenticated]

    def _guard(self, request, patient_no):
        if request.user.role not in RENUMBER_ROLES:
            return None, Response(
                {'error': 'Only the Admin (superuser) account may change a patient number.'},
                status=403)

        patient = Patient.objects.filter(patient_no=normalize(patient_no)).first()
        if patient is None:
            patient = Patient.objects.filter(patient_no=patient_no).first()
        if patient is None:
            return None, Response({'error': f'Patient {patient_no} not found.'}, status=404)

        return patient, None

    def get(self, request, patient_no):
        patient, refusal = self._guard(request, patient_no)
        if refusal is not None:
            return refusal
        return Response(preview(patient))

    def post(self, request, patient_no):
        patient, refusal = self._guard(request, patient_no)
        if refusal is not None:
            return refusal

        try:
            summary = renumber(patient, request.data.get('new_patient_no'), actor=request.user)
        except PatientRenumberError as exc:
            return Response(exc.errors, status=400)

        return Response({
            **summary,
            'patient': PatientSerializer(patient, context={'request': request}).data,
        })
