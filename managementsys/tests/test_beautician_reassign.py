"""Handing a patient in treatment over to another beautician.

The load-bearing property: the hand-over is *recorded*, never overwritten.
The queue shows the new beautician, the old assignment survives with an end
time, and the sessions the first beautician started still name them (HR
performance credits from those).
"""
from decimal import Decimal

import pytest

from managementsys.api.serializers import ActivePatientSerializer
from managementsys.models import (
    ActivePatient, AuditLog, BeauticianAssignment, Beauticians, Patient, Treatment,
    TreatmentSession,
)

pytestmark = pytest.mark.django_db


def _url(visit):
    return f'/api/activepatients/{visit.pk}/beautician/'


@pytest.fixture
def setup(auth_api):
    patient = Patient.objects.create(name='Rina', phone_number='0812')
    visit = ActivePatient.objects.create(patient_no=patient, status=3, consult_status=False)
    ana = Beauticians.objects.create(beautician_name='Ana', bphone_number='1')
    bela = Beauticians.objects.create(beautician_name='Bela', bphone_number='2')
    facial = Treatment.objects.create(code='FCL', name='Facial', category='Facial', price=Decimal('100000'))
    res = auth_api.post('/api/treatment-session/', {
        'active_patient_id': visit.pk, 'beautician': ana.pk, 'treatment_ids': [facial.pk],
    }, format='json')
    assert res.status_code == 201, res.content
    visit.refresh_from_db()
    return visit, ana, bela, facial


def test_starting_a_session_opens_an_assignment(setup):
    visit, ana, _, _ = setup
    rows = list(BeauticianAssignment.objects.filter(active_patient=visit))
    assert [(r.beautician_id, r.ended_at) for r in rows] == [(ana.pk, None)]
    ana.refresh_from_db()
    assert ana.available is False


def test_reassign_shows_new_beautician_and_keeps_history(auth_api, setup):
    visit, ana, bela, _ = setup
    res = auth_api.post(_url(visit), {'beautician': bela.pk, 'reason': 'Ana pulang'}, format='json')
    assert res.status_code == 200, res.content
    body = res.json()
    assert body['current_beautician_id'] == bela.pk
    assert [h['beautician_name'] for h in body['history']] == ['Ana', 'Bela']
    assert body['history'][0]['ended_at'] is not None
    assert body['history'][1]['ended_at'] is None
    assert body['history'][1]['reason'] == 'Ana pulang'

    # What the treatment queue and /appointments render.
    row = ActivePatientSerializer(visit).data
    assert row['current_beautician_id'] == bela.pk
    assert row['current_beautician_name'] == 'Bela'

    # The session Ana started is still hers.
    assert set(TreatmentSession.objects.filter(active_patient=visit)
               .values_list('beautician_id', flat=True)) == {ana.pk}

    ana.refresh_from_db(); bela.refresh_from_db()
    assert ana.available is True
    assert bela.available is False
    assert AuditLog.objects.filter(entity_type='ActivePatient', entity_id=str(visit.pk),
                                   description__contains='Ana -> Bela').exists()


def test_adding_treatments_after_handover_credits_new_beautician(auth_api, setup):
    visit, ana, bela, facial = setup
    auth_api.post(_url(visit), {'beautician': bela.pk}, format='json')
    res = auth_api.post('/api/treatment-session/', {
        'active_patient_id': visit.pk, 'beautician': bela.pk, 'treatment_ids': [facial.pk],
    }, format='json')
    assert res.status_code == 201
    # Same beautician again is not a new hand-over.
    assert BeauticianAssignment.objects.filter(active_patient=visit).count() == 2
    assert list(TreatmentSession.objects.filter(active_patient=visit)
                .order_by('id').values_list('beautician_id', flat=True)) == [ana.pk, bela.pk]


def test_same_beautician_is_a_noop(auth_api, setup):
    visit, ana, _, _ = setup
    res = auth_api.post(_url(visit), {'beautician': ana.pk}, format='json')
    assert res.status_code == 200
    assert BeauticianAssignment.objects.filter(active_patient=visit).count() == 1
    assert not AuditLog.objects.filter(description__contains='Beautician for').exists()


def test_completing_frees_the_current_beautician_and_closes_assignment(auth_api, setup):
    visit, ana, bela, _ = setup
    auth_api.post(_url(visit), {'beautician': bela.pk}, format='json')
    res = auth_api.post('/api/treatment-session/complete/', {'active_patient_id': visit.pk}, format='json')
    assert res.status_code == 200
    bela.refresh_from_db()
    assert bela.available is True
    assert not BeauticianAssignment.objects.filter(active_patient=visit, ended_at__isnull=True).exists()


def test_outgoing_beautician_stays_busy_if_holding_another_patient(auth_api, setup):
    visit, ana, bela, facial = setup
    other = ActivePatient.objects.create(guest_name='Tamu', status=3, consult_status=False)
    auth_api.post('/api/treatment-session/', {
        'active_patient_id': other.pk, 'beautician': ana.pk, 'treatment_ids': [facial.pk],
    }, format='json')
    auth_api.post(_url(visit), {'beautician': bela.pk}, format='json')
    ana.refresh_from_db()
    assert ana.available is False


def test_history_outlives_the_visit(auth_api, setup):
    visit, ana, bela, _ = setup
    auth_api.post(_url(visit), {'beautician': bela.pk}, format='json')
    patient_no = visit.patient_no_id
    visit.delete()
    rows = BeauticianAssignment.objects.filter(patient_no_id=patient_no).order_by('assigned_at', 'id')
    assert [r.beautician_id for r in rows] == [ana.pk, bela.pk]


def test_refused_unless_in_treatment(auth_api, db):
    visit = ActivePatient.objects.create(guest_name='Tamu', status=3, consult_status=False)
    b = Beauticians.objects.create(beautician_name='C', bphone_number='3')
    res = auth_api.post(_url(visit), {'beautician': b.pk}, format='json')
    assert res.status_code == 400
    assert not BeauticianAssignment.objects.exists()


def test_unknown_beautician(auth_api, setup):
    visit, *_ = setup
    assert auth_api.post(_url(visit), {'beautician': 99999}, format='json').status_code == 400
