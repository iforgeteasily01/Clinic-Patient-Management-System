import json
import urllib.request
import urllib.error
import threading

from django.conf import settings
from rest_framework import generics, status
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from rest_framework.views import APIView

from ..api.serializers import IssueTicketSerializer, IssueTicketImageSerializer
from ..models import IssueTicket, IssueTicketImage


def _push_to_vercel(ticket: IssueTicket):
    """Fire-and-forget: send ticket metadata to the Vercel dashboard."""
    vercel_url = getattr(settings, 'CPMS_VERCEL_URL', '').rstrip('/')
    ingest_secret = getattr(settings, 'CPMS_INGEST_SECRET', '')
    if not vercel_url or not ingest_secret:
        return

    payload = json.dumps({
        'ticket_no':    ticket.ticket_no,
        'submitted_by': ticket.submitted_by.display_name,
        'category':     ticket.category,
        'title':        ticket.title,
        'description':  ticket.description,
        'image_count':  ticket.images.count(),
    }).encode('utf-8')

    req = urllib.request.Request(
        f'{vercel_url}/api/tickets',
        data=payload,
        headers={
            'Content-Type':  'application/json',
            'x-cpms-secret': ingest_secret,
        },
        method='POST',
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # Vercel push is best-effort; don't break the local flow


def _push_status_to_vercel(ticket: IssueTicket):
    """Fire-and-forget: sync status update to Vercel."""
    vercel_url = getattr(settings, 'CPMS_VERCEL_URL', '').rstrip('/')
    ingest_secret = getattr(settings, 'CPMS_INGEST_SECRET', '')
    if not vercel_url or not ingest_secret:
        return

    payload = json.dumps({'ticket_no': ticket.ticket_no, 'status': ticket.status}).encode('utf-8')
    req = urllib.request.Request(
        f'{vercel_url}/api/tickets',
        data=payload,
        headers={
            'Content-Type':  'application/json',
            'x-cpms-secret': ingest_secret,
        },
        method='PATCH',
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


class IssueTicketListCreateView(generics.ListCreateAPIView):
    serializer_class = IssueTicketSerializer

    def get_queryset(self):
        qs = IssueTicket.objects.select_related('submitted_by').prefetch_related('images')
        status_filter = self.request.query_params.get('status', '').strip()
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ticket = serializer.save(submitted_by=request.user)
        threading.Thread(target=_push_to_vercel, args=(ticket,), daemon=True).start()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class IssueTicketDetailView(generics.RetrieveUpdateAPIView):
    serializer_class = IssueTicketSerializer
    queryset = IssueTicket.objects.select_related('submitted_by').prefetch_related('images')

    def update(self, request, *args, **kwargs):
        kwargs['partial'] = True
        response = super().update(request, *args, **kwargs)
        ticket = self.get_object()
        if 'status' in request.data:
            threading.Thread(target=_push_status_to_vercel, args=(ticket,), daemon=True).start()
        return response


class IssueTicketImageUploadView(APIView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, pk):
        try:
            ticket = IssueTicket.objects.get(pk=pk)
        except IssueTicket.DoesNotExist:
            return Response({'error': 'Ticket not found.'}, status=status.HTTP_404_NOT_FOUND)

        image = request.FILES.get('image')
        if not image:
            return Response({'error': 'image file is required.'}, status=status.HTTP_400_BAD_REQUEST)

        ticket_image = IssueTicketImage.objects.create(ticket=ticket, image=image)
        serializer = IssueTicketImageSerializer(ticket_image, context={'request': request})
        return Response(serializer.data, status=status.HTTP_201_CREATED)
