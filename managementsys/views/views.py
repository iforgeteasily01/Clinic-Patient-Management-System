from django.shortcuts import render, HttpResponse
from rest_framework import generics
from ..models import *
from ..api.serializers import *
from .beautician_page import *

# Create your views here.
def homepage(request):
    return render(request, 'homepage.html')


#Create List for API
class PatientListCreate(generics.ListCreateAPIView):
    queryset = Patient.objects.all()
    serializer_class = PatientSerializer

class ActPatListCreate(generics.ListCreateAPIView):
    queryset = ActivePatient.objects.all()
    serializer_class = ActivePatientSerializer

class DoctorsListCreate(generics.ListCreateAPIView):
    queryset = Doctors.objects.all()
    serializer_class = DoctorsSerializer

class BeauticiansListCreate(generics.ListCreateAPIView):
    queryset = Beauticians.objects.all()
    serializer_class = BeauticiansSerializer

class MedRecListCreate(generics.ListCreateAPIView):
    queryset = MedRec.objects.all()
    serializer_class = MedRecSerializer

class patStatusListCreate(generics.ListCreateAPIView):
    queryset = patientStatus.objects.all()
    serializer_class = PatStatSerializer

#to update the data based from functions that was mentioned before
class BeauticianUpdate(generics.UpdateAPIView):
    queryset = ActivePatient.objects.all()
    serializer_class = ActivePatientSerializer
    lookup_field = 'patient_id'
    
    def perform_update(self, serializer):
        input_id = serializer.instance.patient_id #colorless doesnt mean wrong, i asked chatgpt. need to test it tho tbh
        target_status = self.request.data.get('target_status')
        message = UpdatePatientTreatment(input_id, target_status)
        print(message)
        return super().perform_update(serializer)