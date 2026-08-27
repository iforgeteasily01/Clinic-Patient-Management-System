"""Change a patient's number — the primary key — and carry the chart with it.

``Patient.patient_no`` is the primary key, so this is not a field edit. Every
row in every table that references the patient has to move in the same breath,
and a medical record whose id embeds the old number has to be rewritten. Django
cannot do this: assigning a new pk and calling ``save()`` writes a **second**
row and orphans the first, which is why ``PatientSerializer`` refuses the field
on update and this module is the only way in.

**Why it exists at all.** Patient numbers are normally generated here and never
touched. But the clinic reconciles against an external system, and when that
system is the authority on a patient's number the clinic's copy has to be
corrected rather than a duplicate chart created.

Three things make it safe:

* **One transaction.** Django creates every FK to ``Patient`` as
  ``DEFERRABLE INITIALLY DEFERRED``, so the parent key can change before the
  children are updated — the constraints are checked once, at commit. A failure
  anywhere leaves the patient exactly as they were.
* **The parent row is locked first.** ``SELECT … FOR UPDATE`` conflicts with the
  ``FOR KEY SHARE`` lock PostgreSQL takes on a parent row when a child row
  referencing it is inserted. Without it a visit checked in *during* the rename
  could be written against the old number after the sweep passed that table and
  be left dangling.
* **The tables are discovered, not listed.** They come from
  ``Patient._meta.related_objects`` at call time, so an FK added next year moves
  too instead of being silently forgotten by a hard-coded list.
"""
import re

from django.db import connection, transaction

from ..models import AuditLog, MedRec, Patient

#: What a patient number may look like. Deliberately loose — the canonical form
#: is ``{initial}{6 digits}`` but legacy and imported numbers ("PN00691") do not
#: follow it, and the whole point of this operation is to accept what an
#: external system says the number is.
VALID_PATIENT_NO = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/-]*$')

MAX_LENGTH = Patient._meta.get_field('patient_no').max_length


class PatientRenumberError(Exception):
    """Validation failure, carrying a DRF-shaped ``errors`` dict."""

    def __init__(self, errors):
        super().__init__(str(errors))
        self.errors = errors


def normalize(raw) -> str:
    """Trim and upper-case a submitted number. Never invents one."""
    return str(raw or '').strip().upper()


def validate_new_number(old_no: str, new_no: str) -> str:
    """Check ``new_no`` on its own terms. Returns the normalised value."""
    new_no = normalize(new_no)

    if not new_no:
        raise PatientRenumberError({'new_patient_no': ['A new patient number is required.']})
    if len(new_no) > MAX_LENGTH:
        raise PatientRenumberError(
            {'new_patient_no': [f'A patient number is at most {MAX_LENGTH} characters.']})
    if not VALID_PATIENT_NO.match(new_no):
        raise PatientRenumberError(
            {'new_patient_no': ['Use letters, digits, and . _ / - only, starting with a letter or digit.']})
    if new_no == normalize(old_no):
        raise PatientRenumberError(
            {'new_patient_no': ['That is already this patient\'s number.']})
    if Patient.objects.filter(patient_no=new_no).exists():
        raise PatientRenumberError(
            {'new_patient_no': [f'{new_no} already belongs to another patient.']})

    return new_no


def preview(patient: Patient) -> dict:
    """How much history a rename would move, without moving any of it.

    The confirmation dialog shows this: "12 invoices, 40 medical records" is a
    far better prompt than "are you sure?", and it is the operator's only chance
    to notice they are about to renumber the wrong chart.
    """
    counts = {}
    for related in _related_columns():
        model = related['model']
        total = model._base_manager.filter(**{related['field']: patient.pk}).count()
        if total:
            counts[model._meta.verbose_name_plural.title()] = total

    return {
        'patient_no': patient.patient_no,
        'name': patient.name,
        'related_rows': counts,
        'total_rows': sum(counts.values()),
        'medical_records_to_rewrite': _medrec_prefix_queryset(patient.pk).count(),
    }


@transaction.atomic
def renumber(patient: Patient, new_no: str, actor=None) -> dict:
    """Move ``patient`` and everything that points at them to ``new_no``.

    Returns a summary of what was touched. Raises
    :class:`PatientRenumberError` for anything the operator can fix.
    """
    old_no = patient.patient_no
    new_no = validate_new_number(old_no, new_no)

    # Blocks any concurrent write that would attach a new row to the old number
    # while the sweep below is in flight. See the module docstring.
    with connection.cursor() as cursor:
        cursor.execute(
            f'SELECT 1 FROM {Patient._meta.db_table} WHERE patient_no = %s FOR UPDATE',
            [old_no],
        )
        if cursor.fetchone() is None:
            raise PatientRenumberError({'patient_no': [f'Patient {old_no} no longer exists.']})

        # Deferred anyway by Django's schema, but stating it makes the
        # parent-before-children order deliberate rather than incidental.
        cursor.execute('SET CONSTRAINTS ALL DEFERRED')

        cursor.execute(
            f'UPDATE {Patient._meta.db_table} SET patient_no = %s WHERE patient_no = %s',
            [new_no, old_no],
        )

        moved = {}
        for related in _related_columns():
            table = related['model']._meta.db_table
            column = related['column']
            cursor.execute(
                f'UPDATE {table} SET {column} = %s WHERE {column} = %s',
                [new_no, old_no],
            )
            if cursor.rowcount:
                moved[f'{table}.{column}'] = cursor.rowcount

    rewritten = _rewrite_medrec_ids(old_no, new_no)

    AuditLog.objects.create(
        performed_by=actor,
        action='RENUMBER',
        entity_type='Patient',
        entity_id=new_no,
        description=(
            f'Patient number changed: {old_no} → {new_no} ({patient.name}). '
            f'{sum(moved.values())} related row(s) moved, '
            f'{len(rewritten)} medical record id(s) rewritten.'
        ),
        metadata={
            'old_patient_no': old_no,
            'new_patient_no': new_no,
            'moved_rows': moved,
            # Every rewritten id, old → new. A medical record number already
            # printed on a document no longer resolves, so the mapping back to
            # it has to survive somewhere.
            'medrec_ids': rewritten,
        },
    )

    patient.patient_no = new_no
    patient._state.adding = False

    return {
        'old_patient_no': old_no,
        'new_patient_no': new_no,
        'moved_rows': moved,
        'total_rows_moved': sum(moved.values()),
        'medrec_ids_rewritten': len(rewritten),
    }


# ── internals ───────────────────────────────────────────────────────────────

def _related_columns():
    """Every concrete FK/O2O column pointing at ``Patient.patient_no``.

    Read from the model metadata rather than written down, so a relation added
    later is picked up without anyone remembering this file exists.
    """
    columns = []
    for relation in Patient._meta.related_objects:
        field = relation.field

        if relation.many_to_many:
            # No model has an M2M to Patient today. If one is ever added, its
            # through-table column would be silently skipped below and left
            # pointing at a number that no longer exists — so fail loudly here
            # rather than orphan a row nobody would think to look for.
            raise PatientRenumberError({'new_patient_no': [
                f'{relation.related_model.__name__}.{field.name} is a many-to-many to '
                f'Patient. patient_renumber does not handle through-tables yet; teach it '
                f'before renumbering.'
            ]})

        if not field.concrete:
            continue

        columns.append({
            'model': relation.related_model,
            'field': field.name,
            'column': field.column,
        })
    return columns


def _medrec_prefix_queryset(patient_no: str):
    """Medical records whose id embeds ``patient_no`` in the canonical shape.

    ``startswith`` on the full ``MR-<no>-`` prefix, not just the number: without
    the trailing dash, renumbering ``J0001`` would also match ``J00012``'s
    records.
    """
    return MedRec.objects.filter(medrec_id__startswith=f'MR-{patient_no}-')


def _rewrite_medrec_ids(old_no: str, new_no: str) -> dict:
    """Re-point ``MR-<old>-…`` ids at the new number. Returns {old: new}.

    ``medrec_id`` is unique, so a collision would abort the whole transaction
    with a database error the operator cannot read. Collisions are possible in
    exactly one way — the target number was itself used by a patient renamed
    away earlier — so they are detected here and reported as a validation
    failure instead.

    The date parsers elsewhere (``crm_dashboard._medrec_date``, the med-rec
    serializers) scan for the 8-digit part rather than splitting positionally,
    so a changed patient segment does not disturb them.
    """
    records = list(_medrec_prefix_queryset(old_no).only('id', 'medrec_id'))
    if not records:
        return {}

    mapping = {}
    for record in records:
        mapping[record.medrec_id] = f'MR-{new_no}-{record.medrec_id[len(f"MR-{old_no}-"):]}'

    clashes = set(
        MedRec.objects
        .filter(medrec_id__in=list(mapping.values()))
        .exclude(id__in=[r.id for r in records])
        .values_list('medrec_id', flat=True)
    )
    if clashes:
        raise PatientRenumberError({'new_patient_no': [
            f'{new_no} was used by another chart before: medical record id(s) '
            f'{", ".join(sorted(clashes))} already exist. Pick a different number.'
        ]})

    for record in records:
        record.medrec_id = mapping[record.medrec_id]
    MedRec.objects.bulk_update(records, ['medrec_id'])

    return mapping
