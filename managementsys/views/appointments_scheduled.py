from django.db import transaction
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from ..api.serializers import (
    ActivePatientSerializer,
    AppointmentLocationSerializer,
    AppointmentSerializer,
)
from ..models import (
    JAKARTA_TZ, ActivePatient, Appointment, AppointmentLocation, AppUser, AuditLog,
)


def _actor(request):
    return request.user if isinstance(request.user, AppUser) else None


def _base_queryset():
    return Appointment.objects.select_related('patient', 'practitioner', 'location')


def _day_bounds(day):
    from datetime import datetime, time

    start_local = datetime.combine(day, time.min, tzinfo=JAKARTA_TZ)
    end_local = datetime.combine(day, time.max, tzinfo=JAKARTA_TZ)
    return start_local, end_local


class ScheduledAppointmentListCreateView(APIView):
    def get(self, request):
        qs = _base_queryset()

        date_raw = request.query_params.get('date', '').strip()
        from_raw = request.query_params.get('from', '').strip()
        to_raw = request.query_params.get('to', '').strip()
        status_raw = request.query_params.get('status', '').strip()
        practitioner_raw = request.query_params.get('practitioner', '').strip()

        if date_raw:
            day = parse_date(date_raw)
            if day is None:
                return Response(
                    {'error': 'Invalid `date`. Use YYYY-MM-DD.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            start, end = _day_bounds(day)
            qs = qs.filter(start_at__gte=start, start_at__lte=end)
        else:
            if from_raw:
                day = parse_date(from_raw)
                if day is None:
                    return Response(
                        {'error': 'Invalid `from`. Use YYYY-MM-DD.'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                qs = qs.filter(start_at__gte=_day_bounds(day)[0])
            if to_raw:
                day = parse_date(to_raw)
                if day is None:
                    return Response(
                        {'error': 'Invalid `to`. Use YYYY-MM-DD.'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                qs = qs.filter(start_at__lte=_day_bounds(day)[1])

        if status_raw:
            qs = qs.filter(status__in=[s for s in status_raw.split(',') if s])
        if practitioner_raw:
            qs = qs.filter(practitioner_id=practitioner_raw)

        return Response(AppointmentSerializer(qs, many=True).data)

    def post(self, request):
        serializer = AppointmentSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        appointment = serializer.save()

        AuditLog.objects.create(
            performed_by=_actor(request),
            action='CREATE',
            entity_type='Appointment',
            entity_id=appointment.appointment_no,
            description=(
                f'Appointment {appointment.appointment_no} booked for '
                f'{appointment.display_name} at '
                f'{appointment.start_at.astimezone(JAKARTA_TZ):%Y-%m-%d %H:%M} WIB'
            ),
        )
        return Response(
            AppointmentSerializer(appointment).data,
            status=status.HTTP_201_CREATED,
        )


class ScheduledAppointmentDetailView(APIView):
    def _get_object(self, pk):
        return _base_queryset().filter(pk=pk).first()

    def get(self, request, pk):
        appointment = self._get_object(pk)
        if appointment is None:
            return Response(
                {'error': f'Appointment {pk} not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(AppointmentSerializer(appointment).data)

    def patch(self, request, pk):
        appointment = self._get_object(pk)
        if appointment is None:
            return Response(
                {'error': f'Appointment {pk} not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        previous_status = appointment.status
        previous_start = appointment.start_at

        serializer = AppointmentSerializer(
            appointment, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        appointment = serializer.save()

        changes = []
        if appointment.status != previous_status:
            changes.append(f'status {previous_status} → {appointment.status}')
        if appointment.start_at != previous_start:
            changes.append(
                f'rescheduled {previous_start.astimezone(JAKARTA_TZ):%Y-%m-%d %H:%M} → '
                f'{appointment.start_at.astimezone(JAKARTA_TZ):%Y-%m-%d %H:%M} WIB'
            )
        AuditLog.objects.create(
            performed_by=_actor(request),
            action='STATUS_CHANGE' if appointment.status != previous_status else 'UPDATE',
            entity_type='Appointment',
            entity_id=appointment.appointment_no,
            description=(
                f'Appointment {appointment.appointment_no} updated'
                + (f': {", ".join(changes)}' if changes else '')
            ),
        )
        return Response(AppointmentSerializer(appointment).data)

    def delete(self, request, pk):
        appointment = self._get_object(pk)
        if appointment is None:
            return Response(
                {'error': f'Appointment {pk} not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        appointment.status = 'cancelled'
        appointment.save(update_fields=['status', 'updated_at'])

        AuditLog.objects.create(
            performed_by=_actor(request),
            action='STATUS_CHANGE',
            entity_type='Appointment',
            entity_id=appointment.appointment_no,
            description=(
                f'Appointment {appointment.appointment_no} cancelled for '
                f'{appointment.display_name}'
            ),
        )
        return Response(AppointmentSerializer(appointment).data)


class AppointmentCheckInView(APIView):
    def post(self, request, pk):
        appointment = _base_queryset().filter(pk=pk).first()
        if appointment is None:
            return Response(
                {'error': f'Appointment {pk} not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if appointment.linked_active_patient_id:
            return Response(
                {'error': 'This appointment is already checked in.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if appointment.status in ('cancelled', 'noshow', 'entered-in-error'):
            return Response(
                {'error': f'Cannot check in an appointment that is {appointment.status}.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if 'consult_status' in request.data:
            consult_status = bool(request.data.get('consult_status'))
        else:
            consult_status = appointment.practitioner_id is not None

        with transaction.atomic():
            active_patient = ActivePatient.objects.create(
                patient_no=appointment.patient,
                guest_name=None if appointment.patient_id else appointment.guest_name,
                status=1 if consult_status else 3,
                consult_status=consult_status,
            )
            appointment.linked_active_patient = active_patient
            appointment.status = 'arrived'
            appointment.save(
                update_fields=['linked_active_patient', 'status', 'updated_at'])

        visit_type = 'consultation' if consult_status else 'treatment'
        AuditLog.objects.create(
            performed_by=_actor(request),
            action='STATUS_CHANGE',
            entity_type='Appointment',
            entity_id=appointment.appointment_no,
            description=(
                f'Appointment {appointment.appointment_no} checked in for '
                f'{appointment.display_name} – {visit_type} '
                f'(ActivePatient #{active_patient.id})'
            ),
        )
        return Response(
            {
                'appointment': AppointmentSerializer(appointment).data,
                'active_patient': ActivePatientSerializer(active_patient).data,
            },
            status=status.HTTP_201_CREATED,
        )


class AppointmentLocationListView(APIView):
    def get(self, request):
        qs = AppointmentLocation.objects.filter(is_active=True)
        return Response(AppointmentLocationSerializer(qs, many=True).data)
