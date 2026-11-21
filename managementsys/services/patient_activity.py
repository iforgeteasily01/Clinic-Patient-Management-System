import datetime

BUCKETS = ('active', 'lapsing', 'inactive', 'never')

DAYS_PER_MONTH = 30


def classify(last_visit_date: 'datetime.date | None', as_of: datetime.date, settings) -> str:
    if last_visit_date is None:
        return 'never'

    days_since = (as_of - last_visit_date).days
    active_days = settings.patient_active_months * DAYS_PER_MONTH
    inactive_days = settings.patient_inactive_months * DAYS_PER_MONTH

    if days_since <= active_days:
        return 'active'
    if days_since <= inactive_days:
        return 'lapsing'
    return 'inactive'
