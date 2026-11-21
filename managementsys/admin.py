from django.contrib import admin
from .models import Patient, ActivePatient, Appointment, AppointmentLocation, Doctors, Beauticians ,MedRec, patientStatus

admin.site.register([Patient, ActivePatient, Doctors, Beauticians ,MedRec, patientStatus])
admin.site.register([Appointment, AppointmentLocation])

class PatientAdmin(admin.ModelAdmin):
    name = 0