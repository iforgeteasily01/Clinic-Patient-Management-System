from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from ..api.serializers import (
    BeauticianExpenseCreateSerializer,
    ExpenseAliasSerializer,
    ExpenseSerializer,
)
from ..models import AppUser, Expense, ExpenseAlias
from ..services.alias_expense import AliasExpenseError, create_alias_expense


def _actor(request):
    return request.user if isinstance(request.user, AppUser) else None


def _is_manager(request):
    return getattr(request.user, 'role', None) in ('superuser', 'manager')


class QuickExpenseAliasListView(APIView):
    def get(self, request):
        aliases = (
            ExpenseAlias.objects
            .filter(scope='general', is_active=True)
            .select_related('account')
        )
        return Response(ExpenseAliasSerializer(aliases, many=True).data)


class QuickExpenseListCreateView(APIView):
    def get(self, request):
        qs = (
            Expense.objects
            .filter(source='general')
            .select_related('payment_account', 'created_by')
            .prefetch_related('items__account', 'items__alias')
        )
        if date_from := request.query_params.get('date_from', '').strip():
            qs = qs.filter(expense_date__gte=date_from)
        if date_to := request.query_params.get('date_to', '').strip():
            qs = qs.filter(expense_date__lte=date_to)
        try:
            limit = int(request.query_params.get('limit', 15))
        except (TypeError, ValueError):
            limit = 15
        limit = min(max(limit, 1), 100)
        rows = qs.order_by('-expense_date', '-created_at')[:limit]
        return Response(ExpenseSerializer(rows, many=True).data)

    def post(self, request):
        if not _is_manager(request):
            return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
        serializer = BeauticianExpenseCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        try:
            expense = create_alias_expense(
                data=serializer.validated_data,
                scope='general',
                source='general',
                actor=_actor(request),
            )
        except AliasExpenseError as exc:
            return Response(exc.errors, status=status.HTTP_400_BAD_REQUEST)
        return Response(ExpenseSerializer(expense).data, status=status.HTTP_201_CREATED)
