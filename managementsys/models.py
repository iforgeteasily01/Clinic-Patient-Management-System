from django.utils import timezone

from django.db import models
from django.db.models import Max, Min, Avg, Sum, Count


class Patient(models.Model):
    patient_no = models.CharField(
        max_length=10, unique=True, primary_key=True, blank=True)
    name = models.CharField(max_length=100)
    address = models.CharField(max_length=100)
    phone_number = models.CharField(max_length=15)
    NIK = models.CharField(max_length=16)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # Only generate if not already set
        if not self.patient_no:
            prefix = self.name[0].upper()

            maxnumber = Patient.objects.filter(
                patient_no__startswith=prefix
            ).aggregate(
                lastnumber=models.Max('patient_no')
            )["lastnumber"]

            if maxnumber:
                max_number = int(maxnumber[1:])
                new_number = max_number + 1
            else:
                new_number = 1

            self.patient_no = f"{prefix}{new_number:06d}"  # 7 chars total

        super().save(*args, **kwargs)


# ActivePatient
class ActivePatient(models.Model):
    # Cascade as in if Patient is deleted, then there is no Active Patient
    patient_no = models.ForeignKey(Patient, on_delete=models.CASCADE)
    status = models.IntegerField()
    consult_status = models.BooleanField()
    visit_time = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.patient_no

# Doctors


class Doctors(models.Model):
    doctor_name = models.CharField(max_length=50)

    def __str__(self):
        return self.doctor_name

# Beauticians


class Beauticians(models.Model):
    beautician_name = models.CharField(max_length=50)
    bphone_number = models.CharField(max_length=15)

    def __str__(self):
        return self.beautician_name

# MedicalRecord


class MedRec(models.Model):
    medrec_id = models.CharField(max_length=30, unique=True, blank=True)
    # set null as in, if there is no doctor that takes care of this patient, then doctor_id is Null rather than deleted
    doctor_id = models.ForeignKey(
        Doctors, on_delete=models.SET_NULL, null=True)
    patient_no = models.ForeignKey(Patient, on_delete=models.CASCADE)
    subjective = models.TextField(default="")
    objective = models.TextField(default="")
    assessment = models.TextField(default="")
    plan = models.TextField(default="")
    sabun_pagi = models.TextField(default="", null=True)
    sabun_malam = models.TextField(default="", null=True)
    toner_pagi = models.TextField(default="", null=True)
    toner_malam = models.TextField(default="", null=True)
    obat1_pagi = models.TextField(default="", null=True)
    obat2_pagi = models.TextField(default="", null=True)
    obat1_malam = models.TextField(default="", null=True)
    obat2_malam = models.TextField(default="", null=True)
    treatment = models.TextField(default="", null=True)

    def __str__(self):
        return self.medrec_id

    def save(self, *args, **kwargs):
        if not self.medrec_id:
            today = timezone.now().strftime("%Y%m%d")
            patient_no = self.patient_no.patient_no

            base_id = f"MR-{patient_no}-{today}"

            count = MedRec.objects.filter(
                medrec_id__startswith=base_id).count()
            self.medrec_id = f"{base_id}-{count+1}"

        super().save(*args, **kwargs)

# patientStatus


class patientStatus(models.Model):
    status_name = models.CharField(max_length=20)


#####
# END#
#####
