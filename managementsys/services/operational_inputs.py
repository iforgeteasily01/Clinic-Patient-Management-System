import calendar
import datetime
from zoneinfo import ZoneInfo

from django.utils import timezone

JAKARTA_TZ = ZoneInfo('Asia/Jakarta')


def jakarta_today():
    return timezone.now().astimezone(JAKARTA_TZ).date()


def period_start_for(frequency, day):
    if frequency == 'weekly':
        return day - datetime.timedelta(days=day.isoweekday() - 1)
    return day.replace(day=1)


def period_key_for(frequency, day):
    if frequency == 'weekly':
        iso_year, iso_week, _ = day.isocalendar()
        return f'{iso_year}-W{iso_week:02d}'
    return f'{day.year}-{day.month:02d}'


def parse_period_key(frequency, key):
    key = (key or '').strip().upper()
    if frequency == 'weekly':
        year_part, _, week_part = key.partition('-W')
        if not week_part:
            raise ValueError(f'Bukan kunci periode mingguan: {key!r}')
        year, week = int(year_part), int(week_part)
        if not 1 <= week <= 53:
            raise ValueError(f'Nomor minggu di luar rentang: {key!r}')
        return datetime.date.fromisocalendar(year, week, 1)
    year_part, _, month_part = key.partition('-')
    if not month_part:
        raise ValueError(f'Bukan kunci periode bulanan: {key!r}')
    year, month = int(year_part), int(month_part)
    if not 1 <= month <= 12:
        raise ValueError(f'Bulan di luar rentang: {key!r}')
    return datetime.date(year, month, 1)


def next_period_start(frequency, period_start):
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
    if template.frequency == 'weekly':
        offset = min(max(int(template.due_day or 1), 1), 7) - 1
        return period_start + datetime.timedelta(days=offset)
    last = calendar.monthrange(period_start.year, period_start.month)[1]
    day = min(max(int(template.due_day or 1), 1), last)
    return period_start.replace(day=day)


def period_label(frequency, period_start):
    if frequency == 'weekly':
        end = period_start + datetime.timedelta(days=6)
        return f'{period_start.strftime("%d")}–{end.strftime("%d %b %Y")}'
    return f'{_MONTHS_ID[period_start.month - 1]} {period_start.year}'


_MONTHS_ID = [
    'Januari', 'Februari', 'Maret', 'April', 'Mei', 'Juni',
    'Juli', 'Agustus', 'September', 'Oktober', 'November', 'Desember',
]


def month_label_short(month):
    return _MONTHS_ID[month - 1][:3]


def iter_period_starts(frequency, start, end):
    cursor = period_start_for(frequency, start)
    limit = period_start_for(frequency, end)
    out = []
    while cursor <= limit:
        out.append(cursor)
        cursor = next_period_start(frequency, cursor)
    return out


def recent_period_starts(frequency, count, as_of=None):
    as_of = as_of or jakarta_today()
    cursor = period_start_for(frequency, as_of)
    out = []
    for _ in range(max(count, 1)):
        out.append(cursor)
        cursor = previous_period_start(frequency, cursor)
    return list(reversed(out))


def outstanding_tasks(templates, entries_by_template, as_of=None, lookback=6):
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
                'expected_amount': str(template.expected_amount),
            })
    tasks.sort(key=lambda t: (-t['days_overdue'], t['template_name']))
    return tasks
