from datetime import date
from decimal import Decimal, ROUND_DOWN

from ..models import ChartOfAccounts, TaxRule
from ..views.financial_reports_utils import (
    ZERO,
    account_movements,
    accounts_by_id,
    signed_balance,
)

ROUNDING_STEPS = {
    'none':     None,
    'rupiah':   Decimal('1'),
    'thousand': Decimal('1000'),
}


class TaxRuleError(Exception):
    pass


def rule_window(rule, date_from, date_to):
    if rule.basis == 'ytd':
        return date(date_to.year, 1, 1), date_to
    return date_from, date_to


def _subtree_ids(root, accounts):
    children = {}
    for acc in accounts.values():
        if acc.parent_id:
            children.setdefault(acc.parent_id, []).append(acc.id)

    out, stack = set(), [root.id]
    while stack:
        node = stack.pop()
        if node in out:
            continue
        out.add(node)
        stack.extend(children.get(node, ()))
    return out


def _component_accounts(component, accounts):
    if component.source == 'account':
        if not component.account_id:
            raise TaxRuleError(f'Komponen {component.pk} tidak menunjuk akun.')
        acc = accounts.get(component.account_id)
        if acc is None:
            raise TaxRuleError(f'Akun {component.account_id} tidak ditemukan.')
        return {acc.id}, f'{acc.account_number} – {acc.name}'

    if component.source == 'subtree':
        if not component.account_id:
            raise TaxRuleError(f'Komponen {component.pk} tidak menunjuk akun.')
        acc = accounts.get(component.account_id)
        if acc is None:
            raise TaxRuleError(f'Akun {component.account_id} tidak ditemukan.')
        return _subtree_ids(acc, accounts), f'{acc.account_number} – {acc.name} (& turunan)'

    if component.source == 'type':
        ids = {a.id for a in accounts.values() if a.account_type == component.account_type}
        label = dict(ChartOfAccounts.ACCOUNT_TYPE_CHOICES).get(
            component.account_type, component.account_type)
        return ids, f'Semua akun {label}'

    return set(), ''


def _resolve_component(component, movements, accounts, results):
    sign = Decimal(component.sign if component.sign in (1, -1) else 1)

    if component.source == 'fixed':
        raw = component.fixed_amount or ZERO
        return sign * raw, {
            'source': 'fixed',
            'label': component.label or 'Nilai tetap',
            'accounts': [],
            'raw_amount': raw,
            'sign': int(sign),
            'amount': sign * raw,
        }

    if component.source == 'rule':
        if not component.source_rule_id:
            raise TaxRuleError(f'Komponen {component.pk} tidak menunjuk aturan.')
        ref = results.get(component.source_rule_id)
        if ref is None:
            raise TaxRuleError(
                f'Aturan rujukan (id {component.source_rule_id}) belum dihitung.')
        raw = ref['result']
        return sign * raw, {
            'source': 'rule',
            'label': component.label or ref['name'],
            'accounts': [],
            'rule_code': ref['code'],
            'raw_amount': raw,
            'sign': int(sign),
            'amount': sign * raw,
        }

    account_ids, auto_label = _component_accounts(component, accounts)

    raw = ZERO
    rows = []
    for acc_id in sorted(account_ids):
        movement = movements.get(acc_id)
        if not movement:
            continue
        acc = accounts[acc_id]
        amount = signed_balance(acc.account_type, movement['net'])
        if amount == ZERO:
            continue
        raw += amount
        rows.append({
            'account_number': acc.account_number,
            'name': acc.name,
            'amount': amount,
        })

    return sign * raw, {
        'source': component.source,
        'label': component.label or auto_label,
        'accounts': rows,
        'raw_amount': raw,
        'sign': int(sign),
        'amount': sign * raw,
    }


def _apply_brackets(base, brackets):
    tax = ZERO
    lower = ZERO
    trace = []
    for br in brackets:
        if base <= lower:
            break
        top = br.upper_bound if br.upper_bound is not None else base
        slice_amount = min(base, top) - lower
        if slice_amount <= ZERO:
            lower = top
            continue
        rate = (br.rate_percent or ZERO) / Decimal('100')
        layer_tax = slice_amount * rate
        tax += layer_tax
        trace.append({
            'from': lower,
            'to': br.upper_bound,
            'rate_percent': br.rate_percent,
            'amount': slice_amount,
            'tax': layer_tax,
        })
        lower = top
    return tax, trace


def _apply_facility(rule, base, turnover):
    rate = (rule.rate_percent or ZERO) / Decimal('100')
    factor = rule.facility_factor if rule.facility_factor is not None else Decimal('1')
    cap = rule.facility_turnover_cap or ZERO
    full_cap = rule.facility_full_rate_cap or ZERO

    if turnover <= ZERO or cap <= ZERO:
        facilitated = ZERO
    elif turnover <= cap:
        facilitated = base
    elif full_cap and turnover > full_cap:
        facilitated = ZERO
    else:
        facilitated = base * (cap / turnover)

    remainder = base - facilitated
    tax = facilitated * rate * factor + remainder * rate

    return tax, {
        'turnover': turnover,
        'turnover_cap': cap,
        'full_rate_cap': full_cap,
        'factor': factor,
        'facilitated_base': facilitated,
        'full_rate_base': remainder,
        'rate_percent': rule.rate_percent,
    }


def _round(amount, mode):
    step = ROUNDING_STEPS.get(mode)
    if step is None:
        return amount.quantize(Decimal('0.01'), rounding=ROUND_DOWN)
    sign = Decimal(-1) if amount < ZERO else Decimal(1)
    magnitude = (abs(amount) / step).quantize(Decimal('1'), rounding=ROUND_DOWN) * step
    return sign * magnitude


def order_rules(rules):
    by_id = {r.id: r for r in rules}

    def deps(rule):
        out = set()
        for c in rule.components.all():
            if c.source == 'rule' and c.source_rule_id in by_id:
                out.add(c.source_rule_id)
        if rule.rate_mode == 'facility' and rule.facility_turnover_rule_id in by_id:
            out.add(rule.facility_turnover_rule_id)
        return out

    ordered, state = [], {}

    def visit(rule, path):
        mark = state.get(rule.id)
        if mark == 2:
            return
        if mark == 1:
            names = ' → '.join(by_id[i].code for i in path + [rule.id])
            raise TaxRuleError(f'Aturan pajak saling merujuk: {names}')
        state[rule.id] = 1
        for dep_id in sorted(deps(rule)):
            visit(by_id[dep_id], path + [rule.id])
        state[rule.id] = 2
        ordered.append(rule)

    for r in rules:
        visit(r, [])
    return ordered


def evaluate_rule(rule, date_from, date_to, accounts, results, movement_cache):
    win_from, win_to = rule_window(rule, date_from, date_to)
    key = (win_from, win_to)
    if key not in movement_cache:
        movement_cache[key] = account_movements(date_from=win_from, date_to=win_to)
    movements = movement_cache[key]

    base = ZERO
    component_trace = []
    for component in rule.components.all():
        amount, trace = _resolve_component(component, movements, accounts, results)
        base += amount
        component_trace.append(trace)

    gross_base = base
    deduction = rule.deduction_amount or ZERO
    if deduction:
        base = max(base - deduction, ZERO)

    rate_trace = {}
    if rule.rate_mode == 'none':
        tax = base
    elif rule.rate_mode == 'bracket':
        tax, layers = _apply_brackets(base, list(rule.brackets.all()))
        rate_trace = {'brackets': layers}
    elif rule.rate_mode == 'facility':
        turnover = ZERO
        if rule.facility_turnover_rule_id:
            ref = results.get(rule.facility_turnover_rule_id)
            if ref is None:
                raise TaxRuleError(
                    f'{rule.code}: aturan peredaran bruto belum dihitung.')
            turnover = ref['base']
        tax, rate_trace = _apply_facility(rule, base, turnover)
    else:
        rate = (rule.rate_percent or ZERO) / Decimal('100')
        tax = base * rate
        rate_trace = {'rate_percent': rule.rate_percent}

    result = _round(tax, rule.rounding)

    return {
        'id': rule.id,
        'code': rule.code,
        'name': rule.name,
        'description': rule.description,
        'basis': rule.basis,
        'rate_mode': rule.rate_mode,
        'rounding': rule.rounding,
        'window': {'date_from': win_from.isoformat(), 'date_to': win_to.isoformat()},
        'components': component_trace,
        'gross_base': gross_base,
        'deduction': deduction,
        'base': base,
        'rate_detail': rate_trace,
        'raw_result': tax,
        'result': result,
        'notes': rule.notes,
    }


def compute(date_from, date_to, rules=None):
    if rules is None:
        rules = [
            r for r in TaxRule.objects
            .filter(is_active=True)
            .prefetch_related('components', 'brackets')
            if r.applies_on(date_to)
        ]

    accounts = accounts_by_id()
    results = {}
    movement_cache = {}

    for rule in order_rules(rules):
        results[rule.id] = evaluate_rule(
            rule, date_from, date_to, accounts, results, movement_cache)

    ordered = sorted(rules, key=lambda r: (r.display_order, r.code))
    return [results[r.id] for r in ordered]
