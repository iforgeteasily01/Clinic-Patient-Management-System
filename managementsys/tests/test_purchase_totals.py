from decimal import Decimal

from django.test import TestCase


def _safe_decimal(val) -> Decimal:
    try:
        return Decimal(str(val))
    except Exception:
        return Decimal('0')


def compute_purchase_totals(items: list, additional_costs: list):
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


class PurchaseItemDiscountTests(TestCase):
    def test_no_discount_no_additional(self):
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
        result = compute_purchase_totals(
            items=[{'quantity': 5, 'unit_cost': 1000,
                    'total_discount': 9999999, 'line_type': 'stock'}],
            additional_costs=[],
        )
        item = result['parsed'][0]
        self.assertEqual(item['total_discount'], Decimal('5000'))
        self.assertEqual(item['base_unit_cost'], Decimal('0'))

    def test_cash_addition_distributed_per_unit(self):
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
        result = compute_purchase_totals(
            items=[{'quantity': 10, 'unit_cost': 10000,
                    'total_discount': 0, 'line_type': 'stock'}],
            additional_costs=[
                {'name': 'Diskon 5%', 'modifier': 'subtract', 'amount_type': 'percent', 'amount': 5}],
        )
        self.assertEqual(result['net_adjustment'], Decimal('-5000'))
        self.assertEqual(result['grand_total'], Decimal('95000'))

    def test_percent_applied_to_running_total(self):
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
        self.assertEqual(result['total_units'], Decimal('10'))
        self.assertEqual(result['per_unit_adj'], Decimal('100'))
        expense_item = result['parsed'][1]
        self.assertEqual(
            expense_item['actual_unit_cost'], expense_item['unit_cost'])

    def test_zero_quantity_item_no_division_error(self):
        result = compute_purchase_totals(
            items=[{'quantity': 0, 'unit_cost': 5000,
                    'total_discount': 0, 'line_type': 'stock'}],
            additional_costs=[],
        )
        item = result['parsed'][0]
        self.assertEqual(item['actual_unit_cost'], Decimal('5000'))

    def test_additional_cost_with_zero_amount_skipped(self):
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
