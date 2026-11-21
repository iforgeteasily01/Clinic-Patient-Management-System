from decimal import Decimal, InvalidOperation

from django.db import transaction
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from ..api.serializers import (
    BankReconciliationDetailSerializer, BankReconciliationListSerializer,
    BankStatementLineSerializer,
)
from ..models import (
    AppUser, AuditLog, BankReconciliation, BankStatementLine, ChartOfAccounts,
    LedgerEntry,
)
from ..services import bank_reconciliation as recon
from ..services import statement_import
from ..services.branches import filter_by_branch, write_branch
from ..services.cash_accounts import cash_bank_account_ids


def _actor(request):
    return request.user if isinstance(request.user, AppUser) else None


def _decimal(value, default='0'):
    try:
        return Decimal(str(value if value not in (None, '') else default))
    except (InvalidOperation, TypeError):
        return Decimal(default)


def _get(pk):
    return (BankReconciliation.objects
            .select_related('account', 'branch', 'created_by', 'completed_by')
            .filter(pk=pk)
            .first())


def _serialize_summary(figures):
    return {
        k: (str(v) if isinstance(v, Decimal) else v)
        for k, v in figures.items()
    }


class ReconciliationListCreateView(APIView):
    def get(self, request):
        qs = (BankReconciliation.objects
              .select_related('account', 'branch', 'created_by', 'completed_by')
              .prefetch_related('lines'))
        qs = filter_by_branch(qs, request)

        if account := request.query_params.get('account', '').strip():
            qs = qs.filter(account_id=account)
        if st := request.query_params.get('status', '').strip():
            qs = qs.filter(status=st)
        if date_from := request.query_params.get('date_from', '').strip():
            qs = qs.filter(statement_end__gte=date_from)
        if date_to := request.query_params.get('date_to', '').strip():
            qs = qs.filter(statement_start__lte=date_to)

        return Response(BankReconciliationListSerializer(qs, many=True).data)

    def post(self, request):
        data = request.data

        account_id = data.get('account')
        if not account_id or int(account_id) not in cash_bank_account_ids():
            return Response({'account': 'Pilih rekening kas/bank yang valid.'},
                            status=status.HTTP_400_BAD_REQUEST)
        account = ChartOfAccounts.objects.filter(pk=account_id).first()

        start = data.get('statement_start')
        end = data.get('statement_end')
        if not start or not end:
            return Response({'error': 'Periode rekening koran wajib diisi.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if str(end) < str(start):
            return Response({'statement_end': 'Tanggal akhir sebelum tanggal awal.'},
                            status=status.HTTP_400_BAD_REQUEST)

        branch = write_branch(request)

        clash = BankReconciliation.objects.filter(
            account=account, branch=branch,
            statement_start__lte=end, statement_end__gte=start,
        ).first()
        if clash is not None:
            return Response(
                {'error': f'Periode ini tumpang tindih dengan rekonsiliasi '
                          f'{clash.statement_start} s/d {clash.statement_end}.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        obj = BankReconciliation.objects.create(
            account=account,
            branch=branch,
            statement_start=start,
            statement_end=end,
            opening_balance=_decimal(data.get('opening_balance')),
            closing_balance=_decimal(data.get('closing_balance')),
            notes=data.get('notes', ''),
            created_by=_actor(request),
        )

        AuditLog.objects.create(
            performed_by=_actor(request),
            action='CREATE',
            entity_type='BankReconciliation',
            entity_id=str(obj.pk),
            description=f'Rekonsiliasi {account.name} {start} s/d {end} dibuka',
        )

        return Response(BankReconciliationDetailSerializer(obj).data,
                        status=status.HTTP_201_CREATED)


class ReconciliationDetailView(APIView):
    def get(self, request, pk):
        obj = _get(pk)
        if obj is None:
            return Response({'error': 'Rekonsiliasi tidak ditemukan.'},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(BankReconciliationDetailSerializer(obj).data)

    def patch(self, request, pk):
        obj = _get(pk)
        if obj is None:
            return Response({'error': 'Rekonsiliasi tidak ditemukan.'},
                            status=status.HTTP_404_NOT_FOUND)
        if obj.is_locked:
            return Response({'error': 'Rekonsiliasi ini sudah diselesaikan.'},
                            status=status.HTTP_400_BAD_REQUEST)

        for field in ('opening_balance', 'closing_balance'):
            if field in request.data:
                setattr(obj, field, _decimal(request.data[field]))
        if 'notes' in request.data:
            obj.notes = request.data['notes']
        obj.save(update_fields=['opening_balance', 'closing_balance', 'notes'])

        return Response(BankReconciliationDetailSerializer(obj).data)

    def delete(self, request, pk):
        obj = _get(pk)
        if obj is None:
            return Response({'error': 'Rekonsiliasi tidak ditemukan.'},
                            status=status.HTTP_404_NOT_FOUND)
        if obj.is_locked:
            return Response(
                {'error': 'Rekonsiliasi yang sudah selesai tidak dapat dihapus. '
                          'Buka kembali terlebih dahulu.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        AuditLog.objects.create(
            performed_by=_actor(request),
            action='DELETE',
            entity_type='BankReconciliation',
            entity_id=str(obj.pk),
            description=f'Rekonsiliasi {obj.account.name} '
                        f'{obj.statement_start} s/d {obj.statement_end} dihapus',
        )
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ReconciliationWorkspaceView(APIView):
    def get(self, request, pk):
        obj = _get(pk)
        if obj is None:
            return Response({'error': 'Rekonsiliasi tidak ditemukan.'},
                            status=status.HTTP_404_NOT_FOUND)

        lines = obj.lines.select_related('ledger_entry').all()
        entries = recon.book_entries(obj)
        matched_ids = {l.ledger_entry_id for l in lines if l.ledger_entry_id}

        return Response({
            'reconciliation': BankReconciliationDetailSerializer(obj).data,
            'lines': BankStatementLineSerializer(lines, many=True).data,
            'book_entries': [{
                'id':          e.pk,
                'date':        e.date,
                'description': e.description,
                'entry_type':  e.entry_type,
                'amount':      str(e.amount),
                'signed_amount': str(recon.signed_amount(e)),
                'source_type': e.source_type,
                'document':    recon.describe_entry(e),
                'entry_number': e.journal_entry.entry_number if e.journal_entry_id else None,
                'is_matched':  e.pk in matched_ids,
            } for e in entries],
            'summary': _serialize_summary(recon.summary(obj)),
        })


class ReconciliationImportPreviewView(APIView):
    def post(self, request, pk):
        obj = _get(pk)
        if obj is None:
            return Response({'error': 'Rekonsiliasi tidak ditemukan.'},
                            status=status.HTTP_404_NOT_FOUND)
        if obj.is_locked:
            return Response({'error': 'Rekonsiliasi ini sudah diselesaikan.'},
                            status=status.HTTP_400_BAD_REQUEST)

        upload = request.FILES.get('file')
        if upload is None:
            return Response({'file': 'Tidak ada file yang diunggah.'},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            result = statement_import.parse(
                upload,
                date_from=obj.statement_start,
                date_to=obj.statement_end,
            )
        except statement_import.StatementParseError as exc:
            return Response({'file': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(result)


class ReconciliationImportConfirmView(APIView):
    def post(self, request, pk):
        obj = _get(pk)
        if obj is None:
            return Response({'error': 'Rekonsiliasi tidak ditemukan.'},
                            status=status.HTTP_404_NOT_FOUND)
        if obj.is_locked:
            return Response({'error': 'Rekonsiliasi ini sudah diselesaikan.'},
                            status=status.HTTP_400_BAD_REQUEST)

        rows = request.data.get('rows') or []
        if not rows:
            return Response({'rows': 'Tidak ada baris untuk diimpor.'},
                            status=status.HTTP_400_BAD_REQUEST)

        replace = bool(request.data.get('replace'))

        created = 0
        with transaction.atomic():
            if replace:
                obj.lines.all().delete()

            start_order = obj.lines.count()
            batch = []
            for offset, row in enumerate(rows):
                amount = _decimal(row.get('amount'), '0')
                if not row.get('date') or amount == 0:
                    continue
                batch.append(BankStatementLine(
                    reconciliation=obj,
                    date=row['date'],
                    description=(row.get('description') or '')[:255],
                    reference=(row.get('reference') or '')[:100],
                    amount=amount,
                    sort_order=start_order + offset,
                ))
            BankStatementLine.objects.bulk_create(batch)
            created = len(batch)

            matched = recon.auto_match(obj)

        AuditLog.objects.create(
            performed_by=_actor(request),
            action='CREATE',
            entity_type='BankStatementLine',
            entity_id=str(obj.pk),
            description=f'{created} baris rekening koran diimpor ke rekonsiliasi '
                        f'#{obj.pk}; {matched} cocok otomatis',
        )

        return Response({
            'imported': created,
            'auto_matched': matched,
            'summary': _serialize_summary(recon.summary(obj)),
        }, status=status.HTTP_201_CREATED)


class ReconciliationAutoMatchView(APIView):
    def post(self, request, pk):
        obj = _get(pk)
        if obj is None:
            return Response({'error': 'Rekonsiliasi tidak ditemukan.'},
                            status=status.HTTP_404_NOT_FOUND)
        if obj.is_locked:
            return Response({'error': 'Rekonsiliasi ini sudah diselesaikan.'},
                            status=status.HTTP_400_BAD_REQUEST)

        matched = recon.auto_match(obj)
        return Response({
            'matched': matched,
            'summary': _serialize_summary(recon.summary(obj)),
        })


class ReconciliationLineActionView(APIView):
    def post(self, request, pk, line_pk, action):
        obj = _get(pk)
        if obj is None:
            return Response({'error': 'Rekonsiliasi tidak ditemukan.'},
                            status=status.HTTP_404_NOT_FOUND)

        line = obj.lines.filter(pk=line_pk).first()
        if line is None:
            return Response({'error': 'Baris tidak ditemukan.'},
                            status=status.HTTP_404_NOT_FOUND)

        try:
            if action == 'match':
                entry = LedgerEntry.objects.filter(pk=request.data.get('ledger_entry')).first()
                if entry is None:
                    return Response({'entry': 'Transaksi buku tidak ditemukan.'},
                                    status=status.HTTP_400_BAD_REQUEST)
                recon.match_line(obj, line, entry)
            elif action == 'unmatch':
                recon.unmatch_line(obj, line)
            elif action == 'ignore':
                if obj.is_locked:
                    raise recon.ReconciliationError(
                        {'error': 'Rekonsiliasi ini sudah diselesaikan.'})
                line.is_ignored = bool(request.data.get('ignored', True))
                if line.is_ignored:
                    line.ledger_entry = None
                    line.match_type = ''
                line.save(update_fields=['is_ignored', 'ledger_entry', 'match_type'])
            else:
                return Response({'error': f'Aksi "{action}" tidak dikenali.'},
                                status=status.HTTP_400_BAD_REQUEST)
        except recon.ReconciliationError as exc:
            return Response(exc.errors, status=status.HTTP_400_BAD_REQUEST)

        line.refresh_from_db()
        return Response({
            'line': BankStatementLineSerializer(line).data,
            'summary': _serialize_summary(recon.summary(obj)),
        })


class ReconciliationCompleteView(APIView):
    def post(self, request, pk):
        obj = _get(pk)
        if obj is None:
            return Response({'error': 'Rekonsiliasi tidak ditemukan.'},
                            status=status.HTTP_404_NOT_FOUND)
        try:
            with transaction.atomic():
                recon.complete(obj, _actor(request))
        except recon.ReconciliationError as exc:
            return Response(exc.errors, status=status.HTTP_400_BAD_REQUEST)

        AuditLog.objects.create(
            performed_by=_actor(request),
            action='STATUS_CHANGE',
            entity_type='BankReconciliation',
            entity_id=str(obj.pk),
            description=f'Rekonsiliasi {obj.account.name} '
                        f'{obj.statement_start} s/d {obj.statement_end} diselesaikan',
        )
        return Response(BankReconciliationDetailSerializer(_get(pk)).data)


class ReconciliationReopenView(APIView):
    def post(self, request, pk):
        obj = _get(pk)
        if obj is None:
            return Response({'error': 'Rekonsiliasi tidak ditemukan.'},
                            status=status.HTTP_404_NOT_FOUND)
        if not obj.is_locked:
            return Response({'error': 'Rekonsiliasi ini belum diselesaikan.'},
                            status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            recon.reopen(obj)

        AuditLog.objects.create(
            performed_by=_actor(request),
            action='STATUS_CHANGE',
            entity_type='BankReconciliation',
            entity_id=str(obj.pk),
            description=f'Rekonsiliasi #{obj.pk} dibuka kembali',
        )
        return Response(BankReconciliationDetailSerializer(_get(pk)).data)
