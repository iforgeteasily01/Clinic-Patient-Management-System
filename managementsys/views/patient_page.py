from ..models import Patient, ActivePatient, patientStatus
from django.http import HttpResponse
from django.db.models import Q


def ListActivePatient():                                        #idk why this function needs a param to define the function the patient is currently in
    
    
    return 0


def UpdatePatientStatus(id_input: str, new_status: int):        #idk why this function needs a param to define the function the patient is currently in

    return HttpResponse("", status = 200)