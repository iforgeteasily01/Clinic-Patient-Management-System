import platform
import sys
import time
from datetime import timedelta

import django
from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, connections
from django.utils import timezone
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from ..auth_backend import IsAppAuthenticated
from ..models import ActivePatient, AuditLog, Invoice, JournalDayLog, Patient

READER_ROLES = ('superuser', 'manager')

_PROCESS_STARTED_AT = time.time()


def _probe(alias):
    started = time.monotonic()
    try:
        with connections[alias].cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
    except Exception as exc:
        return False, None, f'{type(exc).__name__}: {exc}'
    return True, round((time.monotonic() - started) * 1000, 1), None


class SystemHealthView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        ok, latency_ms, error = _probe(DEFAULT_DB_ALIAS)
        return Response(
            {
                'status': 'ok' if ok else 'degraded',
                'database': {'ok': ok, 'latency_ms': latency_ms, 'error': error},
                'server_time': timezone.now(),
                'uptime_seconds': int(time.time() - _PROCESS_STARTED_AT),
            },
            status=200 if ok else 503,
        )


class SystemStatusView(APIView):
    permission_classes = [IsAppAuthenticated]

    def get(self, request):
        if request.user.role not in READER_ROLES:
            return Response(
                {'error': 'Only a superuser or manager may read server status.'},
                status=403,
            )

        now = timezone.now()
        today = timezone.localdate()

        db_ok, db_latency, db_error = _probe(DEFAULT_DB_ALIAS)
        databases = {
            'default': {'ok': db_ok, 'latency_ms': db_latency, 'error': db_error},
        }
        for alias in ('external', 'ipos'):
            if alias in settings.DATABASES:
                ok, latency, error = _probe(alias)
                databases[alias] = {'ok': ok, 'latency_ms': latency, 'error': error}

        last_posted = (
            JournalDayLog.objects.filter(is_posted=True)
            .order_by('-date')
            .values_list('date', flat=True)
            .first()
        )

        return Response({
            'server_time': now,
            'uptime_seconds': int(time.time() - _PROCESS_STARTED_AT),
            'debug': settings.DEBUG,
            'versions': {
                'python': sys.version.split()[0],
                'django': django.get_version(),
                'platform': f'{platform.system()} {platform.release()}',
                'hostname': platform.node(),
            },
            'databases': databases,
            'counts': {
                'patients': Patient.objects.count(),
                'active_visits': ActivePatient.objects.exclude(status=5).count(),
                'invoices_today': Invoice.objects.filter(
                    datetime__date=today, is_voided=False).count(),
                'unposted_invoices': Invoice.objects.filter(
                    posting_status='unposted', is_voided=False).count(),
                'activity_rows': AuditLog.objects.count(),
            },
            'journal': {
                'last_posted_date': last_posted,
            },
            'activity_errors_24h': AuditLog.objects.filter(
                timestamp__gte=now - timedelta(days=1),
                status_code__gte=400,
            ).count(),
        })
