import datetime
from zoneinfo import ZoneInfo

from django.db.models import Count, DecimalField, F, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import InventoryItem, Invoice

_JAKARTA = ZoneInfo('Asia/Jakarta')


class DashboardReportView(APIView):
    def get(self, request):
        now = timezone.now()
        # Use Jakarta local time so "today" and "this month" match clinic hours.
        local_now = now.astimezone(_JAKARTA)

        today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        if local_now.month == 1:
            last_month_start = local_now.replace(
                year=local_now.year - 1, month=12, day=1,
                hour=0, minute=0, second=0, microsecond=0,
            )
        else:
            last_month_start = month_start.replace(month=month_start.month - 1)

        inv_today = Invoice.objects.filter(datetime__gte=today_start)
        inv_month = Invoice.objects.filter(datetime__gte=month_start)
        inv_last_month = Invoice.objects.filter(
            datetime__gte=last_month_start, datetime__lt=month_start
        )

        today_agg = inv_today.aggregate(total=Sum('grand_total'), count=Count('id'))
        month_agg = inv_month.aggregate(total=Sum('grand_total'), count=Count('id'))
        last_month_agg = inv_last_month.aggregate(total=Sum('grand_total'), count=Count('id'))

        payment_breakdown = list(
            inv_month
            .values('payment_method_id', 'payment_method__name')
            .annotate(total=Sum('grand_total'), count=Count('id'))
            .order_by('-total')
        )

        items_qs = InventoryItem.objects.filter(is_service=False, is_active=True).annotate(
            total_stock=Coalesce(Sum('batches__quantity_remaining'), Value(0, output_field=DecimalField()))
        )
        low_stock_items = list(
            items_qs
            .filter(total_stock__lt=F('min_stock'))
            .values('id', 'code', 'name', 'unit_small', 'min_stock', 'total_stock')
            .order_by('name')
        )

        recent = Invoice.objects.select_related('patient_no', 'payment_method').order_by('-datetime')[:10]
        recent_data = [
            {
                'invoice_number': inv.invoice_number,
                'datetime': inv.datetime,
                'patient_name': inv.patient_no.name if inv.patient_no else None,
                'grand_total': str(inv.grand_total),
                'payment_method_id': inv.payment_method_id,
                'payment_method_name': inv.payment_method.name if inv.payment_method_id else None,
            }
            for inv in recent
        ]

        return Response({
            'revenue': {
                'today_total': str(today_agg['total'] or 0),
                'today_count': today_agg['count'] or 0,
                'this_month_total': str(month_agg['total'] or 0),
                'this_month_count': month_agg['count'] or 0,
                'last_month_total': str(last_month_agg['total'] or 0),
                'last_month_count': last_month_agg['count'] or 0,
                'by_payment_method': [
                    {
                        'payment_method_id': r['payment_method_id'],
                        'method': r['payment_method__name'],
                        'total': str(r['total'] or 0),
                        'count': r['count'],
                    }
                    for r in payment_breakdown
                ],
            },
            'inventory': {
                'total_active_items': items_qs.count(),
                'low_stock_count': len(low_stock_items),
                'low_stock_items': low_stock_items,
            },
            'recent_invoices': recent_data,
        })


class SalesRangeReportView(APIView):
    """
    GET /api/reports/sales/?start=YYYY-MM-DD&end=YYYY-MM-DD
    Total sales and breakdown per cash (payment) account over an inclusive date
    range. Defaults to the current month-to-date in Jakarta time.
    """

    def get(self, request):
        today = timezone.now().astimezone(_JAKARTA).date()

        def parse(param, default):
            raw = request.query_params.get(param, '').strip()
            if not raw:
                return default, None
            try:
                return datetime.date.fromisoformat(raw), None
            except ValueError:
                return None, f'Tanggal {param} tidak valid. Gunakan format YYYY-MM-DD.'

        start, err = parse('start', today.replace(day=1))
        if err:
            return Response({'error': err}, status=status.HTTP_400_BAD_REQUEST)
        end, err = parse('end', today)
        if err:
            return Response({'error': err}, status=status.HTTP_400_BAD_REQUEST)

        if start > end:
            return Response(
                {'error': 'Tanggal mulai tidak boleh setelah tanggal akhir.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # `datetime` is stored in UTC; bound the range by Jakarta-local midnights
        # so a day belongs to the range the clinic actually worked it.
        range_start = datetime.datetime(start.year, start.month, start.day, tzinfo=_JAKARTA)
        range_end = datetime.datetime(end.year, end.month, end.day, tzinfo=_JAKARTA) + datetime.timedelta(days=1)

        invoices = Invoice.objects.filter(datetime__gte=range_start, datetime__lt=range_end)
        agg = invoices.aggregate(total=Sum('grand_total'), count=Count('id'))

        breakdown = (
            invoices
            .values('payment_method_id', 'payment_method__name', 'payment_method__account_number')
            .annotate(total=Sum('grand_total'), invoice_count=Count('id'))
            .order_by('-total')
        )

        return Response({
            'start': str(start),
            'end': str(end),
            'total': str(agg['total'] or 0),
            'invoice_count': agg['count'] or 0,
            'by_account': [
                {
                    'account_id': r['payment_method_id'],
                    'account_number': r['payment_method__account_number'],
                    'account_name': r['payment_method__name'] or 'Tidak Diketahui',
                    'total': str(r['total'] or 0),
                    'invoice_count': r['invoice_count'],
                }
                for r in breakdown
            ],
        })
