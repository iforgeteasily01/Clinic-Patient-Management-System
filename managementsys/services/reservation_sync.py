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
    pass


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
        raise ReservationSyncError('%s %s failed: %s' % (method, url, exc)) from exc


def fetch_pending(limit=None):
    query = ('?limit=%d' % int(limit)) if limit else ''
    payload = _request('GET', query=query)
    rows = payload.get('reservations')
    if not isinstance(rows, list):
        raise ReservationSyncError('Response carried no `reservations` list.')
    return rows, bool(payload.get('more'))


def ack(acks):
    if not acks:
        return 0
    payload = _request('POST', body={'acks': acks})
    return int(payload.get('acked', 0))


def match_patient(raw_phone):
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


def _default_duration():
    return int(getattr(settings, 'CPMS_RESERVATION_DURATION_MINUTES', 30))


def _default_branch():
    branch_id = getattr(settings, 'CPMS_RESERVATION_BRANCH_ID', None)
    if not branch_id:
        return None
    return Branch.objects.filter(pk=branch_id).first()


def _parse_start(row):
    raw = row.get('reserved_at_wib') or row.get('reserved_at')
    start = parse_datetime(raw) if raw else None
    if start is None:
        raise ValueError('unparseable reserved_at: %r' % (raw,))
    if timezone.is_naive(start):
        start = timezone.make_aware(start, timezone.get_current_timezone())
    return start


def import_reservation(row):
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


def run_once(limit=None):
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

    acked = ack(acks) if acks else 0

    return {
        'fetched': len(rows),
        'imported': imported,
        'duplicates': duplicates,
        'failed': failed,
        'acked': acked,
        'more': more,
    }
