import threading
import time

from django.core.management.base import BaseCommand

from managementsys.vercel_push import push, VERCEL_INGEST_SECRET


class Command(BaseCommand):
    help = 'Send a test invoice_completed push to Vercel and report the result.'

    def handle(self, *args, **options):
        if not VERCEL_INGEST_SECRET:
            self.stdout.write('FAILED: VERCEL_INGEST_SECRET is not set — check your .env file.')
            return

        result = {'ok': False, 'error': None}
        original_post = None

        # Monkey-patch _post so we can capture the outcome synchronously
        import managementsys.vercel_push as vp
        import urllib.request
        import json

        def _post_capture(payload: dict):
            try:
                body = json.dumps(payload).encode('utf-8')
                req = urllib.request.Request(
                    vp.VERCEL_INGEST_URL,
                    data=body,
                    headers={
                        'Content-Type':  'application/json',
                        'x-cpms-secret': vp.VERCEL_INGEST_SECRET,
                    },
                    method='POST',
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    status = resp.status
                    body_resp = resp.read().decode('utf-8', errors='replace')
                if status == 200:
                    result['ok'] = True
                else:
                    result['error'] = f'HTTP {status}: {body_resp[:200]}'
            except Exception as exc:
                result['error'] = str(exc)

        original_post = vp._post
        vp._post = _post_capture

        push(
            event_type='invoice_completed',
            patient_name='Test Patient',
            patient_no='TST-001',
            payload={
                'invoice_number': 'INV-TEST-001',
                'grand_total': '150.00',
                'payment_method_name': 'Cash',
                'items': [
                    {'name': 'Deep Tissue Massage', 'category': 'Massage', 'qty': '1', 'price': '90.00'},
                    {'name': 'Facial Scrub',         'category': 'Facial',  'qty': '2', 'price': '30.00'},
                ],
            },
        )

        # Wait for the background thread to finish (max 12 s)
        deadline = time.time() + 12
        while time.time() < deadline and not result['ok'] and result['error'] is None:
            time.sleep(0.1)

        vp._post = original_post  # restore

        if result['ok']:
            self.stdout.write('SUCCESS')
        else:
            reason = result['error'] or 'timed out waiting for response'
            self.stdout.write(f'FAILED: {reason}')
