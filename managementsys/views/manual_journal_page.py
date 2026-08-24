"""
managementsys/views/manual_journal_page.py

Manual journal entry: compose a balanced entry by hand, have the system name the
transaction, and post it as a real JournalEntry.

Three endpoints:

* ``GET  /api/accounting/manual-journal/meta/``      window + transaction catalog
* ``POST /api/accounting/manual-journal/classify/``  dry run — balance + naming
* ``POST /api/accounting/manual-journal/``           post it

The rules (catalog, classifier, date window) live in
``services/manual_journal.py`` so the classify and post paths cannot disagree
about what an entry is or whether it is allowed. The write itself goes through
``journal_engine.write_legs``, the single ledger write path — that is what gives
the entry a header, keeps its lines attached to it, and rolls each account
balance in the direction its normal balance implies.
"""
import datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from ..api.serializers import JournalEntryDetailSerializer
from ..models import AppUser, AuditLog
from ..services import manual_journal as mj
from ..services.branches import write_branch
from ..services.journal_engine import LegSet, UnbalancedJournalError, write_legs

# Two decimal places, matching LedgerEntry.amount. Rounding here rather than
# letting the DB truncate keeps the balance check honest: an entry that balances
# on the operator's screen must balance in the ledger.
CENT = Decimal('0.01')


def _actor(request):
    return request.user if isinstance(request.user, AppUser) else None


def _today():
    """Django's active timezone, matching the rest of the accounting layer.

    The window is compared against ``JournalDayLog`` dates, and those — like
    void/edit memo dates and the correction journal's date — come from
    ``timezone.now().date()``. Using Jakarta here instead would put the manual
    entry's idea of "today" up to seven hours ahead of the journal run's, so an
    entry could be dated a day past anything the next sweep considers current.
    The clinic-facing sales reports do use Jakarta, but those only aggregate;
    this sets a ledger date.
    """
    return timezone.now().date()


def _parse_lines(raw):
    """Normalise the ``lines`` payload.

    Returns ``(lines, error)`` where each line is
    ``{'account': int, 'entry_type': str, 'amount': Decimal, 'description': str}``.
    Rows with neither an account nor an amount are dropped — the composer keeps
    a spare blank row on screen and submitting it should not be an error.
    """
    if not isinstance(raw, list):
        return None, 'Format baris tidak valid.'

    lines = []
    for idx, row in enumerate(raw, start=1):
        if not isinstance(row, dict):
            return None, f'Baris {idx}: format tidak valid.'

        account_raw = row.get('account')
        amount_raw = row.get('amount')
        blank_account = account_raw in (None, '', 0)
        blank_amount = amount_raw in (None, '', 0, '0')
        if blank_account and blank_amount:
            continue

        if blank_account:
            return None, f'Baris {idx}: rekening wajib dipilih.'
        try:
            account = int(account_raw)
        except (TypeError, ValueError):
            return None, f'Baris {idx}: rekening tidak valid.'

        entry_type = str(row.get('entry_type', '')).strip().lower()
        if entry_type not in ('debit', 'credit'):
            return None, f'Baris {idx}: pilih debit atau kredit.'

        try:
            amount = Decimal(str(amount_raw)).quantize(CENT)
        except (InvalidOperation, TypeError):
            return None, f'Baris {idx}: jumlah tidak valid.'
        if amount <= 0:
            return None, f'Baris {idx}: jumlah harus lebih dari nol.'

        lines.append({
            'account': account,
            'entry_type': entry_type,
            'amount': amount,
            'description': str(row.get('description') or '').strip(),
        })

    return lines, None


def _legs_for_classify(resolved):
    return [(l['account_obj'], l['entry_type'], l['amount']) for l in resolved]


def _classification_payload(resolved):
    legs = _legs_for_classify(resolved)
    hint = mj.balance_hint(legs)
    return {
        'balance': {
            'total_debit': str(hint['total_debit']),
            'total_credit': str(hint['total_credit']),
            'difference': str(hint['difference']),
            'is_balanced': hint['difference'] == 0,
            'needs_side': hint['needs_side'],
            'needs_amount': str(hint['needs_amount']),
        },
        'classification': mj.classify(legs),
    }


class ManualJournalMetaView(APIView):
    """GET /api/accounting/manual-journal/meta/

    Everything the composer needs before the operator types anything: the date
    window manual entries are allowed into, and the catalog of transaction shapes
    (which the guide page renders in full).
    """

    def get(self, request):
        return Response({
            'window': mj.window_payload(_today()),
            'transaction_types': mj.catalog(),
        })


class ManualJournalClassifyView(APIView):
    """POST /api/accounting/manual-journal/classify/
    Body: ``{ lines: [{ account, entry_type, amount, description }] }``

    Writes nothing. Returns the running debit/credit totals, what it would take
    to balance, and the name of the transaction the lines describe. The composer
    calls this as the operator edits so the second line can be filled in for
    them and the transaction named before they commit to it.
    """

    def post(self, request):
        lines, err = _parse_lines(request.data.get('lines'))
        if err:
            return Response({'error': err}, status=status.HTTP_400_BAD_REQUEST)
        if not lines:
            return Response({
                'balance': {
                    'total_debit': '0', 'total_credit': '0', 'difference': '0',
                    'is_balanced': False, 'needs_side': '', 'needs_amount': '0',
                },
                'classification': mj.classify([]),
            })

        resolved, err = mj.resolve_accounts(lines)
        if err:
            return Response({'error': err}, status=status.HTTP_400_BAD_REQUEST)

        return Response(_classification_payload(resolved))


class ManualJournalCreateView(APIView):
    """POST /api/accounting/manual-journal/
    Body: ``{ date, memo, lines: [{ account, entry_type, amount, description }] }``

    Posts a balanced manual entry as ``source_type='adjustment'``.

    Refused when: the date falls at or before the last journal run (or in the
    future), fewer than two lines are supplied, either side is empty, or debits
    do not equal credits. The balance check is here as well as in ``write_legs``
    so the operator gets the amounts back in the message rather than a bare
    exception.
    """

    def post(self, request):
        data = request.data

        memo = (data.get('memo') or '').strip()
        if not memo:
            return Response({'error': 'Keterangan wajib diisi.'},
                            status=status.HTTP_400_BAD_REQUEST)

        raw_date = (data.get('date') or '').strip()
        if not raw_date:
            return Response({'error': 'Tanggal wajib diisi.'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            entry_date = datetime.date.fromisoformat(raw_date)
        except ValueError:
            return Response({'error': 'Format tanggal tidak valid. Gunakan YYYY-MM-DD.'},
                            status=status.HTTP_400_BAD_REQUEST)

        date_err = mj.validate_date(entry_date, _today())
        if date_err:
            return Response({'error': date_err}, status=status.HTTP_400_BAD_REQUEST)

        lines, err = _parse_lines(data.get('lines'))
        if err:
            return Response({'error': err}, status=status.HTTP_400_BAD_REQUEST)
        if len(lines) < 2:
            return Response(
                {'error': 'Entri manual butuh minimal dua baris — satu debit dan '
                          'satu kredit — agar jurnal tetap seimbang.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        sides = {l['entry_type'] for l in lines}
        if sides != {'debit', 'credit'}:
            missing = 'kredit' if 'credit' not in sides else 'debit'
            return Response(
                {'error': f'Jurnal belum lengkap: tidak ada baris {missing}.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        resolved, err = mj.resolve_accounts(lines)
        if err:
            return Response({'error': err}, status=status.HTTP_400_BAD_REQUEST)

        payload = _classification_payload(resolved)
        if not payload['balance']['is_balanced']:
            return Response(
                {
                    'error': (
                        f"Jurnal tidak seimbang: debit "
                        f"{payload['balance']['total_debit']} vs kredit "
                        f"{payload['balance']['total_credit']} (selisih "
                        f"{payload['balance']['difference']})."
                    ),
                    **payload,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        classification = payload['classification']

        legset = LegSet(memo=memo)
        for line in resolved:
            legset.add(
                line['account_obj'],
                line['entry_type'],
                line['amount'],
                line['description'] or memo,
            )

        try:
            with transaction.atomic():
                entry = write_legs(
                    legset,
                    date=entry_date,
                    source_type='adjustment',
                    actor=_actor(request),
                    memo=memo,
                    # A manual entry has no source document to derive a branch
                    # from, so this is the one place the operator's selection
                    # decides it. Everything with a document ignores this
                    # argument — see journal_engine.document_branch_id.
                    branch_id=getattr(write_branch(request), 'pk', None),
                )
                AuditLog.objects.create(
                    performed_by=_actor(request),
                    action='CREATE',
                    entity_type='JournalEntry',
                    entity_id=str(entry.id),
                    description=(
                        f'Entri jurnal manual {entry.entry_number} ({entry_date}): '
                        f'{classification["name"] or "tidak dikenali"} — '
                        f'{len(resolved)} baris, Rp{legset.total_debit:,.2f}'
                    ),
                )
        except UnbalancedJournalError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                'entry': JournalEntryDetailSerializer(entry).data,
                'classification': classification,
            },
            status=status.HTTP_201_CREATED,
        )
