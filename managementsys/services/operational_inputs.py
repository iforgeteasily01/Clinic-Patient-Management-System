"""Period arithmetic for the operational-input templates.

Every question of the form "which period is this date in?", "when was that
period due?" and "which periods is the operator behind on?" is answered here
and nowhere else. The models store both a ``period_key`` and a
``period_start``; if two callers derived those independently they would
eventually disagree about which week a Sunday belongs to, and the uniqueness
constraint would start admitting duplicates for the same real week.

Monthly periods are keyed ``'2026-08'`` and start on the 1st. Weekly periods
are keyed by ISO week, ``'2026-W34'``, and start on the Monday — ISO, so the
key and the start date can never disagree about the year boundary the way
``%Y-%W`` would.

Dates here are civil dates in Asia/Jakarta. The clinic's "today" is the
operator's today, not UTC's: a task recorded at 06:00 Jakarta on the 1st must
not count as the previous month.
"""
import calendar
import datetime
from zoneinfo import ZoneInfo

from django.utils import timezone

JAKARTA_TZ = ZoneInfo('Asia/Jakarta')


def jakarta_today():
    """Today's civil date in the clinic's timezone."""
    return timezone.now().astimezone(JAKARTA_TZ).date()


# ── Period identity ──────────────────────────────────────────────────────────


def period_start_for(frequency, day):
    """The first day of the period ``day`` falls in."""
    if frequency == 'weekly':
        # isoweekday() is 1..7 with Monday=1, so this always lands on Monday.
        return day - datetime.timedelta(days=day.isoweekday() - 1)
    return day.replace(day=1)


def period_key_for(frequency, day):
    """The canonical key of the period ``day`` falls in."""
    if frequency == 'weekly':
        iso_year, iso_week, _ = day.isocalendar()
        return f'{iso_year}-W{iso_week:02d}'
    return f'{day.year}-{day.month:02d}'


def parse_period_key(frequency, key):
    """Inverse of :func:`period_key_for` — returns the period's start date.

    Raises ``ValueError`` on anything that is not a key this module would have
    produced for ``frequency``, so a malformed key from a client is rejected
    rather than silently filed under some other period.
    """
    key = (key or '').strip().upper()
    if frequency == 'weekly':
        year_part, _, week_part = key.partition('-W')
        if not week_part:
            raise ValueError(f'Bukan kunci periode mingguan: {key!r}')
        year, week = int(year_part), int(week_part)
        if not 1 <= week <= 53:
            raise ValueError(f'Nomor minggu di luar rentang: {key!r}')
        # ISO week 53 does not exist in every year; fromisocalendar raises for
        # those, which is the right answer.
        return datetime.date.fromisocalendar(year, week, 1)
    year_part, _, month_part = key.partition('-')
    if not month_part:
        raise ValueError(f'Bukan kunci periode bulanan: {key!r}')
    year, month = int(year_part), int(month_part)
    if not 1 <= month <= 12:
        raise ValueError(f'Bulan di luar rentang: {key!r}')
    return datetime.date(year, month, 1)


def next_period_start(frequency, period_start):
    """The start of the period following the one beginning ``period_start``."""
    if frequency == 'weekly':
        return period_start + datetime.timedelta(days=7)
    if period_start.month == 12:
        return datetime.date(period_start.year + 1, 1, 1)
    return datetime.date(period_start.year, period_start.month + 1, 1)


def previous_period_start(frequency, period_start):
    if frequency == 'weekly':
        return period_start - datetime.timedelta(days=7)
    if period_start.month == 1:
        return datetime.date(period_start.year - 1, 12, 1)
    return datetime.date(period_start.year, period_start.month - 1, 1)


def due_date_for(template, period_start):
    """When the operator was supposed to have recorded ``period_start``.

    Monthly ``due_day`` is clamped to the month's length, so a template due on
    the 31st is due on the 28th in February rather than raising or rolling into
    March. Weekly ``due_day`` is an ISO weekday offset from the Monday, clamped
    into the week so a bad value cannot push the due date into the next period.
    """
    if template.frequency == 'weekly':
        offset = min(max(int(template.due_day or 1), 1), 7) - 1
        return period_start + datetime.timedelta(days=offset)
    last = calendar.monthrange(period_start.year, period_start.month)[1]
    day = min(max(int(template.due_day or 1), 1), last)
    return period_start.replace(day=day)


def period_label(frequency, period_start):
    """Human label for a period, in Indonesian."""
    if frequency == 'weekly':
        end = period_start + datetime.timedelta(days=6)
        return f'{period_start.strftime("%d")}–{end.strftime("%d %b %Y")}'
    return f'{_MONTHS_ID[period_start.month - 1]} {period_start.year}'


_MONTHS_ID = [
    'Januari', 'Februari', 'Maret', 'April', 'Mei', 'Juni',
    'Juli', 'Agustus', 'September', 'Oktober', 'November', 'Desember',
]


def month_label_short(month):
    """'Agu' for 8. Column headers in the report are tight."""
    return _MONTHS_ID[month - 1][:3]


# ── Period sequences ─────────────────────────────────────────────────────────


def iter_period_starts(frequency, start, end):
    """Every period start in ``[start, end]``, oldest first.

    ``start`` and ``end`` are snapped to their containing periods, so callers
    can pass arbitrary dates.
    """
    cursor = period_start_for(frequency, start)
    limit = period_start_for(frequency, end)
    out = []
    while cursor <= limit:
        out.append(cursor)
        cursor = next_period_start(frequency, cursor)
    return out


def recent_period_starts(frequency, count, as_of=None):
    """The ``count`` most recent period starts up to and including ``as_of``'s.

    Returned oldest-first so a report can lay them out left to right.
    """
    as_of = as_of or jakarta_today()
    cursor = period_start_for(frequency, as_of)
    out = []
    for _ in range(max(count, 1)):
        out.append(cursor)
        cursor = previous_period_start(frequency, cursor)
    return list(reversed(out))


# ── Outstanding work ─────────────────────────────────────────────────────────


def outstanding_tasks(templates, entries_by_template, as_of=None, lookback=6):
    """Periods that are due but have no entry, oldest and most overdue first.

    ``entries_by_template`` maps template id → set of ``period_key``. Only
    periods whose due date has already passed count as outstanding: the current
    month is not "late" on the 2nd when rent is due on the 25th, and surfacing
    it as a task would train the operator to ignore the list.

    ``lookback`` bounds how far back a gap is still reported. Without it, every
    template would report a task for every period since the clinic opened the
    day it is created.
    """
    as_of = as_of or jakarta_today()
    tasks = []
    for template in templates:
        recorded = entries_by_template.get(template.id, frozenset())
        for period_start in recent_period_starts(template.frequency, lookback, as_of):
            key = period_key_for(template.frequency, period_start)
            if key in recorded:
                continue
            due = due_date_for(template, period_start)
            if due > as_of:
                continue
            tasks.append({
                'template_id': template.id,
                'template_name': template.name,
                'category': template.category,
                'frequency': template.frequency,
                'period_key': key,
                'period_start': period_start,
                'period_label': period_label(template.frequency, period_start),
                'due_date': due,
                'days_overdue': (as_of - due).days,
                # A string, like every other money field in this API: DRF's JSON
                # encoder would turn a bare Decimal into a float.
                'expected_amount': str(template.expected_amount),
            })
    # Most overdue first: that is the order the operator should work in.
    tasks.sort(key=lambda t: (-t['days_overdue'], t['template_name']))
    return tasks
