from django.db.models import Q
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from ..api.serializers import BranchSerializer
from ..models import AppUser, AuditLog, Branch, SiteConfig
from ..services.branches import (
    can_cross_branch, home_branch, receipt_identity, resolve_selection,
)


def _actor(request):
    return request.user if isinstance(request.user, AppUser) else None


class BranchListView(APIView):
    def get(self, request):
        user = _actor(request)
        qs = Branch.objects.filter(is_active=True)
        if not can_cross_branch(user):
            hb = home_branch(user)
            qs = qs.filter(pk=hb.pk) if hb else qs.none()

        selected, is_all = resolve_selection(request)
        hb = home_branch(user)
        return Response({
            'branches': BranchSerializer(qs, many=True).data,
            'home_branch_id': hb.pk if hb else None,
            'selected_branch_id': None if is_all else (selected.pk if selected else None),
            'selected_all': is_all,
            'allow_all': can_cross_branch(user),
        })


class ReceiptIdentityView(APIView):
    def get(self, request):
        cfg = SiteConfig.get_solo()
        branch = home_branch(_actor(request))
        return Response({
            **receipt_identity(branch, cfg),
            'receipt_header_extra': cfg.receipt_header_extra,
            'receipt_footer': cfg.receipt_footer,
            'branch_id': branch.pk if branch else None,
        })


class BranchListCreateAdminView(generics.ListCreateAPIView):
    serializer_class = BranchSerializer

    def get_queryset(self):
        qs = Branch.objects.all()
        if self.request.query_params.get('active_only') == 'true':
            qs = qs.filter(is_active=True)
        return qs

    def perform_create(self, serializer):
        is_first = not Branch.objects.exists()
        instance = serializer.save(is_default=serializer.validated_data.get('is_default', False) or is_first)
        AuditLog.objects.create(
            performed_by=_actor(self.request),
            action='CREATE',
            entity_type='Branch',
            entity_id=str(instance.id),
            description=f'Branch added: [{instance.code}] {instance.name}',
        )


class BranchDetailAdminView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Branch.objects.all()
    serializer_class = BranchSerializer

    def perform_update(self, serializer):
        instance = serializer.save()
        AuditLog.objects.create(
            performed_by=_actor(self.request),
            action='UPDATE',
            entity_type='Branch',
            entity_id=str(instance.id),
            description=f'Branch updated: [{instance.code}] {instance.name}',
        )

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()

        if instance.is_default:
            return Response(
                {'error': 'Cabang default tidak dapat dihapus. Tetapkan cabang '
                          'lain sebagai default terlebih dahulu.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        blockers = _usage_counts(instance)
        if blockers:
            detail = ', '.join(f'{n} {label}' for label, n in blockers.items())
            return Response(
                {'error': f'Cabang ini masih memiliki data ({detail}). '
                          f'Nonaktifkan cabang alih-alih menghapusnya.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        AuditLog.objects.create(
            performed_by=_actor(request),
            action='DELETE',
            entity_type='Branch',
            entity_id=str(instance.id),
            description=f'Branch deleted: [{instance.code}] {instance.name}',
        )
        instance.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


def _usage_counts(branch):
    counts = {
        'transaksi penjualan': branch.invoices.count(),
        'rekam medis':         branch.medrecs.count(),
        'gudang':              branch.warehouses.count(),
        'jurnal':              branch.ledger_entries.count(),
        'staf':                branch.staff.count(),
    }
    return {k: v for k, v in counts.items() if v}
