from django.db import transaction
from django.utils import timezone

from ..models import ActivePatient, Beauticians, BeauticianAssignment, TreatmentSession

IN_TREATMENT = 4


def open_assignment(active_patient):
    return (
        BeauticianAssignment.objects
        .filter(active_patient=active_patient, ended_at__isnull=True)
        .select_related('beautician')
        .order_by('-assigned_at', '-id')
        .first()
    )


def busy_beautician_ids(exclude_visit=None):
    live = ActivePatient.objects.filter(status=IN_TREATMENT)
    if exclude_visit is not None:
        live = live.exclude(pk=exclude_visit.pk)

    busy = set(
        BeauticianAssignment.objects
        .filter(active_patient__in=live, ended_at__isnull=True, beautician__isnull=False)
        .values_list('beautician_id', flat=True)
    )
    busy |= set(
        TreatmentSession.objects
        .filter(active_patient__in=live.exclude(beautician_assignments__isnull=False))
        .exclude(beautician__isnull=True)
        .values_list('beautician_id', flat=True)
    )
    return busy


def _free_unless_busy(beautician_ids, exclude_visit=None):
    if not beautician_ids:
        return
    still_busy = busy_beautician_ids(exclude_visit=exclude_visit)
    Beauticians.objects.filter(
        id__in=set(beautician_ids) - still_busy,
    ).update(available=True)


def assign(active_patient, beautician, *, actor=None, reason='', branch=None):
    with transaction.atomic():
        current = open_assignment(active_patient)
        if current is not None and current.beautician_id == beautician.id:
            Beauticians.objects.filter(pk=beautician.pk).update(available=False)
            return current, None

        now = timezone.now()
        if current is not None:
            current.ended_at = now
            current.save(update_fields=['ended_at'])

        assignment = BeauticianAssignment.objects.create(
            active_patient=active_patient,
            patient_no_id=active_patient.patient_no_id,
            guest_name=active_patient.guest_name,
            beautician=beautician,
            branch=branch if branch is not None else active_patient.branch,
            assigned_at=now,
            assigned_by=actor,
            reason=(reason or '')[:200],
        )
        Beauticians.objects.filter(pk=beautician.pk).update(available=False)

        if current is not None and current.beautician_id:
            _free_unless_busy([current.beautician_id])
        return assignment, current


def release(active_patient):
    tied = set(
        TreatmentSession.objects.filter(active_patient=active_patient)
        .exclude(beautician__isnull=True)
        .values_list('beautician_id', flat=True)
    )
    tied |= set(
        BeauticianAssignment.objects.filter(active_patient=active_patient)
        .exclude(beautician__isnull=True)
        .values_list('beautician_id', flat=True)
    )
    BeauticianAssignment.objects.filter(
        active_patient=active_patient, ended_at__isnull=True,
    ).update(ended_at=timezone.now())
    _free_unless_busy(tied, exclude_visit=active_patient)


def history(active_patient):
    return list(
        BeauticianAssignment.objects
        .filter(active_patient=active_patient)
        .select_related('beautician', 'assigned_by')
        .order_by('assigned_at', 'id')
    )
