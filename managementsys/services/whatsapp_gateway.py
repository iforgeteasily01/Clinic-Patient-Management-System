from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15
SEND_TIMEOUT = 60

BULK_MAX = 100

DELAY_MIN_MS = 1000
DELAY_MAX_MS = 60000

READY_STATES = {'ready'}


class GatewayError(Exception):
    def __init__(self, message, *, status=None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


def normalize_phone(raw):
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

    if not national.startswith('8'):
        return None
    if not (9 <= len(national) <= 13):
        return None
    return '62' + national


def to_chat_id(raw):
    phone = normalize_phone(raw)
    return f'{phone}@c.us' if phone else None


class OpenWAClient:
    def __init__(self, base_url, api_key, *, timeout=DEFAULT_TIMEOUT):
        self.base_url = (base_url or '').rstrip('/')
        self.api_key = api_key or ''
        self.timeout = timeout


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


    def health(self):
        return self._request('GET', '/api/health')

    def list_sessions(self):
        data = self._request('GET', '/api/sessions')
        if isinstance(data, dict):
            return data.get('data') or data.get('sessions') or data.get('items') or []
        return data or []

    def create_session(self, name):
        return self._request('POST', '/api/sessions', {'name': name})

    def get_session(self, session_id):
        return self._request('GET', f'/api/sessions/{session_id}')

    def start_session(self, session_id):
        return self._request('POST', f'/api/sessions/{session_id}/start', timeout=45)

    def stop_session(self, session_id):
        return self._request('POST', f'/api/sessions/{session_id}/stop')

    def logout_session(self, session_id):
        return self._request('POST', f'/api/sessions/{session_id}/logout')

    def get_qr(self, session_id):
        return self._request('GET', f'/api/sessions/{session_id}/qr')


    def send_text(self, session_id, chat_id, text):
        return self._request(
            'POST', f'/api/sessions/{session_id}/messages/send-text',
            {'chatId': chat_id, 'text': text}, timeout=SEND_TIMEOUT,
        )

    def send_bulk(self, session_id, messages, *, delay_ms, randomize, batch_id=None):
        payload = {
            'messages': [
                {'chatId': m['chatId'], 'type': 'text', 'content': {'text': m['text']}}
                for m in messages
            ],
            'options': {
                'delayBetweenMessages': max(DELAY_MIN_MS, min(DELAY_MAX_MS, int(delay_ms))),
                'randomizeDelay': bool(randomize),
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
