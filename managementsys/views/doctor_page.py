from managementsys.models import Patient, ActivePatient, Doctors, Beauticians, MedRec, patientStatus
from django.http import HttpResponse

def GetActivePatientForDoctor(status_input: str):
    listofpatients = ActivePatient.objects.filter(status = status_input)
    
    return listofpatients

def GetMedicalRecordByPatientId(id_input:str):
    #use .get() to return single instance
    patient_medrec = MedRec.objects.filter(patient_id = id_input).values().first()
    
    return patient_medrec

def AddMedicalRecord(id_input:str):
    patient_data = MedRec.objects.filter(patient_id = id_input)     #to find medical data and returns the whole row as a list or querylist

    return HttpResponse("", status = 200)

def UpdatePatientStatus(id_input: str, new_status: int):
    patient = ActivePatient.objects.get(patient_id = id_input)
    
    if (patient.status < 5 and patient.status > 0):
        #now is in consult
        patient.consult_status = True
        patient.status = new_status         #inputs new status to the data field
    elif (patient.status == 5):
        patient.consult_status = False      #to make sure it is false
        print("Patient has finished consult")
    else:
        return("Error")

    #guess still need to save after all the changes
    patient.save()

    return HttpResponse("Patient status updated", status = 200)