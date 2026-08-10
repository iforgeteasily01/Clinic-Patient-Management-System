"""GET/POST endpoints the beautician petty-cash flow uses (design doc §4).

A beautician never picks a GL account: they pick a friendly ``ExpenseAlias``
name, and this view resolves it to the real account behind the scenes. The
actual write goes through ``services.expense_create.create_expense`` — the
exact function the manager-facing expense form uses — so there is exactly one
way an ``Expense`` gets written in this system, not two that could drift
apart.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from ..api.serializers import BeauticianExpenseCreateSerializer, ExpenseAliasSerializer, ExpenseSerializer
from ..models import AppUser, ChartOfAccounts, Expense, ExpenseAlias
from ..services.cash_accounts import cash_bank_account_ids
from ..services.expense_create import create_expense


def _actor(request):
    return request.user if isinstance(request.user, AppUser) else None


class BeauticianExpenseAliasListView(APIView):
    """GET /api/beautician/expense-aliases/ — active, scope='beautician' aliases."""

    def get(self, request):
        aliases = (
            ExpenseAlias.objects
            .filter(scope='beautician', is_active=True)
            .select_related('account')
        )
        return Response(ExpenseAliasSerializer(aliases, many=True).data)


class BeauticianExpenseListCreateView(APIView):
    """
    GET  /api/beautician/expenses/  ?date_from=&date_to=&page=&page_size=
         Scoped to source='beautician'. A `beautician` role sees only its own
         rows (``created_by=request.user``); `manager`/`superuser` see all —
         they are the ones curating/auditing the list.

    POST /api/beautician/expenses/
         { expense_date, payment_account_id, payment_memo?, notes?,
           items: [{alias_id, amount, description?}] }
    """

    def get(self, request):
        qs = (
            Expense.objects
            .filter(source='beautician')
            .select_related('payment_account', 'created_by')
            .prefetch_related('items__account', 'items__alias')
        )
        actor = _actor(request)
        if actor is not None and actor.role == 'beautician':
            qs = qs.filter(created_by=actor)

        date_from = request.query_params.get('date_from', '').strip()
        date_to = request.query_params.get('date_to', '').strip()
        if date_from:
            qs = qs.filter(expense_date__gte=date_from)
        if date_to:
            qs = qs.filter(expense_date__lte=date_to)

        try:
            page = max(1, int(request.query_params.get('page', 1)))
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = int(request.query_params.get('page_size', 25))
        except (TypeError, ValueError):
            page_size = 25
        page_size = min(max(page_size, 1), 100)

        qs = qs.order_by('-expense_date', '-created_at')
        total = qs.count()
        offset = (page - 1) * page_size
        rows = qs[offset:offset + page_size]

        return Response({
            'count': total,
            'page': page,
            'page_size': page_size,
            'num_pages': max(1, -(-total // page_size)),
            'results': ExpenseSerializer(rows, many=True).data,
        })

    def post(self, request):
        serializer = BeauticianExpenseCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        data = serializer.validated_data

        payment_account_id = data['payment_account_id']
        if payment_account_id not in cash_bank_account_ids():
            return Response(
                {'payment_account_id': ['Not a cash/bank account.']},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            payment_account = ChartOfAccounts.objects.get(pk=payment_account_id)
        except ChartOfAccounts.DoesNotExist:
            return Response(
                {'payment_account_id': ['Not a cash/bank account.']},
                status=status.HTTP_400_BAD_REQUEST,
            )

        alias_ids = [row['alias_id'] for row in data['items']]
        aliases = {
            a.id: a for a in ExpenseAlias.objects.filter(
                id__in=alias_ids, scope='beautician', is_active=True,
            ).select_related('account')
        }
        missing = [aid for aid in alias_ids if aid not in aliases]
        if missing:
            return Response(
                {'items': f'Alias tidak ditemukan atau tidak aktif: {missing}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        items = []
        for row in data['items']:
            alias = aliases[row['alias_id']]
            # Each item resolves account = alias.account, description =
            # description or alias.name — so the journal memo reads "Beli
            # kapas" and the accountant can still see which GL account it hit,
            # even though the beautician never saw the account number.
            items.append({
                'account': alias.account_id,
                'description': (row.get('description') or '').strip() or alias.name,
                'amount': row['amount'],
                'alias': alias,
            })

        total_paid = sum((row['amount'] for row in items), Decimal('0'))

        expense = create_expense(
            expense_date=data['expense_date'],
            payment_method=None,
            payment_account=payment_account,
            payment_memo=data.get('payment_memo', ''),
            notes=data.get('notes', ''),
            amount_paid=total_paid,
            items=items,
            actor=_actor(request),
            source='beautician',
            # Paid immediately: a beautician spending petty cash has already
            # spent it, there is no payable to track.
            status_override='paid',
        )

        return Response(ExpenseSerializer(expense).data, status=status.HTTP_201_CREATED)


class BeauticianExpenseDetailView(APIView):
    """GET /api/beautician/expenses/<pk>/ — same ownership scoping as the list."""

    def get(self, request, pk):
        try:
            expense = (
                Expense.objects
                .filter(source='beautician')
                .select_related('payment_account', 'created_by')
                .prefetch_related('items__account', 'items__alias')
                .get(pk=pk)
            )
        except Expense.DoesNotExist:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        actor = _actor(request)
        if actor is not None and actor.role == 'beautician' and expense.created_by_id != actor.id:
            # Not "Forbidden" — a beautician probing other ids should not be
            # able to distinguish "not yours" from "does not exist".
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        return Response(ExpenseSerializer(expense).data)
