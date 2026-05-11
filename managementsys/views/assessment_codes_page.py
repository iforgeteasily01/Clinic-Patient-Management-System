from django.db.models import Q
from rest_framework import generics

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
