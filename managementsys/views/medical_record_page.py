from rest_framework import generics

from ..models import MedRec
from ..api.serializers import MedRecSerializer


class MedRecByPatientNoView(generics.ListAPIView):
    serializer_class = MedRecSerializer

    def get_queryset(self):
        patient_no_id = self.kwargs.get('patient_no_id') or self.request.query_params.get('patient_no_id')
        if not patient_no_id:
            return MedRec.objects.none()
        return MedRec.objects.filter(patient_no__patient_no=patient_no_id)
