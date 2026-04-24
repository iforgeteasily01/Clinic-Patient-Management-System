from rest_framework import serializers
from ..models import *


class PatientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Patient
        fields = ["patient_no", "name", "address", "phone_number", "NIK"]


class ActivePatientSerializer(serializers.ModelSerializer):
    class Meta:
        model = ActivePatient
        fields = ["id", "patient_no", "status", "consult_status", "visit_time"]


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
        fields = ["medrec_id", "doctor_id", "patient_no", "subjective", "objective", "assessment", "plan", "sabun_pagi",
                  "sabun_malam", "toner_pagi", "toner_malam", "obat1_pagi", "obat2_pagi", "obat1_malam", "obat2_malam", "treatment"]


class PatStatSerializer(serializers.ModelSerializer):
    class Meta:
        model = patientStatus
        fields = ["status_name"]
