from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from ..models import MedRec, ActivePatient, AuditLog, AppUser
from ..api.serializers import MedRecPendingDraftSerializer, MedRecHistorySerializer

_SOAP_FIELDS = [
    'subjective', 'objective', 'assessment', 'assessment_codes',
    'plan', 'sabun', 'toner',
    'obat1_pagi', 'obat1_pagi_detail', 'obat1_malam', 'obat1_malam_detail',
    'obat2_pagi', 'obat2_pagi_detail', 'obat2_malam', 'obat2_malam_detail', 'treatment',
    'clinician',
]


def _current_user(request):
    return request.user if isinstance(request.user, AppUser) else None


class MedRecDraftCreateView(APIView):
    def post(self, request):
        from ..models import Patient
        patient_no_str = request.data.get('patient_no', '').strip()
        visit_date = request.data.get('visit_date', '').strip() or None

        if not patient_no_str:
            return Response({'error': 'patient_no is required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            patient = Patient.objects.get(patient_no=patient_no_str)
        except Patient.DoesNotExist:
            return Response({'error': 'Patient not found.'}, status=status.HTTP_404_NOT_FOUND)

        active = ActivePatient.objects.filter(
            patient_no=patient,
        ).order_by('-visit_time').first()

        if active and active.medrec_id and active.medrec.status == MedRec.DRAFT:
            serializer = MedRecHistorySerializer(active.medrec)
            return Response(serializer.data, status=status.HTTP_200_OK)

        user = _current_user(request)
        instance = MedRec(
            patient_no=patient,
            status=MedRec.DRAFT,
            clinician=user.display_name if user else '',
        )
        instance.save(visit_date=visit_date)

        if active and not active.medrec_id:
            active.medrec = instance
            active.save(update_fields=['medrec_id'])

        AuditLog.objects.create(
            performed_by=_current_user(request),
            action='CREATE',
            entity_type='MedRec',
            entity_id=str(instance.medrec_id),
            description=f'Draft medical record reserved: {instance.medrec_id}',
        )

        serializer = MedRecHistorySerializer(instance)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class MedRecUpdateView(APIView):
    def patch(self, request, medrec_id):
        try:
            record = MedRec.objects.select_related('patient_no').get(medrec_id=medrec_id)
        except MedRec.DoesNotExist:
            return Response({'error': 'Medical record not found.'}, status=status.HTTP_404_NOT_FOUND)

        for field in _SOAP_FIELDS:
            if field in request.data:
                setattr(record, field, request.data[field])

        record.save()

        active = getattr(record, 'active_visit', None)
        if active and active.status in (1, 2):
            active.status = 3
            active.save(update_fields=['status'])

        AuditLog.objects.create(
            performed_by=_current_user(request),
            action='UPDATE',
            entity_type='MedRec',
            entity_id=str(record.medrec_id),
            description=f'Draft medical record saved: {record.medrec_id}',
        )

        serializer = MedRecHistorySerializer(record)
        return Response(serializer.data)


class MedRecFinalizeView(APIView):
    def post(self, request, medrec_id):
        try:
            record = MedRec.objects.select_related('patient_no').get(medrec_id=medrec_id)
        except MedRec.DoesNotExist:
            return Response({'error': 'Medical record not found.'}, status=status.HTTP_404_NOT_FOUND)

        for field in _SOAP_FIELDS:
            if field in request.data:
                setattr(record, field, request.data[field])

        record.status = MedRec.FINALIZED
        record.save()

        active = getattr(record, 'active_visit', None)
        if active and active.status in (1, 2):
            active.status = 3
            active.save(update_fields=['status'])

        AuditLog.objects.create(
            performed_by=_current_user(request),
            action='UPDATE',
            entity_type='MedRec',
            entity_id=str(record.medrec_id),
            description=f'Medical record finalized: {record.medrec_id}',
        )

        serializer = MedRecHistorySerializer(record)
        return Response(serializer.data)


class MedRecPendingDraftsView(APIView):
    def get(self, request):
        qs = (
            MedRec.objects
            .filter(status=MedRec.DRAFT)
            .select_related('patient_no')
            .prefetch_related('active_visit')
            .exclude(active_visit__status__in=[1, 2])
            .order_by('-medrec_id')
        )
        serializer = MedRecPendingDraftSerializer(qs, many=True)
        return Response(serializer.data)
