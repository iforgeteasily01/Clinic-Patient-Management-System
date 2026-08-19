"""Audience segments for a WhatsApp blast.

A segment answers "who should get this", and every one of them is filtered
through the same three gates before a message is composed:

1. **Opt-in.** ``Patient.wa_opt_in`` must be True. Not configurable, not
   overridable from the UI — a broadcast list the patient never joined is the
   thing consent exists to prevent.
2. **A usable number.** The phone must normalize to an Indonesian mobile
   (services/whatsapp_gateway.normalize_phone). A patient with a landline or a
   typo'd number is dropped, not guessed at.
3. **Cooldown.** Anyone already sent a blast inside
   ``WhatsAppSettings.per_patient_cooldown_days`` is excluded, however they
   qualified. Two segments overlapping is normal; two messages in one day is
   how a number gets reported.

Each gate reports how many it removed, so the UI can say "412 match, 38 opted
in, 5 messaged recently, 33 will be sent" instead of silently shipping 33.

Windows are Jakarta clinic days and a visit is a non-voided invoice — the same
two conventions as views/crm_dashboard.py, for the same reason.
"""
from __future__ import annotations

import datetime

from django.utils import timezone

from ..models import (
    Invoice,
    JAKARTA_TZ,
    Patient,
    ReportSettings,
    WhatsAppBlastRecipient,
)
from .patient_activity import classify
from .whatsapp_gateway import normalize_phone, to_chat_id

# The catalog. `key` is what the API takes, `window_days` is None for segments
# that are not a plain recency window. Order is the order the UI renders.
SEGMENTS = [
    {
        'key': 'recent',
        'label': 'Pasien terkini (7 hari)',
        'description': 'Datang dalam 7 hari terakhir. Cocok untuk follow-up pasca-treatment.',
        'window_days': 7,
    },
    {
        'key': 'last_week',
        'label': 'Kunjungan minggu lalu (8-14 hari)',
        'description': 'Datang 8-14 hari lalu — sudah lewat masa pemulihan awal.',
        'window_days': None,
    },
    {
        'key': 'last_month',
        'label': 'Kunjungan 30 hari terakhir',
        'description': 'Semua pasien yang datang dalam 30 hari terakhir.',
        'window_days': 30,
    },
    {
        'key': 'last_3_months',
        'label': 'Kunjungan 90 hari terakhir',
        'description': 'Semua pasien yang datang dalam 90 hari terakhir.',
        'window_days': 90,
    },
    {
        'key': 'new',
        'label': 'Pasien baru (30 hari)',
        'description': 'Kunjungan pertama mereka terjadi dalam 30 hari terakhir.',
        'window_days': 30,
    },
    {
        'key': 'lapsing',
        'label': 'Kurang aktif',
        'description': 'Belum kembali melewati batas "aktif" di Pengaturan Laporan, '
                       'tetapi belum masuk kategori tidak aktif. Antrean win-back.',
        'window_days': None,
    },
    {
        'key': 'birthday',
        'label': 'Ulang tahun (7 hari ke depan)',
        'description': 'Tanggal lahir jatuh dalam 7 hari ke depan. '
                       'Hanya pasien yang tanggal lahirnya sudah terisi.',
        'window_days': None,
    },
]

SEGMENT_KEYS = {s['key'] for s in SEGMENTS}

BIRTHDAY_LOOKAHEAD_DAYS = 7
NEW_PATIENT_DAYS = 30


def today_jkt():
    """Today as a Jakarta clinic day. Public: whatsapp_page shares it."""
    return timezone.now().astimezone(JAKARTA_TZ).date()


def _day_start(d):
    return datetime.datetime.combine(d, datetime.time.min, tzinfo=JAKARTA_TZ)


def _visited_between(start, end_exclusive):
    """Patient ids with a non-voided invoice in a half-open Jakarta day range."""
    return set(
        Invoice.objects
        .filter(is_voided=False, patient_no__isnull=False,
                datetime__gte=_day_start(start), datetime__lt=_day_start(end_exclusive))
        .values_list('patient_no', flat=True)
        .distinct()
    )


# ── Segment resolution ─────────────────────────────────────────────────────

def _segment_patient_ids(segment, today):
    """Patient ids matching ``segment``, before any of the three gates."""
    tomorrow = today + datetime.timedelta(days=1)

    if segment == 'recent':
        return _visited_between(today - datetime.timedelta(days=6), tomorrow)

    if segment == 'last_week':
        # 8-14 days ago, deliberately excluding the last 7: this segment exists
        # to reach people the 'recent' follow-up has already had, so overlapping
        # it would message the same patients twice in a fortnight.
        return _visited_between(
            today - datetime.timedelta(days=14),
            today - datetime.timedelta(days=6),
        )

    if segment == 'last_month':
        return _visited_between(today - datetime.timedelta(days=29), tomorrow)

    if segment == 'last_3_months':
        return _visited_between(today - datetime.timedelta(days=89), tomorrow)

    if segment == 'new':
        # First-ever visit inside the window. Computed as "visited recently" minus
        # "ever visited before the window" rather than with Min() over all
        # invoices, which would scan the whole table.
        window_start = today - datetime.timedelta(days=NEW_PATIENT_DAYS - 1)
        recent = _visited_between(window_start, tomorrow)
        earlier = set(
            Invoice.objects
            .filter(is_voided=False, patient_no__isnull=False,
                    datetime__lt=_day_start(window_start))
            .values_list('patient_no', flat=True)
            .distinct()
        )
        return recent - earlier

    if segment == 'lapsing':
        settings_obj = ReportSettings.get_solo()
        return {
            pno for pno, last_visit in Patient.objects
            .values_list('patient_no', 'crm_profile__last_visit_date')
            if classify(last_visit, today, settings_obj) == 'lapsing'
        }

    if segment == 'birthday':
        end = today + datetime.timedelta(days=BIRTHDAY_LOOKAHEAD_DAYS)
        ids = set()
        for pno, birth in Patient.objects.filter(birth_date__isnull=False).values_list(
                'patient_no', 'birth_date'):
            if today <= _next_birthday(birth, today) <= end:
                ids.add(pno)
        return ids

    return set()


def _next_birthday(birth_date, today):
    """Next occurrence of a birthday on or after today; 29 Feb observed 1 Mar."""
    def occurrence(year):
        try:
            return birth_date.replace(year=year)
        except ValueError:
            return datetime.date(year, 3, 1)

    this_year = occurrence(today.year)
    return this_year if this_year >= today else occurrence(today.year + 1)


# ── The three gates ────────────────────────────────────────────────────────

def resolve(segment, settings_obj, *, today=None, limit=None):
    """-> (recipients, counts)

    ``recipients`` is a list of dicts ready to compose a message against.
    ``counts`` explains the funnel from raw match to sendable, so the operator
    can see *why* a 400-patient segment produced 33 messages.
    """
    today = today or today_jkt()
    matched_ids = _segment_patient_ids(segment, today)
    matched = len(matched_ids)

    if not matched_ids:
        return [], _counts(0, 0, 0, 0, 0)

    # Gate 1 + 2 in one query: opted in, and a phone worth trying.
    candidates = list(
        Patient.objects
        .filter(patient_no__in=matched_ids, wa_opt_in=True)
        .exclude(phone_number__isnull=True)
        .exclude(phone_number='')
        .select_related('crm_profile__tier')
    )
    opted_in = len(candidates)

    with_number = []
    for patient in candidates:
        chat_id = to_chat_id(patient.phone_number)
        if chat_id:
            with_number.append((patient, chat_id))
    bad_number = opted_in - len(with_number)

    # Gate 3: cooldown. One query for everyone rather than per patient.
    cooldown_days = settings_obj.per_patient_cooldown_days or 0
    recently_messaged = set()
    if cooldown_days:
        cutoff = timezone.now() - datetime.timedelta(days=cooldown_days)
        recently_messaged = set(
            WhatsAppBlastRecipient.objects
            .filter(patient__in=[p for p, _ in with_number],
                    status='sent', sent_at__gte=cutoff)
            .values_list('patient_id', flat=True)
        )

    recipients = []
    for patient, chat_id in with_number:
        if patient.patient_no in recently_messaged:
            continue
        recipients.append({
            'patient': patient,
            'patient_no': patient.patient_no,
            'name': patient.name,
            'phone': normalize_phone(patient.phone_number),
            'chat_id': chat_id,
        })

    in_cooldown = len(with_number) - len(recipients)
    eligible = len(recipients)

    # The daily cap is applied last so `eligible` reports the true size of the
    # audience and the trim is visible as its own number.
    if limit is not None and len(recipients) > limit:
        recipients = recipients[:limit]

    return recipients, _counts(matched, opted_in, bad_number, in_cooldown, eligible)


def _counts(matched, opted_in, bad_number, in_cooldown, eligible):
    return {
        'matched': matched,
        'opted_in': opted_in,
        'not_opted_in': matched - opted_in,
        'unusable_number': bad_number,
        'in_cooldown': in_cooldown,
        'eligible': eligible,
    }


def segment_catalog(settings_obj, *, today=None):
    """The catalog with a live eligible-count per segment, for the picker."""
    today = today or today_jkt()
    out = []
    for spec in SEGMENTS:
        _, counts = resolve(spec['key'], settings_obj, today=today)
        out.append({**spec, 'counts': counts})
    return out
