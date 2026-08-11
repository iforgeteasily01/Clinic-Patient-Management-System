"""
managementsys/services/journal_engine.py

Phase 2 posting engine.

Houses the low-level ledger-writing functions that used to live directly in
``views/invoice_page.py`` and ``views/accounting_page.py`` and ran
synchronously on every Invoice/PurchaseInvoice create or edit. Moved here
verbatim (same math, same account resolution — including the Phase-1
``payment_method.linked_account`` lookup) so the views can call the same
logic either from the on-demand journal run or from the same-day memo path,
instead of duplicating it.

What changed is *when* these run, not what they compute:

  * A normal create/edit of an Invoice/PurchaseInvoice/AccountTransfer no
    longer calls any of this. The document is saved with
    ``posting_status='unposted'`` and sits with zero LedgerEntry rows until a
    journal run (``POST /api/accounting/journal/run/``) sweeps its
    transaction date.
  * Voiding or editing a document whose transaction date has *already* been
    posted (``JournalDayLog.is_posted=True`` — see ``is_date_posted``) is the
    one case that still posts synchronously: a same-day "memo" entry dated
    today, tagged ``source_type='void_memo'`` / ``'edit_memo'``, referencing
    the original document. These memo entries are excluded from the journal
    run's sweep query (they already carry a real date <= today and are
    already-posted by construction) and never touch the original
    transaction date's ``JournalDayLog``.
"""
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.db.models import F

from ..models import (
    ChartOfAccounts, JournalDayLog, JournalEntry, JournalEntrySequence,
    LedgerEntry, Supplier, Treatment, TreatmentCategory,
)


# ── Leg model (Phase 4) ───────────────────────────────────────────────────────
# Everything below used to compute and write in the same breath. Phase 4 splits
# that: ``build_*_legs`` computes a LegSet and touches nothing, ``write_legs``
# turns a LegSet into a JournalEntry plus its LedgerEntry rows. The journal
# preview persists LegSets to staging tables and shows them to the operator; the
# commit writes them. Both sides call the same builders, so the preview cannot
# disagree with the posting except where it says it does (FIFO — see
# ``Leg.is_estimated``).

@dataclass
class Leg:
    """One line of a journal entry, before it exists in the database."""
    account: object                     # ChartOfAccounts | None
    entry_type: str                     # 'debit' | 'credit'
    amount: Decimal
    description: str
    # Set when the account does not exist yet (a supplier's AP sub-account is
    # created lazily at posting time). Preview must not create COA rows, so it
    # carries the label it would create instead and renders it as pending.
    pending_account_label: str = ''
    # True for FIFO-derived lines. Stock can move between preview and commit, so
    # these amounts are recomputed at commit and are shown as estimates.
    is_estimated: bool = False


@dataclass
class LegSet:
    """A complete, self-balancing journal entry that has not been posted yet."""
    legs: list = field(default_factory=list)
    memo: str = ''
    warnings: list = field(default_factory=list)

    def add(self, account, entry_type, amount, description, *,
            pending_account_label='', is_estimated=False):
        self.legs.append(Leg(account, entry_type, Decimal(amount), description,
                             pending_account_label, is_estimated))

    @property
    def total_debit(self):
        return sum((l.amount for l in self.legs if l.entry_type == 'debit'), Decimal('0'))

    @property
    def total_credit(self):
        return sum((l.amount for l in self.legs if l.entry_type == 'credit'), Decimal('0'))

    @property
    def is_balanced(self):
        return self.total_debit == self.total_credit


class UnbalancedJournalError(Exception):
    """Raised by ``write_legs`` rather than posting a lopsided entry.

    A single unbalanced entry silently corrupts every downstream report, and
    the per-day transaction boundary means failing loudly here rolls back only
    that day. Never downgrade this to a warning.
    """


# ── Entry numbering ───────────────────────────────────────────────────────────

def reserve_entry_numbers(year: int, count: int) -> list:
    """Reserve ``count`` consecutive journal entry numbers for ``year``.

    One row lock per call, not per entry — a commit posting two thousand entries
    would otherwise take two thousand locks. Numbers are gapless: the caller
    must use every number it reserves, so commit reserves exactly the number of
    entries it is about to write.
    """
    if count <= 0:
        return []
    with transaction.atomic():
        JournalEntrySequence.objects.get_or_create(year=year, defaults={'last_number': 0})
        seq = JournalEntrySequence.objects.select_for_update().get(year=year)
        start = seq.last_number + 1
        seq.last_number += count
        seq.save(update_fields=['last_number'])
    return [f'JE-{year}-{n:06d}' for n in range(start, start + count)]


# ── Writing a LegSet to the ledger ────────────────────────────────────────────

# Maps a source document instance to the LedgerEntry/JournalEntry FK it fills.
DOCUMENT_FIELDS = {
    'Invoice':         'invoice',
    'PurchaseInvoice': 'purchase_invoice',
    'AccountTransfer': 'transfer',
    'Expense':         'expense',
}


def document_field(document):
    """The FK name for ``document``, or None for entries with no source doc
    (manual adjustments, corrections composed by hand)."""
    if document is None:
        return None
    return DOCUMENT_FIELDS.get(type(document).__name__)


def write_legs(legset, *, date, source_type, document=None, batch=None, actor=None,
               memo=None, entry_number=None, reverses=None, corrects=None):
    """Post a LegSet: create the JournalEntry header, its LedgerEntry lines, and
    roll every affected account balance. Returns the JournalEntry.

    This is the single write path for the ledger from Phase 4 on — the journal
    commit, the void/edit memo path and correction journals all funnel through
    it, which is what guarantees every ledger line has a header to belong to.

    Callers must already be inside a transaction; this function does not open
    one, because the journal commit deliberately owns the per-day boundary.
    """
    if not legset.legs:
        return None
    if not legset.is_balanced:
        raise UnbalancedJournalError(
            f'Jurnal tidak seimbang: debit {legset.total_debit} vs kredit {legset.total_credit} '
            f'({source_type} {date})'
        )

    number = entry_number or reserve_entry_numbers(date.year, 1)[0]
    doc_field = document_field(document)

    entry = JournalEntry.objects.create(
        entry_number=number,
        date=date,
        memo=(memo if memo is not None else legset.memo)[:255],
        source_type=source_type,
        batch=batch,
        created_by=actor,
        total_debit=legset.total_debit,
        total_credit=legset.total_credit,
        is_balanced=True,
        reverses=reverses,
        corrects=corrects,
        **({doc_field: document} if doc_field else {}),
    )

    rows = []
    for leg in legset.legs:
        if leg.account is None:
            # Should never reach here: the commit path resolves pending accounts
            # for real before writing. Guard rather than write a null FK.
            raise UnbalancedJournalError(
                f'Akun belum tersedia untuk baris "{leg.description}" — '
                'jalankan ulang pratinjau.'
            )
        _apply_purchase_balance(leg.account.pk, leg.account.account_type,
                                leg.entry_type, leg.amount)
        rows.append(LedgerEntry(
            account=leg.account,
            date=date,
            description=leg.description[:255],
            entry_type=leg.entry_type,
            amount=leg.amount,
            source_type=source_type,
            journal_entry=entry,
            **({doc_field: document} if doc_field else {}),
        ))
    LedgerEntry.objects.bulk_create(rows, batch_size=500)
    return entry


def reverse_legset(legset):
    """A LegSet that exactly negates ``legset`` — every side flipped, amounts and
    descriptions preserved. Used by correction journals."""
    out = LegSet(memo=legset.memo, warnings=list(legset.warnings))
    flip = {'debit': 'credit', 'credit': 'debit'}
    for leg in legset.legs:
        out.legs.append(Leg(leg.account, flip[leg.entry_type], leg.amount,
                            leg.description, leg.pending_account_label, leg.is_estimated))
    return out


def legset_from_entry(entry):
    """Rebuild a LegSet from a posted JournalEntry's stored lines."""
    out = LegSet(memo=entry.memo)
    for line in entry.lines.select_related('account').all():
        out.legs.append(Leg(line.account, line.entry_type, line.amount, line.description))
    return out


# ── Shared helpers ────────────────────────────────────────────────────────────

def is_date_posted(date) -> bool:
    """True when ``date`` already has a completed journal sweep
    (``JournalDayLog`` row with ``is_posted=True``). A date with no row at all
    has never been swept and counts as unposted."""
    return JournalDayLog.objects.filter(date=date, is_posted=True).exists()


def _sysacct(number):
    return ChartOfAccounts.objects.filter(account_number=number).first()


# ── Invoice posting (moved from views/invoice_page.py) ────────────────────────

# Account types whose natural (positive) balance sits on the debit side.
DEBIT_NORMAL_TYPES = {'asset', 'cogs', 'expense', 'other_expense'}

# System accounts the invoice posting routes to. Numbers are fixed by
# migration 0075; see _revenue_legs for how each is used.
ACC_SALES_DISCOUNT     = 4100000   # contra-revenue (debit-normal within revenue)
ACC_PRODUCT_REVENUE    = 4200000
ACC_TAX_PAYABLE        = 2200000
ACC_ADDITIONAL_CHARGES = 7100000
ACC_INVENTORY          = 1300000
ACC_PRODUCT_COGS       = 5100000
ACC_CASH               = 1100001
# Created by migration 0076 as "Undeposited Funds", retired and renamed to
# "Kas Penjualan iPos (histori)" by 0100 — in practice it only ever held the cash
# side of the imported iPos history. Live documents can no longer reach it: the
# billing queue requires a payment method and the POS always sends one. It stays
# here as the last-resort leg so re-posting a legacy method-less invoice still
# balances instead of silently asserting Cash.
ACC_LEGACY_CLEARING    = 1100011
ACC_OPENING_EQUITY     = 3900000


def _apply_balance(account, entry_type, amount):
    """Move ``account.balance`` by ``amount`` in the direction ``entry_type`` implies.

    A debit raises a debit-normal account and lowers a credit-normal one, so the
    stored balance always matches what the ledger rows derive to.
    """
    is_debit = (entry_type == 'debit')
    natural = account.account_type in DEBIT_NORMAL_TYPES
    delta = amount if is_debit == natural else -amount
    ChartOfAccounts.objects.filter(pk=account.pk).update(balance=F('balance') + delta)


def _le(account, entry_type, amount, invoice, description, date=None, source_type='invoice'):
    """Create a single LedgerEntry row.

    ``date``/``source_type`` default to the pre-Phase-2 behaviour (the
    invoice's own transaction date, ``source_type='invoice'``) so every call
    site that predates the journal-run/memo split is unaffected. The journal
    run and the void/edit memo paths pass these explicitly — the run reuses
    the document's own date; a memo passes ``date=today`` and
    ``source_type='void_memo'``/``'edit_memo'``.
    """
    LedgerEntry.objects.create(
        account=account,
        date=date if date is not None else invoice.datetime.date(),
        description=description,
        entry_type=entry_type,
        amount=amount,
        invoice=invoice,
        source_type=source_type,
    )


def _line_revenue_account(item, item_name, treatments_by_name, fallback_revenue):
    """Resolve the revenue account for one invoice line.

    Lines carrying an ``InventoryItem`` route by that item's category when it is a
    service; physical goods are one shared entity and always use the product
    account. Lines with no item FK (treatments billed by name only) are matched
    back to a ``Treatment`` by name so they reach the same per-category account
    the linked path would have used — without this they earn no revenue leg at all.
    """
    if item is not None:
        cat = item.item_category if item.is_service else None
        return (cat.revenue_account if cat and cat.revenue_account_id else fallback_revenue)

    treatment = treatments_by_name.get((item_name or '').strip().lower())
    if treatment is not None:
        cat = TreatmentCategory.objects.filter(
            name__iexact=(treatment.category or '').strip()
        ).first() if treatment.category else None
        if cat and cat.revenue_account_id:
            return cat.revenue_account
    return fallback_revenue


def _revenue_legs(invoice, lines):
    """Build the complete, self-balancing cash/revenue block for an invoice.

    ``lines`` is a list of ``(item_or_None, item_name, quantity, price)``.

    Returns ``[(account, entry_type, amount, description), ...]`` where total
    debits equal total credits by construction:

        Dr  payment account        grand_total
        Dr  Sales Discount         (balancing plug, when positive)
            Cr  revenue per line   gross price x quantity
            Cr  Tax Payable        invoice.tax
            Cr  Additional Charges invoice.additional_charges

    Revenue is credited gross and every reduction — line ``discount_pct``,
    ``invoice.discount``, and promotion or package redemptions that were never
    written back to ``invoice.discount`` — lands in the single Sales Discount
    contra account. That keeps gross revenue reportable and makes the entry
    balance regardless of where the reduction came from.
    """
    inv_no = invoice.invoice_number
    fallback_revenue = _sysacct(ACC_PRODUCT_REVENUE)

    unlinked = [(n or '').strip().lower() for it, n, _q, _p in lines if it is None]
    treatments_by_name = {}
    if unlinked:
        treatments_by_name = {
            t.name.strip().lower(): t
            for t in Treatment.objects.filter(name__in=[n for n in unlinked if n])
        }
        if len(treatments_by_name) < len(set(filter(None, unlinked))):
            # Fall back to a full scan so case/spacing variants still match.
            treatments_by_name = {t.name.strip().lower(): t for t in Treatment.objects.all()}

    legs = []
    gross = Decimal('0')

    for item, item_name, quantity, price in lines:
        amount = price * quantity
        if amount == 0:
            continue
        gross += amount
        acct = _line_revenue_account(item, item_name, treatments_by_name, fallback_revenue)
        if acct:
            label = item.name if item is not None else (item_name or 'Item')
            legs.append((acct, 'credit', amount, f'Invoice {inv_no} – {label}'))

    if invoice.tax:
        acct = _sysacct(ACC_TAX_PAYABLE)
        if acct:
            legs.append((acct, 'credit', invoice.tax, f'Invoice {inv_no} – Pajak'))

    if invoice.additional_charges:
        acct = _sysacct(ACC_ADDITIONAL_CHARGES)
        if acct:
            legs.append((acct, 'credit', invoice.additional_charges,
                         f'Invoice {inv_no} – Biaya tambahan'))

    # ``invoice.payment_account`` (design doc §3) is the direct cash/bank COA
    # reference and wins whenever it is set. ``invoice.payment_method`` is the
    # older PaymentMethod indirection — the GL account to debit is whatever
    # cash/bank account it resolves to via ``linked_account`` — kept as the
    # fallback for rows written before payment_account existed and for callers
    # (Medya-Cashier POS) that still only send payment_method_id. Only legacy
    # rows with neither reach the ACC_LEGACY_CLEARING fallback; it keeps the
    # entry balanced without asserting a payment method nobody recorded.
    payment_acct = (invoice.payment_account
                    or (invoice.payment_method.linked_account if invoice.payment_method_id else None)
                    or _sysacct(ACC_LEGACY_CLEARING)
                    or _sysacct(ACC_CASH))

    # A split payment carries one InvoicePayment row per tender, and each one
    # debits its own cash/bank account. Collapsing them onto the first method's
    # account (which is what the invoice-level payment_account describes) would
    # overstate that account and leave the others at nil. Any rounding remainder
    # between the rows and grand_total still lands on the invoice-level account
    # so the entry stays balanced without silently inflating Sales Discount.
    splits = list(invoice.payments.select_related(
        'payment_account', 'payment_method__linked_account'))
    if splits and invoice.grand_total:
        allocated = Decimal('0')
        for sp in splits:
            if not sp.amount:
                continue
            acct = (sp.payment_account
                    or (sp.payment_method.linked_account if sp.payment_method_id else None)
                    or payment_acct)
            if not acct:
                continue
            label = sp.payment_method.name if sp.payment_method_id else acct.name
            legs.append((acct, 'debit', sp.amount,
                         f'Invoice {inv_no} – Payment received ({label})'))
            allocated += sp.amount
        remainder = invoice.grand_total - allocated
        if remainder and payment_acct:
            legs.append((payment_acct, 'debit' if remainder > 0 else 'credit', abs(remainder),
                         f'Invoice {inv_no} – Payment received'))
    elif payment_acct and invoice.grand_total:
        legs.append((payment_acct, 'debit', invoice.grand_total,
                     f'Invoice {inv_no} – Payment received'))

    # Whatever is left over is the total discount granted on this invoice.
    credited = sum(a for _ac, side, a, _d in legs if side == 'credit')
    debited = sum(a for _ac, side, a, _d in legs if side == 'debit')
    plug = credited - debited
    if plug:
        acct = _sysacct(ACC_SALES_DISCOUNT)
        if acct:
            side = 'debit' if plug > 0 else 'credit'
            legs.append((acct, side, abs(plug), f'Invoice {inv_no} – Diskon penjualan'))

    return legs


def _post_legs(invoice, legs, reverse=False, date=None, source_type='invoice'):
    """Write ``legs`` to the ledger and roll account balances, optionally flipped.

    ``reverse=True`` posts the exact opposite of every leg, which is what makes
    an edit or a void undo a posting completely. ``date``/``source_type`` let
    the journal run and the void/edit memo path stamp these rows appropriately
    (see ``_le``).
    """
    flip = {'debit': 'credit', 'credit': 'debit'}
    for account, entry_type, amount, description in legs:
        side = flip[entry_type] if reverse else entry_type
        _apply_balance(account, side, amount)
        _le(account, side, amount, invoice,
            f'{description} (koreksi)' if reverse else description,
            date=date, source_type=source_type)


# ── Purchase invoice posting (moved from views/accounting_page.py) ────────────
# The financial reports recompute every balance from LedgerEntry using natural
# (normal-balance) sign — asset/cogs/expense are debit-normal, liability/equity/
# revenue are credit-normal. The cached ChartOfAccounts.balance is kept in the
# same natural sign, matching the invoice posting above.
NORMAL_BALANCE = {
    'asset':         'debit',
    'cogs':          'debit',
    'expense':       'debit',
    'other_expense': 'debit',
    'liability':     'credit',
    'equity':        'credit',
    'revenue':       'credit',
    'other_income':  'credit',
}

INVENTORY_ASSET_NUMBER = 1300000
CENT = Decimal('0.01')

PRICE_VARIANCE_NUMBER = 5200000
COGS_HEAD_NUMBER = 5000000


def _apply_purchase_balance(account_id, account_type, entry_type, amount):
    """Nudge the cached account balance in its natural direction. Distinct name
    from the invoice-side ``_apply_balance`` since the signature differs
    (id + type instead of an account instance) — both are kept so this module
    is a drop-in replacement for both original call sites."""
    normal = NORMAL_BALANCE.get(account_type, 'debit')
    signed = amount if entry_type == normal else -amount
    ChartOfAccounts.objects.filter(pk=account_id).update(balance=F('balance') + signed)


def _post_le(account, entry_type, amount, invoice, description, date, source_type='purchase'):
    """Post one purchase LedgerEntry row and update the account balance cache.

    ``source_type`` defaults to ``'purchase'`` (pre-Phase-2 behaviour); the
    edit/void memo path passes ``'edit_memo'``/``'void_memo'`` explicitly.
    """
    _apply_purchase_balance(account.pk, account.account_type, entry_type, amount)
    LedgerEntry.objects.create(
        account=account,
        date=date,
        description=description,
        entry_type=entry_type,
        amount=amount,
        source_type=source_type,
        purchase_invoice=invoice,
    )


def resolve_ap_account(supplier, *, create=True):
    """The supplier's AP sub-account.

    ``Supplier.ensure_ap_account()`` **creates** the COA row when it is missing,
    which a journal preview must never do — a preview that provisions accounts
    has already changed the books before the operator approved anything. Pass
    ``create=False`` to get ``(None, label)`` instead, so the preview can show
    the account it *would* create as pending.
    """
    if create:
        return supplier.ensure_ap_account(), ''
    if supplier.ap_account_id:
        return supplier.ap_account, ''
    return None, f'Utang Usaha — {supplier.name}'


def build_purchase_legs(invoice, parsed, item_objs, supplier, grand_total, *, create_accounts=True):
    """The accrual double-entry for a purchase invoice, computed and not written:

        Dr Inventory (stock lines) / Dr expense_account (beban lines)
            Cr Accounts Payable — <vendor>   for the invoice total.

    Debits are reconciled so their sum equals the (2-dp) invoice total, which
    absorbs additional-cost rounding and any adjustment that no stock unit
    carried (e.g. an all-expense invoice with a shipping surcharge).

    Pure: no rows written, no balances moved. ``create_accounts=False`` also
    stops it provisioning the vendor's AP account (see ``resolve_ap_account``).
    """
    legset = LegSet(memo=f'Pembelian {invoice.internal_id} — {supplier.name}')
    target = grand_total.quantize(CENT)
    ap_account, ap_pending = resolve_ap_account(supplier, create=create_accounts)
    inventory_asset = ChartOfAccounts.objects.filter(account_number=INVENTORY_ASSET_NUMBER).first()

    exp_ids = {p['expense_acct_id'] for p in parsed
               if p['line_type'] == 'expense' and p['expense_acct_id']}
    exp_map = {a.id: a for a in ChartOfAccounts.objects.filter(id__in=exp_ids)}

    debit_posts = []   # [account, amount, description]
    debit_total = Decimal('0')
    for p, obj in zip(parsed, item_objs):
        amt = (p['quantity'] * obj.actual_unit_cost).quantize(CENT)
        if amt <= 0:
            continue
        if p['line_type'] == 'stock':
            if not inventory_asset:
                legset.warnings.append('Akun persediaan (1300000) tidak ditemukan.')
                continue
            debit_posts.append([inventory_asset, amt,
                                f'Persediaan masuk: {p["item_name"]} — {invoice.internal_id}'])
        else:
            acct = exp_map.get(p['expense_acct_id'])
            if not acct:
                legset.warnings.append(f'Baris "{p["item_name"]}" tidak punya akun beban.')
                continue
            debit_posts.append([acct, amt,
                                f'Beban: {p["item_name"]} — {invoice.internal_id}'])
        debit_total += amt

    residual = target - debit_total
    if debit_posts and residual != 0:
        debit_posts[0][1] += residual
    elif not debit_posts and target > 0 and inventory_asset:
        debit_posts.append([inventory_asset, target, f'Pembelian {invoice.internal_id}'])

    for acct, amt, desc in debit_posts:
        legset.add(acct, 'debit', amt, desc)

    if target > 0 and (ap_account or ap_pending):
        legset.add(ap_account, 'credit', target,
                   f'Utang pembelian {invoice.internal_id} — {supplier.name}',
                   pending_account_label=ap_pending)
    elif target > 0:
        legset.warnings.append('Akun utang usaha tidak dapat ditentukan.')

    return legset


def _post_purchase_accrual(invoice, parsed, item_objs, supplier, grand_total,
                            post_date=None, source_type='purchase'):
    """Write the purchase accrual. Arithmetic lives in ``build_purchase_legs``;
    this is the thin writer kept for the existing create/edit/void call sites.

    ``post_date``/``source_type`` default to the invoice's own purchase_date
    and ``'purchase'`` — pass ``post_date=today, source_type='edit_memo'`` to
    post a same-day memo instead of the original transaction date.
    """
    resolved_date = post_date or invoice.purchase_date
    legset = build_purchase_legs(invoice, parsed, item_objs, supplier, grand_total)
    for leg in legset.legs:
        if leg.account is None:
            continue
        _post_le(leg.account, leg.entry_type, leg.amount, invoice, leg.description,
                 resolved_date, source_type=source_type)


def _unpost_purchase(invoice):
    """Reverse the balance effect of every ledger entry tied to this purchase
    invoice and delete the rows. Used before re-posting an edit of a document
    that is *still unposted* (no journal run has swept its date yet — this is
    then a no-op since no rows exist). Never call this on a document whose
    transaction date has already been posted; the edit-memo path handles that
    case instead by leaving the original rows untouched and writing a same-day
    reversal + repost pair."""
    entries = LedgerEntry.objects.filter(purchase_invoice=invoice).select_related('account')
    for e in entries:
        opp = 'credit' if e.entry_type == 'debit' else 'debit'
        _apply_purchase_balance(e.account_id, e.account.account_type, opp, e.amount)
    entries.delete()


def _ensure_price_variance_account():
    """The GL account that absorbs the purchase-price difference on units that
    were already sold when the invoice is edited. Created lazily under the COGS
    head so it flows into cost of sales, distinct from normal product COGS."""
    acct = ChartOfAccounts.objects.filter(account_number=PRICE_VARIANCE_NUMBER).first()
    if acct:
        return acct
    head = ChartOfAccounts.objects.filter(account_number=COGS_HEAD_NUMBER, is_head=True).first()
    return ChartOfAccounts.objects.create(
        account_number=PRICE_VARIANCE_NUMBER,
        name='Selisih Harga Pembelian',
        account_type='cogs',
        is_system=True,
        is_head=False,
        parent=head,
    )


def _post_purchase_price_variance(invoice, sold_by_key, item_objs, post_date=None, source_type='purchase'):
    """Post the price variance for units sales already consumed.

    For each (item, warehouse) with sold units, the new cost of those units
    (allocated from the edited lines) is compared with the cost the sale
    expensed (snapshotted in sold_by_key as the exact _fifo_deduct amount). The
    difference moves between the Inventory asset and the Purchase Price Variance
    account, so the sold goods' total recognised cost reflects the corrected
    price while their original COGS entry on the sale is left untouched. The
    journal stays balanced; both rows are tied to the invoice so a later edit or
    void reverses them too."""
    variance_acct   = _ensure_price_variance_account()
    inventory_asset = ChartOfAccounts.objects.filter(account_number=INVENTORY_ASSET_NUMBER).first()
    resolved_date   = post_date or invoice.purchase_date

    # New cost of the sold units, allocated across the edited lines per key.
    alloc    = {k: v['qty'] for k, v in sold_by_key.items()}
    new_cost = {k: Decimal('0') for k in sold_by_key}
    names    = {}
    for it in item_objs:
        if it.line_type != 'stock' or not it.item_id or not it.warehouse_id:
            continue
        key = (int(it.item_id), int(it.warehouse_id))
        if key not in alloc:
            continue
        names.setdefault(key, it.item_name)
        take = min(alloc[key], it.quantity)
        if take <= 0:
            continue
        alloc[key] -= take
        new_cost[key] += take * it.actual_unit_cost

    for key, agg in sold_by_key.items():
        variance = (new_cost[key] - agg['cost']).quantize(CENT)
        if variance == 0:
            continue
        nm = names.get(key, '')
        if variance > 0:
            # Sold goods cost more than first recorded → recognise extra cost.
            if inventory_asset:
                _post_le(inventory_asset, 'credit', variance, invoice,
                         f'Koreksi persediaan unit terjual: {nm} — {invoice.internal_id}',
                         resolved_date, source_type=source_type)
            _post_le(variance_acct, 'debit', variance, invoice,
                     f'Selisih harga beli unit terjual: {nm} — {invoice.internal_id}',
                     resolved_date, source_type=source_type)
        else:
            amt = -variance
            if inventory_asset:
                _post_le(inventory_asset, 'debit', amt, invoice,
                         f'Koreksi persediaan unit terjual: {nm} — {invoice.internal_id}',
                         resolved_date, source_type=source_type)
            _post_le(variance_acct, 'credit', amt, invoice,
                     f'Selisih harga beli unit terjual: {nm} — {invoice.internal_id}',
                     resolved_date, source_type=source_type)


# ── Account transfer posting (new for Phase 2 — transfers are deferred too) ───

def build_transfer_legs(transfer):
    """The two-leg entry for an AccountTransfer, computed and not written.

    Note the asymmetry with the writer below: ``post_account_transfer`` moves
    both balances by a raw +/- amount rather than by normal-balance direction,
    because a transfer is always asset-to-asset. The legs here carry the same
    debit/credit sides, so ``write_legs`` reaches the same balances via the
    normal-balance rule.
    """
    legset = LegSet(memo=transfer.description or f'Transfer #{transfer.pk}')
    legset.add(transfer.from_account, 'credit', transfer.amount, transfer.description)
    legset.add(transfer.to_account, 'debit', transfer.amount, transfer.description)
    return legset


def post_account_transfer(transfer, date=None, source_type='transfer'):
    """Write the two-leg journal entry for an AccountTransfer and roll both
    account balances. Used by the journal run to post an unposted transfer, and
    reused with different account balances already applied by the caller when
    needed. Returns nothing; callers must wrap in a transaction."""
    resolved_date = date or transfer.transfer_date
    ChartOfAccounts.objects.filter(pk=transfer.from_account_id).update(
        balance=F('balance') - transfer.amount)
    ChartOfAccounts.objects.filter(pk=transfer.to_account_id).update(
        balance=F('balance') + transfer.amount)
    LedgerEntry.objects.bulk_create([
        LedgerEntry(
            account=transfer.from_account,
            date=resolved_date,
            description=transfer.description,
            entry_type='credit',
            amount=transfer.amount,
            source_type=source_type,
            transfer=transfer,
        ),
        LedgerEntry(
            account=transfer.to_account,
            date=resolved_date,
            description=transfer.description,
            entry_type='debit',
            amount=transfer.amount,
            source_type=source_type,
            transfer=transfer,
        ),
    ])


# ── Expense posting (Phase 3) ──────────────────────────────────────────────────
# Mirrors the purchase-invoice accrual above: each ExpenseItem debits its own
# expense/cogs account, balanced by a single credit leg. Expenses have no
# per-vendor Supplier the way purchases do, so the credit leg reuses the same
# AP control account (2100000, provisioned by Supplier._ensure_ap_control_account)
# as a generic payable clearing account when the expense is unpaid at posting
# time — or the payment method's linked cash/bank account when it was paid in
# full at creation, so a same-day cash expense never round-trips through AP.

def expense_leg_memo(expense, item=None):
    """Resolved journal memo for one leg of an expense.

    ``item=None``  → the credit (cash/bank or AP) leg.
    ``item=ExpenseItem`` → that debit leg, inheriting the cash-side memo when
    the line's own memo is blank.

    This is the single source of truth for the fallback chain. The posting
    path, the API journal preview and any future PDF all call it so they can
    never disagree on a single character.
    """
    if item is None:
        target = Decimal(expense.total_amount or 0).quantize(CENT)
        fully_paid = Decimal(expense.amount_paid or 0) >= target
        return expense_credit_memo(expense, paid=fully_paid)

    payment_memo = (expense.payment_memo or '').strip()
    line_memo = (getattr(item, 'description', '') or '').strip()
    if line_memo:
        return line_memo
    if payment_memo:
        return payment_memo
    account = getattr(item, 'account', None)
    account_name = getattr(account, 'name', '') or ''
    return f'Beban: {account_name}'


def expense_credit_memo(expense, *, paid):
    """The credit-leg memo string, given whether that leg settles cash (paid)
    or books a payable (unpaid).

    ``expense_leg_memo(expense)`` derives ``paid`` from the expense's own
    amounts and delegates here. ``ExpensePayView`` calls this directly with
    ``paid=True``: a settlement leg is a payment by construction, whatever the
    running ``amount_paid`` happens to be mid-transaction.
    """
    memo = (expense.payment_memo or '').strip()
    ref = f'#{expense.pk}' if expense.pk else ''
    if paid:
        return memo or f'Pembayaran beban {ref}'.strip()
    return memo or f'Utang beban {ref}'.strip()


def expense_credit_account(expense):
    """The COA the credit leg of ``expense`` leaves from when it is paid.

    Prefers the explicit ``payment_account``; falls back to the legacy
    ``payment_method.linked_account`` for rows written before the cash-account
    picker existed. Returns ``None`` when neither is set.
    """
    if expense.payment_account_id:
        return expense.payment_account
    if expense.payment_method_id and expense.payment_method.linked_account_id:
        return expense.payment_method.linked_account
    return None


def _post_expense_le(account, entry_type, amount, expense, description, date, source_type='expense'):
    """Post one Expense LedgerEntry row and update the account balance cache."""
    _apply_purchase_balance(account.pk, account.account_type, entry_type, amount)
    LedgerEntry.objects.create(
        account=account,
        date=date,
        description=description,
        entry_type=entry_type,
        amount=amount,
        source_type=source_type,
        expense=expense,
    )


def build_expense_legs(expense, items, *, create_accounts=True):
    """Accrual double-entry for an operating expense, computed and not written:
        Dr ExpenseItem.account (one leg per line)
        Cr AP control account   — when unpaid/partially paid at posting time
        Cr expense.payment_account (or, for legacy rows, the payment method's
                                    linked_account) — when already fully paid

    ``items`` is an iterable of ExpenseItem instances (or anything exposing
    ``account_id``/``account``/``amount``/``description``) already saved
    against ``expense``.

    Every leg's description comes from ``expense_leg_memo``, so the previewed
    rows read exactly like the posted ones — the same function the expense form's
    own journal preview already calls.
    """
    legset = LegSet(memo=expense_leg_memo(expense))
    target = expense.total_amount.quantize(CENT)

    for it in items:
        amt = Decimal(it.amount).quantize(CENT)
        if amt <= 0:
            continue
        legset.add(it.account, 'debit', amt, expense_leg_memo(expense, it))

    if target <= 0:
        return legset

    credit_desc = expense_leg_memo(expense)
    fully_paid_at_creation = expense.amount_paid >= target
    credit_account = expense_credit_account(expense) if fully_paid_at_creation else None
    if credit_account is None:
        # Unpaid, or paid but with no cash/bank account named at all — book the
        # credit to the AP control account so the entry still balances rather
        # than dropping the leg on the floor. The control account is seeded by
        # migration, so a preview can look it up without creating anything.
        if create_accounts:
            credit_account = Supplier._ensure_ap_control_account()
        else:
            credit_account = ChartOfAccounts.objects.filter(account_number=2100000).first()

    if credit_account:
        legset.add(credit_account, 'credit', target, credit_desc)
    else:
        legset.add(None, 'credit', target, credit_desc,
                   pending_account_label='Utang Usaha (pengendali)')

    return legset


def _post_expense_accrual(expense, items, post_date=None, source_type='expense'):
    """Write the expense accrual. Arithmetic lives in ``build_expense_legs``;
    this is the thin writer kept for the existing create/edit/void call sites.

    ``post_date``/``source_type`` default to the expense's own ``expense_date``
    and ``'expense'`` — pass ``post_date=today, source_type='edit_memo'``/
    ``'void_memo'`` for the same-day memo path.
    """
    resolved_date = post_date or expense.expense_date
    legset = build_expense_legs(expense, items)
    for leg in legset.legs:
        if leg.account is None:
            continue
        _post_expense_le(leg.account, leg.entry_type, leg.amount, expense,
                          leg.description, resolved_date, source_type=source_type)


def _unpost_expense(expense):
    """Reverse the balance effect of every ledger entry tied to this expense
    and delete the rows. Mirrors ``_unpost_purchase`` — used before re-posting
    an edit of an expense that is *still unposted* (a no-op if no rows exist
    yet). Never call this on an already-posted expense; the edit-memo path
    handles that case instead."""
    entries = LedgerEntry.objects.filter(expense=expense).select_related('account')
    for e in entries:
        opp = 'credit' if e.entry_type == 'debit' else 'debit'
        _apply_purchase_balance(e.account_id, e.account.account_type, opp, e.amount)
    entries.delete()
