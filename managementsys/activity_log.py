import json
import logging
import re
import time

from django.conf import settings
from django.utils.deprecation import MiddlewareMixin

logger = logging.getLogger(__name__)


LOGGED_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}

INCLUDE_PREFIXES = ('/api/',)

EXCLUDE_PREFIXES = (
    '/api/auth/login/',
    '/api/auth/logout/',
    '/api/admin/activity-log/',
)

REDACT_KEYS = (
    'pin', 'password', 'passwd', 'token', 'secret', 'api_key', 'apikey',
    'authorization', 'auth', 'credential', 'signature',
)

MAX_METADATA_CHARS = 4000
MAX_LIST_ITEMS = 5

_ACTION_BY_METHOD = {
    'POST': 'CREATE',
    'PUT': 'UPDATE',
    'PATCH': 'UPDATE',
    'DELETE': 'DELETE',
}

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


def _redact(value, depth=0):
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
    content_type = (request.content_type or '').lower()

    if content_type.startswith('multipart/'):
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
    trimmed = path.split('?', 1)[0].strip('/')
    parts = [p for p in trimmed.split('/') if p]
    return parts[1:] if parts and parts[0] == 'api' else parts


def describe(method, path):
    segments = _path_segments(path)
    action = _ACTION_BY_METHOD.get(method, method)

    if segments and segments[-1].lower() in _VERB_SEGMENTS:
        action = _VERB_SEGMENTS[segments[-1].lower()]

    numeric = [s for s in segments if _NUMERIC.match(s)]
    entity_id = numeric[-1] if numeric else ''

    resource = ''
    for seg in reversed(segments):
        lowered = seg.lower()
        if _NUMERIC.match(seg) or lowered in _VERB_SEGMENTS:
            continue
        resource = lowered
        break
    if not resource and segments:
        resource = segments[0].lower()

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


class ActivityLogMiddleware(MiddlewareMixin):
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
