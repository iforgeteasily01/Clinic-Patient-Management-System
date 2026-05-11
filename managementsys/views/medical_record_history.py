from rest_framework import generics
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import MedRec
from ..api.serializers import MedRecHistorySerializer


class MedRecHistoryListView(APIView):
    def get(self, request):
        qs = MedRec.objects.select_related('patient_no', 'doctor_id').all()

        patient_name = request.query_params.get('patient_name', '').strip()
        patient_no = request.query_params.get('patient_no', '').strip()
        medrec_id = request.query_params.get('medrec_id', '').strip()
        date = request.query_params.get('date', '').strip()

        if patient_name:
            qs = qs.filter(patient_no__name__icontains=patient_name)
        if patient_no:
            qs = qs.filter(patient_no__patient_no__icontains=patient_no)
        if medrec_id:
            qs = qs.filter(medrec_id__icontains=medrec_id)
        if date:
            # date in YYYY-MM-DD → convert to YYYYMMDD for medrec_id substring match
            date_compact = date.replace('-', '')
            qs = qs.filter(medrec_id__contains=date_compact)

        qs = qs.order_by('-medrec_id')
        serializer = MedRecHistorySerializer(qs, many=True)
        return Response(serializer.data)


class MedRecHistoryDetailView(APIView):
    def get(self, request, medrec_id):
        try:
            record = MedRec.objects.select_related('patient_no', 'doctor_id').get(medrec_id=medrec_id)
        except MedRec.DoesNotExist:
            return Response({'error': 'Medical record not found.'}, status=404)
        serializer = MedRecHistorySerializer(record)
        return Response(serializer.data)
