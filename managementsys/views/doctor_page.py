from managementsys.models import Patient, ActivePatient, Doctors, Beauticians, MedRec, patientStatus
from django.http import HttpResponse

def GetActivePatientForDoctor(status_input: str):
    listofpatients = ActivePatient.objects.filter(status = status_input)
    
    return listofpatients

def GetMedicalRecordByPatientId(id_input:str):
    patient_medrec = MedRec.objects.filter(patient_id = id_input).values().first()
    
    return patient_medrec

def AddMedicalRecord(id_input:str):
    patient_data = MedRec.objects.filter(patient_id = id_input)

    return HttpResponse("", status = 200)

def UpdatePatientStatus(id_input: str, new_status: int):
    patient = ActivePatient.objects.get(patient_id = id_input)
    
    if (patient.status < 5 and patient.status > 0):
        patient.consult_status = True
        patient.status = new_status
    elif (patient.status == 5):
        patient.consult_status = False
        print("Patient has finished consult")
    else:
        return("Error")

    patient.save()

    return HttpResponse("Patient status updated", status = 200)