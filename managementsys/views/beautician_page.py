from ..models import Beauticians, ActivePatient, patientStatus
from django.http import HttpResponse
from django.db.models import Q



def GetActivePatientForBeautician(status_input: int):
    if(status_input <= 2 and status_input > 0):
        list_patient = ActivePatient.objects.filter(status = status_input)
        return list_patient
    else:
        print("Patient has finished treatment")



#since patient_id is unique, then using filter shouldn't ha
def ProcessPatientTreatment(id_input: str):
    ActivePatient.objects.filter(patient_id = id_input).update(status = 3)

    return HttpResponse("Patient finished processing", status = 200)
        
def FinishPatientTreatment(id_input: str):
    ActivePatient.objects.filter(patient_id = id_input).update(status = 4)

    return HttpResponse("Patient finished their visit", status = 200)
