import io

import openpyxl
from django.db.models import Q
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import AssessmentCode
from ..api.serializers import AssessmentCodeSerializer


class AssessmentCodeListCreateView(generics.ListCreateAPIView):
    serializer_class = AssessmentCodeSerializer

    def get_queryset(self):
        qs = AssessmentCode.objects.all()
        search = self.request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(Q(code__icontains=search) | Q(description__icontains=search))
        return qs


class AssessmentCodeDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = AssessmentCode.objects.all()
    serializer_class = AssessmentCodeSerializer


class AssessmentCodeImportPreviewView(APIView):
    """Parse uploaded .xlsx and return rows for user review — no DB writes.

    Expected columns (row 1 = header, skipped):
      A: Code        — ICD-10 code, e.g. L70.0
      B: Description — human-readable label
      C: Category    — 1 / "Common" or 2 / "Uncommon" (default: Common)
      D: Active      — TRUE/1/Yes or FALSE/0/No (default: True)
    """

    def post(self, request):
        uploaded = request.FILES.get('file')
        if not uploaded:
            return Response({'error': 'No file uploaded.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            wb = openpyxl.load_workbook(io.BytesIO(uploaded.read()), read_only=True, data_only=True)
        except Exception:
            return Response(
                {'error': 'Could not parse file. Upload a valid .xlsx file.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ws = wb.active
        rows = []
        errors = []

        for i, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            if not any(cell is not None for cell in row):
                continue

            code_val = str(row[0]).strip().upper() if row[0] is not None else ''
            desc_val = str(row[1]).strip() if len(row) > 1 and row[1] is not None else ''

            raw_cat = row[2] if len(row) > 2 else None
            if raw_cat is None or str(raw_cat).strip() == '':
                category = 1
            elif str(raw_cat).strip().lower() in ('1', 'common'):
                category = 1
            elif str(raw_cat).strip().lower() in ('2', 'uncommon'):
                category = 2
            else:
                errors.append(f'Row {i}: invalid category "{raw_cat}" — use 1/Common or 2/Uncommon.')
                continue

            raw_active = row[3] if len(row) > 3 else None
            if raw_active is None or str(raw_active).strip() == '':
                active = True
            else:
                active = str(raw_active).strip().lower() not in ('false', '0', 'no')

            if not code_val:
                errors.append(f'Row {i}: Code is empty.')
                continue
            if not desc_val:
                errors.append(f'Row {i}: Description is empty.')
                continue

            rows.append({'code': code_val, 'description': desc_val, 'category': category, 'active': active})

        return Response({'rows': rows, 'errors': errors})


class AssessmentCodeImportConfirmView(APIView):
    """Bulk create / update assessment codes from previewed rows."""

    def post(self, request):
        rows = request.data.get('rows', [])
        if not rows:
            return Response({'error': 'No rows to import.'}, status=status.HTTP_400_BAD_REQUEST)

        created = updated = skipped = 0
        for row in rows:
            code = str(row.get('code', '')).strip().upper()
            desc = str(row.get('description', '')).strip()
            category = int(row.get('category', 1))
            active = bool(row.get('active', True))
            if not code or not desc:
                skipped += 1
                continue
            _, was_created = AssessmentCode.objects.update_or_create(
                code=code,
                defaults={'description': desc, 'category': category, 'active': active},
            )
            if was_created:
                created += 1
            else:
                updated += 1

        return Response({'created': created, 'updated': updated, 'skipped': skipped})
