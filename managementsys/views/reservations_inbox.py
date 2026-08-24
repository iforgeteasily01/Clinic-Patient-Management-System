"""
The online reservations inbox.

Bookings arrive here from the public form via services/reservation_sync.py,
which has already created the Appointment — see the ReservationRequest
docstring for why the appointment is not held pending approval. What is left
for a human is the part a machine must not guess: which patient record a phone
number belongs to when it matched none, or more than one.

Check-in is *not* duplicated here. A booking is an Appointment like any other
once it lands, so it checks in through AppointmentCheckInView on the schedule
page, and there is exactly one code path that puts somebody in the queue.
"""

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
    """
    GET /api/reservations/inbox/?filter=attention|all|unmatched&limit=

    Defaults to `attention` — anything not yet looked at, plus anything whose
    patient link is still unresolved. The full list is a click away, but the
    page should open on the work, not the archive.
    """

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
    """
    POST /api/reservations/<pk>/link-patient/   { patient_no }

    Attaches the patient record the phone number could not resolve to on its
    own, and carries the link onto the appointment so the visit files against
    the right chart.

    Refused once the appointment has been checked in: ActivePatient and any
    MedRec hanging off it were created against whoever the row said it was at
    the time, and re-pointing the appointment afterwards would leave the queue
    and the schedule disagreeing about who is in the building.
    """

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
            # The typed name stops being the subject once a record is attached;
            # display_name reads the chart from here on.
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
    """
    POST /api/reservations/<pk>/acknowledge/

    "I have seen this." Nothing more — it only moves the row out of the default
    inbox filter. A guest booking that genuinely has no patient record yet is
    acknowledged and left as a guest, exactly like a walk-in.
    """

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
    """
    POST /api/reservations/sync-now/

    Runs one collection pass on demand. The minute poller is what keeps the
    inbox current; this exists so reception can stop wondering whether a
    booking a patient just phoned about has arrived yet.
    """

    def post(self, request):
        try:
            result = run_once()
        except ReservationSyncError as exc:
            # Not a server fault — the clinic's link to Vercel is down, and the
            # bookings are safe on the other side. Say so plainly.
            return Response(
                {'error': str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(result)
