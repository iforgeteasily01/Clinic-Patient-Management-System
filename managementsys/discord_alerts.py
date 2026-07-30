"""
discord_alerts.py
Sends HTTP error reports (404, 5xx, unhandled exceptions) to a Discord channel
via an incoming webhook, in a background thread so it never blocks the request.

Configured entirely from settings / .env — see DISCORD_* keys in CPMS/settings.py.
"""
import json
import logging
import threading
import time
import traceback
import urllib.request
from datetime import datetime, timezone

from django.conf import settings

logger = logging.getLogger(__name__)

# Discord embed colours (decimal)
COLOR_RED    = 0xE74C3C   # 5xx / exceptions
COLOR_ORANGE = 0xE67E22   # 404 and other 4xx

# Discord hard limits
MAX_DESCRIPTION = 4000
MAX_FIELD_VALUE = 1000

# key -> last-sent unix timestamp, used to throttle repeats
_last_sent: dict[str, float] = {}
_lock = threading.Lock()


def _truncate(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 3] + '...'


def _post(payload: dict):
    """Internal: perform the HTTP POST. Runs in a background thread."""
    url = getattr(settings, 'DISCORD_WEBHOOK_URL', '')
    try:
        body = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            url,
            data=body,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status not in (200, 204):
                logger.warning('[discord_alerts] Unexpected status %s', resp.status)
    except Exception as exc:
        # Never let an alert failure affect the main request
        logger.warning('[discord_alerts] Alert failed: %s', exc)


def _should_send(dedupe_key: str) -> bool:
    """Throttle: at most one alert per dedupe_key per DISCORD_ALERT_COOLDOWN seconds."""
    cooldown = getattr(settings, 'DISCORD_ALERT_COOLDOWN', 60)
    if cooldown <= 0:
        return True
    now = time.time()
    with _lock:
        last = _last_sent.get(dedupe_key, 0)
        if now - last < cooldown:
            return False
        _last_sent[dedupe_key] = now
        # keep the dict from growing forever
        if len(_last_sent) > 500:
            for key in [k for k, v in _last_sent.items() if now - v > cooldown * 10]:
                _last_sent.pop(key, None)
    return True


def send_alert(title: str, description: str = '', fields: dict | None = None,
               color: int = COLOR_RED, dedupe_key: str | None = None):
    """
    Fire-and-forget alert to Discord. Returns immediately, sends in background.
    Safe to call from anywhere — silently does nothing if no webhook is configured.
    """
    url = getattr(settings, 'DISCORD_WEBHOOK_URL', '')
    if not url:
        return
    if dedupe_key and not _should_send(dedupe_key):
        return

    embed = {
        'title':       _truncate(title, 250),
        'color':       color,
        'timestamp':   datetime.now(timezone.utc).isoformat(),
        'footer':      {'text': getattr(settings, 'DISCORD_ALERT_ENV_NAME', 'CPMS')},
    }
    if description:
        embed['description'] = _truncate(description, MAX_DESCRIPTION)
    if fields:
        embed['fields'] = [
            {'name': _truncate(k, 250), 'value': _truncate(v or '-', MAX_FIELD_VALUE), 'inline': len(str(v)) < 40}
            for k, v in fields.items()
        ][:25]

    payload = {'embeds': [embed]}
    mention = getattr(settings, 'DISCORD_ALERT_MENTION', '')
    if mention:
        payload['content'] = mention

    threading.Thread(target=_post, args=(payload,), daemon=True).start()


# --------------------------------------------------------------------------- #
# Middleware
# --------------------------------------------------------------------------- #

def _parse_status_spec(spec: str) -> list[tuple[int, int]]:
    """'404,500-599' -> [(404, 404), (500, 599)]"""
    ranges = []
    for part in str(spec).split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            lo, _, hi = part.partition('-')
            ranges.append((int(lo), int(hi)))
        else:
            ranges.append((int(part), int(part)))
    return ranges


class DiscordErrorAlertMiddleware:
    """
    Reports failing responses and unhandled exceptions to Discord.

    Catches both:
      - any response whose status code matches DISCORD_ALERT_STATUSES
        (covers DRF-handled errors, which never reach process_exception)
      - unhandled exceptions raised by a view, with full traceback
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.statuses = _parse_status_spec(
            getattr(settings, 'DISCORD_ALERT_STATUSES', '404,500-599')
        )
        self.ignore_paths = tuple(getattr(settings, 'DISCORD_ALERT_IGNORE_PATHS', ()))

    def _matches(self, status_code: int) -> bool:
        return any(lo <= status_code <= hi for lo, hi in self.statuses)

    def _ignored(self, path: str) -> bool:
        return any(path.startswith(p) for p in self.ignore_paths)

    @staticmethod
    def _context(request) -> dict:
        user = getattr(request, 'user', None)
        who = getattr(user, 'display_name', None) or 'anonymous'
        return {
            'Path':   f'{request.method} {request.get_full_path()}',
            'User':   f'{who} ({getattr(user, "role", "-")})' if who != 'anonymous' else 'anonymous',
            'Client': request.META.get('HTTP_X_FORWARDED_FOR')
                      or request.META.get('REMOTE_ADDR', '-'),
            'Agent':  request.META.get('HTTP_USER_AGENT', '-'),
        }

    def __call__(self, request):
        response = self.get_response(request)

        if getattr(request, '_discord_alert_sent', False):
            return response
        if not self._matches(response.status_code) or self._ignored(request.path):
            return response

        fields = self._context(request)
        fields['Status'] = str(response.status_code)

        body = ''
        if getattr(settings, 'DISCORD_ALERT_INCLUDE_BODY', True):
            try:
                if not response.streaming and 'json' in response.get('Content-Type', ''):
                    body = response.content.decode('utf-8', 'replace')
            except Exception:
                body = ''

        send_alert(
            title=f'HTTP {response.status_code} — {request.path}',
            description=f'```json\n{_truncate(body, 1500)}\n```' if body else '',
            fields=fields,
            color=COLOR_RED if response.status_code >= 500 else COLOR_ORANGE,
            dedupe_key=f'{response.status_code}:{request.method}:{request.path}',
        )
        return response

    def process_exception(self, request, exception):
        if self._ignored(request.path):
            return None
        tb = ''.join(traceback.format_exception(type(exception), exception, exception.__traceback__))
        fields = self._context(request)
        fields['Exception'] = type(exception).__name__

        send_alert(
            title=f'Unhandled {type(exception).__name__} — {request.path}',
            description=f'```py\n{_truncate(tb[-1800:], 1800)}\n```',
            fields=fields,
            color=COLOR_RED,
            dedupe_key=f'exc:{type(exception).__name__}:{request.path}',
        )
        # Suppress the duplicate 500 alert from __call__ for this same request
        request._discord_alert_sent = True
        return None  # let Django's normal error handling continue
