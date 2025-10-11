from .models import Patient, ActivePatient              #no need to import other things based on the function said
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

def AddActivePatient(pn_input: str): 
    #i assume this is to add from patient list to the active patient list, but so far haven't come up with ideas on how to move the data and make sure there is no duplicates
    #i think first need to either build a filter or a finder to find the patient in the Patient model, then somehow copy it into ActivePatient
    
    #default values
    consult_status = False  #only true when doctor updatepatientstatus


    return HttpResponse("Added successfully", status = 200)