"""
Evaluation of TaxRule rows against the LedgerEntry journal.

A rule is built on the page as data (accounts + rate shape); this module turns
that data into a number for a requested period, together with a full trace of
how it got there. Nothing here writes to the ledger.

Two properties matter more than anything else and are the reason this is not
just a sum:

* **Every figure comes from `account_movements()`**, the same helper the
  financial reports use, so a tax base and the Laba Rugi it is supposed to
  agree with can never diverge. Cached `ChartOfAccounts.balance` is never read.

* **Rules may reference other rules**, which makes the rule set a graph. It is
  evaluated in dependency order with cycle detection, so `PPN Kurang Bayar =
  keluaran - masukan` works and `A -> B -> A` is reported rather than hanging.

Sign convention
---------------
Component amounts are *natural-signed* (`signed_balance`): revenue reads
positive, expenses read positive. A base is the signed sum of its components,
so subtracting an expense group from a revenue group is `sign=-1`, not a
sign-flip buried in the account type.
"""

from datetime import date
from decimal import Decimal, ROUND_DOWN

from ..models import ChartOfAccounts, TaxRule
from ..views.financial_reports_utils import (
    ZERO,
    account_movements,
    accounts_by_id,
    signed_balance,
)

# Rounding step per TaxRule.rounding choice. Indonesian tax figures round
# *down* — never half-up — so a rounded figure is never more than the ledger
# supports.
ROUNDING_STEPS = {
    'none':     None,
    'rupiah':   Decimal('1'),
    'thousand': Decimal('1000'),
}


class TaxRuleError(Exception):
    """A rule cannot be evaluated: bad reference, cycle, or missing account."""


# ── window ───────────────────────────────────────────────────────────────────

def rule_window(rule, date_from, date_to):
    """The (date_from, date_to) a rule actually reads.

    ``basis='ytd'`` widens the start to 1 January of ``date_to``'s year. The
    Rp 4,8bn omzet ceiling and the Pasal 31E caps are annual tests; running
    them against a single month would clear every threshold and silently
    produce the wrong rate.
    """
    if rule.basis == 'ytd':
        return date(date_to.year, 1, 1), date_to
    return date_from, date_to


# ── component resolution ─────────────────────────────────────────────────────

def _subtree_ids(root, accounts):
    """`root` plus every account beneath it, walking the parent chain.

    ChartOfAccounts nests only head -> sub today, but the walk is written to
    handle arbitrary depth so a future third level does not silently drop
    accounts out of a tax base.
    """
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
    """Account ids a component draws from, and the label to show for it."""
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
    """Evaluate one component to (signed_amount, trace_dict).

    ``results`` maps rule code -> already-computed result, for source='rule'.
    """
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


# ── rate application ─────────────────────────────────────────────────────────

def _apply_brackets(base, brackets):
    """Progressive layers. Only the slice of `base` inside a layer is taxed at
    that layer's rate — a bracket rate never applies to the whole base."""
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
    """Pasal 31E.

    Income attributable to the first ``facility_turnover_cap`` of gross
    turnover is taxed at the discounted rate; the rest at the full rate. The
    proportional middle band is the legally correct treatment and is what makes
    this its own mode rather than a simple if/else on the rate:

        turnover <= cap            -> the whole base is facilitated
        cap < turnover <= full_cap -> facilitated share = base * cap / turnover
        turnover > full_cap        -> no facility at all
    """
    rate = (rule.rate_percent or ZERO) / Decimal('100')
    factor = rule.facility_factor if rule.facility_factor is not None else Decimal('1')
    cap = rule.facility_turnover_cap or ZERO
    full_cap = rule.facility_full_rate_cap or ZERO

    if turnover <= ZERO or cap <= ZERO:
        # Nothing to test against — charge the plain rate rather than silently
        # granting a discount the turnover does not support.
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
    # Round the magnitude down and restore the sign, so a refund position
    # (-1_500) rounds to -1_000 rather than -2_000.
    sign = Decimal(-1) if amount < ZERO else Decimal(1)
    magnitude = (abs(amount) / step).quantize(Decimal('1'), rounding=ROUND_DOWN) * step
    return sign * magnitude


# ── ordering ─────────────────────────────────────────────────────────────────

def order_rules(rules):
    """Rules in dependency order: a rule always follows the rules it reads.

    Raises TaxRuleError naming the cycle rather than recursing forever.
    """
    by_id = {r.id: r for r in rules}

    def deps(rule):
        out = set()
        for c in rule.components.all():
            if c.source == 'rule' and c.source_rule_id in by_id:
                out.add(c.source_rule_id)
        if rule.rate_mode == 'facility' and rule.facility_turnover_rule_id in by_id:
            out.add(rule.facility_turnover_rule_id)
        return out

    ordered, state = [], {}  # state: 1 = visiting, 2 = done

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


# ── evaluation ───────────────────────────────────────────────────────────────

def evaluate_rule(rule, date_from, date_to, accounts, results, movement_cache):
    """Evaluate one rule. Returns the result dict that the API serialises."""
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
        # A deduction can only reduce the base to nil, never invert it into a
        # negative tax (PTKP above gross pay means no PPh 21, not a refund).
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
            # 31E tests gross turnover, which is the referenced rule's *base* —
            # its result would be a tax amount, not a turnover.
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
    """Evaluate every applicable rule for the window.

    ``rules`` defaults to all active rules whose effective window covers
    ``date_to``. Returns a list of result dicts in display order.
    """
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

    # Dependency order is an implementation detail; the page shows the order
    # the operator arranged the rules in.
    ordered = sorted(rules, key=lambda r: (r.display_order, r.code))
    return [results[r.id] for r in ordered]
