from decimal import Decimal, InvalidOperation

from django.db import transaction
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import ChartOfAccounts, TaxRule, TaxRuleBracket, TaxRuleComponent
from .financial_reports_page import _parse_range, _unposted_gate, _s


ZERO = Decimal('0')


def _component_json(c):
    return {
        'id': c.id,
        'source': c.source,
        'sign': c.sign,
        'account': c.account_id,
        'account_number': c.account.account_number if c.account_id else None,
        'account_name': c.account.name if c.account_id else None,
        'account_type': c.account_type,
        'source_rule': c.source_rule_id,
        'source_rule_code': c.source_rule.code if c.source_rule_id else None,
        'fixed_amount': _s(c.fixed_amount),
        'label': c.label,
        'display_order': c.display_order,
    }


def _bracket_json(b):
    return {
        'id': b.id,
        'upper_bound': _s(b.upper_bound) if b.upper_bound is not None else None,
        'rate_percent': _s(b.rate_percent),
        'display_order': b.display_order,
    }


def _rule_json(r):
    return {
        'id': r.id,
        'code': r.code,
        'name': r.name,
        'description': r.description,
        'basis': r.basis,
        'rate_mode': r.rate_mode,
        'rate_percent': _s(r.rate_percent),
        'deduction_amount': _s(r.deduction_amount),
        'facility_turnover_rule': r.facility_turnover_rule_id,
        'facility_turnover_cap': _s(r.facility_turnover_cap),
        'facility_full_rate_cap': _s(r.facility_full_rate_cap),
        'facility_factor': _s(r.facility_factor),
        'rounding': r.rounding,
        'display_order': r.display_order,
        'is_active': r.is_active,
        'effective_from': r.effective_from.isoformat() if r.effective_from else None,
        'effective_to': r.effective_to.isoformat() if r.effective_to else None,
        'notes': r.notes,
        'components': [_component_json(c) for c in r.components.all()],
        'brackets': [_bracket_json(b) for b in r.brackets.all()],
    }


def _rules_qs():
    return TaxRule.objects.prefetch_related(
        'components__account', 'components__source_rule', 'brackets')


def _dec(value, field, default=ZERO):
    if value in (None, ''):
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f'{field} bukan angka yang sah.')


def _write_children(rule, data):
    if 'components' in data:
        rule.components.all().delete()
        for i, raw in enumerate(data.get('components') or []):
            source = (raw.get('source') or 'account').strip()
            sign = -1 if int(raw.get('sign', 1)) < 0 else 1
            account_id = raw.get('account') or None
            if source in ('account', 'subtree') and not account_id:
                raise ValueError('Komponen akun wajib memilih akun.')
            if source == 'rule' and not raw.get('source_rule'):
                raise ValueError('Komponen aturan wajib memilih aturan sumber.')
            if source == 'type' and not (raw.get('account_type') or '').strip():
                raise ValueError('Komponen jenis akun wajib memilih jenis.')
            TaxRuleComponent.objects.create(
                rule=rule,
                source=source,
                sign=sign,
                account_id=account_id,
                account_type=(raw.get('account_type') or '').strip(),
                source_rule_id=raw.get('source_rule') or None,
                fixed_amount=_dec(raw.get('fixed_amount'), 'fixed_amount'),
                label=(raw.get('label') or '').strip(),
                display_order=raw.get('display_order', i),
            )

    if 'brackets' in data:
        rule.brackets.all().delete()
        for i, raw in enumerate(data.get('brackets') or []):
            upper = raw.get('upper_bound')
            TaxRuleBracket.objects.create(
                rule=rule,
                upper_bound=_dec(upper, 'upper_bound', default=None) if upper not in (None, '') else None,
                rate_percent=_dec(raw.get('rate_percent'), 'rate_percent'),
                display_order=raw.get('display_order', i),
            )


def _write_rule(rule, data, *, creating):
    for field in ('name', 'description', 'basis', 'rate_mode', 'rounding', 'notes'):
        if field in data:
            setattr(rule, field, (data.get(field) or '').strip())
    if creating or 'code' in data:
        code = (data.get('code') or '').strip()
        if not code:
            raise ValueError('Kode aturan wajib diisi.')
        rule.code = code
    if not rule.name:
        raise ValueError('Nama aturan wajib diisi.')

    for field in ('rate_percent', 'deduction_amount', 'facility_turnover_cap',
                  'facility_full_rate_cap', 'facility_factor'):
        if field in data:
            setattr(rule, field, _dec(data.get(field), field))

    if 'facility_turnover_rule' in data:
        rule.facility_turnover_rule_id = data.get('facility_turnover_rule') or None
    if 'display_order' in data:
        rule.display_order = int(data.get('display_order') or 0)
    if 'is_active' in data:
        rule.is_active = bool(data.get('is_active'))
    for field in ('effective_from', 'effective_to'):
        if field in data:
            setattr(rule, field, data.get(field) or None)


class TaxMetaView(APIView):
    def get(self, request):
        accounts = [
            {
                'id': a.id,
                'account_number': a.account_number,
                'name': a.name,
                'account_type': a.account_type,
                'is_head': a.is_head,
                'parent': a.parent_id,
            }
            for a in ChartOfAccounts.objects.all().only(
                'id', 'account_number', 'name', 'account_type', 'is_head', 'parent_id')
        ]
        return Response({
            'accounts': accounts,
            'account_types': [
                {'value': v, 'label': l} for v, l in ChartOfAccounts.ACCOUNT_TYPE_CHOICES],
            'basis_choices': [
                {'value': v, 'label': l} for v, l in TaxRule.BASIS_CHOICES],
            'rate_modes': [
                {'value': v, 'label': l} for v, l in TaxRule.RATE_MODE_CHOICES],
            'rounding_choices': [
                {'value': v, 'label': l} for v, l in TaxRule.ROUNDING_CHOICES],
            'component_sources': [
                {'value': v, 'label': l} for v, l in TaxRuleComponent.SOURCE_CHOICES],
        })


class TaxRuleListCreateView(APIView):
    def get(self, request):
        return Response([_rule_json(r) for r in _rules_qs()])

    def post(self, request):
        rule = TaxRule()
        try:
            with transaction.atomic():
                _write_rule(rule, request.data, creating=True)
                rule.save()
                _write_children(rule, request.data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(_rule_json(_rules_qs().get(pk=rule.pk)),
                        status=status.HTTP_201_CREATED)


class TaxRuleDetailView(APIView):
    def get(self, request, pk):
        try:
            return Response(_rule_json(_rules_qs().get(pk=pk)))
        except TaxRule.DoesNotExist:
            return Response({'error': 'Aturan tidak ditemukan.'},
                            status=status.HTTP_404_NOT_FOUND)

    def patch(self, request, pk):
        try:
            rule = TaxRule.objects.get(pk=pk)
        except TaxRule.DoesNotExist:
            return Response({'error': 'Aturan tidak ditemukan.'},
                            status=status.HTTP_404_NOT_FOUND)
        try:
            with transaction.atomic():
                _write_rule(rule, request.data, creating=False)
                rule.save()
                _write_children(rule, request.data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(_rule_json(_rules_qs().get(pk=pk)))

    def delete(self, request, pk):
        try:
            rule = TaxRule.objects.get(pk=pk)
        except TaxRule.DoesNotExist:
            return Response({'error': 'Aturan tidak ditemukan.'},
                            status=status.HTTP_404_NOT_FOUND)
        dependants = list(
            rule.referenced_by.select_related('rule').values_list('rule__name', flat=True))
        if dependants:
            return Response(
                {'error': 'Aturan dipakai oleh: ' + ', '.join(sorted(set(dependants)))},
                status=status.HTTP_400_BAD_REQUEST)
        rule.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class TaxComputeView(APIView):
    def get(self, request):
        d_from, d_to, err = _parse_range(request)
        if err:
            return err

        gate = _unposted_gate(d_from, d_to)
        if gate:
            return gate

        from ..services.tax_engine import TaxRuleError, compute

        try:
            rows = compute(d_from, d_to)
        except TaxRuleError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            'date_from': d_from.isoformat(),
            'date_to': d_to.isoformat(),
            'rules': [_result_json(r) for r in rows],
        })


def _result_json(r):
    return {
        **r,
        'gross_base': _s(r['gross_base']),
        'deduction': _s(r['deduction']),
        'base': _s(r['base']),
        'raw_result': _s(r['raw_result']),
        'result': _s(r['result']),
        'components': [
            {
                **c,
                'raw_amount': _s(c['raw_amount']),
                'amount': _s(c['amount']),
                'accounts': [
                    {**a, 'amount': _s(a['amount'])} for a in c['accounts']
                ],
            }
            for c in r['components']
        ],
        'rate_detail': _rate_detail_json(r['rate_detail']),
    }


def _rate_detail_json(detail):
    out = {}
    for key, value in (detail or {}).items():
        if key == 'brackets':
            out[key] = [
                {
                    'from': _s(b['from']),
                    'to': _s(b['to']) if b['to'] is not None else None,
                    'rate_percent': _s(b['rate_percent']),
                    'amount': _s(b['amount']),
                    'tax': _s(b['tax']),
                }
                for b in value
            ]
        elif isinstance(value, Decimal):
            out[key] = _s(value)
        else:
            out[key] = value
    return out
