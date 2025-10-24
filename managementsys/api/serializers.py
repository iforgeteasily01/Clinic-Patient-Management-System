from rest_framework import serializers
from ..models import *

class PatientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Patient
        fields = ["patient_id", "name", "address", "phone_number"]

class ActivePatientSerializer(serializers.ModelSerializer):
    class Meta:
        model = ActivePatient
        fields = ["patient_id", "status", "consult_status"]

class DoctorsSerializer(serializers.ModelSerializer):
    class Meta:
        model = Doctors
        fields = ["doctor_name"]

class BeauticiansSerializer(serializers.ModelSerializer):
    class Meta:
        model = Beauticians
        fields = ["beautician_number", "bphone_number"]

class MedRecSerializer(serializers.ModelSerializer):
    class Meta:
        model = MedRec
        fields = ["medrec_id", "doctor_id", "patient_id", "anamnesis", "action", "medication"]

class PatStatSerializer(serializers.ModelSerializer):
    class Meta:
        model = patientStatus
        fields = ["status_name"]