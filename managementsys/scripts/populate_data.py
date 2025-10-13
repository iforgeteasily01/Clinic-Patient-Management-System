from managementsys.models import Patient, ActivePatient, Doctors, Beauticians, MedRec, patientStatus

def run():
    # Create doctors (no doctor_id field in model)
    d1 = Doctors.objects.create(doctor_name="Dr. Alice Tan")
    d2 = Doctors.objects.create(doctor_name="Dr. Bob Lee")

    # Create beauticians (no beautician_id field in model)
    b1 = Beauticians.objects.create(beautician_name="Cindy Lau", bphone_number="08123456789")
    b2 = Beauticians.objects.create(beautician_name="Dina Hart", bphone_number="08987654321")

    # Create patients - use newPatient() method to auto-generate patient_id
    p1 = Patient(name="John Doe", address="Jl. Merdeka No.10", phone_number="0812345678")
    p1.newPatient()  # This generates the patient_id
    
    p2 = Patient(name="Jane Smith", address="Jl. Sudirman No.20", phone_number="0899998888")
    p2.newPatient()  # This generates the patient_id

    # Create patient statuses (no status_id field in model)
    s1 = patientStatus.objects.create(status_name="Waiting")
    s5 = patientStatus.objects.create(status_name="Under Consultation")
    s2 = patientStatus.objects.create(status_name="Under Treatment")
    s3 = patientStatus.objects.create(status_name="Treatment Done")
    s4 = patientStatus.objects.create(status_name="Finished Visit")
    

    # Create active patients
    a1 = ActivePatient.objects.create(patient_id=p1, status=1, consult_status=True)
    a2 = ActivePatient.objects.create(patient_id=p2, status=2, consult_status=True)

    # Create medical records (field names: medrec_anamnesis, medrec_action, medrec_medication)
    # No medrec_id field, no medrec_no needed (has default)
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