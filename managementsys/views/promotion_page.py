from datetime import date
from decimal import Decimal

from django.db.models import Count
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import AppUser, Patient, PatientCRMProfile, Promotion, PromotionUsage


def validate_promotion(code, patient, subtotal):
    """
    Returns (Promotion, discount_amount, None) on success,
    or (None, Decimal('0'), error_string) on failure.

    patient  — Patient instance or None
    subtotal — Decimal
    """
    zero = Decimal('0')

    try:
        promo = Promotion.objects.select_related('min_tier').get(
            code=code, is_active=True
        )
    except Promotion.DoesNotExist:
        return None, zero, 'Promotion code not found or inactive.'

    today = date.today()

    if promo.valid_from > today:
        return None, zero, 'Promotion is not yet valid.'

    if promo.valid_until is not None and promo.valid_until < today:
        return None, zero, 'Promotion has expired.'

    if promo.max_uses is not None:
        used = promo.usages.count()
        if used >= promo.max_uses:
            return None, zero, 'Promotion usage limit has been reached.'

    if promo.max_uses_per_patient > 0 and patient is not None:
        per_patient = promo.usages.filter(patient_no=patient).count()
        if per_patient >= promo.max_uses_per_patient:
            return None, zero, 'You have already used this promotion the maximum number of times.'

    if promo.min_tier is not None:
        if patient is None:
            return None, zero, 'This promotion requires a registered patient.'
        try:
            crm = PatientCRMProfile.objects.select_related('tier').get(patient_no=patient)
        except PatientCRMProfile.DoesNotExist:
            return None, zero, 'Patient does not meet the tier requirement for this promotion.'
        if crm.tier is None or crm.tier.sort_order < promo.min_tier.sort_order:
            return None, zero, 'Patient tier does not meet the minimum required for this promotion.'

    subtotal = Decimal(str(subtotal))

    if promo.discount_type == 'percent':
        discount = (subtotal * promo.discount_value / Decimal('100')).quantize(Decimal('0.01'))
    else:
        discount = min(promo.discount_value, subtotal)

    return promo, discount, None


class PromotionListCreateView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        qs = (
            Promotion.objects
            .select_related('min_tier', 'created_by')
            .prefetch_related('applicable_categories', 'applicable_items')
            .annotate(usage_count=Count('usages'))
            .order_by('id')
        )
        data = []
        for p in qs:
            data.append(_serialize_promotion(p, usage_count=p.usage_count))
        return Response(data)

    def post(self, request):
        payload = request.data
        errors = {}

        code = (payload.get('code') or '').strip()
        name = (payload.get('name') or '').strip()
        discount_type = payload.get('discount_type')
        discount_value = payload.get('discount_value')
        valid_from = payload.get('valid_from')

        if not code:
            errors['code'] = 'Required.'
        elif Promotion.objects.filter(code=code).exists():
            errors['code'] = 'A promotion with this code already exists.'
        if not name:
            errors['name'] = 'Required.'
        if discount_type not in ('percent', 'fixed'):
            errors['discount_type'] = "Must be 'percent' or 'fixed'."
        if discount_value is None:
            errors['discount_value'] = 'Required.'
        if not valid_from:
            errors['valid_from'] = 'Required.'

        scope = payload.get('scope', 'all')
        if scope not in ('all', 'category', 'item'):
            errors['scope'] = "Must be 'all', 'category', or 'item'."

        if errors:
            return Response(errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            discount_value = Decimal(str(discount_value))
        except Exception:
            return Response({'discount_value': 'Invalid number.'}, status=status.HTTP_400_BAD_REQUEST)

        actor = request.user if isinstance(request.user, AppUser) else None

        min_tier_id = payload.get('min_tier_id')
        created_by_id = payload.get('created_by_id')

        try:
            promo = Promotion.objects.create(
                code=code,
                name=name,
                description=payload.get('description', ''),
                discount_type=discount_type,
                discount_value=discount_value,
                scope=scope,
                valid_from=valid_from,
                valid_until=payload.get('valid_until') or None,
                max_uses=payload.get('max_uses') or None,
                max_uses_per_patient=int(payload.get('max_uses_per_patient') or 0),
                min_tier_id=min_tier_id or None,
                is_auto=bool(payload.get('is_auto', False)),
                is_active=bool(payload.get('is_active', True)),
                created_by_id=created_by_id or (actor.id if actor else None),
            )
        except Exception as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        cat_ids = payload.get('applicable_category_ids', [])
        item_ids = payload.get('applicable_item_ids', [])
        if cat_ids:
            promo.applicable_categories.set(cat_ids)
        if item_ids:
            promo.applicable_items.set(item_ids)

        return Response(_serialize_promotion(promo, usage_count=0), status=status.HTTP_201_CREATED)


class PromotionDetailView(APIView):
    permission_classes = [AllowAny]

    def _get(self, pk):
        try:
            return (
                Promotion.objects
                .select_related('min_tier', 'created_by')
                .prefetch_related('applicable_categories', 'applicable_items')
                .annotate(usage_count=Count('usages'))
                .get(pk=pk)
            )
        except Promotion.DoesNotExist:
            return None

    def get(self, request, pk):
        promo = self._get(pk)
        if promo is None:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(_serialize_promotion(promo, usage_count=promo.usage_count))

    def put(self, request, pk):
        promo = self._get(pk)
        if promo is None:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        payload = request.data
        updatable_scalars = (
            'name', 'description', 'discount_type', 'discount_value',
            'scope', 'valid_from', 'valid_until',
            'max_uses', 'max_uses_per_patient',
            'is_auto', 'is_active',
        )
        for field in updatable_scalars:
            if field in payload:
                value = payload[field]
                if field == 'discount_value':
                    try:
                        value = Decimal(str(value))
                    except Exception:
                        return Response({'discount_value': 'Invalid number.'}, status=status.HTTP_400_BAD_REQUEST)
                if field in ('valid_until', 'max_uses') and value == '':
                    value = None
                setattr(promo, field, value)

        if 'code' in payload:
            new_code = (payload['code'] or '').strip()
            if not new_code:
                return Response({'code': 'Code cannot be blank.'}, status=status.HTTP_400_BAD_REQUEST)
            if Promotion.objects.exclude(pk=pk).filter(code=new_code).exists():
                return Response({'code': 'Code already in use.'}, status=status.HTTP_400_BAD_REQUEST)
            promo.code = new_code

        if 'min_tier_id' in payload:
            promo.min_tier_id = payload['min_tier_id'] or None

        promo.save()

        if 'applicable_category_ids' in payload:
            promo.applicable_categories.set(payload['applicable_category_ids'] or [])
        if 'applicable_item_ids' in payload:
            promo.applicable_items.set(payload['applicable_item_ids'] or [])

        promo = self._get(pk)
        return Response(_serialize_promotion(promo, usage_count=promo.usage_count))

    def delete(self, request, pk):
        promo = self._get(pk)
        if promo is None:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        promo.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PromotionValidateView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        code = (request.data.get('code') or '').strip()
        patient_no = (request.data.get('patient_no') or '').strip()
        subtotal_raw = request.data.get('invoice_subtotal', 0)

        if not code:
            return Response(
                {'valid': False, 'discount_amount': '0.00', 'promotion_name': None, 'error': 'code is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            subtotal = Decimal(str(subtotal_raw))
        except Exception:
            return Response(
                {'valid': False, 'discount_amount': '0.00', 'promotion_name': None, 'error': 'invoice_subtotal must be a number.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        patient = None
        if patient_no:
            try:
                patient = Patient.objects.get(patient_no=patient_no)
            except Patient.DoesNotExist:
                return Response(
                    {'valid': False, 'discount_amount': '0.00', 'promotion_name': None, 'error': 'Patient not found.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        promo, discount, error = validate_promotion(code, patient, subtotal)

        if error:
            return Response({
                'valid': False,
                'discount_amount': '0.00',
                'promotion_name': None,
                'error': error,
            })

        return Response({
            'valid': True,
            'discount_amount': str(discount),
            'promotion_name': promo.name,
            'error': None,
        })


# ── Internal helper ────────────────────────────────────────────────────────

def _serialize_promotion(p, *, usage_count):
    return {
        'id': p.id,
        'code': p.code,
        'name': p.name,
        'description': p.description,
        'discount_type': p.discount_type,
        'discount_value': str(p.discount_value),
        'scope': p.scope,
        'applicable_category_ids': list(p.applicable_categories.values_list('id', flat=True)),
        'applicable_item_ids': list(p.applicable_items.values_list('id', flat=True)),
        'valid_from': str(p.valid_from),
        'valid_until': str(p.valid_until) if p.valid_until else None,
        'max_uses': p.max_uses,
        'max_uses_per_patient': p.max_uses_per_patient,
        'min_tier_id': p.min_tier_id,
        'min_tier_name': p.min_tier.name if p.min_tier_id else None,
        'is_auto': p.is_auto,
        'is_active': p.is_active,
        'created_by_id': p.created_by_id,
        'created_by_name': p.created_by.display_name if p.created_by_id else None,
        'created_at': p.created_at.isoformat(),
        'usage_count': usage_count,
    }
