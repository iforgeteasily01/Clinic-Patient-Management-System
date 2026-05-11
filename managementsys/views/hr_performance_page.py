from datetime import date, timedelta
from collections import defaultdict

from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

from django.db.models import Count, Sum
from django.utils.timezone import make_aware, datetime as tz_datetime

from ..models import Beauticians, Doctors, MedRec, TreatmentSession


def _parse_date_range(request):
    today = date.today()
    month_start = today.replace(day=1)
    date_from_str = request.query_params.get('date_from', month_start.isoformat())
    date_to_str   = request.query_params.get('date_to',   today.isoformat())
    try:
        date_from = date.fromisoformat(date_from_str)
        date_to   = date.fromisoformat(date_to_str)
    except ValueError:
        return None, None, 'date_from and date_to must be YYYY-MM-DD.'
    return date_from, date_to, None


def _dt_range(date_from, date_to):
    """Return an inclusive datetime range (start of date_from → end of date_to)."""
    start = make_aware(tz_datetime(date_from.year, date_from.month, date_from.day, 0, 0, 0))
    end   = make_aware(tz_datetime(date_to.year,   date_to.month,   date_to.day,  23, 59, 59))
    return start, end


def _beautician_performance(date_from, date_to):
    start, end = _dt_range(date_from, date_to)
    sessions = (
        TreatmentSession.objects
        .filter(
            session_time__gte=start,
            session_time__lte=end,
            beautician__isnull=False,
        )
        .select_related('beautician', 'patient_no', 'active_patient')
        .prefetch_related('treatments')
    )

    # Aggregate in Python to handle M2M treatments easily
    beautician_map = {}  # id → dict

    for session in sessions:
        b = session.beautician
        if b.id not in beautician_map:
            beautician_map[b.id] = {
                'beautician_id':   b.id,
                'beautician_name': b.beautician_name,
                'sessions_count':  0,
                'patient_ids':     set(),
                'treatments':      defaultdict(lambda: {'count': 0, 'name': '', 'unit_revenue': '0'}),
                'total_revenue':   0,
            }

        entry = beautician_map[b.id]
        entry['sessions_count'] += 1

        # Unique patients — use patient_no if available, else active_patient pk
        if session.patient_no_id:
            entry['patient_ids'].add(('p', session.patient_no_id))
        elif session.active_patient_id:
            entry['patient_ids'].add(('a', session.active_patient_id))

        for treatment in session.treatments.all():
            t_entry = entry['treatments'][treatment.id]
            t_entry['count']        += 1
            t_entry['name']          = treatment.name
            t_entry['unit_revenue']  = str(treatment.price)
            entry['total_revenue']  += float(treatment.price)

    result = []
    for entry in beautician_map.values():
        result.append({
            'beautician_id':   entry['beautician_id'],
            'beautician_name': entry['beautician_name'],
            'sessions_count':  entry['sessions_count'],
            'unique_patients': len(entry['patient_ids']),
            'treatments_breakdown': [
                {
                    'treatment_id':   tid,
                    'treatment_name': td['name'],
                    'count':          td['count'],
                    'unit_revenue':   td['unit_revenue'],
                }
                for tid, td in entry['treatments'].items()
            ],
            'total_revenue': str(round(entry['total_revenue'], 2)),
        })

    result.sort(key=lambda x: x['sessions_count'], reverse=True)
    return result


def _doctor_performance(date_from, date_to):
    start, end = _dt_range(date_from, date_to)

    # MedRec → ActivePatient (via active_visit OneToOne reverse) → visit_time
    medrecs = (
        MedRec.objects
        .filter(
            active_visit__visit_time__gte=start,
            active_visit__visit_time__lte=end,
            doctor_id__isnull=False,
        )
        .select_related('doctor_id', 'patient_no')
    )

    doctor_map = {}
    for rec in medrecs:
        d = rec.doctor_id
        if d.id not in doctor_map:
            doctor_map[d.id] = {
                'doctor_id':           d.id,
                'doctor_name':         d.doctor_name,
                'consultations_count': 0,
                'patient_ids':         set(),
            }
        entry = doctor_map[d.id]
        entry['consultations_count'] += 1
        if rec.patient_no_id:
            entry['patient_ids'].add(rec.patient_no_id)

    result = []
    for entry in doctor_map.values():
        result.append({
            'doctor_id':           entry['doctor_id'],
            'doctor_name':         entry['doctor_name'],
            'consultations_count': entry['consultations_count'],
            'unique_patients':     len(entry['patient_ids']),
        })

    result.sort(key=lambda x: x['consultations_count'], reverse=True)
    return result


class StaffPerformanceView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        date_from, date_to, err = _parse_date_range(request)
        if err:
            return Response({'error': err}, status=status.HTTP_400_BAD_REQUEST)

        role = request.query_params.get('role', '')

        beauticians = _beautician_performance(date_from, date_to) if role != 'doctor'     else []
        doctors     = _doctor_performance(date_from, date_to)     if role != 'beautician' else []

        return Response({
            'date_from':   date_from.isoformat(),
            'date_to':     date_to.isoformat(),
            'beauticians': beauticians,
            'doctors':     doctors,
        })


class StaffPerformanceDailyView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        date_from, date_to, err = _parse_date_range(request)
        if err:
            return Response({'error': err}, status=status.HTTP_400_BAD_REQUEST)

        staff_id = request.query_params.get('staff_id')
        role     = request.query_params.get('role', '')

        if not staff_id:
            return Response({'error': 'staff_id is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if role not in ('beautician', 'doctor'):
            return Response({'error': "role must be 'beautician' or 'doctor'."}, status=status.HTTP_400_BAD_REQUEST)

        staff_id = int(staff_id)
        start, end = _dt_range(date_from, date_to)

        # Build a zero-filled day map
        day_map = {}
        cur = date_from
        while cur <= date_to:
            day_map[cur.isoformat()] = {'date': cur.isoformat(), 'count': 0, 'revenue': '0'}
            cur += timedelta(days=1)

        if role == 'beautician':
            sessions = (
                TreatmentSession.objects
                .filter(
                    beautician_id=staff_id,
                    session_time__gte=start,
                    session_time__lte=end,
                )
                .prefetch_related('treatments')
            )
            for session in sessions:
                day = session.session_time.date().isoformat()
                if day in day_map:
                    day_map[day]['count'] += 1
                    rev = sum(float(t.price) for t in session.treatments.all())
                    day_map[day]['revenue'] = str(round(
                        float(day_map[day]['revenue']) + rev, 2
                    ))

            return Response([
                {'date': v['date'], 'count': v['count'], 'revenue': v['revenue']}
                for v in day_map.values()
            ])

        else:  # doctor
            medrecs = MedRec.objects.filter(
                doctor_id=staff_id,
                active_visit__visit_time__gte=start,
                active_visit__visit_time__lte=end,
            ).select_related('active_visit')
            for rec in medrecs:
                day = rec.active_visit.visit_time.date().isoformat()
                if day in day_map:
                    day_map[day]['count'] += 1

            return Response([
                {'date': v['date'], 'count': v['count']}
                for v in day_map.values()
            ])
