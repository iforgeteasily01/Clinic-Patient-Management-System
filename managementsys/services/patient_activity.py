"""Single source of truth for patient-activity classification.

Four surfaces read a patient's ``last_visit_date`` and turn it into a bucket:
the CRM list, the patient profile badge, patient search, and the patient
activity report. Four independent copies of the same date arithmetic is four
chances for the CRM page and the report to quietly disagree about which
patients are "lapsing" — this module is the one place that decides, per
docs/stock-movement-patient-activity-design.md §5.
"""
import datetime

# Bucket ids, in the order the design doc's table lists them (and the order a
# report summary row should be presented in).
BUCKETS = ('active', 'lapsing', 'inactive', 'never')

# Months → days at a flat 30 days/month, the same convention ReportSettings
# and the stock-movement classifier use everywhere — a fixed multiplier is
# what makes "inactive window must exceed active window" expressible as a
# plain integer comparison on the settings row.
DAYS_PER_MONTH = 30


def classify(last_visit_date: 'datetime.date | None', as_of: datetime.date, settings) -> str:
    """-> one of BUCKETS.

    ``settings`` is a ``ReportSettings`` instance (or anything exposing
    ``patient_active_months``/``patient_inactive_months``) so this module
    never has to import the model itself — callers pass
    ``ReportSettings.get_solo()``.
    """
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
