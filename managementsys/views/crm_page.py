import logging
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, F, Max, Q, Sum
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import Invoice, Patient, PatientCRMProfile, PatientTier, Promotion, ReportSettings
from ..services.patient_activity import classify

logger = logging.getLogger(__name__)

# Valid ?activity= values. Filtered as date ranges on crm_profile__last_visit_date
# below rather than by calling classify() per row — same cutoffs, one query
# instead of a full table scan in Python.
_ACTIVITY_BUCKETS = {'active', 'lapsing', 'inactive', 'never'}

ALLOWED_ORDERINGS = {
    'total_spend':       F('crm_profile__total_spend').asc(nulls_last=True),
    '-total_spend':      F('crm_profile__total_spend').desc(nulls_last=True),
    'total_visits':      F('crm_profile__total_visits').asc(nulls_last=True),
    '-total_visits':     F('crm_profile__total_visits').desc(nulls_last=True),
    'last_visit_date':   F('crm_profile__last_visit_date').asc(nulls_last=True),
    '-last_visit_date':  F('crm_profile__last_visit_date').desc(nulls_last=True),
    'name':              'name',
    '-name':             '-name',
    'patient_no':        'patient_no',
    '-patient_no':       '-patient_no',
    'tier':              'crm_profile__tier__sort_order',
    '-tier':             '-crm_profile__tier__sort_order',
}


def refresh_crm_profile(patient):
    """Recompute and persist the CRM stats for a single Patient instance.

    Voided invoices are excluded — they represent reversed sales and should not
    count toward spend, visit count, or last-visit date.
    """
    agg = Invoice.objects.filter(patient_no=patient, is_voided=False).aggregate(
        total_spend=Sum('grand_total'),
        total_visits=Count('id'),
        last_visit=Max('datetime'),
    )

    total_spend = agg['total_spend'] or Decimal('0')
    total_visits = agg['total_visits'] or 0
    last_visit_date = agg['last_visit'].date() if agg['last_visit'] else None

    # Highest tier where both thresholds are met (ordered by sort_order desc)
    best_tier = (
        PatientTier.objects
        .filter(
            min_visit_count__lte=total_visits,
            min_total_spend__lte=total_spend,
        )
        .order_by('-sort_order')
        .first()
    )

    PatientCRMProfile.objects.update_or_create(
        patient_no=patient,
        defaults={
            'tier': best_tier,
            'total_spend': total_spend,
            'total_visits': total_visits,
            'last_visit_date': last_visit_date,
        },
    )


# ── CRM Patient views ──────────────────────────────────────────────────────

PAGE_SIZE_CHOICES = {10, 25, 50, 100}
DEFAULT_PAGE_SIZE = 25


class PatientCRMListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        qs = (
            Patient.objects
            .select_related('crm_profile__tier')
            .annotate(active_packages_count=Count('packages', filter=Q(packages__status='active')))
            .order_by('name')
        )

        if q := request.GET.get('q', '').strip():
            qs = qs.filter(
                Q(name__icontains=q) | Q(patient_no__icontains=q)
            )

        if tier_name := request.GET.get('tier', '').strip():
            qs = qs.filter(crm_profile__tier__name__iexact=tier_name)

        as_of = date.today()
        settings_obj = ReportSettings.get_solo()

        activity = request.GET.get('activity', '').strip().lower()
        if activity in _ACTIVITY_BUCKETS:
            active_cutoff = as_of - timedelta(days=settings_obj.patient_active_months * 30)
            inactive_cutoff = as_of - timedelta(days=settings_obj.patient_inactive_months * 30)
            if activity == 'active':
                qs = qs.filter(crm_profile__last_visit_date__gte=active_cutoff)
            elif activity == 'lapsing':
                qs = qs.filter(
                    crm_profile__last_visit_date__lt=active_cutoff,
                    crm_profile__last_visit_date__gte=inactive_cutoff,
                )
            elif activity == 'inactive':
                qs = qs.filter(crm_profile__last_visit_date__lt=inactive_cutoff)
            else:  # 'never' — registered but no non-voided invoice yet
                qs = qs.filter(
                    Q(crm_profile__isnull=True) | Q(crm_profile__last_visit_date__isnull=True)
                )

        ordering = request.GET.get('ordering', '').strip()
        if ordering in ALLOWED_ORDERINGS:
            qs = qs.order_by(ALLOWED_ORDERINGS[ordering])

        # Pagination
        try:
            page_size = int(request.GET.get('page_size', DEFAULT_PAGE_SIZE))
        except (ValueError, TypeError):
            page_size = DEFAULT_PAGE_SIZE
        if page_size not in PAGE_SIZE_CHOICES:
            page_size = DEFAULT_PAGE_SIZE

        try:
            page = max(1, int(request.GET.get('page', 1)))
        except (ValueError, TypeError):
            page = 1

        total = qs.count()
        offset = (page - 1) * page_size
        patients = qs[offset: offset + page_size]

        return Response({
            'count': total,
            'page': page,
            'page_size': page_size,
            'num_pages': max(1, -(-total // page_size)),  # ceiling division
            'results': [_serialize_crm_row(p, as_of=as_of, settings_obj=settings_obj) for p in patients],
        })


class PatientCRMDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, patient_no):
        try:
            patient = (
                Patient.objects
                .select_related('crm_profile__tier')
                .get(patient_no=patient_no)
            )
        except Patient.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        data = _serialize_crm_row(patient)

        # Auto-applicable promotions this patient qualifies for
        today = date.today()
        promo_qs = (
            Promotion.objects
            .filter(is_active=True, is_auto=True)
            .filter(valid_from__lte=today)
            .filter(Q(valid_until__isnull=True) | Q(valid_until__gte=today))
        )

        # Filter by tier requirement
        crm = getattr(patient, 'crm_profile', None)
        patient_tier_order = crm.tier.sort_order if (crm and crm.tier_id) else -1
        eligible_promos = []
        for promo in promo_qs.select_related('min_tier'):
            if promo.min_tier is None or promo.min_tier.sort_order <= patient_tier_order:
                eligible_promos.append({
                    'id': promo.id,
                    'code': promo.code,
                    'name': promo.name,
                    'discount_type': promo.discount_type,
                    'discount_value': str(promo.discount_value),
                    'scope': promo.scope,
                })

        data['auto_promotions'] = eligible_promos
        return Response(data)


# ── Tier CRUD ──────────────────────────────────────────────────────────────

class PatientTierListCreateView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        tiers = PatientTier.objects.annotate(patient_count=Count('patientcrmprofile')).order_by('sort_order')
        return Response([_serialize_tier(t, patient_count=t.patient_count) for t in tiers])

    def post(self, request):
        payload = request.data
        errors = {}

        name = (payload.get('name') or '').strip()
        if not name:
            errors['name'] = 'Required.'
        if errors:
            return Response(errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            tier = PatientTier.objects.create(
                name=name,
                min_visit_count=int(payload.get('min_visit_count') or 0),
                min_total_spend=Decimal(str(payload.get('min_total_spend') or '0')),
                color_hex=payload.get('color_hex') or '#6b7280',
                sort_order=int(payload.get('sort_order') or 0),
            )
        except Exception as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(_serialize_tier(tier, patient_count=0), status=status.HTTP_201_CREATED)


class PatientTierDetailView(APIView):
    permission_classes = [AllowAny]

    def _get(self, pk):
        try:
            return (
                PatientTier.objects
                .annotate(patient_count=Count('patientcrmprofile'))
                .get(pk=pk)
            )
        except PatientTier.DoesNotExist:
            return None

    def get(self, request, pk):
        tier = self._get(pk)
        if tier is None:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(_serialize_tier(tier, patient_count=tier.patient_count))

    def put(self, request, pk):
        tier = self._get(pk)
        if tier is None:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        payload = request.data
        for field in ('name', 'color_hex'):
            if field in payload:
                setattr(tier, field, (payload[field] or '').strip())
        for field in ('min_visit_count', 'sort_order'):
            if field in payload:
                try:
                    setattr(tier, field, int(payload[field]))
                except (TypeError, ValueError):
                    return Response({field: 'Must be an integer.'}, status=status.HTTP_400_BAD_REQUEST)
        if 'min_total_spend' in payload:
            try:
                tier.min_total_spend = Decimal(str(payload['min_total_spend']))
            except Exception:
                return Response({'min_total_spend': 'Invalid number.'}, status=status.HTTP_400_BAD_REQUEST)

        tier.save()
        tier = self._get(pk)
        return Response(_serialize_tier(tier, patient_count=tier.patient_count))

    def delete(self, request, pk):
        tier = self._get(pk)
        if tier is None:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        tier.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Internal serializers ───────────────────────────────────────────────────

def _serialize_tier(t, *, patient_count):
    return {
        'id': t.id,
        'name': t.name,
        'min_visit_count': t.min_visit_count,
        'min_total_spend': str(t.min_total_spend),
        'color_hex': t.color_hex,
        'sort_order': t.sort_order,
        'patient_count': patient_count,
    }


def _serialize_crm_row(patient, *, as_of=None, settings_obj=None):
    """Shared by the CRM list and the CRM detail view — ``activity_bucket``
    goes through ``services.patient_activity.classify`` so this row can never
    disagree with the patient-activity report about which bucket a patient is
    in (design doc §5).

    ``as_of``/``settings_obj`` are optional so a caller serializing many rows
    (the list view) can compute them once and pass them down instead of this
    function re-fetching ``ReportSettings.get_solo()`` per row.
    """
    as_of = as_of or date.today()
    settings_obj = settings_obj or ReportSettings.get_solo()
    crm = getattr(patient, 'crm_profile', None)
    tier = None
    if crm and crm.tier_id:
        tier = {'name': crm.tier.name, 'color_hex': crm.tier.color_hex}
    active_packages = getattr(patient, 'active_packages_count', None)
    if active_packages is None:
        active_packages = patient.packages.filter(status='active').count()
    last_visit_date = crm.last_visit_date if crm else None
    days_since_last_visit = (as_of - last_visit_date).days if last_visit_date else None
    return {
        'patient_no': patient.patient_no,
        'name': patient.name,
        'phone_number': patient.phone_number,
        'tier': tier,
        'total_spend': str(crm.total_spend) if crm else '0.00',
        'total_visits': crm.total_visits if crm else 0,
        'last_visit_date': str(last_visit_date) if last_visit_date else None,
        'active_packages': active_packages,
        'activity_bucket': classify(last_visit_date, as_of, settings_obj),
        'days_since_last_visit': days_since_last_visit,
    }
