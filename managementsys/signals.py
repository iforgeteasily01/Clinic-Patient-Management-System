"""
signals.py
Django signal handlers that push events to Vercel after:
  - A patient checks in      (ActivePatient created)
  - A treatment session starts (TreatmentSession created)
  - An invoice is created    (Invoice created)
"""
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import ActivePatient, Invoice, TreatmentSession
from .vercel_push import push


# ── Patient check-in ──────────────────────────────────────────────────────────

@receiver(post_save, sender=ActivePatient)
def on_active_patient_created(sender, instance, created, **kwargs):
    """Push a checkin event the moment a patient arrives."""
    if not created:
        return

    # Resolve patient name (registered patient or walk-in guest)
    if instance.patient_no_id and instance.patient_no:
        patient_name = instance.patient_no.name
        patient_no   = instance.patient_no.patient_no
    else:
        patient_name = instance.guest_name or 'Guest'
        patient_no   = None

    push(
        event_type   = 'checkin',
        patient_name = patient_name,
        patient_no   = patient_no,
        payload      = {},
    )


# ── Treatment session starts ──────────────────────────────────────────────────

@receiver(post_save, sender=TreatmentSession)
def on_treatment_session_created(sender, instance, created, **kwargs):
    """Push an in_treatment event when a beautician starts a session."""
    if not created:
        return

    if instance.patient_no_id and instance.patient_no:
        patient_name = instance.patient_no.name
        patient_no   = instance.patient_no.patient_no
    elif instance.active_patient_id and instance.active_patient:
        ap = instance.active_patient
        patient_name = (
            ap.patient_no.name if ap.patient_no_id and ap.patient_no
            else ap.guest_name or 'Guest'
        )
        patient_no = ap.patient_no.patient_no if ap.patient_no_id and ap.patient_no else None
    else:
        patient_name = 'Guest'
        patient_no   = None

    push(
        event_type   = 'in_treatment',
        patient_name = patient_name,
        patient_no   = patient_no,
        payload      = {},
    )


# ── Invoice completed ─────────────────────────────────────────────────────────

@receiver(post_save, sender=Invoice)
def on_invoice_created(sender, instance, created, **kwargs):
    """Push an invoice_completed event when an invoice is saved."""
    if not created:
        return

    # Resolve patient
    if instance.patient_no_id and instance.patient_no:
        patient_name = instance.patient_no.name
        patient_no   = instance.patient_no.patient_no
    else:
        patient_name = None
        patient_no   = None

    # Resolve payment method name
    payment_method_name = None
    if instance.payment_method_id and instance.payment_method:
        payment_method_name = instance.payment_method.name

    # Build line items from related InvoiceItems
    # Use prefetch if available, otherwise hit DB (signal context may not have prefetch)
    try:
        items = [
            {
                'name':     line.item.name if line.item_id and line.item else line.item_name,
                'category': (
                    line.item.item_category.name
                    if line.item_id and line.item
                    and line.item.item_category_id and line.item.item_category
                    else 'Uncategorised'
                ),
                'qty':      str(line.quantity),
                'price':    str(line.price),
            }
            for line in instance.items.select_related('item__item_category').all()
        ]
    except Exception:
        items = []

    push(
        event_type   = 'invoice_completed',
        patient_name = patient_name,
        patient_no   = patient_no,
        payload      = {
            'invoice_number':      instance.invoice_number,
            'grand_total':         str(instance.grand_total),
            'payment_method_name': payment_method_name,
            'items':               items,
        },
    )
