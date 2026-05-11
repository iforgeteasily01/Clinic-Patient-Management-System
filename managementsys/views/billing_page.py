from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from ..api.serializers import BillingPatientSerializer
from ..models import ActivePatient, AppUser, AuditLog


def _actor(request):
    return request.user if isinstance(request.user, AppUser) else None


class BillingQueueView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        patients = (
            ActivePatient.objects
            .filter(status=5)
            .select_related('patient_no', 'medrec')
            .prefetch_related(
                'treatmentsession_set__treatments',
                'treatmentsession_set__beautician',
            )
            .order_by('visit_time')
        )
        serializer = BillingPatientSerializer(patients, many=True)
        return Response(serializer.data)


class BillingCompleteView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def delete(self, request, pk):
        try:
            active_patient = ActivePatient.objects.get(id=pk, status=5)
        except ActivePatient.DoesNotExist:
            return Response(
                {'error': 'Billing record not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        label = (
            active_patient.patient_no.name
            if active_patient.patient_no_id
            else active_patient.guest_name
        )
        AuditLog.objects.create(
            performed_by=_actor(request),
            action='DELETE',
            entity_type='ActivePatient',
            entity_id=str(pk),
            description=f'Billing completed for {label}',
        )
        active_patient.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
