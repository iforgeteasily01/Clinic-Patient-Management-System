from django.db import models
from django.db.models import Max, Min, Avg, Sum, Count

# Create your models here.
#Patient
class Patient(models.Model):
    patient_id = models.CharField(max_length=10, unique=True, blank=True)   #unique as in no duplicate patient_id
    name = models.CharField(max_length=100)
    address = models.CharField(max_length=100)
    phone_number = models.CharField(max_length=15)

    def __str__(self):
        return self.name
    
    def newPatient(self, *args, **kwargs):                                  #self makes access to all the variables of class Patient. *args and **kwargs for extra things Django makes / handles internally
        
        #to determine the prefix of the patient and capitalize it
        prefix = self.name[0].upper()
        maxnumber = Patient.objects.filter(patient_id__startswith=prefix).aggregate(lastnumber = models.Max('patient_id'))["lastnumber"] #the [] helps to get a specific value of the dictionary that aggregates returns. since we need the number, then we take just the number part. wont fill in gaps

        if maxnumber:
            max_number = int(maxnumber[1:])         #since index 0 is an alphabet
            new_number = max_number + 1
        else:
            new_number = 1
        
        self.patient_id = f"{prefix}{new_number:09d}"
        
        super().save(*args, **kwargs)


#ActivePatient
class ActivePatient(models.Model):
    patient_id = models.ForeignKey(Patient, on_delete=models.CASCADE) #Cascade as in if Patient is deleted, then there is no Active Patient
    status = models.IntegerField()
    consult_status = models.BooleanField()

#Doctors
class Doctors(models.Model):
    doctor_name = models.CharField(max_length=50)

#Beauticians
class Beauticians(models.Model):
    beautician_name = models.CharField(max_length=50)
    bphone_number = models.CharField(max_length=15)

#MedicalRecord
class MedRec(models.Model):
    medrec_id = models.CharField(max_length=10, unique=True, blank=True)
    doctor_id = models.ForeignKey(Doctors, on_delete=models.SET_NULL, null = True) #set null as in, if there is no doctor that takes care of this patient, then doctor_id is Null rather than deleted
    patient_id = models.ForeignKey(Patient, on_delete=models.CASCADE)
    anamnesis = models.TextField(default="")    #TextField is used because no StringField, and this one has no limit. If you want limit, then models.CharField(max_length=500)
    action = models.TextField(default="")
    medication = models.TextField(default="")

#patientStatus
class patientStatus(models.Model):
    status_name = models.CharField(max_length=20)
    

#####
#END#
#####
