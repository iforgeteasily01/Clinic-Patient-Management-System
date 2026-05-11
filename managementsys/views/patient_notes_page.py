from rest_framework import generics
from rest_framework.exceptions import ValidationError

from ..api.serializers import PatientNoteSerializer
from ..models import Patient, PatientNote


class PatientNoteListCreateView(generics.ListCreateAPIView):
    serializer_class = PatientNoteSerializer

    def get_queryset(self):
        patient_no = self.request.query_params.get('patient_no', '').strip()
        if not patient_no:
            return PatientNote.objects.none()
        return PatientNote.objects.filter(patient_no_id=patient_no).order_by('-date', '-created_at')

    def create(self, request, *args, **kwargs):
        from rest_framework.response import Response
        patient_no = request.data.get('patient_no', '').strip()
        try:
            patient = Patient.objects.get(patient_no=patient_no)
        except Patient.DoesNotExist:
            raise ValidationError({'patient_no': 'Patient not found.'})
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(patient_no=patient)
        return Response(serializer.data, status=201)


class PatientNoteDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = PatientNoteSerializer
    queryset = PatientNote.objects.all()
