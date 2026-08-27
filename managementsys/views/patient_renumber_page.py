"""Change a patient's number.

Its own endpoint rather than a field on ``PUT /api/admin/patients/<no>/``: this
is not an edit, it is a multi-table migration of one patient's entire history,
and it needs a confirmation the operator has actually read. Keeping it separate
also lets the ordinary patient form stay open to every role that already uses
it, while this one stays with the Admin account.

See :mod:`managementsys.services.patient_renumber` for what it moves and why it
is safe to move it.
"""
from rest_framework.response import Response
from rest_framework.views import APIView

from ..auth_backend import IsAppAuthenticated
from ..api.serializers import PatientSerializer
from ..models import Patient
from ..services.patient_renumber import (
    PatientRenumberError, normalize, preview, renumber,
)

#: The Admin account only. ``create_default_admin`` seeds it as
#: ``display_name='Admin', role='superuser'`` — "admin" is that account, not a
#: separate role. Deliberately narrower than the rest of the patient form: a
#: manager edits patients all day, and this is the one action on that screen
#: that rewrites a chart's identity everywhere it appears.
RENUMBER_ROLES = ('superuser',)


class PatientRenumberView(APIView):
    """``GET`` previews the change, ``POST`` performs it.

    ``GET /api/admin/patients/<patient_no>/renumber/``
        Counts what would move. Writes nothing.

    ``POST /api/admin/patients/<patient_no>/renumber/``
        Body ``{"new_patient_no": "J000105"}``. Returns the renumbered patient
        plus a per-table summary of what was carried across.
    """
    permission_classes = [IsAppAuthenticated]

    def _guard(self, request, patient_no):
        if request.user.role not in RENUMBER_ROLES:
            return None, Response(
                {'error': 'Only the Admin (superuser) account may change a patient number.'},
                status=403)

        patient = Patient.objects.filter(patient_no=normalize(patient_no)).first()
        if patient is None:
            # Fall back to an exact match: a legacy number may be lower-case.
            patient = Patient.objects.filter(patient_no=patient_no).first()
        if patient is None:
            return None, Response({'error': f'Patient {patient_no} not found.'}, status=404)

        return patient, None

    def get(self, request, patient_no):
        patient, refusal = self._guard(request, patient_no)
        if refusal is not None:
            return refusal
        return Response(preview(patient))

    def post(self, request, patient_no):
        patient, refusal = self._guard(request, patient_no)
        if refusal is not None:
            return refusal

        try:
            summary = renumber(patient, request.data.get('new_patient_no'), actor=request.user)
        except PatientRenumberError as exc:
            return Response(exc.errors, status=400)

        return Response({
            **summary,
            'patient': PatientSerializer(patient, context={'request': request}).data,
        })
