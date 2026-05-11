from datetime import date, datetime, timedelta

from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

from ..models import AppUser, AttendanceRecord, StaffSchedule, WorkShift
from ..api.serializers import (
    AttendanceRecordSerializer,
    AttendanceSummarySerializer,
    WorkShiftSerializer,
    StaffScheduleSerializer,
)
from django.utils import timezone


class ClockInView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        staff_id = request.data.get('staff_id')
        if not staff_id:
            return Response({'error': 'staff_id is required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            staff = AppUser.objects.get(id=staff_id, is_active=True)
        except AppUser.DoesNotExist:
            return Response({'error': 'Staff not found.'}, status=status.HTTP_404_NOT_FOUND)

        today = date.today()
        record, _ = AttendanceRecord.objects.get_or_create(
            staff=staff, date=today,
            defaults={'status': 'absent'},
        )

        if not record.clock_in:
            now = timezone.now()
            record.clock_in = now

            try:
                schedule = StaffSchedule.objects.get(staff=staff, date=today)
                if schedule.shift:
                    threshold = (
                        datetime.combine(today, schedule.shift.expected_start)
                        + timedelta(minutes=15)
                    )
                    # Make threshold timezone-aware
                    threshold = timezone.make_aware(threshold) if timezone.is_naive(threshold) else threshold
                    record.status = 'late' if now > threshold else 'present'
                else:
                    record.status = 'present'
            except StaffSchedule.DoesNotExist:
                record.status = 'present'

            record.save()

        serializer = AttendanceRecordSerializer(record)
        return Response(serializer.data)


class ClockOutView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        staff_id = request.data.get('staff_id')
        if not staff_id:
            return Response({'error': 'staff_id is required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            staff = AppUser.objects.get(id=staff_id, is_active=True)
        except AppUser.DoesNotExist:
            return Response({'error': 'Staff not found.'}, status=status.HTTP_404_NOT_FOUND)

        today = date.today()
        try:
            record = AttendanceRecord.objects.get(staff=staff, date=today)
        except AttendanceRecord.DoesNotExist:
            return Response({'error': 'No clock-in record for today.'}, status=status.HTTP_404_NOT_FOUND)

        if record.clock_in and not record.clock_out:
            record.clock_out = timezone.now()
            record.save()

        serializer = AttendanceRecordSerializer(record)
        return Response(serializer.data)


class AttendanceListView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        qs = AttendanceRecord.objects.select_related('staff').all()

        staff_id = request.query_params.get('staff_id')
        date_from = request.query_params.get('date_from')
        date_to = request.query_params.get('date_to')
        status_filter = request.query_params.get('status')

        if staff_id:
            qs = qs.filter(staff_id=staff_id)
        if date_from:
            qs = qs.filter(date__gte=date_from)
        if date_to:
            qs = qs.filter(date__lte=date_to)
        if status_filter:
            qs = qs.filter(status=status_filter)

        qs = qs.order_by('-date')
        serializer = AttendanceRecordSerializer(qs, many=True)
        return Response(serializer.data)


class AttendanceSummaryView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        staff_id = request.query_params.get('staff_id')
        month_str = request.query_params.get('month')

        if month_str:
            try:
                year, mon = int(month_str[:4]), int(month_str[5:7])
            except (ValueError, IndexError):
                return Response({'error': 'month must be YYYY-MM.'}, status=status.HTTP_400_BAD_REQUEST)
        else:
            today = date.today()
            year, mon = today.year, today.month

        from calendar import monthrange
        _, last_day = monthrange(year, mon)
        date_from = date(year, mon, 1)
        date_to = date(year, mon, last_day)

        if staff_id:
            staff_qs = AppUser.objects.filter(id=staff_id, is_active=True)
        else:
            staff_qs = AppUser.objects.filter(is_active=True)

        results = []
        for staff in staff_qs:
            records = AttendanceRecord.objects.filter(
                staff=staff, date__gte=date_from, date__lte=date_to
            )
            days_present = records.filter(status__in=['present', 'late']).count()
            days_late = records.filter(status='late').count()
            days_absent = records.filter(status='absent').count()
            total_hours = sum(r.total_hours for r in records)
            results.append({
                'staff_id': staff.id,
                'display_name': staff.display_name,
                'role': staff.role,
                'days_present': days_present,
                'days_late': days_late,
                'days_absent': days_absent,
                'total_hours': total_hours,
            })

        serializer = AttendanceSummarySerializer(results, many=True)
        return Response(serializer.data)


class ShiftListCreateView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        shifts = WorkShift.objects.all()
        serializer = WorkShiftSerializer(shifts, many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = WorkShiftSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ShiftDetailView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def _get_shift(self, pk):
        try:
            return WorkShift.objects.get(pk=pk)
        except WorkShift.DoesNotExist:
            return None

    def get(self, request, pk):
        shift = self._get_shift(pk)
        if not shift:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(WorkShiftSerializer(shift).data)

    def put(self, request, pk):
        shift = self._get_shift(pk)
        if not shift:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        serializer = WorkShiftSerializer(shift, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        shift = self._get_shift(pk)
        if not shift:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        shift.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class StaffScheduleView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        qs = StaffSchedule.objects.select_related('staff', 'shift').all()
        staff_id = request.query_params.get('staff_id')
        month_str = request.query_params.get('month')

        if staff_id:
            qs = qs.filter(staff_id=staff_id)
        if month_str:
            try:
                year, mon = int(month_str[:4]), int(month_str[5:7])
                from calendar import monthrange
                _, last_day = monthrange(year, mon)
                qs = qs.filter(date__gte=date(year, mon, 1), date__lte=date(year, mon, last_day))
            except (ValueError, IndexError):
                return Response({'error': 'month must be YYYY-MM.'}, status=status.HTTP_400_BAD_REQUEST)

        serializer = StaffScheduleSerializer(qs, many=True)
        return Response(serializer.data)

    def post(self, request):
        staff_id = request.data.get('staff_id')
        date_str = request.data.get('date')
        shift_id = request.data.get('shift_id')
        notes = request.data.get('notes', '')

        if not staff_id or not date_str:
            return Response({'error': 'staff_id and date are required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            staff = AppUser.objects.get(id=staff_id, is_active=True)
        except AppUser.DoesNotExist:
            return Response({'error': 'Staff not found.'}, status=status.HTTP_404_NOT_FOUND)

        shift = None
        if shift_id:
            try:
                shift = WorkShift.objects.get(id=shift_id)
            except WorkShift.DoesNotExist:
                return Response({'error': 'Shift not found.'}, status=status.HTTP_404_NOT_FOUND)

        schedule, _ = StaffSchedule.objects.update_or_create(
            staff=staff,
            date=date_str,
            defaults={'shift': shift, 'notes': notes},
        )
        serializer = StaffScheduleSerializer(schedule)
        return Response(serializer.data, status=status.HTTP_200_OK)
