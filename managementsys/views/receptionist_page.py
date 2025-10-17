from ..models import Patient, ActivePatient              #no need to import other things based on the function said
from django.db.models import Q
from django.http import HttpResponse



def GetFilteredPatientList(name_input: str, id_input: str, pn_input: str):
    #Testing if variable is empty
    if not(name_input, id_input, pn_input):
        print("Please input either a Name, Patient ID, or a Phone Number")
    
    request = Q()
    #Using Q object to use input that is given to filter out a patient. 
    #Block below kinda combines all the "filter"
    if name_input:
        request &= Q(name = name_input)
    if id_input:
        request &= Q(patient_id = id_input)
    if pn_input:
        request &= Q(phone_number = pn_input)

    patient = Patient.objects.filter(request)

    if(patient.exists):
        return patient
    else:
        print("Patient does not exist")

    #after everything, returns a filtered data from what i understand
    return patient

def AddActivePatient(id_input: str, is_consult: bool): 
    query = Q()
    if id_input:
        query &= Q(patient_id = id_input)

    patient = Patient.objects.filter(query)
    
    
    if(patient.exists):
        newActivePatient = ActivePatient.objects.create(
        patient_id = id_input,
        consult_status = is_consult
        )
    return HttpResponse("Added successfully", status = 200)