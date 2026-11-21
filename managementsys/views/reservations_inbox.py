from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from ..api.serializers import ReservationRequestSerializer
from ..models import AppUser, AuditLog, Patient, ReservationRequest
from ..services.reservation_sync import ReservationSyncError, run_once


def _actor(request):
    return request.user if isinstance(request.user, AppUser) else None


def _base_queryset():
    return ReservationRequest.objects.select_related(
        'matched_patient', 'appointment', 'acknowledged_by',
    )


class ReservationInboxView(APIView):
    def get(self, request):
        which = request.query_params.get('filter', 'attention').strip()
        qs = _base_queryset()

        if which == 'unmatched':
            qs = qs.exclude(match_status=ReservationRequest.MATCH_MATCHED)
        elif which != 'all':
            qs = qs.filter(
                Q(acknowledged_at__isnull=True)
                | ~Q(match_status=ReservationRequest.MATCH_MATCHED)
            )

        try:
            limit = min(int(request.query_params.get('limit', 200)), 500)
        except (TypeError, ValueError):
            limit = 200

        rows = list(qs[:limit])
        counts = _base_queryset()
        return Response({
            'reservations': ReservationRequestSerializer(rows, many=True).data,
            'summary': {
                'total': counts.count(),
                'unacknowledged': counts.filter(acknowledged_at__isnull=True).count(),
                'needs_link': counts.exclude(
                    match_status=ReservationRequest.MATCH_MATCHED).count(),
            },
        })


class ReservationLinkPatientView(APIView):
    def post(self, request, pk):
        row = _base_queryset().filter(pk=pk).first()
        if row is None:
            return Response(
                {'error': 'Reservation %s not found.' % pk},
                status=status.HTTP_404_NOT_FOUND,
            )

        patient_no = str(request.data.get('patient_no', '')).strip()
        if not patient_no:
            return Response(
                {'error': 'patient_no is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        patient = Patient.objects.filter(patient_no=patient_no).first()
        if patient is None:
            return Response(
                {'error': 'Patient %s not found.' % patient_no},
                status=status.HTTP_400_BAD_REQUEST,
            )

        appointment = row.appointment
        if appointment is not None and appointment.linked_active_patient_id:
            return Response(
                {'error': 'This booking is already checked in — the patient '
                          'cannot be changed now.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        row.matched_patient = patient
        row.match_status = ReservationRequest.MATCH_MATCHED
        row.candidate_patient_nos = []
        row.acknowledged_at = row.acknowledged_at or timezone.now()
        row.acknowledged_by = row.acknowledged_by or _actor(request)
        row.save(update_fields=[
            'matched_patient', 'match_status', 'candidate_patient_nos',
            'acknowledged_at', 'acknowledged_by',
        ])

        if appointment is not None:
            appointment.patient = patient
            appointment.guest_name = None
            appointment.save(update_fields=['patient', 'guest_name', 'updated_at'])

        AuditLog.objects.create(
            performed_by=_actor(request),
            action='UPDATE',
            entity_type='ReservationRequest',
            entity_id=str(row.external_id),
            description=(
                'Online reservation for %s (%s) linked to patient %s'
                % (row.name, row.phone, patient.patient_no)
            ),
        )
        return Response(ReservationRequestSerializer(row).data)


class ReservationAcknowledgeView(APIView):
    def post(self, request, pk):
        row = _base_queryset().filter(pk=pk).first()
        if row is None:
            return Response(
                {'error': 'Reservation %s not found.' % pk},
                status=status.HTTP_404_NOT_FOUND,
            )

        row.acknowledged_at = timezone.now()
        row.acknowledged_by = _actor(request)
        row.save(update_fields=['acknowledged_at', 'acknowledged_by'])
        return Response(ReservationRequestSerializer(row).data)


class ReservationSyncNowView(APIView):
    def post(self, request):
        try:
            result = run_once()
        except ReservationSyncError as exc:
            return Response(
                {'error': str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(result)
