import json
import logging
import os
import threading
import urllib.request
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

VERCEL_INGEST_URL = os.environ.get(
    'VERCEL_INGEST_URL',
    'https://cpms-dashboard-api.vercel.app/api/ingest',
)
VERCEL_INGEST_SECRET = os.environ.get('VERCEL_INGEST_SECRET', '')


def _post(payload: dict):
    if not VERCEL_INGEST_SECRET:
        logger.warning('[vercel_push] VERCEL_INGEST_SECRET not set — skipping push.')
        return

    try:
        body = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            VERCEL_INGEST_URL,
            data=body,
            headers={
                'Content-Type':  'application/json',
                'x-cpms-secret': VERCEL_INGEST_SECRET,
            },
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status != 200:
                logger.warning('[vercel_push] Unexpected status %s', resp.status)
    except Exception as exc:
        logger.warning('[vercel_push] Push failed: %s', exc)


def push(event_type: str, patient_name: str | None, patient_no: str | None, payload: dict):
    data = {
        'event_type':   event_type,
        'timestamp':    datetime.now(timezone.utc).isoformat(),
        'patient_name': patient_name,
        'patient_no':   patient_no,
        'payload':      payload,
    }
    thread = threading.Thread(target=_post, args=(data,), daemon=True)
    thread.start()
