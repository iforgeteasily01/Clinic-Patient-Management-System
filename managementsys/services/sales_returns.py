"""Sales returns: what is returnable, what a refund is worth, and how it posts.

Three responsibilities, kept in one module so the API, the journal preview and
the journal commit can never disagree about any of them:

  * ``returnable_lines`` — how much of each invoice line is still open, and why
    a line might not be returnable at all.
  * ``compute_refund`` — the money. The refund is **derived**, never typed: a
    line's own price and discount as sold, plus its share of the invoice-level
    discount, tax and additional charges.
  * ``build_sales_return_legs`` — the journal entry, as the mirror image of the
    invoice's own posting (``views.invoice_page.build_invoice_legs``).

Why a return is a separate document rather than an invoice edit
---------------------------------------------------------------
``PUT /api/invoices/<pk>/`` already exists and reverses-then-reposts the whole
sale. Using it for a return would rewrite what the books said happened on the
sale date, erase the fact that goods came back at all, and leave the CRM
believing the patient simply bought less. A return is a new event on the day it
happened, so it gets its own document, its own date, and its own journal entry.

Rounding
--------
The invoice-level components (discount, tax, additional charges) are
apportioned **once, against the returned subset as a whole** — not per line and
then summed. A per-line split of a Rp 100 discount across three lines leaves a
one-rupiah residue that would land in Sales Discount and make the entry look
like a pricing decision nobody made. Apportioning once has no residue to place.
Each component is quantized to 2dp, and the entry's Sales Discount plug closes
whatever the quantization moved.
"""
from decimal import Decimal, ROUND_HALF_UP

from ..models import (
    ChartOfAccounts, InvoiceItem, PatientPackageRedemption, SalesReturn,
    TreatmentPackage,
)
# The system account numbers come from the engine and are never re-declared
# here. A return that credited back a different account than the sale debited
# would balance perfectly and still be wrong, and a second copy of these numbers
# is exactly how that happens.
from .journal_engine import (
    ACC_ADDITIONAL_CHARGES, ACC_INVENTORY, ACC_PRODUCT_COGS, ACC_PRODUCT_REVENUE,
    ACC_SALES_DISCOUNT, ACC_TAX_PAYABLE,
)

CENTS = Decimal('0.01')
ZERO = Decimal('0')


class SalesReturnError(Exception):
    """A return that must not be written. Carries a DRF-shaped error dict."""

    def __init__(self, errors):
        self.errors = errors if isinstance(errors, dict) else {'error': errors}
        super().__init__(str(self.errors))


def _q(value):
    return (value or ZERO).quantize(CENTS, rounding=ROUND_HALF_UP)


# ── What is still returnable ──────────────────────────────────────────────────

def returned_quantities(invoice, *, exclude_return_id=None):
    """``{invoice_item_id: quantity already returned}`` for one invoice.

    Voided returns are excluded — a voided return gave nothing back, so its
    quantity is open again. ``exclude_return_id`` lets an edit of an existing
    return ignore its own rows when checking headroom.
    """
    from ..models import SalesReturnItem

    qs = SalesReturnItem.objects.filter(
        sales_return__invoice=invoice,
        sales_return__is_voided=False,
    )
    if exclude_return_id is not None:
        qs = qs.exclude(sales_return_id=exclude_return_id)

    out = {}
    for item_id, qty in qs.values_list('invoice_item_id', 'quantity'):
        out[item_id] = out.get(item_id, ZERO) + (qty or ZERO)
    return out


def _package_block_reason(line):
    """Why this line cannot be returned, or None.

    A treatment package that has already been redeemed is the one hard block.
    Reversing the sale would have to cancel sessions the patient has had, which
    is a clinical conversation and not a cashier's button. Mirrors the identical
    refusal in the invoice-edit path.
    """
    if line.item_id is None or not line.item.is_service:
        return None

    package = TreatmentPackage.objects.filter(catalog_item_id=line.item_id).first()
    if package is None:
        return None

    redeemed = PatientPackageRedemption.objects.filter(
        patient_package__package=package,
        patient_package__purchased_invoice=line.invoice,
    ).exists()
    if redeemed:
        return ('Paket ini sudah dipakai sebagian. Batalkan sesi yang sudah '
                'ditebus terlebih dahulu sebelum melakukan retur.')
    return None


def returnable_lines(invoice, *, exclude_return_id=None):
    """Every invoice line with how much of it is still open for return.

    Returns a list of dicts the API serves directly, so the picker in the UI is
    driven by the same numbers the write path validates against.
    """
    already = returned_quantities(invoice, exclude_return_id=exclude_return_id)
    rows = []
    for line in (InvoiceItem.objects
                 .filter(invoice=invoice)
                 .select_related('item', 'invoice')
                 .order_by('id')):
        sold = line.quantity or ZERO
        returned = already.get(line.id, ZERO)
        blocked = _package_block_reason(line)
        is_service = bool(line.item_id and line.item.is_service)
        rows.append({
            'invoice_item_id':   line.id,
            'item_id':           line.item_id,
            'item_name':         line.item.name if line.item_id else (line.item_name or 'Item'),
            'is_service':        is_service,
            'quantity_sold':     str(sold),
            'quantity_returned': str(returned),
            'quantity_open':     str(max(sold - returned, ZERO)),
            'price':             str(line.price),
            'discount_pct':      str(line.discount_pct),
            # A service cannot go back on a shelf, so restocking is not offered
            # for it — the refund still reverses the revenue.
            'restockable':       not is_service and line.item_id is not None,
            'blocked_reason':    blocked,
        })
    return rows


def validate_lines(invoice, requested, *, exclude_return_id=None):
    """Check requested ``[{invoice_item_id, quantity, restock?}]`` against reality.

    Returns the matching ``InvoiceItem`` objects keyed by id. Raises
    ``SalesReturnError`` rather than returning a flag, because every caller
    would otherwise have to remember to check one.
    """
    if not requested:
        raise SalesReturnError({'items': 'Pilih minimal satu baris untuk diretur.'})

    lines_by_id = {
        line.id: line
        for line in InvoiceItem.objects.filter(invoice=invoice).select_related('item')
    }
    already = returned_quantities(invoice, exclude_return_id=exclude_return_id)

    errors = []
    for row in requested:
        line = lines_by_id.get(row.get('invoice_item_id'))
        if line is None:
            errors.append(f"Baris {row.get('invoice_item_id')} bukan bagian dari faktur ini.")
            continue

        blocked = _package_block_reason(line)
        if blocked:
            errors.append(blocked)
            continue

        qty = Decimal(str(row.get('quantity') or 0))
        if qty <= 0:
            errors.append(f'Jumlah retur untuk {line.item_name or line.item} harus lebih dari nol.')
            continue

        open_qty = (line.quantity or ZERO) - already.get(line.id, ZERO)
        if qty > open_qty:
            errors.append(
                f'Jumlah retur {qty} melebihi sisa yang dapat diretur ({open_qty}) '
                f'untuk {line.item.name if line.item_id else line.item_name}.'
            )

    if errors:
        raise SalesReturnError({'items': errors})

    return lines_by_id


def validate_invoice(invoice):
    """The whole-document preconditions for accepting any return at all."""
    if invoice.is_voided:
        raise SalesReturnError(
            {'invoice': 'Faktur ini sudah dibatalkan — tidak ada yang bisa diretur.'}
        )


# ── The money ─────────────────────────────────────────────────────────────────

def compute_refund(invoice, return_lines):
    """What a set of returned lines is worth, and the parts that make it up.

    ``return_lines`` is a list of ``(invoice_item, quantity)``.

    The invoice's own arithmetic is ``net_of_lines - discount + tax +
    additional_charges = grand_total``, so a returned subset gets its
    proportional share of each invoice-level component. Proportion is taken on
    **net line value**, not gross: two lines of equal list price where one
    carried a 50% line discount did not contribute equally to what the patient
    paid, and refunding as if they had would overpay one of them.

    Returns a dict of Decimals, all quantized:
        gross, net, tax, additional_charges, invoice_discount, total_refund
    """
    invoice_net = ZERO
    for line in InvoiceItem.objects.filter(invoice=invoice):
        invoice_net += (line.price or ZERO) * (line.quantity or ZERO) * (
            Decimal('1') - (line.discount_pct or ZERO) / Decimal('100')
        )

    gross = ZERO
    net = ZERO
    for line, qty in return_lines:
        line_gross = (line.price or ZERO) * qty
        gross += line_gross
        net += line_gross * (Decimal('1') - (line.discount_pct or ZERO) / Decimal('100'))

    # A fully-discounted invoice (net zero) has nothing to apportion against.
    # Ratio 0 rather than a division by zero: there is no invoice-level
    # component to give back, and the line-level figures still stand.
    ratio = (net / invoice_net) if invoice_net else ZERO

    tax = _q((invoice.tax or ZERO) * ratio)
    charges = _q((invoice.additional_charges or ZERO) * ratio)
    inv_discount = _q((invoice.discount or ZERO) * ratio)

    total = _q(net) + tax + charges - inv_discount
    # A refund can never be negative: that would mean handing the clinic money
    # for taking goods back. It can only happen when invoice-level discount
    # exceeds the line's own value, which is a data problem, not a payout.
    total = max(total, ZERO)

    return {
        'gross':              _q(gross),
        'net':                _q(net),
        'tax':                tax,
        'additional_charges': charges,
        'invoice_discount':   inv_discount,
        'total_refund':       total,
    }


# ── The journal entry ─────────────────────────────────────────────────────────

def _sysacct(number):
    return ChartOfAccounts.objects.filter(account_number=number).first()


def build_sales_return_legs(sales_return, *, deduct=True, sim=None):
    """The LegSet for one return — the mirror image of the invoice posting.

        Dr  revenue per line       gross (price x qty)
        Dr  Tax Payable            apportioned tax
        Dr  Additional Charges     apportioned charges
            Cr  refund account     total_refund
            Cr  Sales Discount     the plug (discount being un-granted)

    plus, for every restocked physical line:

        Dr  Inventory asset        FIFO cost returned
            Cr  COGS               same

    ``deduct`` mirrors ``build_invoice_legs``: True actually puts the stock back
    (a real posting cannot know the cost without doing the restock), False runs
    the numbers for a preview and flags the FIFO-derived legs ``is_estimated``,
    because stock moves between a preview and its commit.

    Revenue routing goes through the *invoice's* own resolver, so a return
    credits back exactly the account the sale debited — including the
    per-category service accounts and the name-matched treatment fallback.
    """
    # Local imports: both modules import journal_engine, so importing either at
    # module scope would close a cycle at app load. Same pattern as
    # journal_preview.build_legs_for.
    from ..views.inventory_page import _fifo_restock
    from .journal_engine import LegSet, _line_revenue_account
    from ..models import Treatment

    rtn_no = sales_return.return_number
    legset = LegSet(memo=f'Retur {rtn_no}')

    items = list(sales_return.items.select_related(
        'item__item_category__revenue_account', 'invoice_item'))

    fallback_revenue = _sysacct(ACC_PRODUCT_REVENUE)

    # Same name-matching the invoice uses for lines with no item FK, so a
    # treatment billed by name returns to the account it was sold into. Targeted
    # lookup first, full scan only when a name did not match exactly — the scan
    # is what catches case and spacing variants, and the table is small enough
    # that it is cheaper than storing a normalised key.
    unlinked = [(it.item_name or '').strip().lower() for it in items if it.item_id is None]
    treatments_by_name = {}
    if unlinked:
        wanted = {n for n in unlinked if n}
        treatments_by_name = {
            t.name.strip().lower(): t
            for t in Treatment.objects.filter(name__in=[it.item_name for it in items
                                                        if it.item_id is None])
        }
        if len(treatments_by_name) < len(wanted):
            treatments_by_name = {t.name.strip().lower(): t for t in Treatment.objects.all()}

    for it in items:
        amount = _q(it.gross)
        if amount == ZERO:
            continue
        acct = _line_revenue_account(it.item, it.item_name, treatments_by_name, fallback_revenue)
        if acct:
            label = it.item.name if it.item_id else (it.item_name or 'Item')
            legset.add(acct, 'debit', amount, f'Retur {rtn_no} - {label}')

    parts = compute_refund(
        sales_return.invoice,
        [(it.invoice_item, it.quantity) for it in items],
    )

    if parts['tax']:
        acct = _sysacct(ACC_TAX_PAYABLE)
        if acct:
            legset.add(acct, 'debit', parts['tax'], f'Retur {rtn_no} - Pajak')

    if parts['additional_charges']:
        acct = _sysacct(ACC_ADDITIONAL_CHARGES)
        if acct:
            legset.add(acct, 'debit', parts['additional_charges'],
                       f'Retur {rtn_no} - Biaya tambahan')

    refund_acct = sales_return.refund_account or (
        sales_return.refund_method.linked_account if sales_return.refund_method_id else None
    )
    refund_total = _q(sales_return.total_refund) or parts['total_refund']
    if refund_acct and refund_total:
        legset.add(refund_acct, 'credit', refund_total, f'Retur {rtn_no} - Dana dikembalikan')
    elif refund_total:
        legset.warnings.append(
            'Rekening pengembalian dana belum diisi - kas tidak dijurnal.'
        )

    # The plug closes the entry with whatever discount is being un-granted, the
    # exact counterpart of the invoice's own Sales Discount leg.
    debited = sum(l.amount for l in legset.legs if l.entry_type == 'debit')
    credited = sum(l.amount for l in legset.legs if l.entry_type == 'credit')
    plug = debited - credited
    if plug:
        acct = _sysacct(ACC_SALES_DISCOUNT)
        if acct:
            legset.add(acct, 'credit' if plug > 0 else 'debit', abs(plug),
                       f'Retur {rtn_no} - Diskon penjualan dibatalkan')

    # ── Stock back on the shelf, and the COGS that comes with it ─────────────
    inventory_asset = _sysacct(ACC_INVENTORY)
    fallback_cogs = _sysacct(ACC_PRODUCT_COGS)
    warehouse_id = sales_return.warehouse_id or sales_return.invoice.warehouse_id

    for it in items:
        if not it.restock or it.item_id is None or it.item.is_service:
            continue
        if not warehouse_id or it.quantity <= 0:
            continue

        if deduct:
            cogs = _fifo_restock(it.item_id, warehouse_id, it.quantity)
            # Captured now because it cannot be recomputed later - the batches
            # move on. Same reasoning as StockOutLog.value.
            it.cogs_reversed = _q(cogs)
            it.save(update_fields=['cogs_reversed'])
        else:
            cogs = _estimate_restock_cost(it, warehouse_id, sim)

        cogs = _q(cogs)
        if cogs <= ZERO:
            continue
        if inventory_asset and fallback_cogs:
            legset.add(inventory_asset, 'debit', cogs,
                       f'Retur {rtn_no} - Stok masuk: {it.item.name}',
                       is_estimated=not deduct)
            legset.add(fallback_cogs, 'credit', cogs,
                       f'Retur {rtn_no} - HPP dibalik: {it.item.name}',
                       is_estimated=not deduct)
        else:
            legset.warnings.append(
                'Akun persediaan/HPP tidak lengkap - HPP retur tidak dijurnal.'
            )

    return legset


def _estimate_restock_cost(return_item, warehouse_id, sim=None):
    """What restocking this line would cost back, without touching a row.

    Preview only. Walks the same batches ``_restock_batches`` would refill —
    newest-first, capped at each batch's unused headroom — so the estimate uses
    the same rule as the commit and differs from it only where stock actually
    moved in between. That gap is what ``is_estimated`` warns about.

    ``sim`` is the preview-wide ``FifoSimulation`` the invoice builder uses. A
    return is recorded into it as **negative** consumption: putting units back
    is the opposite of taking them out, so a return previewed before a sale
    correctly leaves that stock available to the sale. Sharing the object is
    what stops two previewed documents from both claiming the same batch
    headroom. Without it (``sim=None``) each line is estimated against the live
    rows alone, which is right for a single document and only drifts across
    several.
    """
    from ..models import InventoryBatch

    remaining = Decimal(return_item.quantity)
    cost = ZERO
    batches = (InventoryBatch.objects
               .filter(item_id=return_item.item_id, warehouse_id=warehouse_id)
               .order_by('-input_date', '-created_at'))
    for batch in batches:
        if remaining <= 0:
            break
        initial = batch.quantity_initial or ZERO
        # ``sim.available`` already nets out what earlier previewed documents
        # took or gave back, so headroom is measured against that rather than
        # against the raw column.
        on_hand = sim.available(batch) if sim is not None else Decimal(batch.quantity_remaining)
        capacity = initial - on_hand
        if capacity <= 0:
            continue
        take = min(capacity, remaining)
        if initial:
            cost += (batch.value / initial) * take
        if sim is not None:
            sim.consume(batch, -take)
        remaining -= take
    return cost


def fingerprint(sales_return):
    """Everything the return's journal entry is derived from.

    Consumed by ``journal_preview.fingerprint_document`` so a staged draft is
    invalidated exactly when the posting would change.
    """
    lines = list(sales_return.items.values_list(
        'invoice_item_id', 'item_id', 'quantity', 'price', 'discount_pct', 'restock',
    ))
    return [
        'sales_return', sales_return.pk, sales_return.datetime,
        sales_return.total_refund, sales_return.refund_account_id,
        sales_return.refund_method_id, sales_return.warehouse_id,
        sales_return.is_voided, lines,
    ]


def default_restock_for(reason, *, is_service):
    """Whether the UI should pre-tick 'return to stock' for a line.

    A service never can. Damaged and expired goods normally cannot, but the
    operator can override — a bad carton can still hold sellable units.
    """
    if is_service:
        return False
    return reason not in SalesReturn.NON_RESTOCKABLE_REASONS
