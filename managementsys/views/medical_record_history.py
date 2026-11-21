from django.db.models import Value
from django.db.models.functions import StrIndex, Substr
from rest_framework.response import Response
from rest_framework.views import APIView

from ..auth_backend import IsAppAuthenticated
from ..models import AppUser, AuditLog, MedRec
from ..api.serializers import MedRecHistorySerializer

EDITOR_ROLES = ('superuser', 'doctor')

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500

EDITABLE_FIELDS = [
    'subjective', 'objective', 'assessment', 'assessment_codes', 'plan',
    'sabun', 'toner',
    'obat1_pagi', 'obat1_pagi_detail', 'obat1_malam', 'obat1_malam_detail',
    'obat2_pagi', 'obat2_pagi_detail', 'obat2_malam', 'obat2_malam_detail',
    'treatment',
]

SORT_FIELDS = {
    'visit_date': ('date_key', 'medrec_id'),
    'medrec_id': ('medrec_id',),
    'patient_name': ('patient_no__name', 'medrec_id'),
    'patient_no': ('patient_no__patient_no', 'medrec_id'),
    'doctor_name': ('doctor_id__doctor_name', 'clinician', 'medrec_id'),
}
DEFAULT_SORT = 'visit_date'


def _current_user(request):
    return request.user if isinstance(request.user, AppUser) else None


def _forbidden():
    return Response(
        {'error': 'Only a superuser or doctor may edit a finalized medical record.'},
        status=403,
    )


def _int_param(params, name, default, minimum, maximum):
    try:
        value = int(params.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _with_date_key(qs):
    return (
        qs.annotate(_tail=Substr('medrec_id', 4))
          .annotate(date_key=Substr('_tail', StrIndex('_tail', Value('-')) + 1, 8))
    )


class MedRecHistoryListView(APIView):
    def get(self, request):
        params = request.query_params
        include_drafts = params.get('include_drafts', '').lower() == 'true'

        qs = MedRec.objects.select_related('patient_no', 'doctor_id').all()
        if not include_drafts:
            qs = qs.filter(status=MedRec.FINALIZED)

        patient_name = params.get('patient_name', '').strip()
        patient_no = params.get('patient_no', '').strip()
        medrec_id = params.get('medrec_id', '').strip()
        date = params.get('date', '').strip()

        if patient_name:
            qs = qs.filter(patient_no__name__icontains=patient_name)
        if patient_no:
            qs = qs.filter(patient_no__patient_no__icontains=patient_no)
        if medrec_id:
            qs = qs.filter(medrec_id__icontains=medrec_id)
        if date:
            date_compact = date.replace('-', '')
            qs = qs.filter(medrec_id__contains=date_compact)

        sort = params.get('sort', '').strip() or DEFAULT_SORT
        if sort not in SORT_FIELDS:
            sort = DEFAULT_SORT
        descending = params.get('dir', 'desc').strip().lower() != 'asc'

        if sort == 'visit_date':
            qs = _with_date_key(qs)

        ordering = [f'-{f}' if descending else f for f in SORT_FIELDS[sort]]
        qs = qs.order_by(*ordering)

        page_size = _int_param(params, 'page_size', DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE)
        total = qs.count()
        total_pages = max(1, -(-total // page_size))
        page = _int_param(params, 'page', 1, 1, total_pages)

        start = (page - 1) * page_size
        rows = qs[start:start + page_size]

        return Response({
            'results': MedRecHistorySerializer(rows, many=True).data,
            'count': total,
            'page': page,
            'page_size': page_size,
            'total_pages': total_pages,
            'sort': sort,
            'dir': 'desc' if descending else 'asc',
        })


class MedRecHistoryDetailView(APIView):
    permission_classes = [IsAppAuthenticated]

    def get(self, request, medrec_id):
        record = MedRec.objects.select_related(
            'patient_no', 'doctor_id').filter(medrec_id=medrec_id).first()
        if record is None:
            return Response({'error': 'Medical record not found.'}, status=404)
        return Response(MedRecHistorySerializer(record).data)

    def patch(self, request, medrec_id):
        user = _current_user(request)
        if user is None or user.role not in EDITOR_ROLES:
            return _forbidden()

        record = MedRec.objects.select_related(
            'patient_no', 'doctor_id').filter(medrec_id=medrec_id).first()
        if record is None:
            return Response({'error': 'Medical record not found.'}, status=404)

        changes = []
        for field in EDITABLE_FIELDS:
            if field not in request.data:
                continue
            before = getattr(record, field)
            after = request.data[field]
            if field == 'assessment_codes':
                if not isinstance(after, list):
                    return Response(
                        {'error': 'assessment_codes must be a list.'}, status=400)
            else:
                after = '' if after is None else str(after)
            if after == before:
                continue
            setattr(record, field, after)
            changes.append((field, before, after))

        if not changes:
            return Response(MedRecHistorySerializer(record).data)

        record.save()

        patient = record.patient_no.name if record.patient_no_id else '-'
        AuditLog.objects.create(
            performed_by=user,
            action='UPDATE',
            entity_type='MedRec',
            entity_id=str(record.medrec_id),
            description=(
                f'Finalized medical record amended: {record.medrec_id} ({patient}) — '
                + '; '.join(f'{f}: {_abbrev(b)} -> {_abbrev(a)}' for f, b, a in changes)
            ),
        )

        return Response(MedRecHistorySerializer(record).data)


def _abbrev(value, limit=80):
    text = '(empty)' if value in ('', None, []) else str(value)
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + '…'
