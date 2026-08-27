"""Read-only API over :class:`~managementsys.models.AuditLog`.

Consumed by the *Activity Log* tab of the server-manager desktop app. Read-only
on purpose: the audit trail is evidence, so there is no endpoint that edits or
deletes a row. Retention is a housekeeping job (``prune_activity_log``), not a
user action.

Access is restricted to ``superuser`` and ``manager`` — the log carries every
actor's movements, and a cashier has no business reading it.
"""

from datetime import datetime, time as dt_time, timedelta

from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from ..api.serializers import ActivityLogSerializer
from ..auth_backend import IsAppAuthenticated
from ..models import AppUser, AuditLog

#: Roles allowed to read the log.
READER_ROLES = ('superuser', 'manager')

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500


def _forbidden():
    return Response({'error': 'Only a superuser or manager may read the activity log.'},
                    status=403)


def _parse_date(value):
    """Accept ``YYYY-MM-DD`` or a full ISO timestamp; return None if unusable."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            return datetime.strptime(text, '%Y-%m-%d').date()
        return datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None


def _as_aware(value, end_of_day=False):
    """Normalise a date or naive datetime into an aware datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.combine(value, dt_time.max if end_of_day else dt_time.min)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


class ActivityLogView(APIView):
    """``GET /api/admin/activity-log/``

    Query parameters, all optional and combinable:

    ``q``            free text over description / path / entity_id
    ``action``       exact action, repeatable (``?action=CREATE&action=VOID``)
    ``entity_type``  exact entity type, repeatable
    ``source``       ``app`` or ``http``
    ``user_id``      actor id; ``0`` means "no actor" (anonymous)
    ``status``       ``ok`` (2xx/3xx) or ``error`` (4xx/5xx)
    ``date_from`` / ``date_to``  inclusive ``YYYY-MM-DD`` bounds
    ``page`` / ``page_size``     1-based paging, page_size capped at 500
    """
    permission_classes = [IsAppAuthenticated]

    def get(self, request):
        if request.user.role not in READER_ROLES:
            return _forbidden()

        qs = AuditLog.objects.select_related('performed_by').all()
        params = request.query_params

        text = params.get('q', '').strip()
        if text:
            qs = qs.filter(
                Q(description__icontains=text)
                | Q(path__icontains=text)
                | Q(entity_type__icontains=text)
                | Q(entity_id__iexact=text)
                | Q(performed_by__display_name__icontains=text)
            )

        actions = [a for a in params.getlist('action') if a.strip()]
        if actions:
            qs = qs.filter(action__in=actions)

        entities = [e for e in params.getlist('entity_type') if e.strip()]
        if entities:
            qs = qs.filter(entity_type__in=entities)

        source = params.get('source', '').strip()
        if source in (AuditLog.SOURCE_APP, AuditLog.SOURCE_HTTP):
            qs = qs.filter(source=source)

        user_id = params.get('user_id', '').strip()
        if user_id == '0':
            qs = qs.filter(performed_by__isnull=True)
        elif user_id.isdigit():
            qs = qs.filter(performed_by_id=int(user_id))

        status_filter = params.get('status', '').strip().lower()
        if status_filter == 'error':
            qs = qs.filter(status_code__gte=400)
        elif status_filter == 'ok':
            qs = qs.filter(Q(status_code__lt=400) | Q(status_code__isnull=True))

        date_from = _as_aware(_parse_date(params.get('date_from')))
        if date_from:
            qs = qs.filter(timestamp__gte=date_from)
        date_to = _as_aware(_parse_date(params.get('date_to')), end_of_day=True)
        if date_to:
            qs = qs.filter(timestamp__lte=date_to)

        try:
            page = max(1, int(params.get('page', 1)))
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = int(params.get('page_size', DEFAULT_PAGE_SIZE))
        except (TypeError, ValueError):
            page_size = DEFAULT_PAGE_SIZE
        page_size = max(1, min(page_size, MAX_PAGE_SIZE))

        total = qs.count()
        start = (page - 1) * page_size
        rows = qs[start:start + page_size]

        return Response({
            'count': total,
            'page': page,
            'page_size': page_size,
            'has_next': start + page_size < total,
            'results': ActivityLogSerializer(rows, many=True).data,
        })


class ActivityLogMetaView(APIView):
    """``GET /api/admin/activity-log/meta/`` — the values worth filtering on.

    Distinct actions and entity types are computed from the last 90 days rather
    than the whole table: a filter dropdown listing every entity type that ever
    existed is a worse dropdown, and the scan stays bounded as the log grows.
    """
    permission_classes = [IsAppAuthenticated]

    LOOKBACK_DAYS = 90

    def get(self, request):
        if request.user.role not in READER_ROLES:
            return _forbidden()

        since = timezone.now() - timedelta(days=self.LOOKBACK_DAYS)
        recent = AuditLog.objects.filter(timestamp__gte=since)

        actions = list(
            recent.values('action')
                  .annotate(n=Count('id'))
                  .order_by('-n')
                  .values_list('action', flat=True)
        )
        entity_types = list(
            recent.values('entity_type')
                  .annotate(n=Count('id'))
                  .order_by('-n')
                  .values_list('entity_type', flat=True)
        )

        users = [
            {'id': u.id, 'display_name': u.display_name, 'role': u.role}
            for u in AppUser.objects.order_by('display_name')
        ]

        oldest = AuditLog.objects.order_by('timestamp').values_list('timestamp', flat=True).first()

        return Response({
            'actions': actions,
            'entity_types': entity_types,
            'users': users,
            'sources': [AuditLog.SOURCE_APP, AuditLog.SOURCE_HTTP],
            'total_rows': AuditLog.objects.count(),
            'oldest_timestamp': oldest,
            'lookback_days': self.LOOKBACK_DAYS,
        })
