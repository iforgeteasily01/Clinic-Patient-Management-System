"""
Who is looking after a visit, and when that changes hands.

`BeauticianAssignment` is the source of truth for the *current* beautician on a
visit; `TreatmentSession.beautician` records who started each session and is
never rewritten. See the model docstring for why.

Busy/free (`Beauticians.available`) is derived from the open assignments of
visits still in treatment (status 4). A visit started before assignments
existed has none, so for those the session's beautician still counts — the
migration backfills live visits, this keeps anything it missed correct.
"""
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
    """Beauticians holding a visit that is in treatment right now."""
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
    """
    Make `beautician` the one looking after this visit.

    Returns (assignment, previous). `previous` is the assignment that was
    closed, or None when nothing changed hands — the first assignment, or the
    same beautician again (adding treatments mid-visit), which is a no-op so
    the history only records real hand-overs.
    """
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
            # The outgoing beautician is free again unless another patient in
            # treatment still holds them.
            _free_unless_busy([current.beautician_id])
        return assignment, current


def release(active_patient):
    """
    The visit is leaving treatment (finished, dismissed, billed, cleared):
    close its open assignment and free everyone tied to it who is not holding
    another patient in treatment.
    """
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
