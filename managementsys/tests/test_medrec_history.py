"""The medical-record archive: paging, sorting, and amending a finalized record.

The amend path is the load-bearing half. A finalized MedRec is a signed
clinical document, so the tests below pin three things: only a superuser or a
doctor may change one, the change lands in ``AuditLog`` with its previous
value, and a request that changes nothing writes no log row.
"""
import pytest
from rest_framework.test import APIClient

from ..models import AuditLog, Doctors, MedRec, Patient
from .factories import AppUserFactory


def _client(role):
    user = AppUserFactory(role=role, pin="654321")
    user.generate_token()
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {user.auth_token}")
    return api, user


def _patient(no, name):
    return Patient.objects.create(patient_no=no, name=name)


def _record(patient, date, **fields):
    """A finalized MedRec dated ``date`` (``YYYY-MM-DD``)."""
    rec = MedRec(patient_no=patient, status=MedRec.FINALIZED, **fields)
    rec.save(visit_date=date)
    return rec


LIST_URL = "/api/medical-records/history/"


def _detail_url(medrec_id):
    return f"/api/medical-records/history/{medrec_id}/"


@pytest.fixture
def archive(db):
    """Three finalized records across two patients, deliberately out of order."""
    zoe = _patient("Z00001", "Zoe Anggraini")
    adi = _patient("A00001", "Adi Pratama")
    return {
        "old": _record(zoe, "2026-01-05"),
        "new": _record(adi, "2026-08-20"),
        "mid": _record(zoe, "2026-04-10"),
        "zoe": zoe,
        "adi": adi,
    }


# ── Listing ────────────────────────────────────────────────────────────────────

def test_blank_search_is_newest_first(archive):
    api, _ = _client("cashier")
    body = api.get(LIST_URL).json()

    assert [r["visit_date"] for r in body["results"]] == [
        "2026-08-20", "2026-04-10", "2026-01-05",
    ]
    assert body["count"] == 3
    assert body["page"] == 1
    assert body["total_pages"] == 1


def test_date_sort_ascending(archive):
    api, _ = _client("cashier")
    body = api.get(LIST_URL, {"sort": "visit_date", "dir": "asc"}).json()

    assert [r["visit_date"] for r in body["results"]] == [
        "2026-01-05", "2026-04-10", "2026-08-20",
    ]


def test_patient_name_sort(archive):
    api, _ = _client("cashier")
    names = [r["patient_name"] for r in
             api.get(LIST_URL, {"sort": "patient_name", "dir": "asc"}).json()["results"]]

    assert names == ["Adi Pratama", "Zoe Anggraini", "Zoe Anggraini"]


def test_unknown_sort_key_falls_back_to_the_date(archive):
    """A bad ``sort`` is ignored, not a 500 — the column list is a UI concern."""
    api, _ = _client("cashier")
    body = api.get(LIST_URL, {"sort": "'; DROP TABLE"}).json()

    assert body["sort"] == "visit_date"
    assert body["results"][0]["visit_date"] == "2026-08-20"


def test_paging_splits_the_result_set(archive):
    api, _ = _client("cashier")

    first = api.get(LIST_URL, {"page_size": 2}).json()
    assert len(first["results"]) == 2
    assert first["total_pages"] == 2

    second = api.get(LIST_URL, {"page_size": 2, "page": 2}).json()
    assert len(second["results"]) == 1
    assert second["results"][0]["visit_date"] == "2026-01-05"


def test_page_past_the_end_clamps_to_the_last_page(archive):
    """Filtering down to fewer pages while on page 9 must not answer nothing."""
    api, _ = _client("cashier")
    body = api.get(LIST_URL, {"page_size": 2, "page": 99}).json()

    assert body["page"] == 2
    assert len(body["results"]) == 1


def test_filters_still_apply(archive):
    api, _ = _client("cashier")
    body = api.get(LIST_URL, {"patient_name": "zoe"}).json()

    assert body["count"] == 2
    assert {r["patient_no"] for r in body["results"]} == {"Z00001"}


def test_drafts_are_excluded_unless_asked_for(archive):
    MedRec(patient_no=archive["adi"], status=MedRec.DRAFT).save(visit_date="2026-08-21")
    api, _ = _client("cashier")

    assert api.get(LIST_URL).json()["count"] == 3
    assert api.get(LIST_URL, {"include_drafts": "true"}).json()["count"] == 4


# ── Amending ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role", ["superuser", "doctor"])
def test_editor_roles_may_amend(archive, role):
    api, _ = _client(role)
    res = api.patch(
        _detail_url(archive["new"].medrec_id),
        {"assessment": "Acne vulgaris, moderate"},
        format="json",
    )

    assert res.status_code == 200
    assert res.json()["assessment"] == "Acne vulgaris, moderate"
    archive["new"].refresh_from_db()
    assert archive["new"].assessment == "Acne vulgaris, moderate"


@pytest.mark.parametrize("role", ["cashier", "beautician", "manager"])
def test_other_roles_are_refused(archive, role):
    api, _ = _client(role)
    res = api.patch(
        _detail_url(archive["new"].medrec_id),
        {"assessment": "tampered"},
        format="json",
    )

    assert res.status_code == 403
    archive["new"].refresh_from_db()
    assert archive["new"].assessment == ""


def test_anonymous_is_refused(archive):
    res = APIClient().patch(
        _detail_url(archive["new"].medrec_id),
        {"assessment": "tampered"},
        format="json",
    )

    assert res.status_code in (401, 403)
    archive["new"].refresh_from_db()
    assert archive["new"].assessment == ""


def test_amendment_is_logged_with_the_previous_value(archive):
    record = _record(archive["adi"], "2026-08-22", plan="Retinol nightly")
    api, user = _client("doctor")

    api.patch(_detail_url(record.medrec_id), {"plan": "Retinol alternate nights"},
              format="json")

    entry = AuditLog.objects.filter(
        entity_type="MedRec",
        entity_id=record.medrec_id,
        source=AuditLog.SOURCE_APP,
    ).latest("timestamp")

    assert entry.action == "UPDATE"
    assert entry.performed_by_id == user.id
    assert "Retinol nightly" in entry.description       # what it was
    assert "Retinol alternate nights" in entry.description  # what it became
    assert "plan" in entry.description


def test_a_no_op_patch_writes_no_log_row(archive):
    """An audit trail full of non-edits makes the real ones harder to find."""
    record = _record(archive["adi"], "2026-08-23", plan="Retinol nightly")
    api, _ = _client("doctor")

    before = AuditLog.objects.filter(
        entity_type="MedRec", source=AuditLog.SOURCE_APP).count()
    res = api.patch(_detail_url(record.medrec_id), {"plan": "Retinol nightly"},
                    format="json")

    assert res.status_code == 200
    assert AuditLog.objects.filter(
        entity_type="MedRec", source=AuditLog.SOURCE_APP).count() == before


def test_identity_fields_cannot_be_reassigned(archive):
    """Only clinical content is editable — not whose chart this is."""
    record = archive["new"]
    doctor = Doctors.objects.create(doctor_name="Dr. Someone Else")
    api, _ = _client("superuser")

    api.patch(
        _detail_url(record.medrec_id),
        {"patient_no": archive["zoe"].patient_no,
         "doctor_id": doctor.pk,
         "medrec_id": "MR-HACKED-20260101-1",
         "status": MedRec.DRAFT,
         "subjective": "kept"},
        format="json",
    )

    record.refresh_from_db()
    assert record.patient_no_id == archive["adi"].patient_no
    assert record.doctor_id_id is None
    assert record.status == MedRec.FINALIZED
    assert record.subjective == "kept"


def test_missing_record_is_404(db):
    api, _ = _client("superuser")
    res = api.patch(_detail_url("MR-NOPE-20260101-1"), {"plan": "x"}, format="json")

    assert res.status_code == 404


def test_assessment_codes_must_be_a_list(archive):
    api, _ = _client("doctor")
    res = api.patch(_detail_url(archive["new"].medrec_id),
                    {"assessment_codes": "L70.0"}, format="json")

    assert res.status_code == 400
