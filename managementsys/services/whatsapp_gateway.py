"""Thin HTTP client for an OpenWA gateway (github.com/rmyndharis/OpenWA).

OpenWA is a self-hosted WhatsApp API gateway running as a **separate Node
service** next to Django, default port 2785. Django never talks to WhatsApp; it
talks to that gateway, and the gateway owns the paired phone session.

Why hand-rolled instead of the official `rmyndharis-openwa` SDK: the whole
surface used here is six endpoints, and `urllib` is already how this project
does outbound HTTP (see vercel_push.py). One dependency-free module also means
a gateway that is down or misconfigured can never break `manage.py migrate`.

Route map (OpenWA prefixes everything with /api):

    GET    /api/health                                    liveness
    POST   /api/sessions                                  {name, config?}
    GET    /api/sessions                                  list
    GET    /api/sessions/{id}                             status
    POST   /api/sessions/{id}/start                       begin pairing
    POST   /api/sessions/{id}/logout                      unpair the phone
    GET    /api/sessions/{id}/qr                          QR payload to scan
    POST   /api/sessions/{id}/messages/send-text          {chatId, text}
    POST   /api/sessions/{id}/messages/send-bulk          {messages[], options}
    GET    /api/sessions/{id}/messages/batch/{batchId}    batch progress
    POST   /api/sessions/{id}/messages/batch/{id}/cancel  stop mid-batch

Every call raises ``GatewayError`` on failure rather than returning a sentinel.
The views catch it and turn it into a 502 with the gateway's own message, so an
operator sees "session not ready" rather than a blank 500.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15
# Sending a batch of 100 asks the gateway to enqueue, not to deliver, so it
# returns quickly — but it does more work than a status poll.
SEND_TIMEOUT = 60

# OpenWA caps a single bulk request at 100 messages (ArrayMaxSize(100) on
# SendBulkMessageDto). Larger audiences are split into consecutive batches.
BULK_MAX = 100

# Gateway-enforced bounds on the inter-message delay. Mirrored here so a bad
# settings value is rejected by us with a clear message instead of by the
# gateway with a validation dump.
DELAY_MIN_MS = 1000
DELAY_MAX_MS = 60000

# Session states that mean "paired and able to send". Anything else — qr_ready,
# initializing, disconnected, action_required, failed — is not sendable.
READY_STATES = {'ready'}


class GatewayError(Exception):
    """A call to the OpenWA gateway failed.

    ``status`` is the HTTP status when there was one, and None for a transport
    failure (gateway down, DNS, timeout) — the settings page renders those two
    cases differently because they need different fixes.
    """

    def __init__(self, message, *, status=None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


# ── Phone number handling ──────────────────────────────────────────────────

def normalize_phone(raw):
    """Indonesian mobile number -> E.164 digits, or None if it cannot be one.

    Accepts the four forms the clinic's data actually contains: ``08123456789``,
    ``628123456789``, ``+62 812-3456-789`` and ``8123456789``. Anything that
    does not resolve to a plausible Indonesian mobile returns None and the
    caller skips that patient — guessing wrong here sends a patient's
    appointment details to a stranger.
    """
    if not raw:
        return None
    digits = re.sub(r'\D', '', str(raw))
    if not digits:
        return None

    if digits.startswith('62'):
        national = digits[2:]
    elif digits.startswith('0'):
        national = digits[1:]
    else:
        national = digits

    # Indonesian mobile numbers start with 8 and run 9-13 digits nationally.
    if not national.startswith('8'):
        return None
    if not (9 <= len(national) <= 13):
        return None
    return '62' + national


def to_chat_id(raw):
    """Phone number -> OpenWA chat id (``628123456789@c.us``), or None."""
    phone = normalize_phone(raw)
    return f'{phone}@c.us' if phone else None


# ── Client ─────────────────────────────────────────────────────────────────

class OpenWAClient:
    def __init__(self, base_url, api_key, *, timeout=DEFAULT_TIMEOUT):
        self.base_url = (base_url or '').rstrip('/')
        self.api_key = api_key or ''
        self.timeout = timeout

    # -- transport ---------------------------------------------------------

    def _request(self, method, path, body=None, *, timeout=None):
        if not self.base_url:
            raise GatewayError('Alamat gateway WhatsApp belum diatur.')

        url = f'{self.base_url}{path}'
        data = json.dumps(body).encode('utf-8') if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header('Accept', 'application/json')
        if data is not None:
            req.add_header('Content-Type', 'application/json')
        if self.api_key:
            req.add_header('X-API-Key', self.api_key)

        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read().decode('utf-8') or '{}'
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            raw = ''
            try:
                raw = exc.read().decode('utf-8')
                payload = json.loads(raw)
            except Exception:
                payload = {}
            # Nest's error envelope is {statusCode, message, error}; `message`
            # can be a string or an array of validation strings.
            detail = payload.get('message') or payload.get('error') or raw or exc.reason
            if isinstance(detail, list):
                detail = '; '.join(str(d) for d in detail)
            raise GatewayError(str(detail), status=exc.code, payload=payload) from exc
        except urllib.error.URLError as exc:
            raise GatewayError(
                f'Gateway tidak dapat dihubungi di {self.base_url} ({exc.reason}).'
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise GatewayError(f'Gateway tidak merespons ({exc}).') from exc
        except json.JSONDecodeError as exc:
            raise GatewayError(
                'Gateway membalas dengan format yang tidak dikenal — '
                'periksa apakah alamat menunjuk ke OpenWA.'
            ) from exc

    # -- health & sessions -------------------------------------------------

    def health(self):
        return self._request('GET', '/api/health')

    def list_sessions(self):
        data = self._request('GET', '/api/sessions')
        # The list endpoint is paginated in some versions and a bare array in
        # others; both are unwrapped to a plain list here.
        if isinstance(data, dict):
            return data.get('data') or data.get('sessions') or data.get('items') or []
        return data or []

    def create_session(self, name):
        return self._request('POST', '/api/sessions', {'name': name})

    def get_session(self, session_id):
        return self._request('GET', f'/api/sessions/{session_id}')

    def start_session(self, session_id):
        # Starting launches Chromium and waits for WhatsApp's socket, which the
        # gateway itself times out at ~30s. Give it room past that so a slow
        # start surfaces as the gateway's error, not ours.
        return self._request('POST', f'/api/sessions/{session_id}/start', timeout=45)

    def stop_session(self, session_id):
        return self._request('POST', f'/api/sessions/{session_id}/stop')

    def logout_session(self, session_id):
        return self._request('POST', f'/api/sessions/{session_id}/logout')

    def get_qr(self, session_id):
        return self._request('GET', f'/api/sessions/{session_id}/qr')

    # -- messaging ---------------------------------------------------------

    def send_text(self, session_id, chat_id, text):
        return self._request(
            'POST', f'/api/sessions/{session_id}/messages/send-text',
            {'chatId': chat_id, 'text': text}, timeout=SEND_TIMEOUT,
        )

    def send_bulk(self, session_id, messages, *, delay_ms, randomize, batch_id=None):
        """``messages`` is a list of {chatId, text}; at most BULK_MAX per call."""
        payload = {
            'messages': [
                {'chatId': m['chatId'], 'type': 'text', 'content': {'text': m['text']}}
                for m in messages
            ],
            'options': {
                'delayBetweenMessages': max(DELAY_MIN_MS, min(DELAY_MAX_MS, int(delay_ms))),
                'randomizeDelay': bool(randomize),
                # Never stop the whole run because one number is not on
                # WhatsApp — that is the single most common failure, and
                # halting would silently drop everyone after it.
                'stopOnError': False,
            },
        }
        if batch_id:
            payload['batchId'] = batch_id
        return self._request(
            'POST', f'/api/sessions/{session_id}/messages/send-bulk',
            payload, timeout=SEND_TIMEOUT,
        )

    def batch_status(self, session_id, batch_id):
        return self._request('GET', f'/api/sessions/{session_id}/messages/batch/{batch_id}')

    def cancel_batch(self, session_id, batch_id):
        return self._request(
            'POST', f'/api/sessions/{session_id}/messages/batch/{batch_id}/cancel')


def client_from_settings(settings_obj):
    return OpenWAClient(settings_obj.base_url, settings_obj.api_key)
