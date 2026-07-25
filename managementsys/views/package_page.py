from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from ..api.serializers import PatientPackageSerializer
from ..models import PatientPackage


class PatientPackagesView(APIView):
    """GET active + exhausted packages for a patient, newest first.

    Used by the patient profile page (CRM) and the POS billing detail to show
    redemption status. Filter by ?status=active to limit to redeemable ones.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, patient_no):
        qs = (
            PatientPackage.objects
            .filter(patient_id=patient_no)
            .select_related('package', 'purchased_invoice')
            .prefetch_related('package__items__treatment', 'redemptions__treatment', 'redemptions__invoice')
            .order_by('-purchased_at')
        )
        status_filter = request.GET.get('status', '').strip()
        if status_filter:
            qs = qs.filter(status=status_filter)
        return Response(PatientPackageSerializer(qs, many=True).data, status=status.HTTP_200_OK)
