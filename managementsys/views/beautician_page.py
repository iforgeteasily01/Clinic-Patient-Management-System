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
def UpdatePatientTreatment(id_input: str, target_status = int):
    ActivePatient.objects.filter(patient_id = id_input).update(status = target_status)

    return HttpResponse("Patient finished processing", status = 200)