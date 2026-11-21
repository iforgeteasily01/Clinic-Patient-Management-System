from managementsys.models import Patient, ActivePatient, Doctors, Beauticians, MedRec, patientStatus

def run():
    d1 = Doctors.objects.create(doctor_name="Dr. Alice Tan")
    d2 = Doctors.objects.create(doctor_name="Dr. Bob Lee")

    b1 = Beauticians.objects.create(beautician_name="Cindy Lau", bphone_number="08123456789")
    b2 = Beauticians.objects.create(beautician_name="Dina Hart", bphone_number="08987654321")

    p1 = Patient(name="John Doe", address="Jl. Merdeka No.10", phone_number="0812345678")
    p1.newPatient()
    
    p2 = Patient(name="Jane Smith", address="Jl. Sudirman No.20", phone_number="0899998888")
    p2.newPatient()

    s1 = patientStatus.objects.create(status_name="Waiting")
    s5 = patientStatus.objects.create(status_name="Under Consultation")
    s2 = patientStatus.objects.create(status_name="Under Treatment")
    s3 = patientStatus.objects.create(status_name="Treatment Done")
    s4 = patientStatus.objects.create(status_name="Finished Visit")
    

    a1 = ActivePatient.objects.create(patient_id=p1, status=1, consult_status=True)
    a2 = ActivePatient.objects.create(patient_id=p2, status=2, consult_status=True)

    MedRec.objects.create(
        doctor_id=d1,
        patient_id=p1,
        anamnesis="Headache for 3 days",
        action="Gave paracetamol",
        medication="Paracetamol 500mg"
    )

    MedRec.objects.create(
        doctor_id=d2,
        patient_id=p2,
        anamnesis="Facial acne treatment",
        action="Applied facial mask and cleanser",
        medication="Topical acne cream"
    )

    print("✅ Sample data inserted successfully!")
    print(f"Created patients: {p1.patient_id} ({p1.name}), {p2.patient_id} ({p2.name})")