import datetime
from collections import OrderedDict
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Q, Sum
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from ..api.serializers import (
    OperationalInputEntrySerializer,
    OperationalInputEntryWriteSerializer,
    OperationalInputTemplateSerializer,
)
from ..models import (
    ActivePatient, AppUser, AuditLog, Expense, Invoice,
    OperationalInputEntry, OperationalInputTemplate, PurchaseInvoice,
)
from ..services.branches import filter_by_branch
from ..services.operational_inputs import (
    JAKARTA_TZ,
    iter_period_starts,
    jakarta_today,
    month_label_short,
    outstanding_tasks,
    parse_period_key,
    period_key_for,
    period_label,
    recent_period_starts,
)

TASK_LOOKBACK_PERIODS = 6


def _actor(request):
    return request.user if isinstance(request.user, AppUser) else None


def _is_manager(request):
    return getattr(request.user, 'role', None) in ('superuser', 'manager')


def _forbidden():
    return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)


def _money(value):
    return None if value is None else str(value)


def _int_param(request, name, default):
    try:
        return int(request.query_params.get(name, default))
    except (TypeError, ValueError):
        return default


class OperationalTemplateListCreateView(APIView):
    def get(self, request):
        qs = (
            OperationalInputTemplate.objects
            .select_related('account')
            .annotate(entry_count=Count('entries'))
        )
        if request.query_params.get('active_only', '').strip().lower() in ('1', 'true', 'yes'):
            qs = qs.filter(is_active=True)
        if frequency := request.query_params.get('frequency', '').strip():
            qs = qs.filter(frequency=frequency)
        if q := request.query_params.get('q', '').strip():
            qs = qs.filter(Q(name__icontains=q) | Q(category__icontains=q))
        return Response(OperationalInputTemplateSerializer(qs, many=True).data)

    def post(self, request):
        if not _is_manager(request):
            return _forbidden()
        serializer = OperationalInputTemplateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        instance = serializer.save()
        AuditLog.objects.create(
            performed_by=_actor(request),
            action='CREATE',
            entity_type='OperationalInputTemplate',
            entity_id=str(instance.id),
            description=f'Operational input template created: {instance.name}',
        )
        return Response(
            OperationalInputTemplateSerializer(instance).data,
            status=status.HTTP_201_CREATED,
        )


class OperationalTemplateDetailView(APIView):
    def _get(self, pk):
        return (
            OperationalInputTemplate.objects
            .select_related('account')
            .annotate(entry_count=Count('entries'))
            .filter(pk=pk)
            .first()
        )

    def get(self, request, pk):
        instance = self._get(pk)
        if instance is None:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(OperationalInputTemplateSerializer(instance).data)

    def put(self, request, pk):
        return self._update(request, pk, partial=False)

    def patch(self, request, pk):
        return self._update(request, pk, partial=True)

    def _update(self, request, pk, partial):
        if not _is_manager(request):
            return _forbidden()
        instance = self._get(pk)
        if instance is None:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        serializer = OperationalInputTemplateSerializer(instance, data=request.data, partial=partial)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        new_frequency = serializer.validated_data.get('frequency', instance.frequency)
        if new_frequency != instance.frequency and instance.entries.exists():
            return Response(
                {'frequency': [
                    'Template sudah memiliki data periode. Hapus data tersebut '
                    'atau buat template baru untuk mengganti frekuensi.',
                ]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        updated = serializer.save()
        AuditLog.objects.create(
            performed_by=_actor(request),
            action='UPDATE',
            entity_type='OperationalInputTemplate',
            entity_id=str(updated.id),
            description=f'Operational input template updated: {updated.name}',
        )
        return Response(OperationalInputTemplateSerializer(self._get(pk)).data)

    def delete(self, request, pk):
        if not _is_manager(request):
            return _forbidden()
        instance = self._get(pk)
        if instance is None:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if instance.entries.exists():
            instance.is_active = False
            instance.save(update_fields=['is_active'])
            AuditLog.objects.create(
                performed_by=_actor(request),
                action='UPDATE',
                entity_type='OperationalInputTemplate',
                entity_id=str(instance.id),
                description=f'Operational input template deactivated (has history): {instance.name}',
            )
            return Response({
                'deactivated': True,
                'detail': 'Template memiliki riwayat input, jadi dinonaktifkan alih-alih dihapus.',
            })
        name = instance.name
        instance.delete()
        AuditLog.objects.create(
            performed_by=_actor(request),
            action='DELETE',
            entity_type='OperationalInputTemplate',
            entity_id=str(pk),
            description=f'Operational input template deleted: {name}',
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class OperationalEntryListCreateView(APIView):
    def get(self, request):
        qs = (
            OperationalInputEntry.objects
            .select_related('template', 'recorded_by')
        )
        if template_id := _int_param(request, 'template', 0):
            qs = qs.filter(template_id=template_id)
        if date_from := request.query_params.get('date_from', '').strip():
            qs = qs.filter(period_start__gte=date_from)
        if date_to := request.query_params.get('date_to', '').strip():
            qs = qs.filter(period_start__lte=date_to)
        if period := request.query_params.get('period', '').strip():
            qs = qs.filter(period_key=period)
        return Response(OperationalInputEntrySerializer(qs, many=True).data)

    def post(self, request):
        if not _is_manager(request):
            return _forbidden()
        serializer = OperationalInputEntryWriteSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        data = serializer.validated_data

        template = OperationalInputTemplate.objects.filter(pk=data['template']).first()
        if template is None:
            return Response(
                {'template': ['Template tidak ditemukan.']},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            period_start = parse_period_key(template.frequency, data['period'])
        except ValueError as exc:
            return Response({'period': [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)

        period_key = period_key_for(template.frequency, period_start)

        with transaction.atomic():
            entry, created = OperationalInputEntry.objects.update_or_create(
                template=template,
                period_key=period_key,
                defaults={
                    'period_start': period_start,
                    'amount': data['amount'],
                    'notes': data.get('notes', ''),
                    'recorded_by': _actor(request),
                },
            )

        AuditLog.objects.create(
            performed_by=_actor(request),
            action='CREATE' if created else 'UPDATE',
            entity_type='OperationalInputEntry',
            entity_id=str(entry.id),
            description=(
                f'Operational input {"recorded" if created else "revised"}: '
                f'{template.name} {period_key} = {entry.amount}'
            ),
        )
        return Response(
            OperationalInputEntrySerializer(entry).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class OperationalEntryDetailView(APIView):
    def delete(self, request, pk):
        if not _is_manager(request):
            return _forbidden()
        entry = OperationalInputEntry.objects.select_related('template').filter(pk=pk).first()
        if entry is None:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        label = f'{entry.template.name} {entry.period_key}'
        entry.delete()
        AuditLog.objects.create(
            performed_by=_actor(request),
            action='DELETE',
            entity_type='OperationalInputEntry',
            entity_id=str(pk),
            description=f'Operational input cleared: {label}',
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


def _outstanding_for_active_templates(lookback=TASK_LOOKBACK_PERIODS, as_of=None):
    templates = list(OperationalInputTemplate.objects.filter(is_active=True))
    recorded = {}
    for template_id, period_key in (
        OperationalInputEntry.objects
        .filter(template__in=templates)
        .values_list('template_id', 'period_key')
    ):
        recorded.setdefault(template_id, set()).add(period_key)
    return outstanding_tasks(templates, recorded, as_of=as_of, lookback=lookback)


class OperationalTasksView(APIView):
    def get(self, request):
        lookback = max(1, min(_int_param(request, 'lookback', TASK_LOOKBACK_PERIODS), 24))
        tasks = _outstanding_for_active_templates(lookback=lookback)
        return Response({
            'as_of': jakarta_today(),
            'lookback_periods': lookback,
            'count': len(tasks),
            'tasks': tasks,
        })


class OperationalCostReportView(APIView):
    def get(self, request):
        frequency = request.query_params.get('frequency', 'monthly').strip()
        if frequency not in ('monthly', 'weekly'):
            return Response(
                {'frequency': ['Pilih monthly atau weekly.']},
                status=status.HTTP_400_BAD_REQUEST,
            )

        today = jakarta_today()
        year = _int_param(request, 'year', 0)
        if year:
            first = datetime.date(year, 1, 1)
            last = min(datetime.date(year, 12, 31), today)
            if last < first:
                period_starts = []
            else:
                period_starts = iter_period_starts(frequency, first, last)
        else:
            count = max(1, min(_int_param(request, 'periods', 12), 60))
            period_starts = recent_period_starts(frequency, count, as_of=today)

        templates = list(
            OperationalInputTemplate.objects
            .filter(frequency=frequency)
            .select_related('account')
            .order_by('category', 'sort_order', 'name')
        )
        period_keys = [period_key_for(frequency, ps) for ps in period_starts]

        recorded = {}
        if templates and period_keys:
            for entry in OperationalInputEntry.objects.filter(
                template__in=templates, period_key__in=period_keys,
            ).values('template_id', 'period_key', 'amount'):
                recorded[(entry['template_id'], entry['period_key'])] = entry['amount']

        columns = [{
            'period_key': key,
            'period_start': start,
            'label': period_label(frequency, start),
            'short_label': (
                month_label_short(start.month) if frequency == 'monthly'
                else f'W{start.isocalendar()[1]:02d}'
            ),
        } for key, start in zip(period_keys, period_starts)]

        rows = []
        column_totals = {key: Decimal('0') for key in period_keys}
        for template in templates:
            cells = []
            row_total = Decimal('0')
            recorded_count = 0
            for key in period_keys:
                amount = recorded.get((template.id, key))
                if amount is None:
                    cells.append(None)
                else:
                    cells.append(amount)
                    row_total += amount
                    column_totals[key] += amount
                    recorded_count += 1
            rows.append({
                'template_id': template.id,
                'name': template.name,
                'category': template.category or 'Tanpa Kategori',
                'account_number': template.account.account_number if template.account_id else None,
                'account_name': template.account.name if template.account_id else None,
                'expected_amount': _money(template.expected_amount),
                'is_active': template.is_active,
                'cells': [_money(c) for c in cells],
                'total': _money(row_total),
                'average': _money(row_total / recorded_count) if recorded_count else None,
                'recorded_periods': recorded_count,
                'missing_periods': len(period_keys) - recorded_count,
            })

        categories = OrderedDict()
        for row in rows:
            bucket = categories.setdefault(row['category'], {
                'category': row['category'],
                'template_ids': [],
                'cells': [Decimal('0')] * len(period_keys),
                'total': Decimal('0'),
            })
            bucket['template_ids'].append(row['template_id'])
            bucket['total'] += row['total']
            for index, amount in enumerate(row['cells']):
                if amount is not None:
                    bucket['cells'][index] += amount

        grand_total = sum(column_totals.values(), Decimal('0'))
        return Response({
            'frequency': frequency,
            'as_of': today,
            'columns': columns,
            'rows': rows,
            'categories': [
                {
                    **bucket,
                    'cells': [_money(c) for c in bucket['cells']],
                    'total': _money(bucket['total']),
                }
                for bucket in categories.values()
            ],
            'column_totals': [_money(column_totals[key]) for key in period_keys],
            'grand_total': _money(grand_total),
            'average_per_period': (
                _money(grand_total / len(period_keys)) if period_keys else None
            ),
            'missing_cells': sum(row['missing_periods'] for row in rows),
        })


class AdminDashboardView(APIView):
    def get(self, request):
        today = jakarta_today()

        day_start_utc = datetime.datetime.combine(
            today, datetime.time.min, tzinfo=JAKARTA_TZ,
        ).astimezone(datetime.timezone.utc)

        carried = (
            ActivePatient.objects
            .exclude(status=0)
            .filter(visit_time__lt=day_start_utc)
            .select_related('patient_no', 'medrec')
            .order_by('visit_time')
        )
        carried_rows = []
        for visit in carried:
            local_visit = visit.visit_time.astimezone(JAKARTA_TZ)
            carried_rows.append({
                'id': visit.id,
                'patient_no': visit.patient_no_id,
                'patient_name': (
                    visit.patient_no.name if visit.patient_no_id else visit.guest_name
                ) or '—',
                'is_guest': visit.patient_no_id is None,
                'status': visit.status,
                'consult_status': visit.consult_status,
                'visit_time': visit.visit_time,
                'visit_date': local_visit.date(),
                'days_waiting': (today - local_visit.date()).days,
                'medrec_id': visit.medrec_id,
            })

        unposted_invoices = filter_by_branch(
            Invoice.objects.filter(posting_status='unposted', is_voided=False),
            request,
        )
        unposted_count = unposted_invoices.count()
        unposted_total = unposted_invoices.aggregate(t=Sum('grand_total'))['t'] or Decimal('0')
        oldest_unposted = (
            unposted_invoices.order_by('datetime')
            .values_list('datetime', flat=True)
            .first()
        )

        awaiting_billing = filter_by_branch(
            ActivePatient.objects.filter(status=5), request, include_null=False,
        ).count()

        unposted_purchases = filter_by_branch(
            PurchaseInvoice.objects.filter(posting_status='unposted', is_voided=False),
            request,
        ).count()
        unposted_expenses = filter_by_branch(
            Expense.objects.filter(posting_status='unposted'), request,
        ).count()

        tasks = _outstanding_for_active_templates(as_of=today)

        return Response({
            'as_of': today,
            'carried_over_visits': {
                'count': len(carried_rows),
                'visits': carried_rows,
            },
            'pending_invoices': {
                'unposted_count': unposted_count,
                'unposted_total': _money(unposted_total),
                'oldest_unposted_at': oldest_unposted,
                'awaiting_billing_count': awaiting_billing,
                'unposted_purchases': unposted_purchases,
                'unposted_expenses': unposted_expenses,
            },
            'operational_tasks': {
                'count': len(tasks),
                'tasks': tasks[:8],
                'overdue_count': sum(1 for t in tasks if t['days_overdue'] > 0),
            },
        })
