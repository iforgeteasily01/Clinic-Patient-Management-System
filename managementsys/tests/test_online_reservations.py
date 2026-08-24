"""Tests for the online reservation bridge.

Three things carry the design and are each pinned here:

* **Phone matching never guesses.** Free-text numbers on both sides are
  normalised, and two hits are handed to a human rather than resolved.
* **The import is idempotent.** The Vercel endpoint re-delivers anything
  unacked, so a redelivery must not produce a second appointment.
* **The appointment it writes is SatuSehat-shaped**, with both ends of the slot
  and a booked status — the same invariants test_scheduled_appointments.py
  enforces on the manual path.
"""
from datetime import timedelta, timezone as dt_timezone
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from managementsys.models import (
    JAKARTA_TZ, ActivePatient, Appointment, Patient, ReservationRequest,
)
from managementsys.services import reservation_sync


def _future_wib(days=1, hour=10):
    return (timezone.now().astimezone(JAKARTA_TZ) + timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0)


def _row(external_id=1, phone='628123456789', name='Siti Rahma', **overrides):
    start = overrides.pop('start', _future_wib())
    row = {
        'id': external_id,
        'name': name,
        'phone': phone,
        'reserved_at': start.astimezone(dt_timezone.utc).isoformat(),
        'reserved_at_wib': start.isoformat(),
        'status': 'pending',
        'service_id': 3,
        'service_name': 'Facial',
        'created_at': timezone.now().isoformat(),
    }
    row.update(overrides)
    return row


# ── Phone matching ───────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestPhoneMatching:
    @pytest.mark.parametrize('stored', [
        '08123456789',
        '628123456789',
        '+628123456789',
        '0812-3456-789',
        '8123456789',
    ])
    def test_matches_every_format_the_book_actually_holds(self, stored):
        patient = Patient.objects.create(name='Siti Rahma', phone_number=stored)
        status, matched, candidates = reservation_sync.match_patient('628123456789')
        assert status == ReservationRequest.MATCH_MATCHED
        assert matched.patient_no == patient.patient_no
        assert candidates == [patient.patient_no]

    def test_no_match_is_not_an_error(self):
        Patient.objects.create(name='Budi', phone_number='08999999999')
        status, matched, candidates = reservation_sync.match_patient('628123456789')
        assert status == ReservationRequest.MATCH_UNMATCHED
        assert matched is None
        assert candidates == []

    def test_two_patients_on_one_number_is_never_resolved(self):
        """A wrong link writes a stranger's visit into somebody's chart."""
        a = Patient.objects.create(name='Siti Rahma', phone_number='08123456789')
        b = Patient.objects.create(name='Siti R.', phone_number='628123456789')
        status, matched, candidates = reservation_sync.match_patient('08123456789')
        assert status == ReservationRequest.MATCH_AMBIGUOUS
        assert matched is None
        assert sorted(candidates) == sorted([a.patient_no, b.patient_no])

    def test_unusable_number_is_its_own_outcome(self):
        # A landline is not "no patient found" — it is a number reception has
        # to look at, and the two need different handling.
        status, matched, _ = reservation_sync.match_patient('0217654321')
        assert status == ReservationRequest.MATCH_INVALID
        assert matched is None

    def test_patients_with_no_phone_are_skipped_not_matched(self):
        Patient.objects.create(name='Tanpa Nomor', phone_number=None)
        Patient.objects.create(name='Kosong', phone_number='')
        status, _, _ = reservation_sync.match_patient('628123456789')
        assert status == ReservationRequest.MATCH_UNMATCHED


# ── Import ───────────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestImport:
    def test_creates_a_bookable_satusehat_shaped_appointment(self):
        patient = Patient.objects.create(name='Siti Rahma', phone_number='08123456789')
        start = _future_wib()

        row_obj, created = reservation_sync.import_reservation(_row(start=start))

        assert created is True
        assert row_obj.match_status == ReservationRequest.MATCH_MATCHED
        assert row_obj.matched_patient_id == patient.patient_no

        appointment = row_obj.appointment
        assert appointment.appointment_no.startswith('APT-')
        assert appointment.status == 'booked'
        assert appointment.source == 'online'
        assert appointment.patient_id == patient.patient_no
        assert appointment.contact_phone == '628123456789'
        assert appointment.service_type == 'Facial'
        # FHIR app-2/app-3: a booked slot carries both ends.
        assert appointment.end_at is not None
        assert appointment.end_at > appointment.start_at
        # And the instant survives the round trip through +07:00.
        assert appointment.start_at.astimezone(JAKARTA_TZ).hour == start.hour
        # Sync fields stay reserved for the sync phase.
        assert appointment.ihs_appointment_id is None
        assert appointment.sync_status == 'not_synced'

    def test_unmatched_booking_lands_as_a_guest_with_its_phone_kept(self):
        row_obj, _ = reservation_sync.import_reservation(_row(name='Dewi Putri'))
        assert row_obj.match_status == ReservationRequest.MATCH_UNMATCHED
        assert row_obj.appointment.patient_id is None
        assert row_obj.appointment.guest_name == 'Dewi Putri'
        # The only way to reach them, so it must be on the appointment itself.
        assert row_obj.appointment.contact_phone == '628123456789'

    def test_redelivery_does_not_book_the_slot_twice(self):
        first, created_first = reservation_sync.import_reservation(_row(external_id=7))
        second, created_second = reservation_sync.import_reservation(_row(external_id=7))

        assert created_first is True
        assert created_second is False
        assert first.pk == second.pk
        assert Appointment.objects.count() == 1

    def test_a_past_slot_still_imports(self):
        """The serializer refuses past start times; the poller must not.

        A booking made at 09:58 for 10:00 can easily reach the clinic after the
        slot has begun, and dropping it would lose a patient who is already on
        their way.
        """
        past = timezone.now().astimezone(JAKARTA_TZ) - timedelta(hours=2)
        row_obj, created = reservation_sync.import_reservation(_row(start=past))
        assert created is True
        assert row_obj.appointment.status == 'booked'

    def test_ambiguous_booking_carries_its_candidates(self):
        a = Patient.objects.create(name='Siti Rahma', phone_number='08123456789')
        b = Patient.objects.create(name='Siti R.', phone_number='+628123456789')
        row_obj, _ = reservation_sync.import_reservation(_row())
        assert row_obj.match_status == ReservationRequest.MATCH_AMBIGUOUS
        assert sorted(row_obj.candidate_patient_nos) == sorted(
            [a.patient_no, b.patient_no])
        assert row_obj.appointment.patient_id is None


# ── One pass ─────────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestRunOnce:
    def test_acks_only_what_committed(self):
        rows = [_row(external_id=1), _row(external_id=2, phone='628222222222')]
        with patch.object(reservation_sync, 'fetch_pending', return_value=(rows, False)), \
             patch.object(reservation_sync, 'ack', return_value=2) as acked:
            result = reservation_sync.run_once()

        assert result['imported'] == 2
        assert result['failed'] == []
        sent = acked.call_args[0][0]
        assert {a['id'] for a in sent} == {1, 2}
        # The appointment number goes back so the Vercel admin page can show
        # that a booking really landed.
        assert all(a['appointment_no'].startswith('APT-') for a in sent)

    def test_one_bad_row_does_not_strand_the_batch(self):
        rows = [_row(external_id=1), {'id': 2, 'name': 'Broken'}, _row(external_id=3)]
        with patch.object(reservation_sync, 'fetch_pending', return_value=(rows, False)), \
             patch.object(reservation_sync, 'ack', return_value=2) as acked:
            result = reservation_sync.run_once()

        assert result['imported'] == 2
        assert [f['id'] for f in result['failed']] == [2]
        # The failure is left unacked deliberately, so it comes back next minute.
        assert {a['id'] for a in acked.call_args[0][0]} == {1, 3}

    def test_offline_clinic_raises_rather_than_losing_bookings(self):
        with patch.object(
            reservation_sync, 'fetch_pending',
            side_effect=reservation_sync.ReservationSyncError('connection refused'),
        ):
            with pytest.raises(reservation_sync.ReservationSyncError):
                reservation_sync.run_once()
        assert ReservationRequest.objects.count() == 0


# ── Inbox endpoints ──────────────────────────────────────────────────────────

@pytest.fixture
def imported(db):
    return reservation_sync.import_reservation(_row())[0]


@pytest.mark.django_db
class TestInboxEndpoints:
    def test_inbox_defaults_to_what_needs_work(self, auth_api, imported):
        # Matched *and* acknowledged, so it should fall out of the default view.
        settled = reservation_sync.import_reservation(
            _row(external_id=2, phone='628222222222'))[0]
        settled.match_status = ReservationRequest.MATCH_MATCHED
        settled.acknowledged_at = timezone.now()
        settled.save()

        res = auth_api.get(reverse('reservations-inbox'))
        assert res.status_code == 200
        ids = [r['id'] for r in res.data['reservations']]
        assert imported.id in ids
        assert settled.id not in ids
        assert res.data['summary']['total'] == 2

        every = auth_api.get(reverse('reservations-inbox'), {'filter': 'all'})
        assert len(every.data['reservations']) == 2

    def test_linking_a_patient_updates_the_appointment_too(self, auth_api, imported):
        patient = Patient.objects.create(name='Siti Rahma', phone_number='08123456789')

        res = auth_api.post(
            reverse('reservation-link-patient', args=[imported.id]),
            {'patient_no': patient.patient_no}, format='json',
        )
        assert res.status_code == 200, res.content

        imported.refresh_from_db()
        imported.appointment.refresh_from_db()
        assert imported.match_status == ReservationRequest.MATCH_MATCHED
        assert imported.matched_patient_id == patient.patient_no
        # The chart is the subject once one is attached.
        assert imported.appointment.patient_id == patient.patient_no
        assert imported.appointment.guest_name is None
        # Linking counts as looking at it.
        assert imported.acknowledged_at is not None

    def test_cannot_repoint_a_booking_already_in_the_queue(self, auth_api, imported):
        """ActivePatient was created against whoever the row said it was."""
        active = ActivePatient.objects.create(
            guest_name='Siti Rahma', status=1, consult_status=False)
        imported.appointment.linked_active_patient = active
        imported.appointment.save()

        patient = Patient.objects.create(name='Siti Rahma', phone_number='08123456789')
        res = auth_api.post(
            reverse('reservation-link-patient', args=[imported.id]),
            {'patient_no': patient.patient_no}, format='json',
        )
        assert res.status_code == 400
        imported.refresh_from_db()
        assert imported.matched_patient_id is None

    def test_unknown_patient_is_refused(self, auth_api, imported):
        res = auth_api.post(
            reverse('reservation-link-patient', args=[imported.id]),
            {'patient_no': 'Z999999'}, format='json',
        )
        assert res.status_code == 400

    def test_acknowledge_only_moves_it_out_of_the_default_view(self, auth_api, imported):
        res = auth_api.post(reverse('reservation-acknowledge', args=[imported.id]))
        assert res.status_code == 200
        imported.refresh_from_db()
        assert imported.acknowledged_at is not None
        # A guest booking stays a guest booking — acknowledging is not linking.
        assert imported.matched_patient_id is None
        assert imported.appointment.patient_id is None

    def test_sync_now_reports_a_down_link_as_unavailable_not_a_crash(self, auth_api):
        with patch(
            'managementsys.views.reservations_inbox.run_once',
            side_effect=reservation_sync.ReservationSyncError('connection refused'),
        ):
            res = auth_api.post(reverse('reservations-sync-now'))
        assert res.status_code == 503
        assert 'connection refused' in res.data['error']


# ── The bridge into the queue ────────────────────────────────────────────────

@pytest.mark.django_db
def test_an_online_booking_checks_in_through_the_ordinary_door(auth_api):
    """One code path puts somebody in the queue, whatever booked them."""
    patient = Patient.objects.create(name='Siti Rahma', phone_number='08123456789')
    row_obj = reservation_sync.import_reservation(_row())[0]

    res = auth_api.post(
        reverse('scheduled-appointment-check-in', args=[row_obj.appointment_id]),
        {}, format='json',
    )
    assert res.status_code == 201, res.content

    row_obj.appointment.refresh_from_db()
    assert row_obj.appointment.status == 'arrived'
    active = ActivePatient.objects.get(pk=res.data['active_patient']['id'])
    assert active.patient_no_id == patient.patient_no
    # No practitioner on a web booking, so it goes straight to the treatment
    # queue, exactly as a treatment-only walk-in does.
    assert active.status == 3
