"""WhatsApp gateway settings, session pairing, and CRM blasts.

The gateway is OpenWA (github.com/rmyndharis/OpenWA) running as its own Node
service; see services/whatsapp_gateway.py for the route map. Django holds the
connection settings, decides *who* gets a message, and keeps the record of what
was sent. It never speaks to WhatsApp itself.

How a blast runs
----------------
OpenWA's ``send-bulk`` is asynchronous: it accepts up to 100 messages, returns a
``batchId`` immediately, and delivers them in the background with a delay
between each. So there is no background worker on the Django side either —

    POST /api/whatsapp/blasts/        writes every recipient row, dispatches
                                      the first batch, returns
    GET  /api/whatsapp/blasts/<id>/   syncs that batch from the gateway, and
                                      dispatches the next one when it drains

The page polls the detail endpoint while a blast is running, which is what
advances it. A blast therefore survives a Django restart: the state is entirely
in the two tables plus the gateway, and the next poll picks it up.

Guard rails, all enforced here and not in the browser
-----------------------------------------------------
* opt-in, usable number, and per-patient cooldown (services/wa_audience.py)
* a daily cap across all blasts
* quiet hours in Jakarta local time
* the session must actually be paired (``status == 'ready'``)

None of them are overridable from the UI. A blast is easy to fire and expensive
to take back.
"""
from __future__ import annotations

import datetime
import logging

from django.db import transaction
from django.utils import timezone
from rest_framework import status as http
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import (
    AppUser,
    AuditLog,
    JAKARTA_TZ,
    MessageTemplate,
    Patient,
    WhatsAppBlast,
    WhatsAppBlastRecipient,
    WhatsAppSettings,
)
from ..services import message_templates as mt
from ..services import wa_audience
from ..services.whatsapp_gateway import (
    BULK_MAX,
    DELAY_MAX_MS,
    DELAY_MIN_MS,
    READY_STATES,
    GatewayError,
    client_from_settings,
    normalize_phone,
    to_chat_id,
)

logger = logging.getLogger(__name__)

# A blast preview never renders more than this many rows. The counts are exact;
# the list is a sample, because a 2000-recipient preview helps nobody and costs
# 2000 template renders.
PREVIEW_ROWS = 50


def _actor(request):
    return request.user if isinstance(request.user, AppUser) else None


def _now_jkt():
    return timezone.now().astimezone(JAKARTA_TZ)


# ── Settings ───────────────────────────────────────────────────────────────

def _serialize_settings(s):
    return {
        'enabled': s.enabled,
        'base_url': s.base_url,
        # Never the key itself. The operator needs to know whether one is set
        # and roughly which, not to be able to read it back out of the browser.
        'api_key_set': bool(s.api_key),
        'api_key_hint': f'…{s.api_key[-4:]}' if len(s.api_key) >= 4 else '',
        'session_id': s.session_id,
        'session_name': s.session_name,
        'delay_between_messages_ms': s.delay_between_messages_ms,
        'randomize_delay': s.randomize_delay,
        'daily_send_cap': s.daily_send_cap,
        'per_patient_cooldown_days': s.per_patient_cooldown_days,
        'quiet_hours_start': s.quiet_hours_start,
        'quiet_hours_end': s.quiet_hours_end,
        'test_recipient': s.test_recipient,
        'updated_at': s.updated_at.isoformat(),
        'limits': {
            'delay_min_ms': DELAY_MIN_MS,
            'delay_max_ms': DELAY_MAX_MS,
            'bulk_max': BULK_MAX,
        },
    }


class WhatsAppSettingsView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        return Response(_serialize_settings(WhatsAppSettings.get_solo()))

    def put(self, request):
        s = WhatsAppSettings.get_solo()
        payload = request.data
        errors = {}

        if 'base_url' in payload:
            s.base_url = (payload['base_url'] or '').strip().rstrip('/')
        if 'session_name' in payload:
            name = (payload['session_name'] or '').strip()
            # OpenWA's CreateSessionDto: 3-50 chars, letters/digits/hyphen only.
            if name and not _valid_session_name(name):
                errors['session_name'] = ('3-50 karakter, hanya huruf, angka, dan tanda hubung.')
            else:
                s.session_name = name
        if 'api_key' in payload:
            key = (payload['api_key'] or '').strip()
            # An empty string means "leave it alone", not "clear it" — the form
            # never receives the current key, so submitting the form unchanged
            # would otherwise wipe it. Clearing is an explicit action below.
            if key:
                s.api_key = key
        if payload.get('clear_api_key'):
            s.api_key = ''
        if 'enabled' in payload:
            s.enabled = bool(payload['enabled'])
        if 'randomize_delay' in payload:
            s.randomize_delay = bool(payload['randomize_delay'])
        if 'test_recipient' in payload:
            s.test_recipient = (payload['test_recipient'] or '').strip()

        for field, lo, hi in [
            ('delay_between_messages_ms', DELAY_MIN_MS, DELAY_MAX_MS),
            ('daily_send_cap', 0, 5000),
            ('per_patient_cooldown_days', 0, 365),
            ('quiet_hours_start', 0, 23),
            ('quiet_hours_end', 0, 23),
        ]:
            if field in payload:
                try:
                    value = int(payload[field])
                except (TypeError, ValueError):
                    errors[field] = 'Harus berupa angka.'
                    continue
                if not (lo <= value <= hi):
                    errors[field] = f'Harus antara {lo} dan {hi}.'
                    continue
                setattr(s, field, value)

        if errors:
            return Response(errors, status=http.HTTP_400_BAD_REQUEST)

        s.updated_by = _actor(request)
        s.save()
        return Response(_serialize_settings(s))


def _valid_session_name(name):
    return 3 <= len(name) <= 50 and all(c.isalnum() or c == '-' for c in name)


# ── Connection status & pairing ────────────────────────────────────────────

class WhatsAppStatusView(APIView):
    """GET /api/whatsapp/status/

    Never raises: an unreachable gateway is the normal state before setup, and
    the settings page has to be able to render and say so.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        s = WhatsAppSettings.get_solo()
        out = {
            'configured': bool(s.base_url and s.api_key),
            'enabled': s.enabled,
            'gateway_reachable': False,
            'gateway_error': '',
            'session': None,
            'session_status': 'unknown',
            'can_send': False,
        }
        if not s.base_url:
            out['gateway_error'] = 'Alamat gateway belum diatur.'
            return Response(out)

        client = client_from_settings(s)
        try:
            client.health()
            out['gateway_reachable'] = True
        except GatewayError as exc:
            out['gateway_error'] = str(exc)
            return Response(out)

        if not s.session_id:
            out['session_status'] = 'not_created'
            return Response(out)

        try:
            session = client.get_session(s.session_id)
        except GatewayError as exc:
            # A 404 here means the session was deleted on the gateway side.
            out['session_status'] = 'missing' if exc.status == 404 else 'error'
            out['gateway_error'] = str(exc)
            return Response(out)

        out['session'] = {
            'id': session.get('id'),
            'name': session.get('name'),
            'status': session.get('status'),
            'phone': session.get('phone'),
            'push_name': session.get('pushName'),
            'connected_at': session.get('connectedAt'),
            'last_error': session.get('lastError'),
            'restriction': session.get('restriction'),
        }
        out['session_status'] = session.get('status') or 'unknown'
        out['can_send'] = bool(s.enabled and out['session_status'] in READY_STATES)
        return Response(out)


class WhatsAppSessionView(APIView):
    """POST /api/whatsapp/session/<action>/ — create | start | stop | logout | qr

    Pairing is inherently a sequence of gateway calls, and folding them into one
    endpoint per action keeps the settings page from having to know OpenWA's
    session lifecycle.
    """
    permission_classes = [AllowAny]

    def post(self, request, action):
        s = WhatsAppSettings.get_solo()
        client = client_from_settings(s)

        try:
            if action == 'create':
                if not s.session_name:
                    return Response({'detail': 'Nama sesi belum diatur.'},
                                    status=http.HTTP_400_BAD_REQUEST)
                # Reuse a session of the same name if the gateway already has
                # one — creating a duplicate is a 409 and leaves the operator
                # stuck with no way forward from the UI.
                existing = next(
                    (x for x in client.list_sessions() if x.get('name') == s.session_name),
                    None,
                )
                session = existing or client.create_session(s.session_name)
                s.session_id = session.get('id') or ''
                s.save(update_fields=['session_id', 'updated_at'])
                _log(request, 'CREATE', f'WhatsApp session {s.session_name} ({s.session_id})')
                return Response({'session_id': s.session_id, 'reused': bool(existing)})

            if not s.session_id:
                return Response({'detail': 'Sesi belum dibuat.'}, status=http.HTTP_400_BAD_REQUEST)

            if action == 'start':
                return Response(client.start_session(s.session_id))
            if action == 'stop':
                return Response(client.stop_session(s.session_id))
            if action == 'logout':
                _log(request, 'UPDATE', f'WhatsApp session {s.session_name} logged out')
                return Response(client.logout_session(s.session_id))
            if action == 'qr':
                return Response(client.get_qr(s.session_id))

        except GatewayError as exc:
            return Response({'detail': str(exc)}, status=http.HTTP_502_BAD_GATEWAY)

        return Response({'detail': 'Aksi tidak dikenal.'}, status=http.HTTP_400_BAD_REQUEST)


class WhatsAppTestMessageView(APIView):
    """POST /api/whatsapp/test-message/  {to?, text?}

    Bypasses opt-in and cooldown by design: it goes to a number the operator
    typed on the settings page, not to a patient, and its whole purpose is to
    prove the pairing works before anyone trusts it with a real audience. It is
    still refused when the session is not ready.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        s = WhatsAppSettings.get_solo()
        raw = (request.data.get('to') or s.test_recipient or '').strip()
        chat_id = to_chat_id(raw)
        if not chat_id:
            return Response(
                {'to': 'Nomor tidak valid. Gunakan format 08xx… atau 628xx….'},
                status=http.HTTP_400_BAD_REQUEST,
            )

        text = (request.data.get('text') or '').strip() or (
            'Tes koneksi WhatsApp dari sistem klinik. '
            'Pesan ini dikirim manual dari halaman Pengaturan.'
        )

        client = client_from_settings(s)
        try:
            session = client.get_session(s.session_id) if s.session_id else {}
            if (session.get('status') or '') not in READY_STATES:
                return Response(
                    {'detail': f"Sesi belum siap (status: {session.get('status') or 'tidak ada'})."},
                    status=http.HTTP_409_CONFLICT,
                )
            result = client.send_text(s.session_id, chat_id, text)
        except GatewayError as exc:
            return Response({'detail': str(exc)}, status=http.HTTP_502_BAD_GATEWAY)

        if raw and raw != s.test_recipient:
            s.test_recipient = raw
            s.save(update_fields=['test_recipient', 'updated_at'])

        _log(request, 'CREATE', f'WhatsApp test message to {chat_id}')
        return Response({'sent': True, 'chat_id': chat_id, 'result': result})


# ── Audience segments ──────────────────────────────────────────────────────

class WhatsAppSegmentsView(APIView):
    """GET /api/whatsapp/segments/ — the picker, with a live count per segment."""
    permission_classes = [AllowAny]

    def get(self, request):
        s = WhatsAppSettings.get_solo()
        return Response({
            'segments': wa_audience.segment_catalog(s),
            'opt_in_total': Patient.objects.filter(wa_opt_in=True).count(),
            'sent_today': _sent_today(),
            'daily_send_cap': s.daily_send_cap,
        })


def _sent_today():
    start = timezone.now().astimezone(JAKARTA_TZ).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return WhatsAppBlastRecipient.objects.filter(status='sent', sent_at__gte=start).count()


# ── Blast preview & send ───────────────────────────────────────────────────

def _resolve_body(payload):
    """-> (body, template, error_response)

    A blast carries either a template id or a literal body. The template's text
    is snapshotted at send time so editing it later cannot rewrite what patients
    were told.
    """
    template = None
    template_id = payload.get('template_id')
    if template_id:
        template = MessageTemplate.objects.filter(pk=template_id).first()
        if template is None:
            return None, None, Response({'template_id': 'Template tidak ditemukan.'},
                                        status=http.HTTP_400_BAD_REQUEST)
        body = template.body
    else:
        body = (payload.get('body') or '').strip()

    if not body:
        return None, None, Response({'body': 'Isi pesan wajib diisi.'},
                                    status=http.HTTP_400_BAD_REQUEST)
    return body, template, None


def _render_for(recipient, body, today):
    patient = recipient['patient']
    crm = getattr(patient, 'crm_profile', None)
    context = mt.build_context(
        patient, crm=crm,
        last_visit=crm.last_visit_date if crm else None,
        today=today,
    )
    return mt.render(body, context)


class WhatsAppBlastPreviewView(APIView):
    """POST /api/whatsapp/blasts/preview/  {segment, template_id|body}

    Resolves the audience and renders a sample without sending anything or
    writing a row. The counts it returns are the same ones the send path
    enforces, so the preview cannot promise a number the send will not honour.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        payload = request.data
        segment = (payload.get('segment') or '').strip()
        if segment not in wa_audience.SEGMENT_KEYS:
            return Response({'segment': 'Segmen tidak dikenal.'},
                            status=http.HTTP_400_BAD_REQUEST)

        body, template, error = _resolve_body(payload)
        if error:
            return error

        s = WhatsAppSettings.get_solo()
        today = wa_audience.today_jkt()
        recipients, counts = wa_audience.resolve(segment, s, today=today)

        remaining = max(0, s.daily_send_cap - _sent_today()) if s.daily_send_cap else len(recipients)
        will_send = min(len(recipients), remaining)

        sample = [
            {
                'patient_no': r['patient_no'],
                'name': r['name'],
                'phone': r['phone'],
                'preview': _render_for(r, body, today),
            }
            for r in recipients[:PREVIEW_ROWS]
        ]

        return Response({
            'segment': segment,
            'counts': counts,
            'will_send': will_send,
            'capped_by_daily_limit': len(recipients) - will_send,
            'daily_send_cap': s.daily_send_cap,
            'sent_today': _sent_today(),
            'sample': sample,
            'sample_truncated': len(recipients) > PREVIEW_ROWS,
            'template_name': template.name if template else None,
            'blocked': _send_blockers(s),
        })


def _send_blockers(s):
    """Reasons a send would be refused right now, as operator-facing strings."""
    blockers = []
    if not s.enabled:
        blockers.append('Integrasi WhatsApp dinonaktifkan di Pengaturan.')
    if not s.base_url or not s.api_key:
        blockers.append('Gateway belum dikonfigurasi (alamat atau API key kosong).')
    if not s.session_id:
        blockers.append('Sesi WhatsApp belum dibuat.')

    hour = _now_jkt().hour
    if _in_quiet_hours(hour, s.quiet_hours_start, s.quiet_hours_end):
        blockers.append(
            f'Di luar jam kirim ({s.quiet_hours_end:02d}:00-{s.quiet_hours_start:02d}:00 WIB).'
        )
    if s.daily_send_cap and _sent_today() >= s.daily_send_cap:
        blockers.append(f'Batas harian {s.daily_send_cap} pesan sudah tercapai.')
    return blockers


def _in_quiet_hours(hour, start, end):
    """Quiet from ``start`` to ``end``, wrapping past midnight when start > end."""
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


class WhatsAppBlastListCreateView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        blasts = WhatsAppBlast.objects.select_related('template', 'created_by')[:50]
        return Response([_serialize_blast(b) for b in blasts])

    def post(self, request):
        payload = request.data
        segment = (payload.get('segment') or '').strip()
        if segment not in wa_audience.SEGMENT_KEYS:
            return Response({'segment': 'Segmen tidak dikenal.'},
                            status=http.HTTP_400_BAD_REQUEST)

        body, template, error = _resolve_body(payload)
        if error:
            return error

        s = WhatsAppSettings.get_solo()
        blockers = _send_blockers(s)
        if blockers:
            return Response({'detail': blockers[0], 'blocked': blockers},
                            status=http.HTTP_409_CONFLICT)

        # The session must be paired *now*, not when the page was loaded.
        client = client_from_settings(s)
        try:
            session = client.get_session(s.session_id)
        except GatewayError as exc:
            return Response({'detail': str(exc)}, status=http.HTTP_502_BAD_GATEWAY)
        if (session.get('status') or '') not in READY_STATES:
            return Response(
                {'detail': f"Sesi WhatsApp belum siap (status: {session.get('status')})."},
                status=http.HTTP_409_CONFLICT,
            )

        today = wa_audience.today_jkt()
        remaining = max(0, s.daily_send_cap - _sent_today()) if s.daily_send_cap else None
        recipients, counts = wa_audience.resolve(segment, s, today=today, limit=remaining)
        if not recipients:
            return Response(
                {'detail': 'Tidak ada penerima yang memenuhi syarat.', 'counts': counts},
                status=http.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            blast = WhatsAppBlast.objects.create(
                name=(payload.get('name') or '').strip(),
                segment=segment,
                segment_params={'counts': counts},
                template=template,
                body_snapshot=body,
                total=len(recipients),
                created_by=_actor(request),
            )
            WhatsAppBlastRecipient.objects.bulk_create([
                WhatsAppBlastRecipient(
                    blast=blast,
                    patient=r['patient'],
                    patient_name=r['name'] or '',
                    chat_id=r['chat_id'],
                    phone=r['phone'] or '',
                    body=_render_for(r, body, today),
                )
                for r in recipients
            ])

        _log(request, 'CREATE',
             f'WhatsApp blast "{blast.name or segment}" — {blast.total} penerima')

        _dispatch_next_batch(blast, s, client)
        blast.refresh_from_db()
        return Response(_serialize_blast(blast, detail=True), status=http.HTTP_201_CREATED)


def _dispatch_next_batch(blast, settings_obj, client):
    """Hand the next <=BULK_MAX pending recipients to the gateway.

    Returns True when a batch went out. A blast with nothing pending left is
    marked completed here — this is the only place that transition happens, so
    it cannot be reached with recipients still unsent.
    """
    pending = list(
        blast.recipients.filter(status='pending', batch_id='').order_by('id')[:BULK_MAX]
    )
    if not pending:
        if blast.status in ('pending', 'processing'):
            blast.status = 'completed'
            blast.save(update_fields=['status', 'updated_at'])
        return False

    batch_id = f'cpms-{blast.id}-{pending[0].id}'
    try:
        result = client.send_bulk(
            settings_obj.session_id,
            [{'chatId': r.chat_id, 'text': r.body} for r in pending],
            delay_ms=settings_obj.delay_between_messages_ms,
            randomize=settings_obj.randomize_delay,
            batch_id=batch_id,
        )
    except GatewayError as exc:
        blast.status = 'failed'
        blast.error = str(exc)
        blast.save(update_fields=['status', 'error', 'updated_at'])
        logger.warning('[whatsapp] blast %s dispatch failed: %s', blast.id, exc)
        return False

    assigned = result.get('batchId') or batch_id
    WhatsAppBlastRecipient.objects.filter(id__in=[r.id for r in pending]).update(batch_id=assigned)
    blast.batch_id = assigned
    blast.status = 'processing'
    blast.error = ''
    blast.save(update_fields=['batch_id', 'status', 'error', 'updated_at'])
    return True


class WhatsAppBlastDetailView(APIView):
    """GET /api/whatsapp/blasts/<pk>/ — syncs from the gateway, then returns.

    Polling this is what advances a blast: it reconciles the current batch and
    dispatches the next one once that batch drains.
    """
    permission_classes = [AllowAny]

    def get(self, request, pk):
        blast = WhatsAppBlast.objects.filter(pk=pk).select_related('template').first()
        if blast is None:
            return Response({'detail': 'Not found.'}, status=http.HTTP_404_NOT_FOUND)

        if blast.status == 'processing':
            _sync(blast)
        return Response(_serialize_blast(blast, detail=True))


def _sync(blast):
    s = WhatsAppSettings.get_solo()
    if not (s.session_id and blast.batch_id):
        return
    client = client_from_settings(s)
    try:
        status_payload = client.batch_status(s.session_id, blast.batch_id)
    except GatewayError as exc:
        # A sync failure is not a blast failure — the gateway may be briefly
        # restarting while the batch is still queued inside it. Record it and
        # let the next poll try again.
        blast.error = str(exc)
        blast.save(update_fields=['error', 'updated_at'])
        return

    by_chat = {}
    for row in status_payload.get('results') or []:
        by_chat.setdefault(row.get('chatId'), row)

    now = timezone.now()
    updated = []
    for recipient in blast.recipients.filter(batch_id=blast.batch_id):
        row = by_chat.get(recipient.chat_id)
        if not row:
            continue
        state = (row.get('status') or '').lower()
        if state not in ('sent', 'failed', 'cancelled') or recipient.status == state:
            continue
        recipient.status = state
        recipient.message_id = row.get('messageId') or ''
        err = row.get('error') or {}
        recipient.error_code = (err.get('code') or '')[:60]
        recipient.error = (err.get('message') or '')[:255]
        if state == 'sent':
            recipient.sent_at = _parse_dt(row.get('sentAt')) or now
        updated.append(recipient)

    if updated:
        WhatsAppBlastRecipient.objects.bulk_update(
            updated, ['status', 'message_id', 'error_code', 'error', 'sent_at'])

    counts = _recount(blast)
    batch_state = (status_payload.get('status') or '').lower()

    if batch_state in ('completed', 'failed', 'cancelled'):
        if batch_state == 'cancelled':
            blast.status = 'cancelled'
            blast.save(update_fields=['status', 'updated_at'])
            blast.recipients.filter(status='pending').update(status='cancelled')
            return
        # Batch drained — move on to the next chunk, or finish.
        _dispatch_next_batch(blast, s, client_from_settings(s))
    else:
        blast.sent_count, blast.failed_count = counts
        blast.save(update_fields=['sent_count', 'failed_count', 'updated_at'])


def _recount(blast):
    sent = blast.recipients.filter(status='sent').count()
    failed = blast.recipients.filter(status='failed').count()
    WhatsAppBlast.objects.filter(pk=blast.pk).update(sent_count=sent, failed_count=failed)
    blast.sent_count, blast.failed_count = sent, failed
    return sent, failed


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None


class WhatsAppBlastCancelView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, pk):
        blast = WhatsAppBlast.objects.filter(pk=pk).first()
        if blast is None:
            return Response({'detail': 'Not found.'}, status=http.HTTP_404_NOT_FOUND)
        if blast.status not in ('pending', 'processing'):
            return Response({'detail': 'Blast sudah selesai.'}, status=http.HTTP_409_CONFLICT)

        s = WhatsAppSettings.get_solo()
        if s.session_id and blast.batch_id:
            try:
                client_from_settings(s).cancel_batch(s.session_id, blast.batch_id)
            except GatewayError as exc:
                logger.warning('[whatsapp] cancel of %s failed: %s', blast.batch_id, exc)

        # Cancel locally regardless: anything still pending must never be
        # dispatched, even if the gateway did not acknowledge. Messages already
        # delivered stay 'sent' — they cannot be recalled.
        blast.recipients.filter(status='pending').update(status='cancelled')
        blast.status = 'cancelled'
        blast.save(update_fields=['status', 'updated_at'])
        _log(request, 'UPDATE', f'WhatsApp blast #{blast.id} dibatalkan')
        return Response(_serialize_blast(blast, detail=True))


def _serialize_blast(b, *, detail=False):
    data = {
        'id': b.id,
        'name': b.name,
        'segment': b.segment,
        'segment_label': next(
            (x['label'] for x in wa_audience.SEGMENTS if x['key'] == b.segment), b.segment),
        'template_name': b.template.name if b.template_id else None,
        'status': b.status,
        'total': b.total,
        'sent_count': b.sent_count,
        'failed_count': b.failed_count,
        'pending_count': max(0, b.total - b.sent_count - b.failed_count),
        'error': b.error,
        'created_at': b.created_at.isoformat(),
        'created_by': b.created_by.display_name if b.created_by_id else None,
    }
    if detail:
        data['body_snapshot'] = b.body_snapshot
        data['counts'] = (b.segment_params or {}).get('counts', {})
        data['recipients'] = [
            {
                'patient_no': r.patient_id,
                'name': r.patient_name,
                'phone': r.phone,
                'status': r.status,
                'error': r.error,
                'error_code': r.error_code,
                'sent_at': r.sent_at.isoformat() if r.sent_at else None,
            }
            for r in b.recipients.all()[:500]
        ]
    return data


# ── Patient opt-in ─────────────────────────────────────────────────────────

class PatientWhatsAppOptInView(APIView):
    """PATCH /api/patients/<patient_no>/wa-opt-in/  {"opt_in": true|false}

    Its own endpoint rather than a field on the patient update: consent changes
    are logged, and this keeps them out of the generic admin PUT where a stale
    form could flip them back by accident.
    """
    permission_classes = [AllowAny]

    def patch(self, request, patient_no):
        patient = Patient.objects.filter(patient_no=patient_no).first()
        if patient is None:
            return Response({'detail': 'Not found.'}, status=http.HTTP_404_NOT_FOUND)

        if 'opt_in' not in request.data:
            return Response({'opt_in': 'Wajib diisi.'}, status=http.HTTP_400_BAD_REQUEST)
        opt_in = bool(request.data['opt_in'])

        if opt_in and not to_chat_id(patient.phone_number):
            return Response(
                {'detail': 'Pasien belum punya nomor WhatsApp yang valid '
                           '(format 08xx… atau 628xx…).'},
                status=http.HTTP_400_BAD_REQUEST,
            )

        patient.wa_opt_in = opt_in
        patient.wa_opt_in_at = timezone.now() if opt_in else None
        patient.save(update_fields=['wa_opt_in', 'wa_opt_in_at', 'updated_at'])

        _log(request, 'UPDATE',
             f'WhatsApp opt-{"in" if opt_in else "out"}: {patient.name} ({patient.patient_no})')
        return Response({
            'patient_no': patient.patient_no,
            'wa_opt_in': patient.wa_opt_in,
            'wa_opt_in_at': patient.wa_opt_in_at.isoformat() if patient.wa_opt_in_at else None,
            'wa_phone': normalize_phone(patient.phone_number),
        })


def _log(request, action, description):
    try:
        AuditLog.objects.create(
            performed_by=_actor(request),
            action=action,
            entity_type='WhatsApp',
            entity_id='',
            description=description,
        )
    except Exception:  # pragma: no cover - logging must never break a send
        logger.exception('[whatsapp] audit log failed')
