"""Sales over a date range — the shared core of the sales report.

``/api/reports/sales/`` (on-screen summary), ``/api/reports/sales/invoices/``
(the paged list behind the total) and ``/api/reports/sales/export/`` (the
workbook) all read through here, so the three can never disagree about which
invoices are in scope. Three rules decide that, and each is load-bearing:

- **Days are cut in Asia/Jakarta.** ``Invoice.datetime`` is stored in UTC; a
  22:30 WIB checkout belongs to the day the clinic worked it, not to the next
  UTC day.
- **A voided invoice is not a sale.** It put nothing in the drawer.
- **Branch scoping follows the request** (``filter_by_branch``), exactly like
  the existing payment-method breakdown, so the per-day rows sum to the total.

Returns are reported **beside** sales, never folded into them. A return is its
own document dated when the goods came back, so a sale on the 3rd refunded on
the 5th shows as a sale on the 3rd and a return on the 5th — which is what the
drawer saw on each day.
"""
import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db.models import Count, Sum
from django.db.models.functions import TruncDate

from ..models import Invoice, InvoicePayment, SalesReturn
from .branches import filter_by_branch

JAKARTA = ZoneInfo('Asia/Jakarta')
ZERO = Decimal('0')

_ID_WEEKDAYS = ['Senin', 'Selasa', 'Rabu', 'Kamis', 'Jumat', 'Sabtu', 'Minggu']


def jakarta_bounds(start, end):
    """Inclusive [start, end] dates -> half-open UTC-comparable datetimes."""
    lo = datetime.datetime(start.year, start.month, start.day, tzinfo=JAKARTA)
    hi = datetime.datetime(end.year, end.month, end.day, tzinfo=JAKARTA) + datetime.timedelta(days=1)
    return lo, hi


def sales_invoices(request, start, end):
    """Non-voided invoices dated in the range, scoped to the request's branch."""
    lo, hi = jakarta_bounds(start, end)
    return filter_by_branch(
        Invoice.objects.filter(datetime__gte=lo, datetime__lt=hi, is_voided=False),
        request,
    )


def sales_returns(request, start, end):
    """Non-voided returns dated in the range, scoped like the invoices."""
    lo, hi = jakarta_bounds(start, end)
    return filter_by_branch(
        SalesReturn.objects.filter(datetime__gte=lo, datetime__lt=hi, is_voided=False),
        request,
    )


def range_totals(invoices, returns):
    """Headline figures for the whole range."""
    agg = invoices.aggregate(
        total=Sum('grand_total'), count=Count('id'),
        discount=Sum('discount'), tax=Sum('tax'),
        additional_charges=Sum('additional_charges'),
    )
    ret = returns.aggregate(total=Sum('total_refund'), count=Count('id'))
    total = agg['total'] or ZERO
    returns_total = ret['total'] or ZERO
    return {
        'total': total,
        'invoice_count': agg['count'] or 0,
        'discount': agg['discount'] or ZERO,
        'tax': agg['tax'] or ZERO,
        'additional_charges': agg['additional_charges'] or ZERO,
        'returns_total': returns_total,
        'returns_count': ret['count'] or 0,
        'net_total': total - returns_total,
    }


def daily_breakdown(invoices, returns, start, end):
    """One row per calendar day in [start, end], **including days with no sales**.

    A zero day is information — a clinic that trades every day and shows a gap
    either was closed or has sales that were never entered — so it is listed
    rather than silently skipped.
    """
    sales_by_day = {
        r['day']: r
        for r in invoices
        .annotate(day=TruncDate('datetime', tzinfo=JAKARTA))
        .values('day')
        .annotate(
            total=Sum('grand_total'), count=Count('id'),
            discount=Sum('discount'), tax=Sum('tax'),
        )
    }
    returns_by_day = {
        r['day']: r
        for r in returns
        .annotate(day=TruncDate('datetime', tzinfo=JAKARTA))
        .values('day')
        .annotate(total=Sum('total_refund'), count=Count('id'))
    }

    rows = []
    day = start
    while day <= end:
        s = sales_by_day.get(day) or {}
        r = returns_by_day.get(day) or {}
        total = s.get('total') or ZERO
        returned = r.get('total') or ZERO
        rows.append({
            'date': day,
            'weekday': _ID_WEEKDAYS[day.weekday()],
            'invoice_count': s.get('count') or 0,
            'total': total,
            'discount': s.get('discount') or ZERO,
            'tax': s.get('tax') or ZERO,
            'returns_total': returned,
            'returns_count': r.get('count') or 0,
            'net_total': total - returned,
        })
        day += datetime.timedelta(days=1)
    return rows


def _payment_labels(invoice_ids):
    """invoice id -> 'Cash 200.000 + Bank BCA 150.000' for split invoices only."""
    labels = {}
    rows = (
        InvoicePayment.objects
        .filter(invoice_id__in=invoice_ids)
        .select_related('payment_method')
        .order_by('invoice_id', 'sort_order', 'id')
    )
    for p in rows:
        name = p.payment_method.name if p.payment_method_id else 'Tidak Diketahui'
        amount = f'{p.amount:,.0f}'.replace(',', '.')
        labels.setdefault(p.invoice_id, []).append(f'{name} {amount}')
    return {k: ' + '.join(v) for k, v in labels.items()}


def invoice_rows(invoices):
    """Serialise ``invoices`` (already sliced to a page) for the list/export.

    The payment column honours split payments the same way the per-method
    breakdown does: a split invoice lists each tender rather than naming only
    the first method the cashier picked.
    """
    invoices = list(
        invoices.select_related('patient_no', 'payment_method', 'cashier', 'branch')
    )
    split_labels = _payment_labels([inv.id for inv in invoices])
    out = []
    for inv in invoices:
        local = inv.datetime.astimezone(JAKARTA)
        if inv.id in split_labels:
            payment = split_labels[inv.id]
        elif inv.payment_method_id:
            payment = inv.payment_method.name
        else:
            payment = ''
        out.append({
            'id': inv.id,
            'invoice_number': inv.invoice_number,
            'date': local.date(),
            'time': local.strftime('%H:%M'),
            'patient_no': inv.patient_no_id or '',
            'patient_name': inv.patient_no.name if inv.patient_no_id else '',
            'cashier': inv.cashier.display_name if inv.cashier_id else '',
            'branch': inv.branch.name if inv.branch_id else '',
            'payment': payment,
            'is_split': inv.id in split_labels,
            'discount': inv.discount,
            'tax': inv.tax,
            'grand_total': inv.grand_total,
            'posting_status': inv.posting_status,
        })
    return out
