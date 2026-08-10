"""GET /api/reports/patient-activity/ and its .xlsx twin.

Bucketing goes through ``services.patient_activity.classify`` — the same
function the CRM list and the patient profile badge use (``crm_page.
_serialize_crm_row``) — so this report can never disagree with those pages
about which bucket a patient is in. See docs/
stock-movement-patient-activity-design.md §5.
"""
import datetime
from decimal import Decimal

from django.db.models import Q
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import Patient, ReportSettings
from ..services.patient_activity import BUCKETS, classify
from .reports_page import _report_to_xlsx

_ALLOWED_ORDERINGS = {
    'name', '-name', 'last_visit_date', '-last_visit_date',
    'total_spend', '-total_spend', 'total_visits', '-total_visits',
}

_SORT_FIELD = {
    'name': 'name', 'last_visit_date': 'last_visit_date',
    'total_spend': 'total_spend', 'total_visits': 'total_visits',
}


def _parse_as_of(request):
    raw = request.query_params.get('as_of', '').strip()
    if not raw:
        return datetime.date.today(), None
    try:
        return datetime.date.fromisoformat(raw), None
    except ValueError:
        return None, f"Format as_of tidak valid: '{raw}'. Gunakan YYYY-MM-DD."


def _build_rows(request):
    """Shared by the JSON view and the .xlsx export. Returns
    (as_of, thresholds_dict, rows) on success, or (None, None, error_response)."""
    as_of, err = _parse_as_of(request)
    if err:
        return None, None, Response({'error': err}, status=status.HTTP_400_BAD_REQUEST)

    settings_obj = ReportSettings.get_solo()

    tier = request.query_params.get('tier', '').strip()
    q = request.query_params.get('q', '').strip()

    qs = Patient.objects.select_related('crm_profile__tier')
    if tier:
        qs = qs.filter(crm_profile__tier__name__iexact=tier)
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(patient_no__icontains=q))

    rows = []
    for patient in qs:
        crm = getattr(patient, 'crm_profile', None)
        last_visit_date = crm.last_visit_date if crm else None
        bucket = classify(last_visit_date, as_of, settings_obj)
        rows.append({
            'patient_no': patient.patient_no,
            'name': patient.name,
            'phone_number': patient.phone_number,
            'tier': {'name': crm.tier.name, 'color_hex': crm.tier.color_hex} if (crm and crm.tier_id) else None,
            'last_visit_date': last_visit_date,
            'days_since_last_visit': (as_of - last_visit_date).days if last_visit_date else None,
            'total_visits': crm.total_visits if crm else 0,
            'total_spend': crm.total_spend if crm else Decimal('0'),
            'activity_bucket': bucket,
        })

    thresholds = {
        'patient_active_months': settings_obj.patient_active_months,
        'patient_inactive_months': settings_obj.patient_inactive_months,
    }
    return as_of, thresholds, rows


def _sort_rows(rows, ordering):
    reverse = ordering.startswith('-')
    field = _SORT_FIELD[ordering.lstrip('-')]
    non_null = [r for r in rows if r[field] is not None]
    null_rows = [r for r in rows if r[field] is None]
    non_null.sort(key=lambda r: r[field], reverse=reverse)
    return non_null + null_rows


def _serialize_row(r):
    return {
        'patient_no': r['patient_no'], 'name': r['name'], 'phone_number': r['phone_number'],
        'tier': r['tier'],
        'last_visit_date': str(r['last_visit_date']) if r['last_visit_date'] else None,
        'days_since_last_visit': r['days_since_last_visit'],
        'total_visits': r['total_visits'],
        'total_spend': str(r['total_spend']),
        'activity_bucket': r['activity_bucket'],
    }


def _filter_by_bucket(rows, request):
    """-> (filtered_rows, error_response_or_None)."""
    bucket_param = request.query_params.get('bucket', '').strip().lower()
    if not bucket_param:
        return rows, None
    if bucket_param not in BUCKETS:
        return None, Response(
            {'error': f"Bucket tidak dikenal: '{bucket_param}'. Pilihan: {', '.join(BUCKETS)}."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return [r for r in rows if r['activity_bucket'] == bucket_param], None


class PatientActivityReportView(APIView):
    def get(self, request):
        as_of, thresholds, rows = _build_rows(request)
        if as_of is None:
            return rows  # the error Response

        summary = {b: {'count': 0, 'total_spend': Decimal('0')} for b in BUCKETS}
        for r in rows:
            s = summary[r['activity_bucket']]
            s['count'] += 1
            s['total_spend'] += r['total_spend']

        filtered, err = _filter_by_bucket(rows, request)
        if err:
            return err

        ordering = request.query_params.get('ordering', '').strip()
        if ordering not in _ALLOWED_ORDERINGS:
            ordering = 'last_visit_date'
        filtered = _sort_rows(filtered, ordering)

        try:
            page = max(1, int(request.query_params.get('page', 1)))
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = int(request.query_params.get('page_size', 50))
        except (TypeError, ValueError):
            page_size = 50
        page_size = min(max(page_size, 1), 200)

        count = len(filtered)
        num_pages = max(1, -(-count // page_size))
        start = (page - 1) * page_size
        page_rows = filtered[start:start + page_size]

        return Response({
            'as_of': str(as_of),
            'thresholds': thresholds,
            'summary': {
                b: {'count': s['count'], 'total_spend': str(s['total_spend'].quantize(Decimal('0.01')))}
                for b, s in summary.items()
            },
            'count': count, 'page': page, 'page_size': page_size, 'num_pages': num_pages,
            'results': [_serialize_row(r) for r in page_rows],
        })


_EXPORT_COLUMNS = [
    'No. Pasien', 'Nama', 'Telepon', 'Tier',
    'Tgl Kunjungan Terakhir', 'Hari Sejak Kunjungan',
    'Total Kunjungan', 'Total Belanja', 'Status Aktivitas',
]

_BUCKET_LABELS = {'active': 'Aktif', 'lapsing': 'Kurang Aktif', 'inactive': 'Tidak Aktif', 'never': 'Belum Pernah'}


class PatientActivityExportView(APIView):
    """GET /api/reports/patient-activity/export/ — same params, returns .xlsx."""

    def get(self, request):
        as_of, _thresholds, rows = _build_rows(request)
        if as_of is None:
            return rows  # the error Response

        rows, err = _filter_by_bucket(rows, request)
        if err:
            return err

        ordering = request.query_params.get('ordering', '').strip()
        if ordering not in _ALLOWED_ORDERINGS:
            ordering = 'last_visit_date'
        rows = _sort_rows(rows, ordering)

        table_rows = [
            [
                r['patient_no'], r['name'], r['phone_number'] or '',
                r['tier']['name'] if r['tier'] else '',
                r['last_visit_date'].isoformat() if r['last_visit_date'] else '',
                r['days_since_last_visit'] if r['days_since_last_visit'] is not None else '',
                r['total_visits'], float(r['total_spend']),
                _BUCKET_LABELS[r['activity_bucket']],
            ]
            for r in rows
        ]
        report = {'kind': 'flat', 'columns': _EXPORT_COLUMNS, 'rows': table_rows}
        return _report_to_xlsx(
            report, 'Laporan Aktivitas Pasien', f'per {as_of.isoformat()}',
            f'patient_activity_{as_of}.xlsx',
        )
