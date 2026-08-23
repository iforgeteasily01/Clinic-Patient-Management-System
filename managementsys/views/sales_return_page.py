"""Sales return endpoints.

    GET  /api/invoices/<pk>/returnable/  what is still open on this invoice
    GET  /api/returns/                   list, filterable
    POST /api/returns/                   create
    GET  /api/returns/<pk>/              detail
    DELETE /api/returns/<pk>/            void

A return is written ``unposted`` and carries no ledger rows until a journal run
sweeps its date -- the same lifecycle as every other document since Phase 2. The
one thing that happens on save is *nothing physical*: stock goes back on the
shelf at posting time, inside ``services.sales_returns.build_sales_return_legs``,
because the FIFO cost the journal needs cannot be known without doing the
restock. That mirrors the invoice exactly, whose FIFO deduction also waits.

Voiding is the same story in reverse. A return whose date has not been posted
yet is simply flagged voided and drops out of the sweep. A return that has
already posted needs its stock taken back off the shelf and a memo entry dated
today -- see ``_void_posted_return``.
"""
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from ..api.serializers import SalesReturnDetailSerializer, SalesReturnListSerializer
from ..models import (
    AppUser, AuditLog, ChartOfAccounts, Invoice, PaymentMethod, SalesReturn,
    SalesReturnItem, Warehouse,
)
from ..services.branches import filter_by_branch, write_branch
from ..services.cash_accounts import cash_bank_account_ids
from ..services.journal_engine import is_date_posted, reverse_legset, write_legs
from ..services.sales_returns import (
    SalesReturnError, compute_refund, returnable_lines,
    validate_invoice, validate_lines,
)
from .crm_page import refresh_crm_profile


def _actor(request):
    return request.user if isinstance(request.user, AppUser) else None


def _decimal(value, default='0'):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal(default)


class InvoiceReturnableView(APIView):
    """What is still returnable on one invoice.

    The picker in the UI renders straight from this, so the quantities the
    operator sees are the same ones the POST validates against -- there is no
    second implementation of "how much is left" to drift.
    """

    def get(self, request, pk):
        invoice = Invoice.objects.filter(pk=pk).first()
        if invoice is None:
            return Response({'error': 'Faktur tidak ditemukan.'},
                            status=status.HTTP_404_NOT_FOUND)
        try:
            validate_invoice(invoice)
        except SalesReturnError as exc:
            return Response(exc.errors, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            'invoice_id':     invoice.pk,
            'invoice_number': invoice.invoice_number,
            'datetime':       invoice.datetime,
            'patient_name':   invoice.patient_no.name if invoice.patient_no_id else None,
            'patient_no':     invoice.patient_no_id,
            'grand_total':    str(invoice.grand_total),
            'warehouse_id':   invoice.warehouse_id,
            'branch_id':      invoice.branch_id,
            'lines':          returnable_lines(invoice),
        })


class SalesReturnPreviewView(APIView):
    """What a proposed return would refund, before anything is written.

    Exists so the operator sees the number before committing to it. Uses the
    same ``compute_refund`` the write path does, so the preview cannot promise
    an amount the POST would not honour.
    """

    def post(self, request):
        invoice = Invoice.objects.filter(pk=request.data.get('invoice')).first()
        if invoice is None:
            return Response({'invoice': 'Faktur tidak ditemukan.'},
                            status=status.HTTP_404_NOT_FOUND)
        try:
            validate_invoice(invoice)
            lines_by_id = validate_lines(invoice, request.data.get('items') or [])
        except SalesReturnError as exc:
            return Response(exc.errors, status=status.HTTP_400_BAD_REQUEST)

        pairs = [
            (lines_by_id[row['invoice_item_id']], _decimal(row.get('quantity')))
            for row in request.data['items']
        ]
        parts = compute_refund(invoice, pairs)
        return Response({k: str(v) for k, v in parts.items()})


class SalesReturnListCreateView(APIView):
    """
    GET  /api/returns/?invoice=&patient=&date_from=&date_to=&q=&include_voided=
    POST /api/returns/
         { invoice, datetime?, reason?, notes?, refund_method?, refund_account,
           warehouse?, items: [{invoice_item_id, quantity, restock?}] }
    """

    def get(self, request):
        qs = (SalesReturn.objects
              .select_related('invoice__patient_no', 'refund_method', 'refund_account',
                              'processed_by', 'branch')
              .prefetch_related('items__item'))
        qs = filter_by_branch(qs, request)

        if inv := request.query_params.get('invoice', '').strip():
            qs = qs.filter(invoice_id=inv)
        if patient := request.query_params.get('patient', '').strip():
            qs = qs.filter(invoice__patient_no_id=patient)
        if date_from := request.query_params.get('date_from', '').strip():
            qs = qs.filter(datetime__date__gte=date_from)
        if date_to := request.query_params.get('date_to', '').strip():
            qs = qs.filter(datetime__date__lte=date_to)
        if q := request.query_params.get('q', '').strip():
            from django.db.models import Q
            qs = qs.filter(
                Q(return_number__icontains=q) |
                Q(invoice__invoice_number__icontains=q) |
                Q(invoice__patient_no__name__icontains=q)
            )

        # Voided returns are hidden by default for the same reason voided
        # invoices are: they describe something that did not end up happening.
        if request.query_params.get('include_voided', '').lower() not in ('1', 'true', 'yes'):
            qs = qs.filter(is_voided=False)

        return Response(SalesReturnListSerializer(qs, many=True).data)

    def post(self, request):
        data = request.data

        invoice = (Invoice.objects
                   .select_related('warehouse', 'patient_no')
                   .filter(pk=data.get('invoice'))
                   .first())
        if invoice is None:
            return Response({'invoice': 'Faktur tidak ditemukan.'},
                            status=status.HTTP_404_NOT_FOUND)

        try:
            validate_invoice(invoice)
            lines_by_id = validate_lines(invoice, data.get('items') or [])
        except SalesReturnError as exc:
            return Response(exc.errors, status=status.HTTP_400_BAD_REQUEST)

        refund_method, refund_account, err = _resolve_refund_target(data)
        if err:
            return Response(err, status=status.HTTP_400_BAD_REQUEST)

        warehouse = None
        if wh_id := data.get('warehouse'):
            warehouse = Warehouse.objects.filter(pk=wh_id).first()
        warehouse = warehouse or invoice.warehouse

        with transaction.atomic():
            sales_return = SalesReturn.objects.create(
                invoice=invoice,
                datetime=data.get('datetime') or timezone.now(),
                # The return belongs where the sale did, not where the operator
                # is browsing from. A refund booked into a different branch than
                # the revenue it reverses would leave both branches wrong.
                branch=invoice.branch or write_branch(request, locked=True),
                reason=data.get('reason') or 'customer_change',
                notes=(data.get('notes') or '')[:500],
                refund_method=refund_method,
                refund_account=refund_account,
                warehouse=warehouse,
                processed_by=_actor(request),
            )

            rows = []
            for row in data['items']:
                line = lines_by_id[row['invoice_item_id']]
                is_service = bool(line.item_id and line.item.is_service)
                rows.append(SalesReturnItem(
                    sales_return=sales_return,
                    invoice_item=line,
                    item=line.item,
                    item_name=line.item.name if line.item_id else (line.item_name or ''),
                    quantity=_decimal(row['quantity']),
                    # Priced as sold, not as currently listed -- see the model.
                    price=line.price,
                    discount_pct=line.discount_pct,
                    # A service can never be restocked whatever the client says.
                    restock=bool(row.get('restock', True)) and not is_service and line.item_id is not None,
                ))
            SalesReturnItem.objects.bulk_create(rows)

            parts = compute_refund(
                invoice,
                [(r.invoice_item, r.quantity) for r in rows],
            )
            sales_return.total_refund = parts['total_refund']
            sales_return.save(update_fields=['total_refund'])

            AuditLog.objects.create(
                performed_by=_actor(request),
                action='CREATE',
                entity_type='SalesReturn',
                entity_id=str(sales_return.pk),
                description=(
                    f'Retur {sales_return.return_number} atas faktur '
                    f'{invoice.invoice_number}: {len(rows)} baris, '
                    f'Rp{sales_return.total_refund:,.2f}'
                ),
            )

            # Lifetime value nets out refunds, so the profile is stale the
            # moment a return is written — same reason the invoice paths
            # refresh it. Visit count is deliberately untouched; see
            # crm_page.refresh_crm_profile.
            if invoice.patient_no_id:
                refresh_crm_profile(invoice.patient_no)

        return Response(
            SalesReturnDetailSerializer(sales_return).data,
            status=status.HTTP_201_CREATED,
        )


class SalesReturnDetailView(APIView):
    """Detail and void. There is no edit.

    A return is a physical event -- goods handed over, money handed back. Editing
    one would mean the clinic is unsure what came through the door, and the
    reverse-and-repost machinery an edit needs is exactly where invoice editing
    is most dangerous. Void and re-enter instead: two honest documents beat one
    rewritten one.
    """

    def _get(self, pk):
        return (SalesReturn.objects
                .select_related('invoice__patient_no', 'refund_method', 'refund_account',
                                'warehouse', 'processed_by', 'branch')
                .prefetch_related('items__item')
                .filter(pk=pk)
                .first())

    def get(self, request, pk):
        obj = self._get(pk)
        if obj is None:
            return Response({'error': 'Retur tidak ditemukan.'},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(SalesReturnDetailSerializer(obj).data)

    def delete(self, request, pk):
        obj = self._get(pk)
        if obj is None:
            return Response({'error': 'Retur tidak ditemukan.'},
                            status=status.HTTP_404_NOT_FOUND)
        if obj.is_voided:
            return Response({'error': 'Retur ini sudah dibatalkan.'},
                            status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            if obj.posting_status == 'posted':
                _void_posted_return(obj, _actor(request))
            # An unposted return has no ledger rows and no restocked stock --
            # nothing physical happened yet, so flagging it voided is the whole
            # of the reversal. The sweep skips voided rows.

            obj.is_voided = True
            obj.voided_at = timezone.now()
            obj.voided_by = _actor(request)
            obj.save(update_fields=['is_voided', 'voided_at', 'voided_by'])

            AuditLog.objects.create(
                performed_by=_actor(request),
                action='DELETE',
                entity_type='SalesReturn',
                entity_id=str(obj.pk),
                description=f'Retur {obj.return_number} dibatalkan',
            )

            if obj.invoice.patient_no_id:
                refresh_crm_profile(obj.invoice.patient_no)

        return Response(SalesReturnDetailSerializer(self._get(pk)).data)


def _void_posted_return(sales_return, actor):
    """Undo a return that has already reached the ledger.

    Two things have to come back out: the journal entry, and the stock. The
    entry is reversed as a memo dated **today** rather than by deleting rows,
    because the original posting is history -- the same rule the invoice
    void/edit memo path follows. The stock is deducted again, because a voided
    return means those units were never actually returned to the shelf.

    ``reverse_legset`` is used rather than rebuilding the legs: rebuilding would
    re-run FIFO against today's batches and could reverse an amount that was
    never posted.
    """
    from .inventory_page import _fifo_deduct

    entry = sales_return.journal_entries.order_by('id').first()
    if entry is not None:
        from ..services.journal_engine import legset_from_entry

        memo_date = timezone.now().date() if is_date_posted(entry.date) else entry.date
        write_legs(
            reverse_legset(legset_from_entry(entry)),
            date=memo_date,
            source_type='void_memo',
            document=sales_return,
            actor=actor,
            memo=f'Pembatalan retur {sales_return.return_number}',
            reverses=entry,
        )

    warehouse_id = sales_return.warehouse_id or sales_return.invoice.warehouse_id
    if warehouse_id:
        for item in sales_return.items.select_related('item'):
            if not item.restock or item.item_id is None or item.item.is_service:
                continue
            if item.quantity > 0:
                _fifo_deduct(item.item_id, warehouse_id, item.quantity, commit=True)


def _resolve_refund_target(data):
    """(payment_method, cash_account, error) for where the refund goes out.

    Accepts either a ``PaymentMethod`` (resolved to its linked account, the way
    the POS sends payment) or a direct ``ChartOfAccounts``. The account is
    checked against the curated cash/bank set for the same reason the expense
    form checks it: refunding out of Accounts Receivable would post a journal
    nobody can explain.
    """
    method = None
    if method_id := data.get('refund_method'):
        method = PaymentMethod.objects.filter(pk=method_id).first()
        if method is None:
            return None, None, {'refund_method': 'Metode pembayaran tidak ditemukan.'}

    account = None
    account_id = data.get('refund_account') or (method.linked_account_id if method else None)
    if account_id:
        if account_id not in cash_bank_account_ids():
            return None, None, {'refund_account': 'Bukan rekening kas/bank.'}
        account = ChartOfAccounts.objects.filter(pk=account_id).first()

    if account is None:
        return None, None, {'refund_account': 'Rekening pengembalian dana wajib diisi.'}

    return method, account, None
