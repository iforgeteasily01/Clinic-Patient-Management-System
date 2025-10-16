from django.contrib import admin
from .models import Patient, ActivePatient, Doctors, Beauticians ,MedRec, patientStatus
# Register your models here.

admin.site.register([Patient, ActivePatient, Doctors, Beauticians ,MedRec, patientStatus])

#class PatientAdmin(admin.ModelAdmin):
