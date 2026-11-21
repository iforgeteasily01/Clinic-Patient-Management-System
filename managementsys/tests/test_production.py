from datetime import date
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from managementsys.models import (
    AppUser, InventoryItem, InventoryBatch, Warehouse, ProductionRun,
)
from managementsys.views.production_page import build_cost_breakdown


def _mk_item(code, name, unit_small, is_service=False):
    return InventoryItem.objects.create(
        code=code, name=name, selling_price=Decimal('0'),
        unit_small=unit_small, is_service=is_service,
    )


def _mk_batch(item, warehouse, qty_initial, value, qty_remaining=None):
    qi = Decimal(str(qty_initial))
    return InventoryBatch.objects.create(
        item=item, warehouse=warehouse, input_date=date.today(),
        quantity_initial=qi,
        quantity_remaining=Decimal(str(qty_remaining)) if qty_remaining is not None else qi,
        value=Decimal(str(value)),
    )


class ProductionTestBase(TestCase):
    def setUp(self):
        self.user = AppUser.objects.create(display_name='Tester', role='superuser')
        self.user.set_pin('123456')
        self.user.save()
        self.token = self.user.generate_token()

        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token}')

        self.W = Warehouse.objects.create(code='W1', name='Main')

        self.A = _mk_item('A', 'Item A', 'ml')
        self.batchA = _mk_batch(self.A, self.W, 1000, 100000)

        self.B = _mk_item('B', 'Item B', 'g')
        self.batchB1 = _mk_batch(self.B, self.W, 100, 5000)
        self.batchB2 = _mk_batch(self.B, self.W, 100, 8000)

        self.X = _mk_item('X', 'Compound X', 'ml')


class PreviewMathTests(ProductionTestBase):
    def test_preview_weighted_average_math(self):
        lines = [
            {'item_id': self.A.id, 'quantity': 200, 'unit': 'small'},
            {'item_id': self.B.id, 'quantity': 50,  'unit': 'small'},
        ]
        breakdown, total, per_unit = build_cost_breakdown(lines, Decimal('250'))

        self.assertEqual(Decimal(breakdown[0]['line_cost']), Decimal('20000.00'))
        self.assertEqual(Decimal(breakdown[1]['unit_cost_small']), Decimal('65.0000'))
        self.assertEqual(Decimal(breakdown[1]['line_cost']), Decimal('3250.00'))
        self.assertEqual(total, Decimal('23250.00'))
        self.assertEqual(per_unit, Decimal('93.0000'))


class ProductionRunTests(ProductionTestBase):
    def test_run_deducts_fifo_and_cost_matches(self):
        payload = {
            'output_item': self.X.id,
            'output_warehouse': self.W.id,
            'output_quantity': 250,
            'lines': [
                {'item_id': self.A.id, 'quantity': 200, 'unit': 'small'},
                {'item_id': self.B.id, 'quantity': 120, 'unit': 'small'},
            ],
        }
        resp = self.client.post('/api/inventory/production/runs/', payload, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)

        self.batchA.refresh_from_db()
        self.batchB1.refresh_from_db()
        self.batchB2.refresh_from_db()
        self.assertEqual(self.batchA.quantity_remaining, Decimal('800.0000'))
        self.assertEqual(self.batchB1.quantity_remaining, Decimal('0.0000'))
        self.assertEqual(self.batchB2.quantity_remaining, Decimal('80.0000'))

        run = ProductionRun.objects.get(pk=resp.data['id'])
        self.assertEqual(run.total_cost, Decimal('26600.00'))

        out_batches = InventoryBatch.objects.filter(item=self.X)
        self.assertEqual(out_batches.count(), 1)
        out = out_batches.first()
        self.assertEqual(out.quantity_initial, Decimal('250.0000'))
        self.assertEqual(out.quantity_remaining, Decimal('250.0000'))
        self.assertEqual(out.value, Decimal('26600.00'))

        self.assertEqual(run.total_cost, run.produced_batch.value)

        line_costs = sum((li.cost for li in run.ingredients.all()), Decimal('0'))
        self.assertEqual(line_costs, run.total_cost)

    def test_insufficient_stock_leaves_stock_untouched(self):
        payload = {
            'output_item': self.X.id,
            'output_warehouse': self.W.id,
            'output_quantity': 250,
            'lines': [
                {'item_id': self.A.id, 'quantity': 5000, 'unit': 'small'},
            ],
        }
        resp = self.client.post('/api/inventory/production/runs/', payload, format='json')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn('shortages', resp.data)

        self.batchA.refresh_from_db()
        self.assertEqual(self.batchA.quantity_remaining, Decimal('1000.0000'))
        self.assertEqual(ProductionRun.objects.count(), 0)
        self.assertEqual(InventoryBatch.objects.filter(item=self.X).count(), 0)

    def test_service_item_rejected_as_output(self):
        svc = _mk_item('SVC', 'A Service', 'ea', is_service=True)
        payload = {
            'output_item': svc.id,
            'output_warehouse': self.W.id,
            'output_quantity': 1,
            'lines': [{'item_id': self.A.id, 'quantity': 10, 'unit': 'small'}],
        }
        resp = self.client.post('/api/inventory/production/runs/', payload, format='json')
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_service_item_rejected_as_ingredient(self):
        svc = _mk_item('SVC2', 'A Service', 'ea', is_service=True)
        payload = {
            'output_item': self.X.id,
            'output_warehouse': self.W.id,
            'output_quantity': 250,
            'lines': [{'item_id': svc.id, 'quantity': 1, 'unit': 'small'}],
        }
        resp = self.client.post('/api/inventory/production/runs/', payload, format='json')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(ProductionRun.objects.count(), 0)
