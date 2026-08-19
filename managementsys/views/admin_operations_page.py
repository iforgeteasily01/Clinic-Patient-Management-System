"""Admin module endpoints: the operator dashboard and operational-cost inputs.

Two unrelated jobs share this file because they share an audience — the manager
who opens /admin and wants to know what is behind.

**Operational inputs are planning records.** Recording a period's figure writes
an ``OperationalInputEntry`` and nothing else: no ``Expense``, no
``LedgerEntry``, no journal document. That is deliberate (product decision,
19 Aug 2026) — the general ledger keeps exactly one way in, and this module
cannot become a second one. The report here is therefore an *operational* view
of cost, not a financial statement, and will not tie to the P&L unless someone
also entered the real spend through /accounting/expenses.

All period arithmetic is delegated to ``services.operational_inputs``. Do not
recompute a period key or a due date locally.
"""
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

# How many periods back a missing entry is still reported as a task. Six months
# is long enough to catch a genuinely forgotten month and short enough that a
# template created today does not immediately owe a year of history.
TASK_LOOKBACK_PERIODS = 6


def _actor(request):
    return request.user if isinstance(request.user, AppUser) else None


def _is_manager(request):
    return getattr(request.user, 'role', None) in ('superuser', 'manager')


def _forbidden():
    return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)


def _money(value):
    """Money as a decimal string.

    A bare ``Decimal`` handed to ``Response`` is encoded as a JSON float by
    DRF's encoder, which would make these the only money fields in the API that
    arrive as numbers. Every serializer-produced amount here is a string, so
    these are too, and the frontend keeps one rule for parsing money.
    """
    return None if value is None else str(value)


def _int_param(request, name, default):
    try:
        return int(request.query_params.get(name, default))
    except (TypeError, ValueError):
        return default


# ── Templates ────────────────────────────────────────────────────────────────


class OperationalTemplateListCreateView(APIView):
    """
    GET  /api/admin/operations/templates/  ?active_only=1  ?frequency=  ?q=
    POST /api/admin/operations/templates/
    """

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
    """GET/PUT/PATCH/DELETE /api/admin/operations/templates/<pk>/"""

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
        # Changing frequency would orphan every existing entry under a period
        # key from the other calendar ('2026-08' vs '2026-W34'), which the
        # report then cannot place. Refuse rather than silently corrupt history.
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
        # Soft-retire a template that carries history: deleting it would take
        # every recorded period with it (CASCADE) and silently rewrite the
        # operational-cost report for closed months.
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


# ── Entries ──────────────────────────────────────────────────────────────────


class OperationalEntryListCreateView(APIView):
    """
    GET  /api/admin/operations/entries/  ?template=&date_from=&date_to=&period=
    POST /api/admin/operations/entries/  { template, period, amount, notes? }

    POST is an upsert: re-posting the same (template, period) replaces the
    figure rather than 409ing. Correcting a typo in last month's electricity
    bill is the common case, and there is no ledger here that a rewrite could
    corrupt — that is the whole point of these rows being planning-only.
    """

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

        # Re-derive the key from the parsed start rather than trusting the
        # client's spelling, so '2026-8' and '2026-08' cannot become two rows
        # for the same month.
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
    """DELETE /api/admin/operations/entries/<pk>/ — clears one recorded period."""

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


# ── Outstanding tasks ────────────────────────────────────────────────────────


def _outstanding_for_active_templates(lookback=TASK_LOOKBACK_PERIODS, as_of=None):
    """Every due-but-unrecorded period across active templates."""
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
    """GET /api/admin/operations/tasks/ ?lookback= — periods still to record."""

    def get(self, request):
        lookback = max(1, min(_int_param(request, 'lookback', TASK_LOOKBACK_PERIODS), 24))
        tasks = _outstanding_for_active_templates(lookback=lookback)
        return Response({
            'as_of': jakarta_today(),
            'lookback_periods': lookback,
            'count': len(tasks),
            'tasks': tasks,
        })


# ── The operational-cost report ──────────────────────────────────────────────


class OperationalCostReportView(APIView):
    """
    GET /api/admin/operations/report/ ?frequency=monthly ?periods=12 ?year=

    A template × period matrix of recorded figures with row, column and
    category totals. ``frequency`` picks which calendar the columns are on —
    monthly and weekly templates cannot share a column axis, so the report
    renders one at a time rather than inventing a fake common period.

    Amounts are ``Decimal`` and serialise as strings; the frontend parses them.
    """

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
            # A calendar year of columns, but never beyond the current period —
            # empty future columns make the row totals look like a shortfall.
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

        # One query for the whole matrix; the grid is assembled in Python
        # because the column set is a period list, not a database axis.
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
                # Average over periods that were actually recorded, not over
                # every column: dividing by 12 when only 3 months are filled in
                # reports a monthly cost that is a quarter of the real one.
                'average': _money(row_total / recorded_count) if recorded_count else None,
                'recorded_periods': recorded_count,
                'missing_periods': len(period_keys) - recorded_count,
            })

        # Category subtotals, in the same order the rows appear.
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


# ── Admin dashboard ──────────────────────────────────────────────────────────


class AdminDashboardView(APIView):
    """
    GET /api/admin/dashboard/

    What the manager needs on opening /admin:

    * ``carried_over_visits`` — ``ActivePatient`` rows whose visit started
      before today. A finished visit is *deleted* by the billing flow, so any
      row still standing from an earlier day is by definition an unfinished
      one; there is no separate "closed" state to filter out. Status 0 is
      excluded to match the queue endpoint.
    * ``pending_invoices`` — two different backlogs the operator asked to see
      together: sales invoices not yet swept into the journal, and patients
      parked at billing (status 5) with no invoice raised at all.
    * ``operational_tasks`` — due-but-unrecorded operational input periods.
    """

    def get(self, request):
        today = jakarta_today()

        # ── Carried-over visits ──────────────────────────────────────────────
        # visit_time is a timestamp in UTC; compare in Jakarta or a 21:00 local
        # check-in on the 18th reads as the 19th and never looks carried over.
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

        # ── Pending invoices ────────────────────────────────────────────────
        unposted_invoices = Invoice.objects.filter(
            posting_status='unposted', is_voided=False,
        )
        unposted_count = unposted_invoices.count()
        unposted_total = unposted_invoices.aggregate(t=Sum('grand_total'))['t'] or Decimal('0')
        oldest_unposted = (
            unposted_invoices.order_by('datetime')
            .values_list('datetime', flat=True)
            .first()
        )

        awaiting_billing = ActivePatient.objects.filter(status=5).count()

        # Purchase and expense backlogs are the same kind of "not journalled
        # yet" debt and cost one count each, so the dashboard reports them
        # rather than making the operator open /accounting to find out.
        unposted_purchases = PurchaseInvoice.objects.filter(
            posting_status='unposted', is_voided=False,
        ).count()
        unposted_expenses = Expense.objects.filter(posting_status='unposted').count()

        # ── Operational input tasks ─────────────────────────────────────────
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
                # The dashboard shows the worst offenders; the full list lives
                # on /admin/operational-inputs.
                'tasks': tasks[:8],
                'overdue_count': sum(1 for t in tasks if t['days_overdue'] > 0),
            },
        })
