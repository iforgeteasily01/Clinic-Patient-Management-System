from decimal import Decimal

from django.test import TestCase


# ── Pure calculation helpers (mirror accounting_page.py logic) ─────────────────

def _safe_decimal(val) -> Decimal:
    try:
        return Decimal(str(val))
    except Exception:
        return Decimal('0')


def compute_purchase_totals(items: list, additional_costs: list):
    """
    Replicates the server-side calculation in PurchaseInvoiceListCreateView.post.

    items: list of dicts with keys:
        quantity, unit_cost, total_discount, line_type ('stock'|'expense')

    additional_costs: list of dicts with keys:
        name, modifier ('add'|'subtract'), amount_type ('cash'|'percent'), amount

    Returns:
        {
            'items_subtotal': Decimal,   # sum of adjusted item subtotals
            'net_adjustment': Decimal,   # net effect of additional costs
            'grand_total': Decimal,
            'per_unit_adj': Decimal,     # adjustment per stock unit
            'parsed': list of dict with 'base_unit_cost' and 'actual_unit_cost',
        }
    """
    parsed = []
    items_subtotal = Decimal('0')
    total_units = Decimal('0')

    for row in items:
        qty = _safe_decimal(row.get('quantity', 0))
        cost = _safe_decimal(row.get('unit_cost', 0))
        total_discount = _safe_decimal(row.get('total_discount', 0))
        line_type = row.get('line_type', 'stock')

        gross = qty * cost
        discount_capped = min(total_discount, gross)
        adjusted_sub = gross - discount_capped

        items_subtotal += adjusted_sub
        if line_type == 'stock' and qty > 0:
            total_units += qty

        parsed.append({
            'line_type': line_type,
            'quantity': qty,
            'unit_cost': cost,
            'total_discount': discount_capped,
            'adjusted_sub': adjusted_sub,
        })

    running_total = items_subtotal
    net_adjustment = Decimal('0')

    for ac in additional_costs:
        modifier = ac.get('modifier', 'add')
        amount_type = ac.get('amount_type', 'cash')
        amount = _safe_decimal(ac.get('amount', 0))
        if amount <= 0:
            continue

        if amount_type == 'percent':
            adj = running_total * amount / Decimal('100')
        else:
            adj = amount

        if modifier == 'subtract':
            adj = -adj

        running_total += adj
        net_adjustment += adj

    grand_total = items_subtotal + net_adjustment
    per_unit_adj = Decimal('0')
    if total_units > 0:
        per_unit_adj = net_adjustment / total_units

    result_parsed = []
    for p in parsed:
        qty = p['quantity']
        if qty > 0:
            base_unit_cost = p['adjusted_sub'] / qty
        else:
            base_unit_cost = p['unit_cost']
        actual = base_unit_cost + \
            (per_unit_adj if p['line_type'] == 'stock' else Decimal('0'))
        result_parsed.append(
            {**p, 'base_unit_cost': base_unit_cost, 'actual_unit_cost': actual})

    return {
        'items_subtotal': items_subtotal,
        'net_adjustment': net_adjustment,
        'grand_total': grand_total,
        'per_unit_adj': per_unit_adj,
        'total_units': total_units,
        'parsed': result_parsed,
    }


# ── Tests ──────────────────────────────────────────────────────────────────────

class PurchaseItemDiscountTests(TestCase):

    def test_no_discount_no_additional(self):
        """Basic: 10 items × 10000, no discount, no additional costs."""
        result = compute_purchase_totals(
            items=[{'quantity': 10, 'unit_cost': 10000,
                    'total_discount': 0, 'line_type': 'stock'}],
            additional_costs=[],
        )
        self.assertEqual(result['items_subtotal'], Decimal('100000'))
        self.assertEqual(result['grand_total'], Decimal('100000'))
        self.assertEqual(result['net_adjustment'], Decimal('0'))
        item = result['parsed'][0]
        self.assertEqual(item['base_unit_cost'], Decimal('10000'))
        self.assertEqual(item['actual_unit_cost'], Decimal('10000'))

    def test_row_discount_reduces_unit_cost(self):
        """10 items × 10000 with total_discount=10000 → actual unit cost = 9000."""
        result = compute_purchase_totals(
            items=[{'quantity': 10, 'unit_cost': 10000,
                    'total_discount': 10000, 'line_type': 'stock'}],
            additional_costs=[],
        )
        self.assertEqual(result['items_subtotal'], Decimal('90000'))
        item = result['parsed'][0]
        self.assertEqual(item['base_unit_cost'], Decimal('9000'))
        self.assertEqual(item['actual_unit_cost'], Decimal('9000'))

    def test_discount_cannot_exceed_gross(self):
        """total_discount > gross is capped at gross → actual cost = 0."""
        result = compute_purchase_totals(
            items=[{'quantity': 5, 'unit_cost': 1000,
                    'total_discount': 9999999, 'line_type': 'stock'}],
            additional_costs=[],
        )
        item = result['parsed'][0]
        self.assertEqual(item['total_discount'], Decimal('5000'))   # capped
        self.assertEqual(item['base_unit_cost'], Decimal('0'))

    def test_cash_addition_distributed_per_unit(self):
        """
        Single item: 10 units × 10000, discount 10000 → base = 9000.
        Additional cost: +5000 cash → per_unit_adj = 500 → actual = 9500.
        """
        result = compute_purchase_totals(
            items=[{'quantity': 10, 'unit_cost': 10000,
                    'total_discount': 10000, 'line_type': 'stock'}],
            additional_costs=[
                {'name': 'Ongkir', 'modifier': 'add', 'amount_type': 'cash', 'amount': 5000}],
        )
        self.assertEqual(result['items_subtotal'], Decimal('90000'))
        self.assertEqual(result['net_adjustment'], Decimal('5000'))
        self.assertEqual(result['grand_total'], Decimal('95000'))
        self.assertEqual(result['per_unit_adj'], Decimal('500'))
        item = result['parsed'][0]
        self.assertEqual(item['actual_unit_cost'], Decimal('9500'))

    def test_cash_subtraction_distributed_per_unit(self):
        """Additional discount -5000 on 10 units reduces unit cost by 500."""
        result = compute_purchase_totals(
            items=[{'quantity': 10, 'unit_cost': 10000,
                    'total_discount': 0, 'line_type': 'stock'}],
            additional_costs=[
                {'name': 'Diskon', 'modifier': 'subtract', 'amount_type': 'cash', 'amount': 5000}],
        )
        self.assertEqual(result['net_adjustment'], Decimal('-5000'))
        item = result['parsed'][0]
        self.assertEqual(item['actual_unit_cost'], Decimal('9500'))

    def test_percent_addition(self):
        """10% additional on items_subtotal=100000 → +10000 → grand_total=110000."""
        result = compute_purchase_totals(
            items=[{'quantity': 10, 'unit_cost': 10000,
                    'total_discount': 0, 'line_type': 'stock'}],
            additional_costs=[
                {'name': 'PPh', 'modifier': 'add', 'amount_type': 'percent', 'amount': 10}],
        )
        self.assertEqual(result['net_adjustment'], Decimal('10000'))
        self.assertEqual(result['grand_total'], Decimal('110000'))
        item = result['parsed'][0]
        self.assertEqual(item['actual_unit_cost'], Decimal('11000'))

    def test_percent_subtraction(self):
        """5% off 100000 = -5000 → grand_total = 95000."""
        result = compute_purchase_totals(
            items=[{'quantity': 10, 'unit_cost': 10000,
                    'total_discount': 0, 'line_type': 'stock'}],
            additional_costs=[
                {'name': 'Diskon 5%', 'modifier': 'subtract', 'amount_type': 'percent', 'amount': 5}],
        )
        self.assertEqual(result['net_adjustment'], Decimal('-5000'))
        self.assertEqual(result['grand_total'], Decimal('95000'))

    def test_percent_applied_to_running_total(self):
        """
        Cash +10000 then 10% of new total.
        items_subtotal=100000, +10000 cash → running=110000, then 10%=11000 → grand=121000.
        """
        result = compute_purchase_totals(
            items=[{'quantity': 10, 'unit_cost': 10000,
                    'total_discount': 0, 'line_type': 'stock'}],
            additional_costs=[
                {'name': 'Fee', 'modifier': 'add',
                    'amount_type': 'cash', 'amount': 10000},
                {'name': 'Tax', 'modifier': 'add',
                    'amount_type': 'percent', 'amount': 10},
            ],
        )
        self.assertEqual(result['grand_total'], Decimal('121000'))
        self.assertEqual(result['net_adjustment'], Decimal('21000'))

    def test_multi_item_equal_per_unit_distribution(self):
        """
        Item A: 10 × 1000, Item B: 5 × 2000. Additional +1500 cash.
        total_units = 15, per_unit_adj = 100.
        Item A actual = 1100, Item B actual = 2100.
        """
        result = compute_purchase_totals(
            items=[
                {'quantity': 10, 'unit_cost': 1000,
                    'total_discount': 0, 'line_type': 'stock'},
                {'quantity': 5,  'unit_cost': 2000,
                    'total_discount': 0, 'line_type': 'stock'},
            ],
            additional_costs=[
                {'name': 'Ongkir', 'modifier': 'add', 'amount_type': 'cash', 'amount': 1500}],
        )
        self.assertEqual(result['total_units'], Decimal('15'))
        self.assertEqual(result['per_unit_adj'], Decimal('100'))
        self.assertEqual(result['parsed'][0]
                         ['actual_unit_cost'], Decimal('1100'))
        self.assertEqual(result['parsed'][1]
                         ['actual_unit_cost'], Decimal('2100'))

    def test_expense_line_not_included_in_unit_distribution(self):
        """Expense-type lines do not receive per_unit_adj."""
        result = compute_purchase_totals(
            items=[
                {'quantity': 10, 'unit_cost': 1000,
                    'total_discount': 0, 'line_type': 'stock'},
                {'quantity': 1,  'unit_cost': 500,
                    'total_discount': 0, 'line_type': 'expense'},
            ],
            additional_costs=[
                {'name': 'Fee', 'modifier': 'add', 'amount_type': 'cash', 'amount': 1000}],
        )
        # Only 10 stock units; expense unit is not in total_units
        self.assertEqual(result['total_units'], Decimal('10'))
        self.assertEqual(result['per_unit_adj'], Decimal('100'))
        # Expense line's actual_unit_cost is unchanged (no adj applied)
        expense_item = result['parsed'][1]
        self.assertEqual(
            expense_item['actual_unit_cost'], expense_item['unit_cost'])

    def test_zero_quantity_item_no_division_error(self):
        """Zero-quantity item should not raise ZeroDivisionError."""
        result = compute_purchase_totals(
            items=[{'quantity': 0, 'unit_cost': 5000,
                    'total_discount': 0, 'line_type': 'stock'}],
            additional_costs=[],
        )
        item = result['parsed'][0]
        self.assertEqual(item['actual_unit_cost'], Decimal('5000'))

    def test_additional_cost_with_zero_amount_skipped(self):
        """Additional cost entries with amount=0 are ignored."""
        result = compute_purchase_totals(
            items=[{'quantity': 5, 'unit_cost': 2000,
                    'total_discount': 0, 'line_type': 'stock'}],
            additional_costs=[
                {'name': 'Skip me', 'modifier': 'add',
                    'amount_type': 'cash', 'amount': 0},
            ],
        )
        self.assertEqual(result['net_adjustment'], Decimal('0'))
        self.assertEqual(result['grand_total'], Decimal('10000'))

    def test_grand_total_equals_items_subtotal_plus_net_adjustment(self):
        """Invariant: grand_total == items_subtotal + net_adjustment."""
        result = compute_purchase_totals(
            items=[
                {'quantity': 3, 'unit_cost': 5000,
                    'total_discount': 1500, 'line_type': 'stock'},
                {'quantity': 2, 'unit_cost': 7500,
                    'total_discount': 0,    'line_type': 'stock'},
            ],
            additional_costs=[
                {'name': 'Tax',     'modifier': 'add',
                    'amount_type': 'percent', 'amount': 11},
                {'name': 'Diskon',  'modifier': 'subtract',
                    'amount_type': 'cash',    'amount': 2000},
            ],
        )
        self.assertEqual(
            result['grand_total'],
            result['items_subtotal'] + result['net_adjustment'],
        )
