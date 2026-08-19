"""CRM dashboard, CRM patient profile, and WhatsApp message templates.

Scope boundary with the rest of the app
---------------------------------------
``/api/crm/patients/`` (crm_page.py) is the *patient directory* — one row per
patient, sortable, paginated, the thing the old CRM page showed. It moved to
``/patients`` in the UI and is untouched here.

This module is the *relationship* view: who came recently, who is worth calling,
and what one patient's history looks like when the accounting is stripped off
it. Nothing here writes to the ledger, creates a document, or sends a message.

Two conventions worth stating because they are easy to get wrong later:

* **Jakarta, not UTC.** Every window here is a clinic day as staff experience
  it, so days are cut in ``Asia/Jakarta`` (models.JAKARTA_TZ) — the same
  convention as reports_page.py. The accounting layer's ``timezone.now().date()``
  would put a 22:30 WIB checkout on the previous day.
* **A visit is a non-voided invoice**, matching ``refresh_crm_profile`` so this
  page and the directory can never disagree about a visit count. Patients still
  in the queue today have no invoice yet, so today's list unions in open
  ``ActivePatient`` rows and marks them ``in_clinic`` — without that, the
  dashboard is empty every morning until the first checkout.
"""
import datetime
import logging
from decimal import Decimal

from django.db.models import (
    Count, DecimalField, ExpressionWrapper, F, Max, Q, Sum, Value,
)
from django.db.models.functions import ExtractDay, ExtractMonth
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import (
    ActivePatient,
    AppUser,
    Invoice,
    InvoiceItem,
    JAKARTA_TZ,
    MedRec,
    MessageTemplate,
    Patient,
    PatientPackage,
    ReportSettings,
    TreatmentSession,
)
from ..services import message_templates as mt
from ..services.patient_activity import classify

logger = logging.getLogger(__name__)

WINDOW_DAYS = 30
RECENT_DAYS = 7
BIRTHDAY_LOOKAHEAD_DAYS = 30

# Age bands for the demographic split. Open-ended at both ends so every patient
# with a birth_date lands in exactly one.
AGE_BANDS = [('<20', 0, 19), ('20-29', 20, 29), ('30-39', 30, 39),
             ('40-49', 40, 49), ('50+', 50, 200)]

_MONEY = DecimalField(max_digits=18, decimal_places=2)

# Net revenue of one invoice line. InvoiceItem stores a per-line percentage
# discount, so summing `price` alone overstates every discounted line.
_LINE_REVENUE = ExpressionWrapper(
    F('price') * F('quantity') * (Value(Decimal('1')) - F('discount_pct') / Value(Decimal('100'))),
    output_field=_MONEY,
)

# The multiplication above widens the scale (Postgres keeps every digit of
# price * quantity * ratio), so anything serialized out of it is rounded back
# to rupiah-with-cents rather than shipping a 25-decimal string to the browser.
_RUPIAH = Decimal('0.01')


def _money(value):
    return str((value or Decimal('0')).quantize(_RUPIAH))


# ── Time helpers ───────────────────────────────────────────────────────────

def _today():
    return timezone.now().astimezone(JAKARTA_TZ).date()


def _day_start(d):
    return datetime.datetime.combine(d, datetime.time.min, tzinfo=JAKARTA_TZ)


def _visits_between(start, end_exclusive):
    """Non-voided, patient-attached invoices in a half-open Jakarta day range."""
    return Invoice.objects.filter(
        is_voided=False,
        patient_no__isnull=False,
        datetime__gte=_day_start(start),
        datetime__lt=_day_start(end_exclusive),
    )


def _pct_delta(current, previous):
    """Percentage change, or None when there is no baseline to compare against.

    None rather than 0 or 100: a metric that went from nothing to something has
    no meaningful percentage, and rendering "+100%" there invites the operator
    to read a first month of data as growth.
    """
    if not previous:
        return None
    return round((float(current) - float(previous)) / float(previous) * 100, 1)


def _empty_recent_entry(patient):
    return {
        'patient': patient,
        'visit_dates': set(),
        'visits': 0,
        'spend': Decimal('0'),
        'last_dt': None,
        'last_treatments': [],
        'last_products': [],
        'in_clinic': False,
    }


# ── Dashboard ──────────────────────────────────────────────────────────────

class CRMDashboardView(APIView):
    """GET /api/crm/dashboard/

    One request, because every tile on the page is a different cut of the same
    two windows and splitting it would mean re-scanning invoices per tile.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        today = _today()
        tomorrow = today + datetime.timedelta(days=1)
        window_start = today - datetime.timedelta(days=WINDOW_DAYS - 1)
        prev_start = window_start - datetime.timedelta(days=WINDOW_DAYS)
        recent_start = today - datetime.timedelta(days=RECENT_DAYS - 1)

        settings_obj = ReportSettings.get_solo()

        # ── Window aggregates ──────────────────────────────────────────────
        cur_rows = list(
            _visits_between(window_start, tomorrow)
            .values('patient_no')
            .annotate(visits=Count('id'), spend=Sum('grand_total'))
        )
        prev_rows = list(
            _visits_between(prev_start, window_start)
            .values('patient_no')
            .annotate(visits=Count('id'), spend=Sum('grand_total'))
        )

        cur_ids = {r['patient_no'] for r in cur_rows}
        prev_ids = {r['patient_no'] for r in prev_rows}

        cur_visits = sum(r['visits'] for r in cur_rows)
        prev_visits = sum(r['visits'] for r in prev_rows)
        cur_spend = sum((r['spend'] or Decimal('0')) for r in cur_rows)
        prev_spend = sum((r['spend'] or Decimal('0')) for r in prev_rows)

        # New vs returning. "New" means no non-voided invoice anywhere before
        # the window opened — a patient registered years ago who is only now
        # buying something is new revenue, not a returning one.
        seen_before = set(
            Invoice.objects
            .filter(is_voided=False, patient_no__isnull=False,
                    datetime__lt=_day_start(window_start))
            .values_list('patient_no', flat=True)
            .distinct()
        )
        new_ids = cur_ids - seen_before
        returning_ids = cur_ids & seen_before

        # Retention: of the patients who came in the *previous* window, how many
        # came back in this one. The denominator is deliberately the previous
        # window and not all patients — it answers "are the people we saw last
        # month still coming", which is the decision this page exists for.
        retained = len(prev_ids & cur_ids)
        retention_rate = round(retained / len(prev_ids) * 100, 1) if prev_ids else None

        repeat_patients = sum(1 for r in cur_rows if r['visits'] > 1)

        # ── Activity buckets across the whole book ─────────────────────────
        buckets = {'active': 0, 'lapsing': 0, 'inactive': 0, 'never': 0}
        for last_visit in Patient.objects.values_list('crm_profile__last_visit_date', flat=True):
            buckets[classify(last_visit, today, settings_obj)] += 1

        # ── Demographics ───────────────────────────────────────────────────
        # Both fields arrived in migration 0110, so coverage is reported
        # alongside every split — a 70% female clinic and a 70% unfilled form
        # look identical without it.
        total_patients = Patient.objects.count()
        gender = {'F': 0, 'M': 0, 'unknown': 0}
        for row in Patient.objects.values('gender').annotate(n=Count('patient_no')):
            gender[row['gender'] if row['gender'] in ('F', 'M') else 'unknown'] += row['n']

        age_counts = {label: 0 for label, _, _ in AGE_BANDS}
        dated = 0
        for birth in Patient.objects.filter(birth_date__isnull=False).values_list('birth_date', flat=True):
            age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
            dated += 1
            for label, lo, hi in AGE_BANDS:
                if lo <= age <= hi:
                    age_counts[label] += 1
                    break

        # ── Top treatments and products in the window ──────────────────────
        line_rows = (
            InvoiceItem.objects
            .filter(
                invoice__is_voided=False,
                invoice__datetime__gte=_day_start(window_start),
                invoice__datetime__lt=_day_start(tomorrow),
            )
            .values('item__is_service', 'item_name', 'item__name')
            .annotate(
                qty=Sum('quantity'),
                revenue=Sum(_LINE_REVENUE),
                lines=Count('id'),
            )
        )
        treatments, products = [], []
        for row in line_rows:
            entry = {
                'name': row['item__name'] or row['item_name'] or '—',
                'quantity': float(row['qty'] or 0),
                'revenue': _money(row['revenue']),
                'lines': row['lines'],
            }
            (treatments if row['item__is_service'] else products).append(entry)
        treatments.sort(key=lambda e: Decimal(e['revenue']), reverse=True)
        products.sort(key=lambda e: Decimal(e['revenue']), reverse=True)

        return Response({
            'as_of': str(today),
            'window_days': WINDOW_DAYS,
            'recent_days': RECENT_DAYS,
            'summary': {
                'patients_last_30d':   len(cur_ids),
                'patients_prev_30d':   len(prev_ids),
                'patients_delta_pct':  _pct_delta(len(cur_ids), len(prev_ids)),
                'visits_last_30d':     cur_visits,
                'visits_delta_pct':    _pct_delta(cur_visits, prev_visits),
                'revenue_last_30d':    str(cur_spend),
                'revenue_delta_pct':   _pct_delta(cur_spend, prev_spend),
                'avg_spend_per_visit': str((cur_spend / cur_visits).quantize(Decimal('1')) if cur_visits else 0),
                'avg_visits_per_patient': round(cur_visits / len(cur_ids), 2) if cur_ids else 0,
                'new_patients':        len(new_ids),
                'returning_patients':  len(returning_ids),
                'repeat_patients':     repeat_patients,
                'retention_rate':      retention_rate,
                'total_patients':      total_patients,
            },
            'activity': buckets,
            'demographics': {
                'gender': gender,
                'gender_known_pct': (round((gender['F'] + gender['M']) / total_patients * 100, 1)
                                     if total_patients else 0),
                'age_bands': [{'label': k, 'count': v} for k, v in age_counts.items()],
                'age_known_pct': round(dated / total_patients * 100, 1) if total_patients else 0,
            },
            'top_treatments': treatments[:10],
            'top_products':   products[:10],
            'upcoming_birthdays': _upcoming_birthdays(today, BIRTHDAY_LOOKAHEAD_DAYS),
            'recent_patients': _recent_patients(recent_start, today, settings_obj),
        })


def _upcoming_birthdays(today, lookahead):
    """Patients whose birthday falls in the next ``lookahead`` days.

    Filtered on (month, day) in the database rather than pulling every dated
    patient into Python. When the window runs past 31 December the two
    day-of-year ranges are asked for separately and OR-ed.
    """
    end = today + datetime.timedelta(days=lookahead)
    qs = Patient.objects.filter(birth_date__isnull=False).annotate(
        bmonth=ExtractMonth('birth_date'), bday=ExtractDay('birth_date'),
    )

    def on_or_after(d):
        return Q(bmonth__gt=d.month) | Q(bmonth=d.month, bday__gte=d.day)

    def on_or_before(d):
        return Q(bmonth__lt=d.month) | Q(bmonth=d.month, bday__lte=d.day)

    if end.year == today.year:
        qs = qs.filter(on_or_after(today) & on_or_before(end))
    else:
        qs = qs.filter(on_or_after(today) | on_or_before(end))

    out = []
    for p in qs[:200]:
        out.append({
            'patient_no': p.patient_no,
            'name': p.name,
            'phone_number': p.phone_number,
            'birth_date': str(p.birth_date),
            'turns': _next_birthday(p.birth_date, today).year - p.birth_date.year,
            'days_until': (_next_birthday(p.birth_date, today) - today).days,
            'date_label': mt.format_date_id(p.birth_date, with_year=False),
        })
    out.sort(key=lambda e: e['days_until'])
    return out


def _next_birthday(birth_date, today):
    """The next occurrence of ``birth_date``'s day on or after ``today``.

    A 29 February birthday is observed on 1 March in a common year — the clinic
    has to greet those patients on some specific day, and skipping three years
    out of four is not it.
    """
    def occurrence(year):
        try:
            return birth_date.replace(year=year)
        except ValueError:
            return datetime.date(year, 3, 1)

    this_year = occurrence(today.year)
    return this_year if this_year >= today else occurrence(today.year + 1)


def _recent_patients(recent_start, today, settings_obj):
    """Unique patients seen in the last RECENT_DAYS, today's flagged.

    The union the CRM page asks for: everyone who came in the window, deduped,
    with ``visited_today`` set for those here today. Patients currently in the
    queue with no invoice yet are folded in from ActivePatient and marked
    ``in_clinic`` — they have no spend to report and that is not missing data.
    """
    tomorrow = today + datetime.timedelta(days=1)
    invoices = (
        _visits_between(recent_start, tomorrow)
        .select_related('patient_no__crm_profile__tier')
        .prefetch_related('items__item')
        .order_by('datetime')
    )

    by_patient = {}
    for inv in invoices:
        patient = inv.patient_no
        local_dt = inv.datetime.astimezone(JAKARTA_TZ)
        entry = by_patient.setdefault(patient.patient_no, _empty_recent_entry(patient))
        entry['visits'] += 1
        entry['spend'] += inv.grand_total or Decimal('0')
        entry['visit_dates'].add(local_dt.date())
        # Ordered by datetime ascending, so the last write wins and the two
        # lists below always describe the most recent visit in the window.
        entry['last_dt'] = local_dt
        entry['last_treatments'] = [
            li.item.name for li in inv.items.all() if li.item_id and li.item.is_service
        ]
        entry['last_products'] = [
            (li.item.name if li.item_id else li.item_name)
            for li in inv.items.all() if not (li.item_id and li.item.is_service)
        ]

    # Patients in the queue right now, invoice or not.
    open_visits = (
        ActivePatient.objects
        .filter(patient_no__isnull=False, visit_time__gte=_day_start(today))
        .select_related('patient_no__crm_profile__tier')
    )
    for visit in open_visits:
        patient = visit.patient_no
        entry = by_patient.setdefault(patient.patient_no, _empty_recent_entry(patient))
        entry['in_clinic'] = True
        entry['visit_dates'].add(today)
        local_dt = visit.visit_time.astimezone(JAKARTA_TZ)
        if entry['last_dt'] is None or local_dt > entry['last_dt']:
            entry['last_dt'] = local_dt

    rows = []
    for entry in by_patient.values():
        patient = entry['patient']
        crm = getattr(patient, 'crm_profile', None)
        last_visit_date = crm.last_visit_date if crm else None
        rows.append({
            'patient_no': patient.patient_no,
            'name': patient.name,
            'phone_number': patient.phone_number,
            'tier': ({'name': crm.tier.name, 'color_hex': crm.tier.color_hex}
                     if crm and crm.tier_id else None),
            'visited_today': today in entry['visit_dates'],
            'in_clinic': entry['in_clinic'],
            'visits_in_window': entry['visits'],
            'distinct_days_in_window': len(entry['visit_dates']),
            'spend_in_window': str(entry['spend']),
            'last_visit_at': entry['last_dt'].isoformat() if entry['last_dt'] else None,
            'last_treatments': entry['last_treatments'],
            'last_products': entry['last_products'],
            'total_visits': crm.total_visits if crm else 0,
            'total_spend': str(crm.total_spend) if crm else '0.00',
            'activity_bucket': classify(last_visit_date, today, settings_obj),
            'birth_date': str(patient.birth_date) if patient.birth_date else None,
        })

    # Today first, then most recent — the operator works top-down through the
    # people who are here now before chasing the rest of the week.
    rows.sort(key=lambda r: (r['visited_today'], r['last_visit_at'] or ''), reverse=True)
    return rows


# ── CRM patient profile ────────────────────────────────────────────────────

class CRMPatientProfileView(APIView):
    """GET /api/crm/patients/<patient_no>/profile/

    Deliberately *not* the same shape as PatientProfilePage's data. That page is
    the transactional record — full invoice lines, payment methods, totals, SOAP
    notes. This one answers "what do we know about this person and what have we
    actually done to them", so each visit collapses to a date, the treatments
    performed and the products taken home. Money is summarised per visit, never
    itemised: a line-by-line receipt is one click away on the patient page and
    reproducing it here just makes the history harder to read.
    """
    permission_classes = [AllowAny]

    def get(self, request, patient_no):
        try:
            patient = (
                Patient.objects
                .select_related('crm_profile__tier')
                .get(patient_no=patient_no)
            )
        except Patient.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        today = _today()
        settings_obj = ReportSettings.get_solo()
        crm = getattr(patient, 'crm_profile', None)

        invoices = list(
            Invoice.objects
            .filter(patient_no=patient, is_voided=False)
            .prefetch_related('items__item')
            .order_by('-datetime')
        )

        # Treatment sessions carry the beautician, which invoices do not. Keyed
        # by Jakarta date so a session can be matched to the visit it belongs
        # to; two sessions on one day merge, which is what the operator sees.
        sessions_by_day = {}
        for session in (
            TreatmentSession.objects
            .filter(patient_no=patient)
            .select_related('beautician')
            .prefetch_related('treatments')
        ):
            day = session.session_time.astimezone(JAKARTA_TZ).date()
            bucket = sessions_by_day.setdefault(day, {'beauticians': [], 'treatments': []})
            if session.beautician and session.beautician.beautician_name not in bucket['beauticians']:
                bucket['beauticians'].append(session.beautician.beautician_name)
            for t in session.treatments.all():
                if t.name not in bucket['treatments']:
                    bucket['treatments'].append(t.name)

        # Doctor seen per day, from the medical record. The note text stays out
        # of this payload on purpose — clinical content belongs on the medical
        # records page, behind its own role check.
        #
        # The visit date comes out of ``medrec_id`` (MR-<patient>-<YYYYMMDD>-<n>)
        # rather than from the ActivePatient row that created it: that row is
        # deleted when billing closes the visit, so joining through it would
        # leave the doctor blank on every completed visit — which is all of them.
        # ``doctor_id`` is unpopulated on the great majority of records — the
        # SOAP form writes the free-text ``clinician`` instead — so the FK is
        # preferred where it exists and the text used otherwise.
        doctors_by_day = {}
        for rec in MedRec.objects.filter(patient_no=patient).select_related('doctor_id'):
            day = _medrec_date(rec.medrec_id)
            name = rec.doctor_id.doctor_name if rec.doctor_id else (rec.clinician or '').strip()
            if day and name:
                doctors_by_day.setdefault(day, name)

        visits = []
        for inv in invoices:
            local_dt = inv.datetime.astimezone(JAKARTA_TZ)
            day = local_dt.date()
            treatments, tprods = [], []
            for li in inv.items.all():
                target = treatments if (li.item_id and li.item.is_service) else tprods
                target.append({
                    'name': li.item.name if li.item_id else (li.item_name or '—'),
                    'quantity': float(li.quantity),
                })
            session = sessions_by_day.get(day, {})
            # Sessions record treatments the invoice may not name (a package
            # redemption bills at Rp 0 under the package's own line), so the two
            # sources are merged rather than one preferred.
            # Case-insensitive, because the treatment catalog and the invoice
            # line can spell the same treatment differently ('Skin booster' vs
            # 'Skin Booster') and listing it twice reads as two treatments.
            billed = {t['name'].casefold() for t in treatments}
            session_only = [
                {'name': name, 'quantity': 1.0}
                for name in session.get('treatments', [])
                if name.casefold() not in billed
            ]
            visits.append({
                'invoice_id': inv.id,
                'invoice_number': inv.invoice_number,
                'datetime': local_dt.isoformat(),
                'date': str(day),
                'total': str(inv.grand_total),
                'treatments': treatments + session_only,
                'products': tprods,
                'beauticians': session.get('beauticians', []),
                'doctor': doctors_by_day.get(day),
            })

        # Lifetime favourites, over every visit rather than the recent window —
        # this is the "what does this person actually buy" question.
        fav_treatments, fav_products = _favourites(patient)

        packages = [
            {
                'id': pkg.id,
                'package_name': pkg.package.name,
                'status': pkg.status,
                'purchased_at': str(pkg.purchased_at.astimezone(JAKARTA_TZ).date()),
            }
            for pkg in PatientPackage.objects.filter(patient=patient).select_related('package')
            .order_by('-purchased_at')
        ]

        last_visit_date = crm.last_visit_date if crm else None
        first_visit = invoices[-1].datetime.astimezone(JAKARTA_TZ).date() if invoices else None
        spend = crm.total_spend if crm else Decimal('0')
        visit_count = crm.total_visits if crm else 0

        return Response({
            'patient': {
                'patient_no': patient.patient_no,
                'name': patient.name,
                'phone_number': patient.phone_number,
                'address': patient.address,
                'NIK': patient.NIK,
                'birth_date': str(patient.birth_date) if patient.birth_date else None,
                'birth_date_label': mt.format_date_id(patient.birth_date),
                'age': patient.age_on(today),
                'days_until_birthday': (
                    (_next_birthday(patient.birth_date, today) - today).days
                    if patient.birth_date else None
                ),
                'gender': patient.gender,
                'tier': ({'name': crm.tier.name, 'color_hex': crm.tier.color_hex}
                         if crm and crm.tier_id else None),
            },
            'stats': {
                'total_visits': visit_count,
                'total_spend': str(spend),
                'avg_spend': str((spend / visit_count).quantize(Decimal('1'))) if visit_count else '0',
                'first_visit': str(first_visit) if first_visit else None,
                'last_visit': str(last_visit_date) if last_visit_date else None,
                'days_since_last_visit': (today - last_visit_date).days if last_visit_date else None,
                'activity_bucket': classify(last_visit_date, today, settings_obj),
                'avg_days_between_visits': _avg_gap(invoices),
                'active_packages': sum(1 for p in packages if p['status'] == 'active'),
            },
            'favourite_treatments': fav_treatments,
            'favourite_products': fav_products,
            'visits': visits,
            'packages': packages,
            'message_context': mt.build_context(
                patient,
                crm=crm,
                last_visit=last_visit_date,
                last_treatments=[t['name'] for t in (visits[0]['treatments'] if visits else [])],
                today=today,
            ),
        })


def _medrec_date(medrec_id):
    """Visit date encoded in a medrec_id, or None if it is not the known shape.

    Legacy and imported records do not all follow MR-<patient>-<YYYYMMDD>-<n>,
    so a parse failure means "unknown day", never an exception.
    """
    parts = (medrec_id or '').split('-')
    for part in parts:
        if len(part) == 8 and part.isdigit():
            try:
                return datetime.date(int(part[:4]), int(part[4:6]), int(part[6:]))
            except ValueError:
                return None
    return None


def _favourites(patient):
    """Lifetime treatment / product frequency for one patient, most-used first."""
    rows = (
        InvoiceItem.objects
        .filter(invoice__patient_no=patient, invoice__is_voided=False)
        .values('item__is_service', 'item__name', 'item_name')
        .annotate(
            times=Count('id'),
            qty=Sum('quantity'),
            last=Max('invoice__datetime'),
            spend=Sum(_LINE_REVENUE),
        )
    )
    treatments, products = [], []
    for row in rows:
        entry = {
            'name': row['item__name'] or row['item_name'] or '—',
            'times': row['times'],
            'quantity': float(row['qty'] or 0),
            'spend': _money(row['spend']),
            'last_used': (row['last'].astimezone(JAKARTA_TZ).date().isoformat()
                          if row['last'] else None),
        }
        (treatments if row['item__is_service'] else products).append(entry)
    treatments.sort(key=lambda e: e['times'], reverse=True)
    products.sort(key=lambda e: e['times'], reverse=True)
    return treatments[:12], products[:12]


def _avg_gap(invoices):
    """Mean days between consecutive visits, or None with fewer than two.

    Distinct days, not invoices: two invoices raised in one afternoon are one
    visit, and counting them separately drags the average toward zero and makes
    a monthly patient look weekly.
    """
    days = sorted({inv.datetime.astimezone(JAKARTA_TZ).date() for inv in invoices})
    if len(days) < 2:
        return None
    return round((days[-1] - days[0]).days / (len(days) - 1), 1)


# ── Message templates ──────────────────────────────────────────────────────

class MessageTemplateListCreateView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        qs = MessageTemplate.objects.all()
        if request.GET.get('active_only') == '1':
            qs = qs.filter(is_active=True)
        return Response({
            'placeholders': [{'token': k, 'label': v} for k, v in mt.PLACEHOLDERS.items()],
            'results': [_serialize_template(t) for t in qs],
        })

    def post(self, request):
        payload = request.data
        name = (payload.get('name') or '').strip()
        body = (payload.get('body') or '').strip()
        if not name or not body:
            return Response(
                {'detail': 'name dan body wajib diisi.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        template = MessageTemplate.objects.create(
            name=name,
            body=body,
            category=_valid_category(payload.get('category')),
            is_active=bool(payload.get('is_active', True)),
            sort_order=_as_int(payload.get('sort_order'), 0),
            created_by=_actor(request),
        )
        return Response(_serialize_template(template), status=status.HTTP_201_CREATED)


class MessageTemplateDetailView(APIView):
    permission_classes = [AllowAny]

    def _get(self, pk):
        return MessageTemplate.objects.filter(pk=pk).first()

    def put(self, request, pk):
        template = self._get(pk)
        if template is None:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        payload = request.data
        if 'name' in payload:
            name = (payload['name'] or '').strip()
            if not name:
                return Response({'name': 'Wajib diisi.'}, status=status.HTTP_400_BAD_REQUEST)
            template.name = name
        if 'body' in payload:
            body = (payload['body'] or '').strip()
            if not body:
                return Response({'body': 'Wajib diisi.'}, status=status.HTTP_400_BAD_REQUEST)
            template.body = body
        if 'category' in payload:
            template.category = _valid_category(payload['category'])
        if 'is_active' in payload:
            template.is_active = bool(payload['is_active'])
        if 'sort_order' in payload:
            template.sort_order = _as_int(payload['sort_order'], template.sort_order)
        template.save()
        return Response(_serialize_template(template))

    def delete(self, request, pk):
        template = self._get(pk)
        if template is None:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        template.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class MessageTemplateRenderView(APIView):
    """POST /api/crm/message-templates/<pk>/render/  {"patient_no": "..."}

    Rendering server-side rather than substituting in the browser keeps one
    definition of what a token means, and lets the placeholder set grow without
    a frontend release.
    """
    permission_classes = [AllowAny]

    def post(self, request, pk):
        template = MessageTemplate.objects.filter(pk=pk).first()
        if template is None:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        patient_no = (request.data.get('patient_no') or '').strip()
        if not patient_no:
            return Response({'patient_no': 'Wajib diisi.'}, status=status.HTTP_400_BAD_REQUEST)

        patient = (
            Patient.objects.select_related('crm_profile__tier')
            .filter(patient_no=patient_no).first()
        )
        if patient is None:
            return Response({'detail': 'Pasien tidak ditemukan.'}, status=status.HTTP_404_NOT_FOUND)

        crm = getattr(patient, 'crm_profile', None)
        last_visit = crm.last_visit_date if crm else None
        last_treatments = []
        last_invoice = (
            Invoice.objects.filter(patient_no=patient, is_voided=False)
            .prefetch_related('items__item').order_by('-datetime').first()
        )
        if last_invoice:
            last_treatments = [
                li.item.name for li in last_invoice.items.all()
                if li.item_id and li.item.is_service
            ]

        context = mt.build_context(
            patient, crm=crm, last_visit=last_visit,
            last_treatments=last_treatments, today=_today(),
        )
        return Response({
            'text': mt.render(template.body, context),
            'context': context,
        })


def _actor(request):
    return request.user if isinstance(request.user, AppUser) else None


def _valid_category(value):
    valid = {c for c, _ in MessageTemplate.CATEGORY_CHOICES}
    value = (value or '').strip()
    return value if value in valid else 'followup'


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _serialize_template(t):
    return {
        'id': t.id,
        'name': t.name,
        'category': t.category,
        'category_label': t.get_category_display(),
        'body': t.body,
        'is_active': t.is_active,
        'sort_order': t.sort_order,
        'updated_at': t.updated_at.isoformat(),
    }
