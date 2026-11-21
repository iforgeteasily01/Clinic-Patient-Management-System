import re

from django.db import connection, transaction

from ..models import AuditLog, MedRec, Patient

VALID_PATIENT_NO = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/-]*$')

MAX_LENGTH = Patient._meta.get_field('patient_no').max_length


class PatientRenumberError(Exception):
    def __init__(self, errors):
        super().__init__(str(errors))
        self.errors = errors


def normalize(raw) -> str:
    return str(raw or '').strip().upper()


def validate_new_number(old_no: str, new_no: str) -> str:
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
    old_no = patient.patient_no
    new_no = validate_new_number(old_no, new_no)

    with connection.cursor() as cursor:
        cursor.execute(
            f'SELECT 1 FROM {Patient._meta.db_table} WHERE patient_no = %s FOR UPDATE',
            [old_no],
        )
        if cursor.fetchone() is None:
            raise PatientRenumberError({'patient_no': [f'Patient {old_no} no longer exists.']})

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


def _related_columns():
    columns = []
    for relation in Patient._meta.related_objects:
        field = relation.field

        if relation.many_to_many:
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
    return MedRec.objects.filter(medrec_id__startswith=f'MR-{patient_no}-')


def _rewrite_medrec_ids(old_no: str, new_no: str) -> dict:
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
