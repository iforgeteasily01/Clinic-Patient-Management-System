from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import generics
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from ..api.serializers import MANAGING_ROLES, PatientNoteSerializer
from ..models import ActivePatient, AppUser, Patient, PatientNote


def _parse_date_param(params, key):
    raw = (params.get(key) or '').strip()
    if not raw:
        return None
    try:
        value = parse_date(raw)
    except ValueError:
        value = None
    if value is None:
        raise ValidationError({key: 'Expected a YYYY-MM-DD date.'})
    return value


class PatientNoteListCreateView(generics.ListCreateAPIView):
    serializer_class = PatientNoteSerializer

    def get_queryset(self):
        params = self.request.query_params

        patient_no = (params.get('patient_no') or '').strip()
        raw_visit = (params.get('active_patient_id') or '').strip()

        subject = None
        if patient_no:
            subject = Q(patient_no_id=patient_no)
        if raw_visit.isdigit():
            visit_q = Q(active_patient_id=int(raw_visit))
            subject = visit_q if subject is None else (subject | visit_q)

        if subject is None:
            return PatientNote.objects.none()

        qs = PatientNote.objects.filter(subject)

        if (params.get('today') or '').strip() in ('1', 'true', 'True'):
            qs = qs.filter(date=timezone.localdate())

        exact = _parse_date_param(params, 'date')
        if exact is not None:
            qs = qs.filter(date=exact)

        date_from = _parse_date_param(params, 'date_from')
        if date_from is not None:
            qs = qs.filter(date__gte=date_from)

        date_to = _parse_date_param(params, 'date_to')
        if date_to is not None:
            qs = qs.filter(date__lte=date_to)

        return qs.select_related('author_user').order_by('-date', '-created_at')

    def create(self, request, *args, **kwargs):
        patient_no = (request.data.get('patient_no') or '').strip()
        raw_visit = request.data.get('active_patient_id')

        patient = None
        if patient_no:
            try:
                patient = Patient.objects.get(patient_no=patient_no)
            except Patient.DoesNotExist:
                raise ValidationError({'patient_no': 'Patient not found.'})

        active_patient = None
        if raw_visit not in (None, '', 'null'):
            try:
                active_patient = ActivePatient.objects.select_related('patient_no').get(
                    pk=int(raw_visit),
                )
            except (TypeError, ValueError):
                raise ValidationError({'active_patient_id': 'Expected an integer id.'})
            except ActivePatient.DoesNotExist:
                raise ValidationError({'active_patient_id': 'Visit not found.'})
            if patient is None and active_patient.patient_no_id:
                patient = active_patient.patient_no

        if patient is None and active_patient is None:
            raise ValidationError(
                {'patient_no': 'Provide patient_no or active_patient_id.'},
            )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        save_kwargs = {'patient_no': patient, 'active_patient': active_patient}
        if not serializer.validated_data.get('date'):
            save_kwargs['date'] = timezone.localdate()

        actor = request.user if isinstance(request.user, AppUser) else None
        if actor is not None:
            save_kwargs['author_user'] = actor
            save_kwargs['author_role'] = actor.role
            if not (serializer.validated_data.get('author') or '').strip():
                save_kwargs['author'] = actor.display_name

        serializer.save(**save_kwargs)
        return Response(serializer.data, status=201)


class PatientNoteDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = PatientNoteSerializer
    queryset = PatientNote.objects.select_related('author_user')

    def _assert_can_modify(self, note):
        user = self.request.user
        if not isinstance(user, AppUser):
            raise PermissionDenied('Authentication required.')
        if note.author_user_id and note.author_user_id == user.id:
            return
        if user.role in MANAGING_ROLES:
            return
        raise PermissionDenied(
            'Only the note author, a manager, or a superuser can change this note.',
        )

    def perform_update(self, serializer):
        self._assert_can_modify(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        self._assert_can_modify(instance)
        instance.delete()
