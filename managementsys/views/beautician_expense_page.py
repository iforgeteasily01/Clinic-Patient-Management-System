"""GET/POST endpoints the beautician petty-cash flow uses (design doc §4).

A beautician never picks a GL account: they pick a friendly ``ExpenseAlias``
name, and this view resolves it to the real account behind the scenes. The
actual write goes through ``services.alias_expense.create_alias_expense`` and
from there ``services.expense_create.create_expense`` — the exact function the
manager-facing expense form uses — so there is exactly one way an ``Expense``
gets written in this system, not two that could drift apart. The admin
quick-purchase form (``views/admin_quick_expense_page.py``) shares the same
service with scope='general'.
"""
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from ..api.serializers import BeauticianExpenseCreateSerializer, ExpenseAliasSerializer, ExpenseSerializer
from ..models import AppUser, Expense, ExpenseAlias
from ..services.alias_expense import AliasExpenseError, create_alias_expense


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
        try:
            expense = create_alias_expense(
                data=serializer.validated_data,
                scope='beautician',
                source='beautician',
                actor=_actor(request),
            )
        except AliasExpenseError as exc:
            return Response(exc.errors, status=status.HTTP_400_BAD_REQUEST)
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
