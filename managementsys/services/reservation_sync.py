"""
Collects online bookings from the public reservation form.

The clinic backend runs on a LAN with no public address, so the Vercel app
cannot push to it the way Django pushes events out (``vercel_push.py``). The
direction is reversed: this module polls ``GET /api/reservation-sync``, writes
what it finds, and only then acks — so a crash mid-import costs a redelivery,
never a booking.

Nothing here talks to SatuSehat. It fills the FHIR-shaped ``Appointment``
fields correctly (status, both ends of the slot, service coding) so the future
sync stays a mapping walk, which is the same contract
``views/appointments_scheduled.py`` already keeps.
"""

import json
import logging
import urllib.error
import urllib.request
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from ..models import Appointment, Branch, Patient, ReservationRequest
from .whatsapp_gateway import normalize_phone

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15


class ReservationSyncError(Exception):
    """The Vercel endpoint could not be reached, or answered badly."""


# ── Transport ────────────────────────────────────────────────────────────────

def _endpoint():
    base = (getattr(settings, 'CPMS_VERCEL_URL', '') or '').rstrip('/')
    if not base:
        raise ReservationSyncError('CPMS_VERCEL_URL is not set.')
    return base + '/api/reservation-sync'


def _secret():
    secret = getattr(settings, 'CPMS_INGEST_SECRET', '') or ''
    if not secret:
        raise ReservationSyncError('CPMS_INGEST_SECRET is not set.')
    return secret


def _request(method, query='', body=None):
    url = _endpoint() + query
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            'Content-Type': 'application/json',
            'x-cpms-secret': _secret(),
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            return json.loads(resp.read().decode('utf-8') or '{}')
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', 'replace')[:300]
        raise ReservationSyncError(
            '%s %s returned HTTP %s: %s' % (method, url, exc.code, detail)
        ) from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        # A clinic that lost its internet is the normal case, not an incident:
        # the rows stay queued on Vercel and the next poll collects them.
        raise ReservationSyncError('%s %s failed: %s' % (method, url, exc)) from exc


def fetch_pending(limit=None):
    """Every booking Vercel still has queued. Returns (rows, more)."""
    query = ('?limit=%d' % int(limit)) if limit else ''
    payload = _request('GET', query=query)
    rows = payload.get('reservations')
    if not isinstance(rows, list):
        raise ReservationSyncError('Response carried no `reservations` list.')
    return rows, bool(payload.get('more'))


def ack(acks):
    """Mark rows collected. ``acks`` is [{'id': int, 'appointment_no': str|None}]."""
    if not acks:
        return 0
    payload = _request('POST', body={'acks': acks})
    return int(payload.get('acked', 0))


# ── Patient matching ─────────────────────────────────────────────────────────

def match_patient(raw_phone):
    """Resolve a booking phone number to a patient record.

    Returns ``(match_status, patient_or_None, candidate_patient_nos)``.

    ``Patient.phone_number`` is free text — the book holds ``08123456789``,
    ``0812-3456-789`` and ``+628123456789`` for the same number — so no SQL
    predicate matches all of them. Both sides are normalised in Python instead,
    over a scan of two small columns. At clinic scale that is a few
    milliseconds once a minute, and it is *exact* where a LIKE would be a guess.

    Two matches are never collapsed into one. A wrong link writes a stranger's
    visit into somebody's medical record — far worse than a row a human has to
    look at.
    """
    normalized = normalize_phone(raw_phone)
    if not normalized:
        return ReservationRequest.MATCH_INVALID, None, []

    hits = [
        patient_no
        for patient_no, phone in (
            Patient.objects
            .exclude(phone_number__isnull=True)
            .exclude(phone_number='')
            .values_list('patient_no', 'phone_number')
        )
        if normalize_phone(phone) == normalized
    ]

    if not hits:
        return ReservationRequest.MATCH_UNMATCHED, None, []
    if len(hits) > 1:
        return ReservationRequest.MATCH_AMBIGUOUS, None, sorted(hits)
    return (
        ReservationRequest.MATCH_MATCHED,
        Patient.objects.filter(patient_no=hits[0]).first(),
        hits,
    )


# ── Import ───────────────────────────────────────────────────────────────────

def _default_duration():
    return int(getattr(settings, 'CPMS_RESERVATION_DURATION_MINUTES', 30))


def _default_branch():
    """The branch an online booking is filed under.

    Unset means null, which Appointment already allows and which
    ``filter_by_branch`` keeps visible under any branch selection — a booking
    nobody can see would be worse than one filed group-wide.
    """
    branch_id = getattr(settings, 'CPMS_RESERVATION_BRANCH_ID', None)
    if not branch_id:
        return None
    return Branch.objects.filter(pk=branch_id).first()


def _parse_start(row):
    """Prefer the WIB form; both carry an explicit offset, so either is exact."""
    raw = row.get('reserved_at_wib') or row.get('reserved_at')
    start = parse_datetime(raw) if raw else None
    if start is None:
        raise ValueError('unparseable reserved_at: %r' % (raw,))
    if timezone.is_naive(start):
        start = timezone.make_aware(start, timezone.get_current_timezone())
    return start


def import_reservation(row):
    """Write one booking. Returns ``(ReservationRequest, created)``.

    Idempotent on ``external_id``, which is the whole story for redeliveries:
    the poll endpoint keeps handing a row back until it is acked, and a second
    delivery must not produce a second appointment.
    """
    external_id = row.get('id')
    if not isinstance(external_id, int):
        raise ValueError('reservation has no integer id: %r' % (row,))

    existing = ReservationRequest.objects.filter(external_id=external_id).first()
    if existing is not None:
        return existing, False

    start_at = _parse_start(row)
    name = (row.get('name') or '').strip() or 'Tanpa Nama'
    phone = (row.get('phone') or '').strip()
    service_name = (row.get('service_name') or '').strip()

    match_status, patient, candidates = match_patient(phone)

    appointment = Appointment.objects.create(
        patient=patient,
        # A matched booking keeps the typed name too: the patient wrote it, and
        # reception uses it to confirm the link is the right one.
        guest_name=None if patient else name,
        practitioner=None,
        location=None,
        branch=_default_branch(),
        service_category='',
        service_type=service_name,
        appointment_type='routine',
        source='online',
        contact_phone=phone,
        reason='',
        start_at=start_at,
        # FHIR invariants app-2/app-3: a booked appointment carries both ends.
        # The public form books a slot rather than a duration, so this is the
        # slot length — reception lengthens it if the visit needs more.
        end_at=start_at + timedelta(minutes=_default_duration()),
        status='booked',
        note='Reservasi online atas nama %s (%s).' % (name, phone),
    )

    request_row = ReservationRequest.objects.create(
        external_id=external_id,
        name=name,
        phone=phone,
        reserved_at=start_at,
        service_name=service_name,
        service_id=row.get('service_id'),
        match_status=match_status,
        matched_patient=patient,
        candidate_patient_nos=(
            candidates if match_status == ReservationRequest.MATCH_AMBIGUOUS else []
        ),
        appointment=appointment,
        raw=row,
    )
    return request_row, True


# ── One pass ─────────────────────────────────────────────────────────────────

def run_once(limit=None):
    """Fetch, import and ack one batch. Returns a summary dict.

    Import failures are per row: one malformed booking must not strand the rest
    of the batch behind it. A failed row is left unacked, so it comes back next
    minute and stays visible in the log rather than disappearing.
    """
    rows, more = fetch_pending(limit)

    imported, duplicates, failed, acks = 0, 0, [], []

    for row in rows:
        try:
            with transaction.atomic():
                request_row, created = import_reservation(row)
        except Exception as exc:
            logger.warning(
                '[reservation_sync] import failed for row %s: %s',
                row.get('id'), exc,
            )
            failed.append({'id': row.get('id'), 'error': str(exc)})
            continue

        if created:
            imported += 1
        else:
            duplicates += 1
        acks.append({
            'id': request_row.external_id,
            'appointment_no': (
                request_row.appointment.appointment_no
                if request_row.appointment_id else None
            ),
        })

    # Acked only once the writes have committed. Rows in `failed` stay queued
    # on Vercel deliberately.
    acked = ack(acks) if acks else 0

    return {
        'fetched': len(rows),
        'imported': imported,
        'duplicates': duplicates,
        'failed': failed,
        'acked': acked,
        'more': more,
    }
