from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from managementsys.models import (
    InventoryItem, InventoryBatch, ProductionRecipe, ProductionRecipeIngredient,
    ProductionRun, ProductionRunIngredient, Warehouse, AuditLog,
)
from managementsys.api.serializers import (
    ProductionRecipeSerializer, ProductionRunSerializer,
)
from managementsys.views.inventory_page import (  # reuse, do not copy
    _to_small, _actor, _fifo_deduct, _fifo_deduct_global,
)


def unit_cost_small(item_id, warehouse_id=None) -> Decimal:
    """Weighted-average cost per smallest unit across remaining batches."""
    qs = InventoryBatch.objects.filter(item_id=item_id, quantity_remaining__gt=0)
    if warehouse_id:
        qs = qs.filter(warehouse_id=warehouse_id)
    total_qty = Decimal('0')
    total_val = Decimal('0')
    for b in qs:
        if b.quantity_initial:
            total_val += (b.value / b.quantity_initial) * b.quantity_remaining
        total_qty += b.quantity_remaining
    if total_qty <= 0:
        return Decimal('0')
    return total_val / total_qty


def available_small(item_id, warehouse_id=None) -> Decimal:
    qs = InventoryBatch.objects.filter(item_id=item_id, quantity_remaining__gt=0)
    if warehouse_id:
        qs = qs.filter(warehouse_id=warehouse_id)
    return qs.aggregate(t=Sum('quantity_remaining'))['t'] or Decimal('0')


def build_cost_breakdown(lines, output_quantity, warehouse_id=None):
    """
    lines: iterable of dicts {item_id, quantity, unit}
    output_quantity: Decimal (smallest unit yield)
    Returns (breakdown_list, total_cost, cost_per_output_unit).
    Each breakdown row: {item_id, item_code, item_name, unit, quantity,
                         quantity_small, unit_cost_small, line_cost,
                         available_small, insufficient}
    Raises ValueError on bad unit/qty (propagate as 400 upstream).
    """
    breakdown = []
    total = Decimal('0')
    for ln in lines:
        item = InventoryItem.objects.get(pk=ln['item_id'])
        qty = Decimal(str(ln['quantity']))
        unit = ln.get('unit', 'small')
        qty_small = _to_small(item, qty, unit)
        ucost = unit_cost_small(item.id, warehouse_id)
        line_cost = (ucost * qty_small).quantize(Decimal('0.01'))
        avail = available_small(item.id, warehouse_id)
        breakdown.append({
            'item_id': item.id,
            'item_code': item.code,
            'item_name': item.name,
            'unit': unit,
            'quantity': str(qty),
            'quantity_small': str(qty_small),
            'unit_cost_small': str(ucost.quantize(Decimal('0.0001'))),
            'line_cost': str(line_cost),
            'available_small': str(avail),
            'insufficient': avail < qty_small,
        })
        total += line_cost
    out_q = Decimal(str(output_quantity or '0'))
    per_unit = (total / out_q).quantize(Decimal('0.0001')) if out_q > 0 else Decimal('0')
    return breakdown, total.quantize(Decimal('0.01')), per_unit


# ── Recipe CRUD ──────────────────────────────────────────────────────────────

class RecipeListCreateView(APIView):
    def get(self, request):
        qs = ProductionRecipe.objects.prefetch_related('ingredients').all()
        if request.GET.get('active') == '1':
            qs = qs.filter(is_active=True)
        return Response(ProductionRecipeSerializer(qs, many=True).data)

    def post(self, request):
        ser = ProductionRecipeSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        recipe = ser.save(created_by=_actor(request))
        return Response(ProductionRecipeSerializer(recipe).data,
                        status=status.HTTP_201_CREATED)


class RecipeDetailView(APIView):
    def _get(self, pk):
        return get_object_or_404(
            ProductionRecipe.objects.prefetch_related('ingredients'), pk=pk)

    def get(self, request, pk):
        return Response(ProductionRecipeSerializer(self._get(pk)).data)

    def put(self, request, pk):
        ser = ProductionRecipeSerializer(self._get(pk), data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data)

    def delete(self, request, pk):
        self._get(pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class RecipeCostView(APIView):
    """GET live cost breakdown for a saved recipe. ?warehouse_id= optional."""
    def get(self, request, pk):
        recipe = get_object_or_404(
            ProductionRecipe.objects.prefetch_related('ingredients'), pk=pk)
        wh = request.GET.get('warehouse_id') or None
        lines = [{'item_id': i.item_id, 'quantity': i.quantity, 'unit': i.unit}
                 for i in recipe.ingredients.all()]
        try:
            breakdown, total, per_unit = build_cost_breakdown(
                lines, recipe.output_quantity, wh)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            'recipe_id': recipe.id,
            'output_quantity': str(recipe.output_quantity),
            'lines': breakdown,
            'total_cost': str(total),
            'cost_per_output_unit': str(per_unit),
        })


# ── Ad-hoc cost preview ──────────────────────────────────────────────────────

class ProductionPreviewView(APIView):
    """POST { lines:[{item_id, quantity, unit}], output_quantity, warehouse_id? }"""
    def post(self, request):
        d = request.data
        wh = d.get('warehouse_id') or None
        try:
            breakdown, total, per_unit = build_cost_breakdown(
                d.get('lines', []), d.get('output_quantity', 0), wh)
        except (ValueError, KeyError, InventoryItem.DoesNotExist) as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'lines': breakdown, 'total_cost': str(total),
                         'cost_per_output_unit': str(per_unit)})


# ── Production run execution ──────────────────────────────────────────────────

class ProductionRunListCreateView(APIView):
    def get(self, request):
        qs = ProductionRun.objects.prefetch_related('ingredients').all()[:200]
        return Response(ProductionRunSerializer(qs, many=True).data)

    def post(self, request):
        d = request.data
        # --- validate output ---
        try:
            output_item = InventoryItem.objects.get(pk=d['output_item'])
            warehouse = Warehouse.objects.get(pk=d['output_warehouse'])
            output_qty = Decimal(str(d['output_quantity']))
            lines = d['lines']  # [{item_id, quantity, unit, source_warehouse_id?}]
        except (KeyError, InventoryItem.DoesNotExist, Warehouse.DoesNotExist):
            return Response({'error': 'Invalid output item, warehouse, or payload.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if output_item.is_service:
            return Response({'error': 'Cannot produce a service item.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if output_qty <= 0 or not lines:
            return Response({'error': 'Output quantity and ingredients are required.'},
                            status=status.HTTP_400_BAD_REQUEST)

        # --- resolve qty_small + pre-check stock (deduct nothing yet) ---
        resolved = []
        shortages = []
        for ln in lines:
            try:
                item = InventoryItem.objects.get(pk=ln['item_id'])
            except (KeyError, InventoryItem.DoesNotExist):
                return Response({'error': 'Invalid ingredient item.'},
                                status=status.HTTP_400_BAD_REQUEST)
            if item.is_service:
                return Response({'error': f'{item.code} is a service item.'},
                                status=status.HTTP_400_BAD_REQUEST)
            src_wh = ln.get('source_warehouse_id') or None
            try:
                qty_small = _to_small(item, Decimal(str(ln['quantity'])),
                                      ln.get('unit', 'small'))
            except (ValueError, KeyError) as e:
                return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
            avail = available_small(item.id, src_wh)
            if avail < qty_small:
                shortages.append({'item_code': item.code,
                                  'needed': str(qty_small), 'available': str(avail)})
            resolved.append((item, qty_small, src_wh))
        if shortages:
            return Response({'error': 'Insufficient stock.', 'shortages': shortages},
                            status=status.HTTP_400_BAD_REQUEST)

        # --- commit atomically ---
        try:
            with transaction.atomic():
                total_cost = Decimal('0')
                consumed = []
                for item, qty_small, src_wh in resolved:
                    if src_wh:
                        shortfall, cogs = _fifo_deduct(item.id, int(src_wh), qty_small)
                    else:
                        shortfall, cogs = _fifo_deduct_global(item.id, qty_small)
                    if shortfall > 0:                      # race-condition guard
                        raise ValueError(f'Stock changed for {item.code}; aborted.')
                    total_cost += cogs
                    consumed.append((item, qty_small, cogs, src_wh))

                total_cost = total_cost.quantize(Decimal('0.01'))
                batch = InventoryBatch.objects.create(
                    item=output_item, warehouse=warehouse, input_date=date.today(),
                    quantity_initial=output_qty, quantity_remaining=output_qty,
                    value=total_cost, created_by=_actor(request))

                run = ProductionRun.objects.create(
                    recipe_id=d.get('recipe') or None, output_item=output_item,
                    output_warehouse=warehouse, output_quantity=output_qty,
                    total_cost=total_cost, produced_batch=batch,
                    notes=d.get('notes', ''), produced_by=_actor(request))
                for item, qty_small, cogs, src_wh in consumed:
                    ProductionRunIngredient.objects.create(
                        run=run, item=item, quantity_small=qty_small,
                        cost=cogs.quantize(Decimal('0.01')),
                        source_warehouse_id=int(src_wh) if src_wh else None)

                AuditLog.objects.create(
                    performed_by=_actor(request), action='CREATE',
                    entity_type='ProductionRun', entity_id=str(run.id),
                    description=(f'Produced {output_qty} {output_item.unit_small} of '
                                 f'[{output_item.code}] {output_item.name} '
                                 f'(cost {total_cost}) from {len(consumed)} ingredients'))
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_409_CONFLICT)

        return Response(ProductionRunSerializer(run).data,
                        status=status.HTTP_201_CREATED)


class ProductionRunDetailView(APIView):
    def get(self, request, pk):
        try:
            run = ProductionRun.objects.prefetch_related('ingredients').get(pk=pk)
        except ProductionRun.DoesNotExist:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(ProductionRunSerializer(run).data)
