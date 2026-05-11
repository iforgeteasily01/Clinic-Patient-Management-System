from rest_framework import status
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import Patient, PatientPhoto
from ..api.serializers import PatientPhotoSerializer


class PatientPhotoUploadView(APIView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        patient_no = request.data.get('patient_no', '').strip()
        body_area = request.data.get('body_area', '').strip()
        photo_date = request.data.get('photo_date', None)
        image = request.FILES.get('image')

        if not patient_no:
            return Response({'error': 'patient_no is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if not body_area:
            return Response({'error': 'body_area is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if not image:
            return Response({'error': 'image file is required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            patient = Patient.objects.get(patient_no=patient_no)
        except Patient.DoesNotExist:
            return Response({'error': 'Patient not found.'}, status=status.HTTP_404_NOT_FOUND)

        kwargs = {'patient_no': patient, 'body_area': body_area, 'image': image}
        if photo_date:
            kwargs['photo_date'] = photo_date

        photo = PatientPhoto.objects.create(**kwargs)
        serializer = PatientPhotoSerializer(photo, context={'request': request})
        return Response({'success': True, 'message': 'Photo uploaded.', 'photo': serializer.data},
                        status=status.HTTP_201_CREATED)


class PatientPhotoListView(APIView):
    def get(self, request):
        patient_no = request.query_params.get('patient_no', '').strip()
        photo_date = request.query_params.get('date', '').strip()

        if not patient_no:
            return Response({'error': 'patient_no is required.'}, status=status.HTTP_400_BAD_REQUEST)

        qs = PatientPhoto.objects.filter(patient_no__patient_no=patient_no)
        if photo_date:
            qs = qs.filter(photo_date=photo_date)

        serializer = PatientPhotoSerializer(qs, many=True, context={'request': request})
        return Response(serializer.data)
