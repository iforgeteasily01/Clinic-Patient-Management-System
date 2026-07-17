"""Integration tests for the scheduled-appointment endpoints.

Covers the booking lifecycle (create → reschedule → cancel), the FHIR-derived
validation rules, and the check-in bridge into the walk-in ActivePatient queue.

The timezone tests are the important ones: settings.TIME_ZONE is UTC while the
clinic runs on Asia/Jakarta, and a wrong offset is the most common cause of a
SatuSehat rejection. See docs/satusehat-appointment-page-design.md.
"""
from datetime import timedelta, timezone as dt_timezone

import pytest
from django.urls import reverse
from django.utils import timezone

from managementsys.models import (
    JAKARTA_TZ, ActivePatient, Appointment, AppointmentLocation, Doctors, Patient,
)


@pytest.fixture
def doctor(db):
    return Doctors.objects.create(doctor_name="Dr. Melia", nik="3171000000000001")


@pytest.fixture
def room(db):
    return AppointmentLocation.objects.create(name="Ruang 1A", room_code="1A")


@pytest.fixture
def patient(db):
    return Patient.objects.create(name="Budi Santoso", NIK="3171000000000002")


def _slot(days=1, hour=10):
    """A future Jakarta-local slot, returned as (start, end) aware datetimes."""
    start = (timezone.now().astimezone(JAKARTA_TZ) + timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0)
    return start, start + timedelta(minutes=30)


def _payload(patient=None, doctor=None, room=None, **overrides):
    start, end = _slot()
    body = {
        "start_at": start.isoformat(),
        "end_at": end.isoformat(),
        "status": "booked",
    }
    if patient is not None:
        body["patient"] = patient.patient_no
    if doctor is not None:
        body["practitioner"] = doctor.id
    if room is not None:
        body["location"] = room.id
    body.update(overrides)
    return body


@pytest.mark.django_db
class TestAppointmentCreate:
    def test_books_appointment_with_generated_number(self, auth_api, patient, doctor, room):
        res = auth_api.post(
            reverse("scheduled-appointments"),
            _payload(patient, doctor, room, service_type="Konsultasi"),
            format="json",
        )
        assert res.status_code == 201, res.content

        appointment = Appointment.objects.get(pk=res.data["id"])
        assert appointment.appointment_no.startswith("APT-")
        assert appointment.status == "booked"
        assert appointment.patient_id == patient.patient_no
        assert appointment.practitioner_id == doctor.id
        # Reserved for the sync phase — must stay untouched by this flow.
        assert appointment.ihs_appointment_id is None
        assert appointment.sync_status == "not_synced"

    def test_appointment_numbers_increment(self, auth_api, patient, doctor, room):
        url = reverse("scheduled-appointments")
        first = auth_api.post(url, _payload(patient, doctor, room), format="json")
        second = auth_api.post(url, _payload(patient, doctor, room), format="json")
        assert first.data["appointment_no"] != second.data["appointment_no"]
        assert second.data["appointment_no"].endswith("000002")

    def test_guest_booking_without_patient_record(self, auth_api, doctor, room):
        res = auth_api.post(
            reverse("scheduled-appointments"),
            _payload(None, doctor, room, guest_name="Tamu Siti"),
            format="json",
        )
        assert res.status_code == 201, res.content
        assert res.data["display_name"] == "Tamu Siti"

    def test_requires_patient_or_guest_name(self, auth_api, doctor, room):
        res = auth_api.post(
            reverse("scheduled-appointments"),
            _payload(None, doctor, room),
            format="json",
        )
        assert res.status_code == 400
        assert "guest_name" in res.data

    def test_booked_status_requires_end_at(self, auth_api, patient, doctor, room):
        body = _payload(patient, doctor, room)
        body.pop("end_at")
        res = auth_api.post(reverse("scheduled-appointments"), body, format="json")
        # FHIR invariant app-2/app-3: a booked slot carries start and end.
        assert res.status_code == 400
        assert "end_at" in res.data

    def test_end_must_be_after_start(self, auth_api, patient, doctor, room):
        start, _ = _slot()
        res = auth_api.post(
            reverse("scheduled-appointments"),
            _payload(patient, doctor, room,
                     end_at=(start - timedelta(minutes=5)).isoformat()),
            format="json",
        )
        assert res.status_code == 400
        assert "end_at" in res.data

    def test_rejects_past_start_on_create(self, auth_api, patient, doctor, room):
        past = timezone.now() - timedelta(days=1)
        res = auth_api.post(
            reverse("scheduled-appointments"),
            _payload(patient, doctor, room,
                     start_at=past.isoformat(),
                     end_at=(past + timedelta(minutes=30)).isoformat()),
            format="json",
        )
        assert res.status_code == 400
        assert "start_at" in res.data

    def test_requires_authentication(self, api, patient, doctor, room):
        res = api.post(
            reverse("scheduled-appointments"),
            _payload(patient, doctor, room),
            format="json",
        )
        assert res.status_code in (401, 403)


@pytest.mark.django_db
class TestTimezoneHandling:
    """The clinic is Asia/Jakarta; the DB stores UTC. Offsets must survive both ways."""

    def test_emitted_datetimes_carry_jakarta_offset(self, auth_api, patient, doctor, room):
        res = auth_api.post(
            reverse("scheduled-appointments"),
            _payload(patient, doctor, room),
            format="json",
        )
        assert res.status_code == 201, res.content
        for field in ("start_at", "end_at", "created_at"):
            assert res.data[field].endswith("+07:00"), f"{field} = {res.data[field]}"

    def test_naive_input_is_read_as_jakarta_local(self, auth_api, patient, doctor, room):
        start, _ = _slot(hour=10)
        res = auth_api.post(
            reverse("scheduled-appointments"),
            _payload(patient, doctor, room,
                     start_at=start.strftime("%Y-%m-%dT10:00:00"),
                     end_at=start.strftime("%Y-%m-%dT10:30:00")),
            format="json",
        )
        assert res.status_code == 201, res.content

        # 10:00 WIB is 03:00 UTC — the naive string must not be taken as UTC.
        appointment = Appointment.objects.get(pk=res.data["id"])
        assert appointment.start_at.astimezone(dt_timezone.utc).hour == 3
        assert res.data["start_at"].startswith(start.strftime("%Y-%m-%dT10:00:00"))

    def test_utc_input_is_preserved_as_the_same_instant(self, auth_api, patient, doctor, room):
        start, end = _slot(hour=10)
        res = auth_api.post(
            reverse("scheduled-appointments"),
            _payload(patient, doctor, room,
                     start_at=start.astimezone(dt_timezone.utc).isoformat(),
                     end_at=end.astimezone(dt_timezone.utc).isoformat()),
            format="json",
        )
        assert res.status_code == 201, res.content
        # Sent as 03:00Z, must read back as 10:00+07:00 — same instant.
        assert res.data["start_at"].startswith(start.strftime("%Y-%m-%dT10:00:00"))

    def test_date_filter_uses_jakarta_day_boundaries(self, auth_api, patient, doctor, room):
        # 08:00 WIB is 01:00 UTC the same day; 23:00 WIB is 16:00 UTC. Both must
        # land on the Jakarta date, not the UTC one.
        early_start, early_end = _slot(days=2, hour=8)
        late_start, late_end = _slot(days=2, hour=23)
        url = reverse("scheduled-appointments")
        for start, end in ((early_start, early_end), (late_start, late_end)):
            res = auth_api.post(
                url,
                _payload(patient, doctor, room,
                         start_at=start.isoformat(), end_at=end.isoformat()),
                format="json",
            )
            assert res.status_code == 201, res.content

        res = auth_api.get(url, {"date": early_start.strftime("%Y-%m-%d")})
        assert res.status_code == 200
        assert len(res.data) == 2


@pytest.mark.django_db
class TestAppointmentListFilters:
    def test_filters_by_status_and_practitioner(self, auth_api, patient, doctor, room):
        url = reverse("scheduled-appointments")
        booked = auth_api.post(url, _payload(patient, doctor, room), format="json")
        other_doctor = Doctors.objects.create(doctor_name="Dr. Rina")
        auth_api.post(url, _payload(patient, other_doctor, room), format="json")

        res = auth_api.get(url, {"practitioner": doctor.id})
        assert [r["id"] for r in res.data] == [booked.data["id"]]

        auth_api.delete(
            reverse("scheduled-appointment-detail", args=[booked.data["id"]]))
        res = auth_api.get(url, {"status": "cancelled"})
        assert [r["id"] for r in res.data] == [booked.data["id"]]

    def test_rejects_malformed_date(self, auth_api):
        res = auth_api.get(reverse("scheduled-appointments"), {"date": "20-07-2026"})
        assert res.status_code == 400


@pytest.mark.django_db
class TestAppointmentUpdate:
    def test_reschedule_to_a_new_slot(self, auth_api, patient, doctor, room):
        created = auth_api.post(
            reverse("scheduled-appointments"),
            _payload(patient, doctor, room),
            format="json",
        )
        new_start, new_end = _slot(days=3, hour=14)
        res = auth_api.patch(
            reverse("scheduled-appointment-detail", args=[created.data["id"]]),
            {"start_at": new_start.isoformat(), "end_at": new_end.isoformat()},
            format="json",
        )
        assert res.status_code == 200, res.content
        assert res.data["start_at"].startswith(new_start.strftime("%Y-%m-%dT14:00:00"))

    def test_patch_allows_correcting_into_the_past(self, auth_api, patient, doctor, room):
        """The future-start rule applies on create only — edits fix mistakes."""
        created = auth_api.post(
            reverse("scheduled-appointments"),
            _payload(patient, doctor, room),
            format="json",
        )
        past = timezone.now() - timedelta(days=1)
        res = auth_api.patch(
            reverse("scheduled-appointment-detail", args=[created.data["id"]]),
            {"start_at": past.isoformat(),
             "end_at": (past + timedelta(minutes=30)).isoformat()},
            format="json",
        )
        assert res.status_code == 200, res.content

    def test_delete_soft_cancels_and_keeps_the_row(self, auth_api, patient, doctor, room):
        created = auth_api.post(
            reverse("scheduled-appointments"),
            _payload(patient, doctor, room),
            format="json",
        )
        res = auth_api.delete(
            reverse("scheduled-appointment-detail", args=[created.data["id"]]))
        assert res.status_code == 200, res.content

        appointment = Appointment.objects.get(pk=created.data["id"])
        assert appointment.status == "cancelled"

    def test_detail_404_for_unknown_id(self, auth_api):
        res = auth_api.get(reverse("scheduled-appointment-detail", args=[999999]))
        assert res.status_code == 404


@pytest.mark.django_db
class TestCheckIn:
    def test_check_in_creates_linked_active_patient(self, auth_api, patient, doctor, room):
        created = auth_api.post(
            reverse("scheduled-appointments"),
            _payload(patient, doctor, room),
            format="json",
        )
        res = auth_api.post(
            reverse("scheduled-appointment-check-in", args=[created.data["id"]]),
            {},
            format="json",
        )
        assert res.status_code == 201, res.content

        appointment = Appointment.objects.get(pk=created.data["id"])
        assert appointment.status == "arrived"
        assert appointment.linked_active_patient_id is not None

        # A booking with a practitioner enters the queue as a consult (status 1).
        active = ActivePatient.objects.get(pk=appointment.linked_active_patient_id)
        assert active.patient_no_id == patient.patient_no
        assert active.status == 1
        assert active.consult_status is True

    def test_treatment_only_booking_skips_to_beautician_queue(self, auth_api, patient, room):
        created = auth_api.post(
            reverse("scheduled-appointments"),
            _payload(patient, None, room),
            format="json",
        )
        res = auth_api.post(
            reverse("scheduled-appointment-check-in", args=[created.data["id"]]),
            {},
            format="json",
        )
        assert res.status_code == 201, res.content

        appointment = Appointment.objects.get(pk=created.data["id"])
        active = ActivePatient.objects.get(pk=appointment.linked_active_patient_id)
        assert active.status == 3
        assert active.consult_status is False

    def test_guest_check_in_carries_the_guest_name(self, auth_api, room):
        created = auth_api.post(
            reverse("scheduled-appointments"),
            _payload(None, None, room, guest_name="Tamu Siti"),
            format="json",
        )
        auth_api.post(
            reverse("scheduled-appointment-check-in", args=[created.data["id"]]),
            {},
            format="json",
        )
        appointment = Appointment.objects.get(pk=created.data["id"])
        active = ActivePatient.objects.get(pk=appointment.linked_active_patient_id)
        assert active.patient_no_id is None
        assert active.guest_name == "Tamu Siti"

    def test_double_check_in_is_rejected(self, auth_api, patient, doctor, room):
        created = auth_api.post(
            reverse("scheduled-appointments"),
            _payload(patient, doctor, room),
            format="json",
        )
        url = reverse("scheduled-appointment-check-in", args=[created.data["id"]])
        assert auth_api.post(url, {}, format="json").status_code == 201
        assert auth_api.post(url, {}, format="json").status_code == 400
        assert ActivePatient.objects.count() == 1

    def test_cancelled_appointment_cannot_check_in(self, auth_api, patient, doctor, room):
        created = auth_api.post(
            reverse("scheduled-appointments"),
            _payload(patient, doctor, room),
            format="json",
        )
        auth_api.delete(
            reverse("scheduled-appointment-detail", args=[created.data["id"]]))
        res = auth_api.post(
            reverse("scheduled-appointment-check-in", args=[created.data["id"]]),
            {},
            format="json",
        )
        assert res.status_code == 400
        assert ActivePatient.objects.count() == 0


@pytest.mark.django_db
class TestSatuSehatReadiness:
    def test_ready_when_all_references_resolvable(self, auth_api, patient, doctor, room):
        res = auth_api.post(
            reverse("scheduled-appointments"),
            _payload(patient, doctor, room),
            format="json",
        )
        assert res.data["satusehat_readiness"] == {"ready": True, "gaps": []}

    def test_reports_each_missing_reference(self, auth_api):
        doctor_without_nik = Doctors.objects.create(doctor_name="Dr. Rina")
        res = auth_api.post(
            reverse("scheduled-appointments"),
            _payload(None, doctor_without_nik, None, guest_name="Tamu Siti"),
            format="json",
        )
        # Informational only — an unready booking is still accepted.
        assert res.status_code == 201, res.content
        readiness = res.data["satusehat_readiness"]
        assert readiness["ready"] is False
        assert set(readiness["gaps"]) == {
            "guest booking — no patient record",
            "practitioner has no NIK",
            "no location",
        }


@pytest.mark.django_db
class TestAppointmentLocations:
    def test_lists_only_active_rooms(self, auth_api, room):
        AppointmentLocation.objects.create(name="Ruang Lama", is_active=False)
        res = auth_api.get(reverse("appointment-locations"))
        assert res.status_code == 200
        assert [r["name"] for r in res.data] == ["Ruang 1A"]
