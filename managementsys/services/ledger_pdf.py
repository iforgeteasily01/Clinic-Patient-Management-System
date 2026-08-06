"""Server-side PDF rendering for the COA ledger — Feature C of
``docs/DESIGN_expense_redesign_and_coa_print.md`` §4.

Why this lives in ``services/`` and not in a view: reportlab imports are heavy
and the layout vocabulary (flowables, table styles, canvases) has nothing to do
with HTTP. The view parses/validates query params, pulls the rows from
``financial_reports_utils.ledger_rows_with_balance()`` and hands the finished
data here; this module never touches the request and never walks a balance.

Why reportlab and not WeasyPrint: WeasyPrint needs GTK/Cairo/Pango system
libraries. This project runs on Windows with no Docker (root ``CLAUDE.md``),
where that install is genuinely painful. reportlab is pure Python.

Reusability
-----------
``report_header()``, ``NumberedCanvas`` and the ``format_*`` helpers are
deliberately free of any ledger-specific knowledge — the Trial Balance and Cash
Flow reports have no print path yet and should build on exactly these pieces
rather than growing a second header block that drifts out of sync.
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import (
    CondPageBreak, LongTable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

# ── Locale ───────────────────────────────────────────────────────────────────
# Hardcoded on purpose: the server locale is not guaranteed to be id_ID and a
# report that silently renders "August" on one machine and "Agustus" on another
# is worse than no report.
MONTHS_ID = [
    'Januari', 'Februari', 'Maret', 'April', 'Mei', 'Juni',
    'Juli', 'Agustus', 'September', 'Oktober', 'November', 'Desember',
]
MONTHS_ID_SHORT = [
    'Jan', 'Feb', 'Mar', 'Apr', 'Mei', 'Jun',
    'Jul', 'Agu', 'Sep', 'Okt', 'Nov', 'Des',
]

# Fallback when SiteConfig has no clinic_name set. SiteConfig.clinic_name is the
# real source (see resolve_clinic_name below) — this only covers a fresh install
# where the admin has not filled in the receipt settings yet.
# TODO: remove once SiteConfig.clinic_name is enforced non-blank at setup.
DEFAULT_CLINIC_NAME = 'Klinik'

ZERO = Decimal('0')
EMPTY = '—'  # em dash, stands in for a zero/absent amount

_FONT = 'Helvetica'
_FONT_BOLD = 'Helvetica-Bold'
_FONT_ITALIC = 'Helvetica-Oblique'

_INK = colors.HexColor('#111827')
_MUTED = colors.HexColor('#6b7280')
_RULE = colors.HexColor('#d1d5db')
_BAND = colors.HexColor('#f3f4f6')
_DANGER = colors.HexColor('#dc2626')

ENTRY_TYPE_LABELS = {
    '': 'Semua transaksi',
    'debit': 'Hanya debit',
    'credit': 'Hanya kredit',
}


# ── Formatting helpers (single source of truth) ──────────────────────────────

def format_amount(value):
    """Indonesian money formatting: 1234567.5 -> '1.234.567,50'.

    The ONE place thousands/decimal separators are decided. Returns the em-dash
    placeholder for None so callers never have to special-case a missing value;
    a genuine zero still renders as '0,00'.
    """
    if value is None:
        return EMPTY
    try:
        v = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return EMPTY
    neg = v < 0
    s = f'{abs(v):,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')
    return f'-{s}' if neg else s


def _amount_cell(value):
    """Debit/Kredit cell: blank amounts and zeros read as '—', not '0,00'."""
    if value is None or Decimal(value) == ZERO:
        return EMPTY
    return format_amount(value)


def format_date_long(d):
    """05 Agustus 2026"""
    if not d:
        return ''
    return f'{d.day:02d} {MONTHS_ID[d.month - 1]} {d.year}'


def format_date_short(d):
    """05 Agu 2026"""
    if not d:
        return ''
    return f'{d.day:02d} {MONTHS_ID_SHORT[d.month - 1]} {d.year}'


def format_date_cell(d):
    """05/08 — the narrow Tgl column; the year lives in the period header."""
    if not d:
        return ''
    return f'{d.day:02d}/{d.month:02d}'


def format_month_long(d):
    """Agustus 2026"""
    if not d:
        return ''
    return f'{MONTHS_ID[d.month - 1]} {d.year}'


def format_datetime(dt):
    """05 Agu 2026 14:22"""
    if not dt:
        return ''
    return f'{format_date_short(dt)} {dt.hour:02d}:{dt.minute:02d}'


def period_label(date_from, date_to):
    if date_from and date_to:
        return f'{format_date_short(date_from)} – {format_date_short(date_to)}'
    if date_from:
        return f'Sejak {format_date_short(date_from)}'
    if date_to:
        return f'Sampai {format_date_short(date_to)}'
    return 'Seluruh periode'


def resolve_clinic_name():
    """Clinic name for the report banner.

    Source of truth is ``SiteConfig.clinic_name`` (the singleton behind the
    receipt/branding settings — the same field ``reports_page.py`` prints on the
    Excel reports). Read-only on purpose: ``SiteConfig.get_solo()`` would
    get_or_create, and printing a report should never write a row.
    """
    try:
        from ..models import SiteConfig
        cfg = SiteConfig.objects.only('clinic_name').first()
    except Exception:  # pragma: no cover - DB unavailable / app not loaded
        cfg = None
    return (getattr(cfg, 'clinic_name', '') or '').strip() or DEFAULT_CLINIC_NAME


def ledger_ref(entry):
    """The Ref column: whatever identifier the row actually carries.

    Built here rather than read off ``LedgerEntrySerializer`` because the PDF
    wants one short human string, not four nullable fields. Order matters —
    an entry has at most one of these relations, so first hit wins.
    """
    inv = getattr(entry, 'invoice', None)
    if inv is not None:
        return str(getattr(inv, 'invoice_number', '') or f'INV-{inv.pk}')

    pur = getattr(entry, 'purchase_invoice', None)
    if pur is not None:
        return str(getattr(pur, 'internal_id', '') or f'PI-{pur.pk}')

    trf = getattr(entry, 'transfer', None)
    if trf is not None:
        return f'TRF-{trf.pk}'

    exp = getattr(entry, 'expense', None)
    if exp is not None:
        return f'EXP-{exp.pk}'

    return ''


# ── Generic building blocks (shared with future reports) ─────────────────────

class NumberedCanvas(pdf_canvas.Canvas):
    """Two-pass page numbering: 'Hal. 3 dari 7'.

    A page footer cannot know the total page count while that page is being
    laid out, so every page state is buffered and only flushed in ``save()``,
    once the total is known. Standard reportlab recipe; kept generic so the
    Trial Balance / Cash Flow PDFs can pass the same ``canvasmaker``.
    """

    _FOOTER_MARGIN_X = 15 * mm
    _FOOTER_MARGIN_Y = 10 * mm

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self._draw_page_number(total)
            super().showPage()
        super().save()

    def _draw_page_number(self, total):
        self.setFont(_FONT, 7.5)
        self.setFillColor(_MUTED)
        self.drawRightString(
            self._pagesize[0] - self._FOOTER_MARGIN_X,
            self._FOOTER_MARGIN_Y,
            f'Hal. {self._pageNumber} dari {total}',
        )


_STYLE_CLINIC = ParagraphStyle('clinic', fontName=_FONT_BOLD, fontSize=13, leading=16, textColor=_INK)
_STYLE_TITLE = ParagraphStyle('title', fontName=_FONT_BOLD, fontSize=11, leading=14, textColor=_INK, spaceBefore=2)
_STYLE_SUBTITLE = ParagraphStyle('subtitle', fontName=_FONT, fontSize=9, leading=12, textColor=_MUTED)
_STYLE_WARNING = ParagraphStyle('warning', fontName=_FONT_BOLD, fontSize=8.5, leading=11, textColor=_DANGER)
_STYLE_META = ParagraphStyle('meta', fontName=_FONT, fontSize=8, leading=10.5, textColor=_INK)
_STYLE_GROUP = ParagraphStyle('group', fontName=_FONT_BOLD, fontSize=9, leading=12, textColor=_INK)
_STYLE_CELL = ParagraphStyle('cell', fontName=_FONT, fontSize=7.5, leading=9.5, textColor=_INK)


def report_header(clinic, title, subtitle='', meta=(), warning='', width=None):
    """Flowables for the banner every printed report shares.

    ``meta`` is a sequence of ``(label, value)`` pairs rendered as an aligned
    two-column block ("Akun : Bank BCA"); blank values are dropped. ``warning``
    renders in red directly under the subtitle — reserved for facts that change
    how the numbers should be read, not for decoration.

    Ledger-agnostic: pass whatever label/value pairs the report needs.
    """
    flow = [Paragraph(clinic or DEFAULT_CLINIC_NAME, _STYLE_CLINIC)]
    if title:
        flow.append(Paragraph(title, _STYLE_TITLE))
    if subtitle:
        flow.append(Paragraph(subtitle, _STYLE_SUBTITLE))

    pairs = [(str(label), str(value)) for label, value in meta if str(value or '').strip()]
    if pairs:
        label_w = 24 * mm
        value_w = (width - label_w) if width else 120 * mm
        rows = [[Paragraph(f'{lbl}', _STYLE_META), Paragraph(f': {val}', _STYLE_META)] for lbl, val in pairs]
        block = Table(rows, colWidths=[label_w, value_w], hAlign='LEFT')
        block.setStyle(TableStyle([
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
            ('TOPPADDING', (0, 0), (-1, -1), 0.5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0.5),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))
        flow += [Spacer(1, 4), block]

    if warning:
        flow += [Spacer(1, 4), Paragraph(warning, _STYLE_WARNING)]

    flow.append(Spacer(1, 8))
    return flow


# ── Ledger-specific layout ───────────────────────────────────────────────────

_COLUMNS = ['Tgl', 'Keterangan', 'Ref', 'Debit', 'Kredit', 'Saldo']
# Proportions of the printable width, with and without the Saldo column.
_WIDTHS_WITH_SALDO = [0.075, 0.360, 0.145, 0.130, 0.130, 0.160]
_WIDTHS_NO_SALDO = [0.075, 0.480, 0.165, 0.140, 0.140]


def _col_widths(total_width, show_running):
    props = _WIDTHS_WITH_SALDO if show_running else _WIDTHS_NO_SALDO
    return [total_width * p for p in props]


def _table_style(subtotal_rows=()):
    cmds = [
        ('FONTNAME', (0, 0), (-1, 0), _FONT_BOLD),
        ('FONTSIZE', (0, 0), (-1, 0), 7.5),
        ('TEXTCOLOR', (0, 0), (-1, 0), _INK),
        ('BACKGROUND', (0, 0), (-1, 0), _BAND),
        ('FONTNAME', (0, 1), (-1, -1), _FONT),
        ('FONTSIZE', (0, 1), (-1, -1), 7.5),
        ('ALIGN', (3, 0), (-1, -1), 'RIGHT'),
        ('ALIGN', (0, 0), (0, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LINEBELOW', (0, 0), (-1, 0), 0.6, _RULE),
        ('LINEBELOW', (0, 1), (-1, -1), 0.25, _RULE),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ]
    for r in subtotal_rows:
        cmds += [
            ('FONTNAME', (0, r), (-1, r), _FONT_BOLD),
            ('BACKGROUND', (0, r), (-1, r), _BAND),
            ('SPAN', (0, r), (2, r)),
        ]
    return TableStyle(cmds)


def _entry_row(entry, show_running):
    cells = [
        format_date_cell(entry.date),
        Paragraph(_escape(getattr(entry, 'description', '') or ''), _STYLE_CELL),
        Paragraph(_escape(ledger_ref(entry)), _STYLE_CELL),
        _amount_cell(entry.amount if entry.entry_type == 'debit' else None),
        _amount_cell(entry.amount if entry.entry_type == 'credit' else None),
    ]
    if show_running:
        cells.append(format_amount(getattr(entry, 'running_balance', None)))
    return cells


def _escape(text):
    """Paragraph parses a mini-HTML dialect — a stray '&' or '<' in a memo
    would otherwise blow up the whole render."""
    return (
        str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    )


def _banner_row(label, amount, n_cols, italic=False):
    """A full-width label + one right-aligned amount in the last column,
    laid out on the ledger's own grid so it lines up with the table above."""
    row = [label] + [''] * (n_cols - 1)
    row[-1] = format_amount(amount)
    style = [
        ('SPAN', (0, 0), (n_cols - 2, 0)),
        ('FONTNAME', (0, 0), (-1, 0), _FONT_ITALIC if italic else _FONT_BOLD),
        ('FONTSIZE', (0, 0), (-1, 0), 8),
        ('ALIGN', (-1, 0), (-1, 0), 'RIGHT'),
        ('BACKGROUND', (0, 0), (-1, 0), _BAND),
        ('TOPPADDING', (0, 0), (-1, 0), 4),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('LINEBELOW', (0, 0), (-1, 0), 0.5, _RULE),
    ]
    return row, style


def _group_rows(rows, group_by):
    """-> [(label_or_None, [rows])] in ascending order.

    ``rows`` is already ascending (ledger_rows_with_balance guarantees it), so a
    simple run-length grouping preserves order without re-sorting.
    """
    if group_by not in ('day', 'month'):
        return [(None, list(rows))]

    groups = []
    key = object()
    for r in rows:
        k = r.date if group_by == 'day' else (r.date.year, r.date.month)
        if not groups or k != key:
            label = format_date_long(r.date) if group_by == 'day' else format_month_long(r.date)
            groups.append((label, []))
            key = k
        groups[-1][1].append(r)
    return groups


def build_ledger_pdf(account, rows, opening, closing, totals, opts) -> bytes:
    """Render one account's ledger to PDF bytes.

    ``account``  ChartOfAccounts (needs ``name`` and ``account_number``)
    ``rows``     ascending LedgerEntry list, each with ``running_balance``
    ``opening``  natural-signed balance before ``date_from``
    ``closing``  natural-signed balance after the last entry in the window
    ``totals``   mapping with ``debit``/``credit`` (a 2-tuple also works)
    ``opts``     mapping; every key optional —
                 ``date_from``, ``date_to``, ``entry_type``, ``title``,
                 ``subtitle``, ``group_by`` (none|day|month),
                 ``page_break`` (none|group), ``show_opening``,
                 ``show_running``, ``show_subtotals``,
                 ``orientation`` (portrait|landscape), ``printed_by``,
                 ``printed_at``, ``unposted_count``, ``clinic_name``.

    The caller owns validation. This function renders whatever it is given.
    """
    o = dict(opts or {})
    date_from = o.get('date_from')
    date_to = o.get('date_to')
    group_by = o.get('group_by') or 'none'
    page_break = o.get('page_break') or 'none'
    show_opening = o.get('show_opening', True)
    show_running = o.get('show_running', True)
    show_subtotals = o.get('show_subtotals', True)
    is_landscape = (o.get('orientation') or 'portrait') == 'landscape'
    title = o.get('title') or 'Buku Besar'
    subtitle = o.get('subtitle') or ''
    printed_by = o.get('printed_by') or ''
    printed_at = o.get('printed_at') or datetime.now()
    unposted_count = int(o.get('unposted_count') or 0)
    clinic = o.get('clinic_name') or resolve_clinic_name()

    if isinstance(totals, dict):
        total_debit = totals.get('debit', ZERO)
        total_credit = totals.get('credit', ZERO)
    else:
        total_debit, total_credit = (list(totals) + [ZERO, ZERO])[:2]

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4) if is_landscape else A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=13 * mm, bottomMargin=16 * mm,
        title=f'{title} - {account.name}',
        author=clinic,
        subject=subtitle or 'Buku Besar',
    )

    n_cols = 6 if show_running else 5
    widths = _col_widths(doc.width, show_running)
    header_cells = _COLUMNS if show_running else _COLUMNS[:-1]

    warning = ''
    if unposted_count > 0:
        warning = f'Perhatian: {unposted_count} tanggal dalam rentang ini belum dijurnal.'

    story = report_header(
        clinic, title, subtitle,
        meta=[
            ('Akun', account.name),                       # name first, deliberately
            ('No. Akun', account.account_number),
            ('Periode', period_label(date_from, date_to)),
            ('Filter', ENTRY_TYPE_LABELS.get(o.get('entry_type') or '', 'Semua transaksi')),
            ('Dicetak', f'{format_datetime(printed_at)}'
                        + (f' oleh {printed_by}' if printed_by else '')),
        ],
        warning=warning,
        width=doc.width,
    )

    if show_opening:
        label = ('Saldo Awal per ' + format_date_long(date_from)) if date_from else 'Saldo Awal'
        row, style = _banner_row(label, opening, n_cols, italic=True)
        t = Table([row], colWidths=widths, hAlign='LEFT')
        t.setStyle(TableStyle(style))
        story += [t, Spacer(1, 6)]

    blocks = []
    if rows:
        for label, grows in _group_rows(rows, group_by):
            block = []
            if label:
                # Keep a group heading off the very bottom of a page — a heading
                # stranded above a page break reads as an empty group.
                block += [
                    CondPageBreak(24 * mm),
                    Paragraph(f'—— {_escape(label)} ——', _STYLE_GROUP),
                    Spacer(1, 3),
                ]

            data = [header_cells] + [_entry_row(e, show_running) for e in grows]
            subtotal_rows = ()
            if label and show_subtotals:
                g_debit = sum((e.amount for e in grows if e.entry_type == 'debit'), ZERO)
                g_credit = sum((e.amount for e in grows if e.entry_type == 'credit'), ZERO)
                sub = [f'Subtotal {label}', '', '', _amount_cell(g_debit), _amount_cell(g_credit)]
                if show_running:
                    sub.append(format_amount(getattr(grows[-1], 'running_balance', None)))
                data.append(sub)
                subtotal_rows = (len(data) - 1,)

            table = LongTable(data, colWidths=widths, repeatRows=1, hAlign='LEFT')
            table.setStyle(_table_style(subtotal_rows))
            block.append(table)
            blocks.append(block)
    else:
        empty = [''] * n_cols
        empty[1] = Paragraph('Tidak ada transaksi pada rentang ini.', _STYLE_CELL)
        table = LongTable([header_cells, empty], colWidths=widths, repeatRows=1, hAlign='LEFT')
        table.setStyle(_table_style())
        blocks = [[table]]

    for i, block in enumerate(blocks):
        if i:
            # A fresh flowable per gap: platypus mutates flowables during layout,
            # so one shared instance cannot be reused across the story.
            story.append(PageBreak() if page_break == 'group' else Spacer(1, 10))
        story.extend(block)

    story.append(Spacer(1, 8))

    total_row = ['TOTAL', '', '', _amount_cell(total_debit), _amount_cell(total_credit)]
    if show_running:
        total_row.append('')
    foot = [total_row]
    foot_style = [
        ('SPAN', (0, 0), (2, 0)),
        ('FONTNAME', (0, 0), (-1, 0), _FONT_BOLD),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('ALIGN', (3, 0), (-1, -1), 'RIGHT'),
        ('BACKGROUND', (0, 0), (-1, 0), _BAND),
        ('LINEABOVE', (0, 0), (-1, 0), 0.8, _INK),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ]
    closing_label = ('Saldo Akhir per ' + format_date_long(date_to)) if date_to else 'Saldo Akhir'
    crow = [closing_label] + [''] * (n_cols - 1)
    crow[-1] = format_amount(closing)
    foot.append(crow)
    foot_style += [
        ('SPAN', (0, 1), (n_cols - 2, 1)),
        ('FONTNAME', (0, 1), (-1, 1), _FONT_BOLD),
        ('ALIGN', (-1, 1), (-1, 1), 'RIGHT'),
        ('LINEABOVE', (0, 1), (-1, 1), 0.4, _RULE),
    ]
    ftable = Table(foot, colWidths=widths, hAlign='LEFT')
    ftable.setStyle(TableStyle(foot_style))
    story.append(ftable)

    # multiBuild + NumberedCanvas: the canvas buffers pages so the footer can
    # print a total that is only known once the last page is laid out.
    doc.multiBuild(story, canvasmaker=NumberedCanvas)
    return buf.getvalue()
