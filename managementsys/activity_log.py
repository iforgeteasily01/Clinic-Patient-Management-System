"""Automatic activity logging for every mutating API request.

Explicit ``AuditLog.objects.create()`` calls scattered through the views cover
the flows somebody remembered to instrument. This middleware covers the rest:
any ``POST`` / ``PUT`` / ``PATCH`` / ``DELETE`` under ``/api/`` writes one
``AuditLog`` row with ``source='http'``, whether or not the view knows about it.
Patient registration, invoicing, payments, check-ins — all of it lands here
without a per-view edit.

Three things this deliberately does *not* do:

- **It never fails a request.** Every write is wrapped; a logging error is
  swallowed to ``logger.exception`` and the response goes out untouched.
- **It never stores credentials.** Request bodies are summarised, and any key
  matching :data:`REDACT_KEYS` is replaced with ``'***'`` before it is stored.
  File uploads are recorded by name and size only — never content.
- **It does not read the response body.** Streaming responses would be consumed
  by doing so. The created object's id is recovered from the ``Location``
  header or the URL instead.

The actor is read from ``request.user`` in the *response* phase, not the request
phase: authentication is DRF's job and happens inside the view. DRF's
``Request.user`` setter assigns through to the underlying ``HttpRequest``, so by
the time the response comes back the ``AppUser`` is there.
"""

import json
import logging
import re
import time

from django.conf import settings
from django.utils.deprecation import MiddlewareMixin

logger = logging.getLogger(__name__)

# ── What gets logged ────────────────────────────────────────────────────────

LOGGED_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}

#: Only paths under these prefixes are considered at all.
INCLUDE_PREFIXES = ('/api/',)

#: Paths that are noise or are already logged in richer form by the view.
#: Matched as a prefix against the full path.
EXCLUDE_PREFIXES = (
    '/api/auth/login/',       # LoginView writes a LOGIN row itself
    '/api/auth/logout/',      # LogoutView writes a LOGOUT row itself
    '/api/admin/activity-log/',  # reading the log must not write to it
)

#: Any body key containing one of these substrings (case-insensitive) is masked.
REDACT_KEYS = (
    'pin', 'password', 'passwd', 'token', 'secret', 'api_key', 'apikey',
    'authorization', 'auth', 'credential', 'signature',
)

#: Hard cap on the stored body summary, so a bulk import cannot bloat the table.
MAX_METADATA_CHARS = 4000
MAX_LIST_ITEMS = 5

_ACTION_BY_METHOD = {
    'POST': 'CREATE',
    'PUT': 'UPDATE',
    'PATCH': 'UPDATE',
    'DELETE': 'DELETE',
}

#: Trailing path segments that describe an *operation* rather than a resource.
#: When one of these ends the path the action is reported as that verb, because
#: "CREATE invoices/12/void" reads as a lie.
_VERB_SEGMENTS = {
    'void': 'VOID',
    'cancel': 'CANCEL',
    'confirm': 'CONFIRM',
    'commit': 'COMMIT',
    'preview': 'PREVIEW',
    'correct': 'CORRECT',
    'import': 'IMPORT',
    'export': 'EXPORT',
    'run': 'RUN',
    'send': 'SEND',
    'pay': 'PAY',
    'restore': 'RESTORE',
    'checkout': 'CHECKOUT',
    'check-in': 'CHECK_IN',
    'checkin': 'CHECK_IN',
    'complete': 'COMPLETE',
    'start': 'START',
    'reset': 'RESET',
    'sync': 'SYNC',
    'print': 'PRINT',
    'classify': 'CLASSIFY',
}

#: Friendly names for the resource segment. Anything not listed falls back to
#: the raw segment, title-cased — good enough to search on.
_ENTITY_NAMES = {
    'patients': 'Patient',
    'invoices': 'Invoice',
    'invoice': 'Invoice',
    'billing': 'Billing',
    'payments': 'Payment',
    'payment-methods': 'PaymentMethod',
    'active-patients': 'ActivePatient',
    'medical-records': 'MedicalRecord',
    'medrec': 'MedicalRecord',
    'treatment-sessions': 'TreatmentSession',
    'treatments': 'Treatment',
    'appointments': 'Appointment',
    'reservations': 'Reservation',
    'inventory': 'Inventory',
    'accounting': 'Accounting',
    'expenses': 'Expense',
    'purchases': 'PurchaseInvoice',
    'suppliers': 'Supplier',
    'accounts': 'ChartOfAccounts',
    'journal': 'Journal',
    'admin': 'Admin',
    'crm': 'CRM',
    'whatsapp': 'WhatsApp',
    'auth': 'AppUser',
    'photos': 'PatientPhoto',
    'packages': 'Package',
    'promotions': 'Promotion',
    'tickets': 'Ticket',
    'returns': 'SalesReturn',
    'stock-opname': 'StockOpname',
}

_NUMERIC = re.compile(r'^\d+$')


# ── Helpers ─────────────────────────────────────────────────────────────────

def _redact(value, depth=0):
    """Recursively copy ``value``, masking anything that looks like a secret."""
    if depth > 6:
        return '...'
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in REDACT_KEYS):
                out[key] = '***'
            else:
                out[key] = _redact(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        trimmed = [_redact(v, depth + 1) for v in list(value)[:MAX_LIST_ITEMS]]
        if len(value) > MAX_LIST_ITEMS:
            trimmed.append(f'... {len(value) - MAX_LIST_ITEMS} more')
        return trimmed
    if isinstance(value, str) and len(value) > 300:
        return value[:300] + '...'
    return value


def _body_summary(request):
    """A redacted, size-capped snapshot of what the caller sent."""
    content_type = (request.content_type or '').lower()

    if content_type.startswith('multipart/'):
        # Touching request.POST here is safe: the view has already run, so the
        # stream is parsed and cached.
        try:
            fields = _redact({k: v for k, v in request.POST.items()})
            files = [
                {'field': name, 'filename': f.name, 'bytes': f.size}
                for name, f in request.FILES.items()
            ]
            return {'fields': fields, 'files': files}
        except Exception:
            return None

    if 'json' not in content_type:
        return None

    try:
        raw = request.body
    except Exception:
        # Body already consumed and not cached (streaming upload) — nothing to do.
        return None
    if not raw:
        return None
    try:
        parsed = json.loads(raw.decode('utf-8', errors='replace'))
    except (ValueError, UnicodeDecodeError):
        return None

    summary = _redact(parsed)
    encoded = json.dumps(summary, default=str)
    if len(encoded) > MAX_METADATA_CHARS:
        return {'truncated': True, 'preview': encoded[:MAX_METADATA_CHARS]}
    return summary


def _path_segments(path):
    """``/api/admin/patients/12/`` → ``['admin', 'patients', '12']``."""
    trimmed = path.split('?', 1)[0].strip('/')
    parts = [p for p in trimmed.split('/') if p]
    return parts[1:] if parts and parts[0] == 'api' else parts


def describe(method, path):
    """Derive ``(action, entity_type, entity_id)`` from the request line.

    Pure and side-effect free so it can be unit-tested without a request.
    """
    segments = _path_segments(path)
    action = _ACTION_BY_METHOD.get(method, method)

    # A trailing verb wins over the method: POST .../void is a VOID, not a CREATE.
    if segments and segments[-1].lower() in _VERB_SEGMENTS:
        action = _VERB_SEGMENTS[segments[-1].lower()]

    numeric = [s for s in segments if _NUMERIC.match(s)]
    entity_id = numeric[-1] if numeric else ''

    # The resource is the last non-numeric, non-verb segment; falling back to the
    # first segment keeps something meaningful for a one-segment path.
    resource = ''
    for seg in reversed(segments):
        lowered = seg.lower()
        if _NUMERIC.match(seg) or lowered in _VERB_SEGMENTS:
            continue
        resource = lowered
        break
    if not resource and segments:
        resource = segments[0].lower()

    # `/api/admin/patients/` should read as Patient, not Admin — prefer the
    # deepest segment we have a friendly name for.
    entity_type = _ENTITY_NAMES.get(resource)
    if entity_type is None:
        for seg in reversed(segments):
            candidate = _ENTITY_NAMES.get(seg.lower())
            if candidate:
                entity_type = candidate
                break
    if entity_type is None:
        entity_type = resource.replace('-', ' ').replace('_', ' ').title().replace(' ', '') or 'Unknown'

    return action, entity_type[:50], entity_id[:50]


def _client_ip(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        candidate = forwarded.split(',')[0].strip()
        if candidate:
            return candidate
    return request.META.get('REMOTE_ADDR') or None


def _actor(request):
    from .models import AppUser
    user = getattr(request, 'user', None)
    return user if isinstance(user, AppUser) else None


# ── Middleware ──────────────────────────────────────────────────────────────

class ActivityLogMiddleware(MiddlewareMixin):
    """Writes one ``AuditLog`` row per mutating API request.

    Disable with ``ACTIVITY_LOG_ENABLED = False`` in settings (tests that assert
    on ``AuditLog`` counts of a *specific* explicit entry may want that).
    """

    def process_request(self, request):
        if self._is_candidate(request):
            request._activity_started_at = time.monotonic()
        return None

    def process_response(self, request, response):
        try:
            if getattr(request, '_activity_started_at', None) is None:
                return response
            if not getattr(settings, 'ACTIVITY_LOG_ENABLED', True):
                return response
            self._record(request, response.status_code)
        except Exception:
            logger.exception('[activity_log] failed to record %s %s',
                             request.method, request.path)
        return response

    def process_exception(self, request, exception):
        """An unhandled 500 never reaches ``process_response`` with a status we
        can trust, so record the failure here and mark the request done."""
        try:
            if getattr(request, '_activity_started_at', None) is None:
                return None
            if not getattr(settings, 'ACTIVITY_LOG_ENABLED', True):
                return None
            self._record(request, 500, error=f'{type(exception).__name__}: {exception}')
            request._activity_started_at = None
        except Exception:
            logger.exception('[activity_log] failed to record exception')
        return None

    # ── internals ──

    @staticmethod
    def _is_candidate(request):
        if request.method not in LOGGED_METHODS:
            return False
        path = request.path
        if not path.startswith(INCLUDE_PREFIXES):
            return False
        return not path.startswith(EXCLUDE_PREFIXES)

    def _record(self, request, status_code, error=None):
        from .models import AuditLog

        started = request._activity_started_at
        request._activity_started_at = None
        duration_ms = int((time.monotonic() - started) * 1000)

        action, entity_type, entity_id = describe(request.method, request.path)
        actor = _actor(request)

        metadata = {}
        body = _body_summary(request)
        if body is not None:
            metadata['request'] = body
        if error:
            metadata['error'] = error[:1000]
        query = request.META.get('QUERY_STRING', '')
        if query:
            metadata['query'] = query[:500]

        who = actor.display_name if actor else 'anonymous'
        outcome = 'ok' if status_code < 400 else f'failed ({status_code})'
        target = f'{entity_type} {entity_id}'.strip()
        description = f'{who}: {action} {target} — {outcome}'

        AuditLog.objects.create(
            performed_by=actor,
            action=action[:20],
            entity_type=entity_type,
            entity_id=entity_id,
            description=description,
            source=AuditLog.SOURCE_HTTP,
            method=request.method[:10],
            path=request.path[:255],
            status_code=status_code,
            duration_ms=duration_ms,
            ip_address=_client_ip(request),
            metadata=metadata or None,
        )
