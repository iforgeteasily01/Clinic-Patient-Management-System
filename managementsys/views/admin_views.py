import io

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

_HEADER_FONT = Font(bold=True, color='FFFFFF')
_HEADER_FILL = PatternFill('solid', fgColor='0284C7')

from ..api.serializers import (
    AppUserAdminSerializer,
    BeauticiansSerializer,
    ChartOfAccountsSerializer,
    DoctorsSerializer,
    PatientSerializer,
    TreatmentCategorySerializer,
    TreatmentSerializer,
)
from ..models import AppUser, AuditLog, Beauticians, ChartOfAccounts, Doctors, Patient, Treatment, TreatmentCategory


def _actor(request):
    return request.user if isinstance(request.user, AppUser) else None


class DoctorListCreateAdminView(generics.ListCreateAPIView):
    queryset = Doctors.objects.all().order_by('doctor_name')
    serializer_class = DoctorsSerializer

    def perform_create(self, serializer):
        instance = serializer.save()
        AuditLog.objects.create(
            performed_by=_actor(self.request),
            action='CREATE',
            entity_type='Doctor',
            entity_id=str(instance.id),
            description=f'Doctor added: {instance.doctor_name}',
        )


class DoctorDetailAdminView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Doctors.objects.all()
    serializer_class = DoctorsSerializer

    def perform_update(self, serializer):
        instance = serializer.save()
        AuditLog.objects.create(
            performed_by=_actor(self.request),
            action='UPDATE',
            entity_type='Doctor',
            entity_id=str(instance.id),
            description=f'Doctor updated: {instance.doctor_name}',
        )

    def perform_destroy(self, instance):
        AuditLog.objects.create(
            performed_by=_actor(self.request),
            action='DELETE',
            entity_type='Doctor',
            entity_id=str(instance.id),
            description=f'Doctor deleted: {instance.doctor_name}',
        )
        instance.delete()


class PatientListCreateAdminView(generics.ListCreateAPIView):
    serializer_class = PatientSerializer

    def get_queryset(self):
        search = self.request.GET.get('search', '').strip()
        qs = Patient.objects.all().order_by('name')
        if search:
            qs = qs.filter(
                Q(name__icontains=search) | Q(patient_no__icontains=search)
            )
        return qs[:50]

    def perform_create(self, serializer):
        instance = serializer.save()
        AuditLog.objects.create(
            performed_by=_actor(self.request),
            action='CREATE',
            entity_type='Patient',
            entity_id=instance.patient_no,
            description=f'Patient added via admin: {instance.name} ({instance.patient_no})',
        )


class PatientDetailAdminView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Patient.objects.all()
    serializer_class = PatientSerializer
    lookup_field = 'patient_no'
    lookup_url_kwarg = 'patient_no'

    def perform_update(self, serializer):
        instance = serializer.save()
        AuditLog.objects.create(
            performed_by=_actor(self.request),
            action='UPDATE',
            entity_type='Patient',
            entity_id=instance.patient_no,
            description=f'Patient updated: {instance.name} ({instance.patient_no})',
        )

    def perform_destroy(self, instance):
        AuditLog.objects.create(
            performed_by=_actor(self.request),
            action='DELETE',
            entity_type='Patient',
            entity_id=instance.patient_no,
            description=f'Patient deleted: {instance.name} ({instance.patient_no})',
        )
        instance.delete()


class TreatmentListCreateAdminView(generics.ListCreateAPIView):
    queryset = Treatment.objects.all().order_by('category', 'name')
    serializer_class = TreatmentSerializer

    def perform_create(self, serializer):
        instance = serializer.save()
        AuditLog.objects.create(
            performed_by=_actor(self.request),
            action='CREATE',
            entity_type='Treatment',
            entity_id=str(instance.id),
            description=f'Treatment added: {instance.name} ({instance.code})',
        )


class TreatmentDetailAdminView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Treatment.objects.all()
    serializer_class = TreatmentSerializer

    def perform_update(self, serializer):
        instance = serializer.save()
        AuditLog.objects.create(
            performed_by=_actor(self.request),
            action='UPDATE',
            entity_type='Treatment',
            entity_id=str(instance.id),
            description=f'Treatment updated: {instance.name} ({instance.code})',
        )

    def perform_destroy(self, instance):
        AuditLog.objects.create(
            performed_by=_actor(self.request),
            action='DELETE',
            entity_type='Treatment',
            entity_id=str(instance.id),
            description=f'Treatment deleted: {instance.name} ({instance.code})',
        )
        instance.delete()


class BeauticianListCreateAdminView(generics.ListCreateAPIView):
    queryset = Beauticians.objects.all().order_by('beautician_name')
    serializer_class = BeauticiansSerializer

    def perform_create(self, serializer):
        instance = serializer.save()
        AuditLog.objects.create(
            performed_by=_actor(self.request),
            action='CREATE',
            entity_type='Beautician',
            entity_id=str(instance.id),
            description=f'Beautician added: {instance.beautician_name}',
        )


class BeauticianDetailAdminView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Beauticians.objects.all()
    serializer_class = BeauticiansSerializer

    def perform_update(self, serializer):
        instance = serializer.save()
        AuditLog.objects.create(
            performed_by=_actor(self.request),
            action='UPDATE',
            entity_type='Beautician',
            entity_id=str(instance.id),
            description=f'Beautician updated: {instance.beautician_name}',
        )

    def perform_destroy(self, instance):
        AuditLog.objects.create(
            performed_by=_actor(self.request),
            action='DELETE',
            entity_type='Beautician',
            entity_id=str(instance.id),
            description=f'Beautician deleted: {instance.beautician_name}',
        )
        instance.delete()


class AppUserListCreateAdminView(generics.ListCreateAPIView):
    queryset = AppUser.objects.all().order_by('display_name')
    serializer_class = AppUserAdminSerializer

    def perform_create(self, serializer):
        instance = serializer.save()
        AuditLog.objects.create(
            performed_by=_actor(self.request),
            action='CREATE',
            entity_type='AppUser',
            entity_id=str(instance.id),
            description=f'User created: {instance.display_name} ({instance.role})',
        )


class AppUserDetailAdminView(generics.RetrieveUpdateDestroyAPIView):
    queryset = AppUser.objects.all()
    serializer_class = AppUserAdminSerializer

    def perform_update(self, serializer):
        instance = serializer.save()
        AuditLog.objects.create(
            performed_by=_actor(self.request),
            action='UPDATE',
            entity_type='AppUser',
            entity_id=str(instance.id),
            description=f'User updated: {instance.display_name} ({instance.role})',
        )

    def perform_destroy(self, instance):
        AuditLog.objects.create(
            performed_by=_actor(self.request),
            action='DELETE',
            entity_type='AppUser',
            entity_id=str(instance.id),
            description=f'User deleted: {instance.display_name}',
        )
        instance.delete()


# ── Chart of Accounts ──────────────────────────────────────────────────────

class ChartOfAccountsListCreateView(generics.ListCreateAPIView):
    serializer_class = ChartOfAccountsSerializer

    def get_queryset(self):
        qs = ChartOfAccounts.objects.all().order_by('account_number')
        range_param = self.request.query_params.get('range', '').strip()
        if range_param == 'cash':
            qs = qs.filter(account_number__gte=1100000, account_number__lte=1199999)
        elif range_param == 'inventory':
            qs = qs.filter(account_number__gte=1200000, account_number__lte=1299999)
        return qs

    def perform_create(self, serializer):
        instance = serializer.save()
        AuditLog.objects.create(
            performed_by=_actor(self.request),
            action='CREATE',
            entity_type='ChartOfAccounts',
            entity_id=str(instance.id),
            description=f'Account created: {instance.account_number} – {instance.name}',
        )


class ChartOfAccountsDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = ChartOfAccounts.objects.all()
    serializer_class = ChartOfAccountsSerializer

    def perform_update(self, serializer):
        instance = serializer.save()
        AuditLog.objects.create(
            performed_by=_actor(self.request),
            action='UPDATE',
            entity_type='ChartOfAccounts',
            entity_id=str(instance.id),
            description=f'Account updated: {instance.account_number} – {instance.name}',
        )

    def perform_destroy(self, instance):
        if instance.is_system:
            raise ValidationError('System accounts cannot be deleted.')
        AuditLog.objects.create(
            performed_by=_actor(self.request),
            action='DELETE',
            entity_type='ChartOfAccounts',
            entity_id=str(instance.id),
            description=f'Account deleted: {instance.account_number} – {instance.name}',
        )
        instance.delete()


# ── Treatment Categories ────────────────────────────────────────────────────

class TreatmentCategoryListCreateView(generics.ListCreateAPIView):
    queryset = TreatmentCategory.objects.all()
    serializer_class = TreatmentCategorySerializer

    def perform_create(self, serializer):
        instance = serializer.save()
        AuditLog.objects.create(
            performed_by=_actor(self.request),
            action='CREATE',
            entity_type='TreatmentCategory',
            entity_id=str(instance.id),
            description=f'Treatment category added: {instance.name}',
        )


class TreatmentCategoryDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = TreatmentCategory.objects.all()
    serializer_class = TreatmentCategorySerializer

    def perform_update(self, serializer):
        instance = serializer.save()
        AuditLog.objects.create(
            performed_by=_actor(self.request),
            action='UPDATE',
            entity_type='TreatmentCategory',
            entity_id=str(instance.id),
            description=f'Treatment category updated: {instance.name}',
        )

    def perform_destroy(self, instance):
        AuditLog.objects.create(
            performed_by=_actor(self.request),
            action='DELETE',
            entity_type='TreatmentCategory',
            entity_id=str(instance.id),
            description=f'Treatment category deleted: {instance.name}',
        )
        instance.delete()


# ── Excel template + import ───────────────────────────────────────────────────

class TreatmentTemplateView(APIView):
    """GET — download a blank .xlsx template for bulk treatment import."""

    def get(self, request):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Treatments'

        headers = ['code', 'name', 'category', 'price', 'active']
        ws.append(headers)
        for cell in ws[1]:
            cell.font      = _HEADER_FONT
            cell.fill      = _HEADER_FILL
            cell.alignment = Alignment(horizontal='center')

        notes = [
            'Unique treatment code (required)',
            'Treatment name (required)',
            'Category name (optional)',
            'Price in IDR (required, e.g. 150000)',
            'yes / no (optional, default yes)',
        ]
        ws.append(notes)
        note_font = Font(italic=True, color='595959')
        for cell in ws[2]:
            cell.font      = note_font
            cell.alignment = Alignment(wrap_text=True)

        examples = [
            ('FC-001', 'Facial Basic',    'Facial',    150000, 'yes'),
            ('FC-002', 'Facial Premium',  'Facial',    250000, 'yes'),
            ('SK-001', 'Skin Brightening', 'Skincare', 320000, 'yes'),
        ]
        for row in examples:
            ws.append(list(row))

        col_widths = [16, 30, 20, 16, 12]
        for i, width in enumerate(col_widths, start=1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = width

        ws.row_dimensions[2].height = 36

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        response = HttpResponse(
            buf.read(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        response['Content-Disposition'] = 'attachment; filename="treatments_template.xlsx"'
        return response


class TreatmentImportView(APIView):
    """
    POST multipart/form-data with file=<xlsx>
    Columns (row 1 = header, skipped):
      code | name | category | price | active
    active: yes/true/1 → True; no/false/0 → False; blank → True
    Returns { created, updated, errors: [{row, message}] }
    """

    def post(self, request):
        uploaded = request.FILES.get('file')
        if not uploaded:
            return Response({'error': 'No file uploaded.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            wb = openpyxl.load_workbook(io.BytesIO(uploaded.read()), read_only=True, data_only=True)
        except Exception:
            return Response({'error': 'Could not parse file. Upload a valid .xlsx file.'}, status=status.HTTP_400_BAD_REQUEST)

        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return Response({'error': 'File is empty.'}, status=status.HTTP_400_BAD_REQUEST)

        created = updated = 0
        errors = []

        for i, row in enumerate(rows[1:], start=2):
            padded = list(row) + [None] * 5
            code      = str(padded[0]).strip() if padded[0] is not None else ''
            name      = str(padded[1]).strip() if padded[1] is not None else ''
            category  = str(padded[2]).strip() if padded[2] is not None else ''
            price_raw = padded[3]
            active_raw = padded[4]

            if not code:
                errors.append({'row': i, 'message': 'Code is required.'})
                continue
            if not name:
                errors.append({'row': i, 'message': f'Row {i}: name is required.'})
                continue
            try:
                price = float(price_raw) if price_raw not in (None, '') else None
                if price is None:
                    raise ValueError
            except (ValueError, TypeError):
                errors.append({'row': i, 'message': f'Row {i}: invalid price "{price_raw}".'})
                continue

            if active_raw is None or str(active_raw).strip() == '':
                active = True
            else:
                active = str(active_raw).strip().lower() in ('yes', 'true', '1')

            try:
                with transaction.atomic():
                    obj, was_created = Treatment.objects.update_or_create(
                        code=code,
                        defaults={'name': name, 'category': category, 'price': price, 'active': active},
                    )
                    AuditLog.objects.create(
                        performed_by=_actor(request),
                        action='CREATE' if was_created else 'UPDATE',
                        entity_type='Treatment',
                        entity_id=str(obj.id),
                        description=f'Treatment {"imported" if was_created else "updated via import"}: {obj.name} ({obj.code})',
                    )
                if was_created:
                    created += 1
                else:
                    updated += 1
            except Exception as exc:
                errors.append({'row': i, 'message': f'Row {i}: {exc}'})

        return Response({'created': created, 'updated': updated, 'errors': errors}, status=status.HTTP_200_OK)
