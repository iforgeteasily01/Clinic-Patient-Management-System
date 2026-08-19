import secrets
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth.hashers import check_password, make_password
from django.db import models
from django.db.models import Max, Min, Avg, Sum, Count
from django.utils import timezone

# settings.TIME_ZONE is UTC, so anything that must read as clinic-local time
# converts explicitly. Same convention as views/reports_page.py.
JAKARTA_TZ = ZoneInfo('Asia/Jakarta')


class Patient(models.Model):
    patient_no = models.CharField(
        max_length=10, unique=True, primary_key=True, blank=True)
    name = models.CharField(max_length=100, null=True)
    address = models.CharField(max_length=100, null=True, blank=True)
    phone_number = models.CharField(max_length=15, null=True, blank=True)
    NIK = models.CharField(max_length=16, null=True, blank=True)
    # Resolved from the SatuSehat Master Patient Index (by NIK). Unused until the sync phase.
    ihs_id = models.CharField(max_length=64, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # Only generate if not already set
        if not self.patient_no:
            if not self.name:
                raise ValueError("Patient name is required to generate patient_no.")
            prefix = self.name[0].upper()

            # Find the highest counter among existing patient_no values for this
            # prefix. Legacy/imported IDs (e.g. "PN00691") don't follow the
            # canonical {letter}{digits} format, so skip any whose remainder
            # after the prefix isn't purely numeric instead of crashing on int().
            existing_nos = Patient.objects.filter(
                patient_no__startswith=prefix
            ).values_list('patient_no', flat=True)

            max_number = 0
            for pno in existing_nos:
                digits = pno[len(prefix):]
                if digits.isdigit():
                    max_number = max(max_number, int(digits))

            new_number = max_number + 1

            self.patient_no = f"{prefix}{new_number:06d}"  # 7 chars total

        super().save(*args, **kwargs)


# ActivePatient
class ActivePatient(models.Model):
    patient_no = models.ForeignKey(Patient, on_delete=models.SET_NULL, null=True, blank=True)
    guest_name = models.CharField(max_length=100, null=True, blank=True)
    status = models.IntegerField()
    consult_status = models.BooleanField()
    visit_time = models.DateTimeField(auto_now_add=True)
    medrec = models.OneToOneField(
        'MedRec', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='active_visit',
    )

    def __str__(self):
        if self.patient_no_id:
            return str(self.patient_no)
        return self.guest_name or f'Guest #{self.pk}'

# Doctors


class Doctors(models.Model):
    doctor_name = models.CharField(max_length=50)
    # NIK resolves the clinician against the SatuSehat Master Nakes Index, which
    # returns the ihs_id. Both unused until the sync phase.
    nik = models.CharField(max_length=16, null=True, blank=True)
    ihs_id = models.CharField(max_length=64, null=True, blank=True)

    def __str__(self):
        return self.doctor_name

# Beauticians


class Beauticians(models.Model):
    beautician_name = models.CharField(max_length=50)
    bphone_number = models.CharField(max_length=15)
    available = models.BooleanField(default=True)

    def __str__(self):
        return self.beautician_name

# MedicalRecord


class MedRec(models.Model):
    DRAFT = 'draft'
    FINALIZED = 'finalized'
    STATUS_CHOICES = [(DRAFT, 'Draft'), (FINALIZED, 'Finalized')]

    medrec_id = models.CharField(max_length=30, unique=True, blank=True)
    # set null as in, if there is no doctor that takes care of this patient, then doctor_id is Null rather than deleted
    doctor_id = models.ForeignKey(
        Doctors, on_delete=models.SET_NULL, null=True)
    patient_no = models.ForeignKey(Patient, on_delete=models.CASCADE)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=FINALIZED)
    subjective = models.TextField(default="")
    objective = models.TextField(default="")
    assessment = models.TextField(default="")
    assessment_codes = models.JSONField(default=list, blank=True)
    plan = models.TextField(default="")
    sabun = models.TextField(default="", null=True)
    toner = models.TextField(default="", null=True)
    obat1_pagi = models.TextField(default="", null=True)
    obat1_pagi_detail = models.TextField(default="", null=True)
    obat1_malam = models.TextField(default="", null=True)
    obat1_malam_detail = models.TextField(default="", null=True)
    obat2_pagi = models.TextField(default="", null=True)
    obat2_pagi_detail = models.TextField(default="", null=True)
    obat2_malam = models.TextField(default="", null=True)
    obat2_malam_detail = models.TextField(default="", null=True)
    treatment = models.TextField(default="", null=True)
    clinician = models.CharField(max_length=100, default='', blank=True)

    def __str__(self):
        return self.medrec_id

    def save(self, *args, **kwargs):
        if not self.medrec_id:
            visit_date = kwargs.pop('visit_date', None)
            if visit_date:
                date_str = visit_date.replace('-', '')
            else:
                date_str = timezone.now().strftime("%Y%m%d")
            patient_no = self.patient_no.patient_no

            base_id = f"MR-{patient_no}-{date_str}"

            count = MedRec.objects.filter(
                medrec_id__startswith=base_id).count()
            self.medrec_id = f"{base_id}-{count+1}"
        else:
            kwargs.pop('visit_date', None)

        super().save(*args, **kwargs)

# patientStatus


class patientStatus(models.Model):
    status_name = models.CharField(max_length=20)


# Treatment


class TreatmentQuerySet(models.QuerySet):
    def delete(self):
        catalog_ids = list(
            self.filter(catalog_item_id__isnull=False)
                .values_list('catalog_item_id', flat=True)
        )
        result = super().delete()
        if catalog_ids:
            InventoryItem.objects.filter(pk__in=catalog_ids, is_service=True).delete()
        return result


class Treatment(models.Model):
    objects = TreatmentQuerySet.as_manager()

    code = models.CharField(max_length=60, unique=True)
    name = models.CharField(max_length=100)
    category = models.CharField(max_length=50)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)
    catalog_item = models.OneToOneField(
        'InventoryItem',
        null=True, blank=True,
        on_delete=models.PROTECT,
        related_name='treatment',
    )

    class Meta:
        ordering = ['sort_order', 'name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        # Link the mirror catalog item to the matching TreatmentCategory so POS
        # revenue/COGS route to that category's own GL accounts (else fallbacks).
        category = TreatmentCategory.resolve_by_name(self.category)
        if is_new or not self.catalog_item_id:
            item = InventoryItem.objects.create(
                code=self.code,
                name=self.name,
                selling_price=self.price,
                unit_small='session',
                is_service=True,
                is_active=self.active,
                min_stock=0,
                item_category=category,
            )
            Treatment.objects.filter(pk=self.pk).update(catalog_item=item)
            self.catalog_item_id = item.pk
        else:
            InventoryItem.objects.filter(pk=self.catalog_item_id).update(
                code=self.code,
                name=self.name,
                selling_price=self.price,
                is_active=self.active,
                item_category=category,
            )

    def delete(self, *args, **kwargs):
        catalog_item_id = self.catalog_item_id
        super().delete(*args, **kwargs)
        if catalog_item_id:
            InventoryItem.objects.filter(pk=catalog_item_id, is_service=True).delete()


# TreatmentSession


class TreatmentSession(models.Model):
    active_patient = models.ForeignKey(ActivePatient, on_delete=models.SET_NULL, null=True, blank=True)
    patient_no = models.ForeignKey(Patient, on_delete=models.SET_NULL, null=True, blank=True)
    beautician = models.ForeignKey(Beauticians, on_delete=models.SET_NULL, null=True)
    treatments = models.ManyToManyField(Treatment, blank=True)
    session_time = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        label = str(self.patient_no) if self.patient_no_id else (self.active_patient.guest_name if self.active_patient_id else '?')
        return f"{label} - {self.session_time}"


class AppUser(models.Model):
    ROLE_CHOICES = [
        ('superuser',  'Superuser'),
        ('doctor',     'Doctor'),
        ('beautician', 'Beautician'),
        ('cashier',    'Cashier'),
        ('manager',    'Manager'),
    ]

    display_name    = models.CharField(max_length=50)
    pin_hash        = models.CharField(max_length=128)
    role            = models.CharField(max_length=20, choices=ROLE_CHOICES, default='superuser')
    avatar_color      = models.CharField(max_length=7, default='#0284c7')
    theme_primary     = models.CharField(max_length=7, default='#0284c7')
    theme_secondary   = models.CharField(max_length=7, default='#7c3aed')
    theme_background  = models.CharField(max_length=7, default='#f1f5f9')
    profile_picture   = models.ImageField(upload_to='profile_pictures/', null=True, blank=True)
    is_active       = models.BooleanField(default=True)
    auth_token      = models.CharField(max_length=64, blank=True, default='')
    base_salary     = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    def set_pin(self, raw_pin: str):
        self.pin_hash = make_password(str(raw_pin))

    def check_pin(self, raw_pin: str) -> bool:
        return check_password(str(raw_pin), self.pin_hash)

    def generate_token(self) -> str:
        self.auth_token = secrets.token_hex(32)
        self.save(update_fields=['auth_token'])
        return self.auth_token

    def clear_token(self):
        self.auth_token = ''
        self.save(update_fields=['auth_token'])

    def __str__(self):
        return self.display_name


class SoapTemplate(models.Model):
    FIELD_CHOICES = [
        ('subjective', 'Subjective'),
        ('objective',  'Objective'),
        ('assessment', 'Assessment'),
        ('plan',       'Plan'),
    ]
    field      = models.CharField(max_length=20, choices=FIELD_CHOICES)
    title      = models.CharField(max_length=100)
    body       = models.TextField()
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['field', 'sort_order', 'title']

    def __str__(self):
        return f'[{self.field}] {self.title}'


class InventoryItem(models.Model):
    code = models.CharField(max_length=60, unique=True)
    name = models.CharField(max_length=60)
    selling_price = models.DecimalField(max_digits=14, decimal_places=2)
    unit_small = models.CharField(max_length=30)
    unit_medium = models.CharField(max_length=30, blank=True, default='')
    unit_medium_qty = models.PositiveIntegerField(null=True, blank=True)  # small per 1 medium
    unit_large = models.CharField(max_length=30, blank=True, default='')
    unit_large_qty = models.PositiveIntegerField(null=True, blank=True)  # medium per 1 large
    category = models.CharField(max_length=100, blank=True, default='')
    item_category = models.ForeignKey(
        'TreatmentCategory',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='inventory_items',
    )
    legal_code = models.CharField(max_length=100, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    is_service = models.BooleanField(default=False)  # True for treatment-backed non-stock items
    min_stock = models.PositiveIntegerField(default=0)  # in smallest unit
    created_by = models.ForeignKey('AppUser', on_delete=models.SET_NULL, null=True, blank=True, related_name='inv_items_created')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'[{self.code}] {self.name}'


class Warehouse(models.Model):
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'[{self.code}] {self.name}'


class InventoryBatch(models.Model):
    item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT, related_name='batches')
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name='batches')
    input_date = models.DateField()
    quantity_initial = models.DecimalField(max_digits=14, decimal_places=4)    # in smallest unit
    quantity_remaining = models.DecimalField(max_digits=14, decimal_places=4)  # in smallest unit; decremented FIFO
    value = models.DecimalField(max_digits=14, decimal_places=2)  # total batch purchase value
    purchase_invoice = models.ForeignKey(
        'PurchaseInvoice',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='inventory_batches',
    )
    created_by = models.ForeignKey('AppUser', on_delete=models.SET_NULL, null=True, blank=True, related_name='inv_batches_created')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['item', 'input_date', 'created_at']

    def __str__(self):
        return f'{self.item} @ {self.warehouse} [{self.input_date}] {self.quantity_remaining}/{self.quantity_initial}'


class ChartOfAccounts(models.Model):
    ACCOUNT_TYPE_CHOICES = [
        ('asset',         'Asset'),
        ('liability',     'Liability'),
        ('equity',        'Equity'),
        ('revenue',       'Revenue'),
        ('cogs',          'Cost of Goods Sold'),
        ('expense',       'Expense'),
        ('other_income',  'Other Income'),
        ('other_expense', 'Other Expense'),
    ]

    account_number = models.IntegerField(unique=True)
    name           = models.CharField(max_length=100)
    account_type   = models.CharField(max_length=20, choices=ACCOUNT_TYPE_CHOICES)
    balance        = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    is_system      = models.BooleanField(default=False)
    # Head accounts are the top-level grouping accounts (one per account type).
    # Sub-accounts (is_head=False) belong to a head via parent FK.
    is_head        = models.BooleanField(default=False)
    parent         = models.ForeignKey(
        'self',
        null=True, blank=True,
        on_delete=models.PROTECT,
        related_name='sub_accounts',
        limit_choices_to={'is_head': True},
    )

    # Head account number per type — used to auto-file orphan sub-accounts.
    TYPE_HEAD_NUMBER = {
        'asset':         1000000,
        'liability':     2000000,
        'equity':        3000000,
        'revenue':       4000000,
        'cogs':          5000000,
        'expense':       6000000,
        'other_income':  7000000,
        'other_expense': 8000000,
    }

    class Meta:
        ordering = ['account_number']
        verbose_name        = 'Chart of Account'
        verbose_name_plural = 'Chart of Accounts'

    def __str__(self):
        return f'{self.account_number} – {self.name}'

    def save(self, *args, **kwargs):
        # Sub-accounts created in code (per-category revenue/COGS/expense,
        # per-vendor payables) frequently omit ``parent``. Without one they are
        # invisible on the Chart of Accounts page, which renders the tree from
        # the heads down — so file them under their type's head automatically.
        if not self.is_head and self.parent_id is None:
            head_number = self.TYPE_HEAD_NUMBER.get(self.account_type)
            if head_number is not None:
                head = ChartOfAccounts.objects.filter(
                    account_number=head_number, is_head=True
                ).first()
                if head is not None:
                    self.parent = head
        super().save(*args, **kwargs)


class PaymentMethod(models.Model):
    """How a customer/supplier paid, decoupled from the GL account it resolves to.

    Multiple payment methods (e.g. "Cash", "QRIS", "Debit BCA") can all settle
    to the same cash/bank ``ChartOfAccounts`` row, or each can have its own —
    the mapping lives here instead of being baked into the account itself.
    """
    name           = models.CharField(max_length=100)
    code           = models.CharField(max_length=30, blank=True, default='')
    linked_account = models.ForeignKey(
        'ChartOfAccounts',
        on_delete=models.PROTECT,
        related_name='payment_methods',
        limit_choices_to={'account_type': 'asset'},
    )
    is_active  = models.BooleanField(default=True)
    is_system  = models.BooleanField(default=False)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'name']
        verbose_name = 'Payment Method'
        verbose_name_plural = 'Payment Methods'

    def __str__(self):
        return self.name


class AuditLog(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)
    performed_by = models.ForeignKey(AppUser, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=20)  # LOGIN, LOGOUT, CREATE, UPDATE, DELETE, STATUS_CHANGE
    entity_type = models.CharField(max_length=50)
    entity_id = models.CharField(max_length=50, blank=True)
    description = models.TextField()

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f'{self.timestamp:%Y-%m-%d %H:%M} | {self.action} {self.entity_type}'


class PatientPhoto(models.Model):
    patient_no = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name='photos')
    photo_date = models.DateField(default=date.today)
    body_area = models.CharField(max_length=100)
    image = models.ImageField(upload_to='patient_photos/%Y/%m/%d/')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['uploaded_at']

    def __str__(self):
        return f"{self.patient_no_id} - {self.body_area} ({self.photo_date})"


POSTING_STATUS_CHOICES = [('unposted', 'Unposted'), ('posted', 'Posted')]


class Invoice(models.Model):
    invoice_number     = models.CharField(max_length=30, unique=True, blank=True)
    datetime           = models.DateTimeField()
    patient_no         = models.ForeignKey(Patient, on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')
    # payment_method stays, unchanged, PROTECT: PurchasePayment, Expense and the
    # repair_accounting_balance / void_duplicate_billing_invoices commands still
    # reference it, and cash_accounts.cash_bank_account_ids() unions
    # PaymentMethod.linked_account into its allowed set. payment_account below is
    # the direct COA reference new code should use; it is not a replacement, the
    # two are resolved together in journal_engine's payment leg.
    payment_method     = models.ForeignKey('PaymentMethod', on_delete=models.PROTECT, null=True, blank=True, related_name='invoices')
    payment_account    = models.ForeignKey(
        'ChartOfAccounts',
        on_delete=models.PROTECT,
        null=True, blank=True,
        limit_choices_to={'account_type': 'asset'},
        related_name='invoices_received_into',
        help_text='Cash/bank account debited by this invoice. Must be one of '
                  'services.cash_accounts.cash_bank_account_ids().',
    )
    discount           = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    cashier            = models.ForeignKey('AppUser', on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices_as_cashier')
    warehouse          = models.ForeignKey('Warehouse', on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')
    tax                = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    additional_charges = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    grand_total        = models.DecimalField(max_digits=14, decimal_places=2)
    promotion          = models.ForeignKey('Promotion', on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')
    notes              = models.CharField(max_length=500, blank=True, default='')
    promotion_code     = models.CharField(max_length=50, blank=True, default='')
    is_voided          = models.BooleanField(default=False)
    voided_at          = models.DateTimeField(null=True, blank=True)
    voided_by          = models.ForeignKey('AppUser', on_delete=models.SET_NULL, null=True, blank=True, related_name='voided_invoices')
    # Phase 2: journal posting moved off the request path. A freshly created or
    # edited invoice sits 'unposted' until a journal run (POST
    # /api/accounting/journal/run/) sweeps its transaction date. Void/edit of an
    # already-posted invoice is handled live via same-day memo entries instead
    # (see managementsys/services/journal_engine.py).
    posting_status     = models.CharField(max_length=10, choices=POSTING_STATUS_CHOICES, default='unposted')

    class Meta:
        ordering = ['-datetime']

    def __str__(self):
        return self.invoice_number

    def save(self, *args, **kwargs):
        if not self.invoice_number:
            today = timezone.now().strftime("%Y%m%d")
            base = f"INV-{today}"
            count = Invoice.objects.filter(invoice_number__startswith=base).count()
            self.invoice_number = f"{base}-{count + 1}"
        super().save(*args, **kwargs)


class InvoiceItem(models.Model):
    invoice      = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='items')
    item         = models.ForeignKey('InventoryItem', on_delete=models.PROTECT, null=True, blank=True, related_name='invoice_items')
    item_name    = models.CharField(max_length=255, blank=True, default='')
    quantity     = models.DecimalField(max_digits=14, decimal_places=3)
    price        = models.DecimalField(max_digits=14, decimal_places=2)
    discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f"{self.invoice.invoice_number} – {self.item.name} ×{self.quantity}"


class InvoicePayment(models.Model):
    """One tender against a sales invoice — the split-payment row.

    An invoice settled by a single method needs none of these; ``Invoice
    .payment_account`` alone describes it and the journal engine keeps posting
    one debit leg. A split payment (Rp 200.000 cash + Rp 150.000 BCA) writes one
    row per method, and the engine debits each account for its own amount
    instead of collapsing the whole grand total onto the first method's account.

    ``Invoice.payment_method`` / ``payment_account`` stay populated from the
    first row so every existing list, filter, export and receipt that reads a
    single method off the invoice keeps working.

    The rows must sum to ``Invoice.grand_total`` — they are the debit side of
    the entry, not the cash tendered, so change given to the patient is never
    part of them. The API enforces this on write.
    """
    invoice        = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='payments')
    payment_method = models.ForeignKey(
        'PaymentMethod',
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name='invoice_payments',
    )
    payment_account = models.ForeignKey(
        'ChartOfAccounts',
        on_delete=models.PROTECT,
        null=True, blank=True,
        limit_choices_to={'account_type': 'asset'},
        related_name='invoice_payments_into',
        help_text='Cash/bank account debited by this tender. Must be one of '
                  'services.cash_accounts.cash_bank_account_ids().',
    )
    amount     = models.DecimalField(max_digits=14, decimal_places=2)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'id']

    def __str__(self):
        label = self.payment_method.name if self.payment_method_id else (
            self.payment_account.name if self.payment_account_id else '?')
        return f'{self.invoice.invoice_number} – {label} {self.amount}'


class LedgerEntry(models.Model):
    ENTRY_TYPE_CHOICES = [('debit', 'Debit'), ('credit', 'Credit')]
    SOURCE_TYPE_CHOICES = [
        ('invoice',    'Sales Invoice'),
        ('purchase',   'Purchase Invoice'),
        ('transfer',   'Account Transfer'),
        ('adjustment', 'Manual Adjustment'),
        ('stock',      'Stock Movement'),
        ('opname',     'Stock Opname'),
        ('manual',     'Manual Entry'),
        # Phase 2 same-day exception postings — written synchronously when a
        # document whose transaction date is already 'posted' is voided or
        # edited, rather than waiting for the next journal run. Always dated
        # today (the void/edit date), never the original transaction date.
        ('void_memo',  'Void Memo (reversal)'),
        ('edit_memo',  'Edit Memo (reversal + repost)'),
        # Un-void: the document is brought back. Every row currently attached to
        # it is reversed (netting its posted state to zero) and the original
        # entry is re-posted, all dated the restore day.
        ('restore_memo', 'Restore Memo (un-void repost)'),
        # Phase 3 — operating expense accrual/payment postings.
        ('expense',    'Expense'),
    ]

    account          = models.ForeignKey('ChartOfAccounts', on_delete=models.PROTECT, related_name='ledger_entries')
    date             = models.DateField()
    description      = models.CharField(max_length=255)
    entry_type       = models.CharField(max_length=6, choices=ENTRY_TYPE_CHOICES)
    amount           = models.DecimalField(max_digits=18, decimal_places=2)
    source_type      = models.CharField(max_length=15, choices=SOURCE_TYPE_CHOICES, blank=True, default='')
    # The journal document this line belongs to. Nullable only so migration 0098
    # can backfill historic rows in chunks — every line written from Phase 4
    # onward goes through ``write_legs`` and always has one. Grouping lines under
    # a header is what makes a per-entry detail page and correction journals
    # possible; the per-document FKs below are kept because JournalHistoryPage
    # and the financial reports query them directly.
    journal_entry    = models.ForeignKey('JournalEntry', on_delete=models.CASCADE, null=True, blank=True, related_name='lines')
    invoice          = models.ForeignKey('Invoice', on_delete=models.SET_NULL, null=True, blank=True, related_name='ledger_entries')
    purchase_invoice = models.ForeignKey('PurchaseInvoice', on_delete=models.SET_NULL, null=True, blank=True, related_name='ledger_entries')
    transfer         = models.ForeignKey('AccountTransfer', on_delete=models.SET_NULL, null=True, blank=True, related_name='ledger_entries')
    expense          = models.ForeignKey('Expense', on_delete=models.SET_NULL, null=True, blank=True, related_name='ledger_entries')
    stock_out_log    = models.ForeignKey('StockOutLog', on_delete=models.SET_NULL, null=True, blank=True, related_name='ledger_entries')
    created_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-created_at']
        verbose_name_plural = 'Ledger Entries'

    def __str__(self):
        return f'{self.date} | {self.entry_type.upper()} {self.amount} → {self.account}'


class TreatmentCategory(models.Model):
    name = models.CharField(max_length=50, unique=True)
    show_to_beautician = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)
    revenue_account = models.OneToOneField(
        'ChartOfAccounts',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='treatment_category',
    )
    class Meta:
        ordering = ['sort_order', 'name']
        verbose_name_plural = 'Treatment Categories'

    def __str__(self):
        return self.name

    @classmethod
    def resolve_by_name(cls, name):
        """Return the category matching ``name`` (case-insensitive), creating it
        — and its GL accounts, via ``save()`` — when absent.

        Returns ``None`` for a blank name so the caller falls back to the system
        revenue/COGS accounts. Used to route a treatment's POS revenue and COGS
        to the right per-category accounts by matching ``Treatment.category``.
        """
        name = (name or '').strip()
        if not name:
            return None
        existing = cls.objects.filter(name__iexact=name).first()
        if existing:
            return existing
        return cls.objects.create(name=name)

    @staticmethod
    def _next_account_number(range_min, range_max, step=1000):
        max_num = (
            ChartOfAccounts.objects
            .filter(account_number__gte=range_min, account_number__lte=range_max)
            .aggregate(m=Max('account_number'))['m']
        )
        nxt = (max_num + step) if max_num is not None else range_min
        if nxt > range_max:
            raise ValueError(f'Account range {range_min}–{range_max} is exhausted.')
        return nxt

    def ensure_accounts(self):
        """Create any missing COA accounts for this category.

        Safe to call on existing categories. Persists via a direct queryset
        update to avoid re-triggering the save() auto-create branch.
        Returns a dict describing what was created (empty if nothing was needed).
        """
        created = {}
        if not self.revenue_account_id:
            self.revenue_account = ChartOfAccounts.objects.create(
                account_number=self._next_account_number(4400000, 4999999),
                name=f'Treatment Revenue – {self.name}',
                account_type='revenue',
            )
            created['revenue_account'] = str(self.revenue_account)
        if created:
            TreatmentCategory.objects.filter(pk=self.pk).update(
                revenue_account_id=self.revenue_account_id,
            )
        return created

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        if is_new:
            if not self.revenue_account_id:
                self.revenue_account = ChartOfAccounts.objects.create(
                    account_number=self._next_account_number(4400000, 4999999),
                    name=f'Treatment Revenue – {self.name}',
                    account_type='revenue',
                )
        else:
            if self.revenue_account_id:
                ChartOfAccounts.objects.filter(pk=self.revenue_account_id).update(
                    name=f'Treatment Revenue – {self.name}',
                )
        super().save(*args, **kwargs)


class AssessmentCode(models.Model):
    CATEGORY_CHOICES = [
        (1, 'Common'),
        (2, 'Uncommon'),
    ]

    code = models.CharField(max_length=20, unique=True)
    description = models.CharField(max_length=200)
    active = models.BooleanField(default=True)
    category = models.IntegerField(choices=CATEGORY_CHOICES, default=1)

    class Meta:
        ordering = ['category', 'code']

    def __str__(self):
        return f'{self.code} – {self.description}'


class PatientNote(models.Model):
    """A free-form note about a patient, written by staff during the day.

    Two kinds of subject exist because walk-in guests never get a Patient row:
      * registered patient → ``patient_no`` is set (``active_patient`` may be set too)
      * walk-in guest      → only ``active_patient`` is set
    The CheckConstraint below guarantees at least one of the two is present.
    """

    # Nullable so a guest visit can own a note. Anything reading ``patient_no``
    # off a note must cope with None.
    patient_no = models.ForeignKey(
        Patient, on_delete=models.CASCADE, related_name='notes',
        null=True, blank=True,
    )
    active_patient = models.ForeignKey(
        'ActivePatient', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='notes',
    )
    date = models.DateField(db_index=True)
    content = models.TextField()
    # Legacy free-text attribution. Kept for rows written before author_user
    # existed, and as the fallback for author_display.
    author = models.CharField(max_length=100, blank=True, default='')
    author_user = models.ForeignKey(
        'AppUser', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='patient_notes',
    )
    # Snapshot of author_user.role at write time. Users get re-roled and deleted,
    # but the POS must still be able to label the note "Beautician" as of when it
    # was written — so this is stored, not derived from author_user.role.
    author_role = models.CharField(max_length=20, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date', '-created_at']
        indexes = [
            models.Index(fields=['patient_no', 'date'], name='patientnote_pat_date_idx'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(patient_no__isnull=False)
                    | models.Q(active_patient__isnull=False)
                ),
                name='patientnote_has_subject',
            ),
        ]

    def __str__(self):
        subject = self.patient_no_id or (
            f'visit #{self.active_patient_id}' if self.active_patient_id else '?'
        )
        return f'Note for {subject} on {self.date}'


class PatientTier(models.Model):
    name = models.CharField(max_length=30)
    min_visit_count = models.IntegerField(default=0)
    min_total_spend = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    color_hex = models.CharField(max_length=7, default='#6b7280')
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['sort_order']

    def __str__(self):
        return self.name


class Promotion(models.Model):
    DISCOUNT_TYPE_CHOICES = [
        ('percent', 'Percentage'),
        ('fixed',   'Fixed Amount'),
    ]
    SCOPE_CHOICES = [
        ('all',      'All'),
        ('category', 'Treatment Category'),
        ('item',     'Specific Item'),
    ]

    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, default='')
    discount_type = models.CharField(max_length=10, choices=DISCOUNT_TYPE_CHOICES)
    discount_value = models.DecimalField(max_digits=10, decimal_places=2)
    scope = models.CharField(max_length=10, choices=SCOPE_CHOICES, default='all')
    applicable_categories = models.ManyToManyField('TreatmentCategory', blank=True)
    applicable_items = models.ManyToManyField('InventoryItem', blank=True)
    valid_from = models.DateField()
    valid_until = models.DateField(null=True, blank=True)
    max_uses = models.IntegerField(null=True, blank=True)
    max_uses_per_patient = models.IntegerField(default=0)
    min_tier = models.ForeignKey(
        'PatientTier', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='promotions',
    )
    is_auto = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        'AppUser', on_delete=models.SET_NULL,
        null=True, blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'[{self.code}] {self.name}'


class PromotionUsage(models.Model):
    promotion = models.ForeignKey(Promotion, on_delete=models.PROTECT, related_name='usages')
    patient_no = models.ForeignKey(
        'Patient', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='promotion_usages',
    )
    invoice = models.OneToOneField(
        'Invoice', on_delete=models.CASCADE, related_name='promotion_usage',
    )
    discount_applied = models.DecimalField(max_digits=14, decimal_places=2)
    used_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.promotion.code} → {self.invoice_id}'


class PatientCRMProfile(models.Model):
    patient_no = models.OneToOneField(
        'Patient', on_delete=models.CASCADE,
        primary_key=True, related_name='crm_profile',
    )
    tier = models.ForeignKey(
        'PatientTier', on_delete=models.SET_NULL,
        null=True, blank=True,
    )
    total_spend = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_visits = models.IntegerField(default=0)
    last_visit_date = models.DateField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'CRM:{self.patient_no_id}'


class WorkShift(models.Model):
    name = models.CharField(max_length=50)
    expected_start = models.TimeField()
    expected_end = models.TimeField()
    color_hex = models.CharField(max_length=7, default='#0284c7')

    class Meta:
        ordering = ['expected_start']

    def __str__(self):
        return self.name


class StaffSchedule(models.Model):
    staff = models.ForeignKey(AppUser, on_delete=models.CASCADE, related_name='schedules')
    date = models.DateField()
    shift = models.ForeignKey(WorkShift, on_delete=models.SET_NULL, null=True, blank=True)
    notes = models.TextField(blank=True, default='')

    class Meta:
        unique_together = [('staff', 'date')]
        ordering = ['date']

    def __str__(self):
        return f'{self.staff} – {self.date}'


class AttendanceRecord(models.Model):
    STATUS_CHOICES = [
        ('present',  'Present'),
        ('late',     'Late'),
        ('absent',   'Absent'),
        ('half_day', 'Half Day'),
        ('day_off',  'Day Off'),
    ]

    staff = models.ForeignKey(AppUser, on_delete=models.CASCADE, related_name='attendance_records')
    date = models.DateField()
    clock_in = models.DateTimeField(null=True, blank=True)
    clock_out = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='absent')
    notes = models.TextField(blank=True, default='')

    class Meta:
        unique_together = [('staff', 'date')]
        ordering = ['-date']

    @property
    def total_hours(self):
        if self.clock_in and self.clock_out:
            return round((self.clock_out - self.clock_in).seconds / 3600, 2)
        return 0

    @property
    def late_minutes(self):
        if not self.clock_in:
            return 0
        try:
            schedule = StaffSchedule.objects.get(staff=self.staff, date=self.date)
        except StaffSchedule.DoesNotExist:
            return 0
        if not schedule.shift:
            return 0
        from datetime import datetime, date as date_type
        expected = datetime.combine(date_type.today(), schedule.shift.expected_start)
        actual = datetime.combine(date_type.today(), self.clock_in.time())
        diff = (actual - expected).seconds // 60
        return diff if actual > expected else 0

    def __str__(self):
        return f'{self.staff} – {self.date} ({self.status})'


# ── Treatment Packages ─────────────────────────────────────────────────────
# A TreatmentPackage bundles a fixed number of treatment sessions sold for a
# single upfront price. Sale → PatientPackage row. Each visit that uses a
# session creates a PatientPackageRedemption (and a Rp 0 invoice line so the
# audit trail stays consistent).


class TreatmentPackage(models.Model):
    code            = models.CharField(max_length=60, unique=True)
    name            = models.CharField(max_length=150)
    description     = models.TextField(blank=True, default='')
    price           = models.DecimalField(max_digits=12, decimal_places=2)
    active          = models.BooleanField(default=True)
    catalog_item    = models.OneToOneField(
        'InventoryItem',
        null=True, blank=True,
        on_delete=models.PROTECT,
        related_name='treatment_package',
    )

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def total_sessions(self):
        return sum(i.sessions for i in self.items.all())

    def _category_for_catalog(self):
        first = self.items.select_related('treatment').first()
        if not first:
            return ''
        return first.treatment.category

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new or not self.catalog_item_id:
            item = InventoryItem.objects.create(
                code=self.code,
                name=self.name,
                selling_price=self.price,
                unit_small='package',
                is_service=True,
                is_active=self.active,
                min_stock=0,
                category=self._category_for_catalog(),
            )
            TreatmentPackage.objects.filter(pk=self.pk).update(catalog_item=item)
            self.catalog_item_id = item.pk
        else:
            InventoryItem.objects.filter(pk=self.catalog_item_id).update(
                code=self.code,
                name=self.name,
                selling_price=self.price,
                is_active=self.active,
            )

    def delete(self, *args, **kwargs):
        catalog_item_id = self.catalog_item_id
        super().delete(*args, **kwargs)
        if catalog_item_id:
            InventoryItem.objects.filter(pk=catalog_item_id, is_service=True).delete()


class TreatmentPackageItem(models.Model):
    package   = models.ForeignKey(TreatmentPackage, on_delete=models.CASCADE, related_name='items')
    treatment = models.ForeignKey(Treatment, on_delete=models.PROTECT, related_name='package_items')
    sessions  = models.PositiveIntegerField(default=1)

    class Meta:
        unique_together = [('package', 'treatment')]
        ordering = ['id']

    def __str__(self):
        return f'{self.package.name} · {self.treatment.name} ×{self.sessions}'


class PatientPackage(models.Model):
    STATUS_CHOICES = [
        ('active',     'Active'),
        ('exhausted',  'Exhausted'),
    ]

    patient            = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name='packages')
    package            = models.ForeignKey(TreatmentPackage, on_delete=models.PROTECT, related_name='patient_packages')
    purchased_invoice  = models.ForeignKey(Invoice, on_delete=models.SET_NULL, null=True, blank=True, related_name='packages_sold')
    purchased_at       = models.DateTimeField(auto_now_add=True)
    status             = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')

    class Meta:
        ordering = ['-purchased_at']

    def __str__(self):
        return f'{self.patient_id} · {self.package.name} ({self.status})'

    def remaining_for(self, treatment_id):
        """Sessions left for a specific treatment in this package."""
        entitled = (
            self.package.items
            .filter(treatment_id=treatment_id)
            .aggregate(s=Sum('sessions'))['s'] or 0
        )
        used = self.redemptions.filter(treatment_id=treatment_id).count()
        return max(entitled - used, 0)

    def total_remaining(self):
        entitled = self.package.total_sessions
        used = self.redemptions.count()
        return max(entitled - used, 0)

    def refresh_status(self):
        new_status = 'exhausted' if self.total_remaining() == 0 else 'active'
        if new_status != self.status:
            self.status = new_status
            self.save(update_fields=['status'])


class PatientPackageRedemption(models.Model):
    patient_package = models.ForeignKey(PatientPackage, on_delete=models.CASCADE, related_name='redemptions')
    treatment       = models.ForeignKey(Treatment, on_delete=models.PROTECT, related_name='package_redemptions')
    invoice         = models.ForeignKey(Invoice, on_delete=models.SET_NULL, null=True, blank=True, related_name='package_redemptions')
    redeemed_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-redeemed_at']

    def __str__(self):
        return f'{self.patient_package_id} · {self.treatment.name} @ {self.redeemed_at:%Y-%m-%d}'


class StockOpnameSession(models.Model):
    STATUS_CHOICES = [('draft', 'Draft'), ('completed', 'Selesai')]

    date = models.DateField()
    conducted_by = models.CharField(max_length=200)
    notes = models.TextField(blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    warehouse = models.ForeignKey('Warehouse', on_delete=models.SET_NULL, null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey('AppUser', on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'StockOpname #{self.id} – {self.date} – {self.conducted_by}'


class StockOpnameItem(models.Model):
    session = models.ForeignKey(StockOpnameSession, on_delete=models.CASCADE, related_name='items')
    item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT)
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT)
    shelf1_qty = models.PositiveIntegerField(default=0)
    shelf2_qty = models.PositiveIntegerField(default=0)
    system_qty = models.PositiveIntegerField(default=0)
    is_loss = models.BooleanField(default=False)

    class Meta:
        unique_together = [('session', 'item', 'warehouse')]


class StockOutLog(models.Model):
    """One stock issue that is not a sale.

    Until Phase 5 this was a bare movement log: it deducted FIFO stock and threw
    the resulting cost away, so damaged, expired and mis-keyed inventory left the
    balance sheet without ever reaching the P&L. The accountant's June 2026 books
    carry 5000900 "Koreksi/Obat Rusak/ED" while CPMS carried nothing at all.

    Three fields close that gap:

    * ``reason`` — the operator's intent, which is what decides the GL account.
      Free-text ``notes`` could never do that job; it is kept for detail.
    * ``value`` — the FIFO cost consumed, captured at deduction time. It cannot
      be recomputed later: the batches it drew on have already moved on.
    * ``posting_status`` — makes the row a first-class journal document, swept
      by the same preview → review → commit run as invoices and expenses.

    ``REASON_ACCOUNTS`` maps reason to the account the cost is charged to. A
    reason mapped to ``None`` moves stock without touching the P&L (a warehouse
    transfer is one asset account to itself) and is never swept.
    """

    # Reason codes, chosen from the free-text notes staff already wrote
    # ('Pindah Gudang', 'Expired', 'Salah input', 'Kirim ke Kirana', …) so the
    # backfill in migration 0105 can classify existing rows instead of guessing.
    REASON_TRANSFER   = 'transfer'
    REASON_EXPIRED    = 'expired'
    REASON_DAMAGED    = 'damaged'
    REASON_LOST       = 'lost'
    REASON_DATA_ENTRY = 'data_entry'
    REASON_INTERNAL   = 'internal_use'
    REASON_KIRANA     = 'kirana'
    REASON_SAMPLE     = 'sample'
    REASON_OTHER      = 'other'

    REASON_CHOICES = [
        (REASON_TRANSFER,   'Pindah Gudang'),
        (REASON_EXPIRED,    'Kedaluwarsa (ED)'),
        (REASON_DAMAGED,    'Rusak'),
        (REASON_LOST,       'Hilang/Selisih'),
        (REASON_DATA_ENTRY, 'Koreksi Salah Input'),
        (REASON_INTERNAL,   'Pemakaian Internal Klinik'),
        (REASON_KIRANA,     'Kirim ke Kirana'),
        (REASON_SAMPLE,     'Sampel/Tester'),
        (REASON_OTHER,      'Lain-lain'),
    ]

    # reason -> ChartOfAccounts.account_number the cost is debited to.
    # None means "no journal at all": the stock moved but the clinic still owns
    # it, so debiting an expense would understate inventory twice over.
    #
    # Numbers are the client's own COA, seeded by migration 0094 — not system
    # accounts, so staff may rename them and this mapping still resolves.
    REASON_ACCOUNTS = {
        REASON_TRANSFER:   None,
        REASON_EXPIRED:    5000900,   # Koreksi/Obat Rusak/ED/dr. Melia
        REASON_DAMAGED:    5000900,
        REASON_LOST:       5000900,
        REASON_DATA_ENTRY: 5000900,
        REASON_INTERNAL:   6100005,   # Barang Habis Pakai Ruang Facial (Obat)
        REASON_KIRANA:     6100030,   # Obat Kirana
        REASON_SAMPLE:     6300010,   # Biaya Promosi/Iklan
        REASON_OTHER:      5000900,
    }

    item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT, related_name='stock_out_logs')
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name='stock_out_logs')
    out_date = models.DateField()
    quantity = models.DecimalField(max_digits=14, decimal_places=4)
    reason = models.CharField(max_length=20, choices=REASON_CHOICES, default=REASON_OTHER)
    # FIFO cost consumed by this issue, in rupiah. Written once, at deduction
    # time, by StockOutView — never derived afterwards.
    value = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    notes = models.TextField(blank=True, default='')
    posting_status = models.CharField(max_length=10, choices=POSTING_STATUS_CHOICES, default='unposted')
    created_by = models.ForeignKey(AppUser, null=True, blank=True, on_delete=models.SET_NULL, related_name='stock_out_logs')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.out_date} | {self.get_reason_display()} {self.quantity} × {self.item_id}'

    @property
    def expense_account_number(self):
        """The COA number this issue is charged to, or None when it is not a P&L event."""
        return self.REASON_ACCOUNTS.get(self.reason)

    @property
    def is_journalable(self):
        """True when the sweep should build a journal entry for this row.

        A zero-value issue is skipped as well as an untracked reason: FIFO found
        no batch to draw on, so there is no cost to move and a zero-amount entry
        would be noise in the journal.
        """
        return bool(self.expense_account_number) and self.value > 0


class StockOpnameTemplate(models.Model):
    warehouse = models.OneToOneField(
        'Warehouse', on_delete=models.CASCADE, related_name='so_template'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        'AppUser', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='so_templates_created',
    )

    def __str__(self):
        return f'SO Template – {self.warehouse}'


class StockOpnameTemplateItem(models.Model):
    template = models.ForeignKey(
        StockOpnameTemplate, on_delete=models.CASCADE, related_name='template_items'
    )
    item = models.ForeignKey('InventoryItem', on_delete=models.CASCADE)
    section = models.CharField(max_length=200)
    section_order = models.PositiveIntegerField(default=0)
    item_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['section_order', 'item_order']
        unique_together = [('template', 'item')]

    def __str__(self):
        return f'{self.template} – {self.item.code} @ {self.section}'


class SiteConfig(models.Model):
    """Singleton row (always pk=1) holding clinic-wide receipt settings."""
    clinic_name          = models.CharField(max_length=200, default='')
    address_line1        = models.CharField(max_length=200, default='')
    address_line2        = models.CharField(max_length=200, default='')
    phone_fax            = models.CharField(max_length=200, default='')
    receipt_header_extra = models.TextField(default='')
    receipt_footer       = models.TextField(default='Terima kasih atas kunjungan Anda')
    logo                 = models.ImageField(upload_to='site/', null=True, blank=True)

    class Meta:
        verbose_name = 'Site Configuration'

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class ReportSettings(models.Model):
    """Singleton row (always pk=1) holding tunable report classification windows.

    Deliberately not folded into ``SiteConfig``: that model is about receipt
    printing, and mixing report windows into it would make the Receipt
    Settings admin page lie about its scope. Same ``get_solo()`` shape as
    ``SiteConfig`` so both singletons are handled identically everywhere else.
    """

    # ── Stock movement classification ──
    stock_fast_window_days = models.IntegerField(default=30)
    stock_fast_top_percent = models.IntegerField(default=20)   # 1..100
    stock_slow_months      = models.IntegerField(default=2)
    stock_dead_months      = models.IntegerField(default=4)

    # ── Patient activity classification ──
    patient_active_months   = models.IntegerField(default=6)
    patient_inactive_months = models.IntegerField(default=12)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Report Settings'

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class IssueTicket(models.Model):
    STATUS_CHOICES = [
        ('open', 'Open'),
        ('in_progress', 'In Progress'),
        ('resolved', 'Resolved'),
        ('closed', 'Closed'),
    ]
    CATEGORY_CHOICES = [
        ('sistem',    'Sistem'),
        ('peralatan', 'Peralatan'),
        ('jaringan',  'Jaringan'),
        ('produk',    'Produk'),
    ]
    ticket_no    = models.CharField(max_length=20, unique=True, editable=False)
    submitted_by = models.ForeignKey('AppUser', on_delete=models.PROTECT, related_name='tickets')
    category     = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='sistem')
    title        = models.CharField(max_length=255)
    description  = models.TextField()
    status       = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')
    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.ticket_no:
            from django.utils import timezone
            today = timezone.now().strftime('%Y%m%d')
            prefix = f'TKT-{today}-'
            last = IssueTicket.objects.filter(ticket_no__startswith=prefix).order_by('-ticket_no').first()
            seq = (int(last.ticket_no.split('-')[-1]) + 1) if last else 1
            self.ticket_no = f'{prefix}{seq:04d}'
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.ticket_no} — {self.title}'


class IssueTicketImage(models.Model):
    ticket      = models.ForeignKey(IssueTicket, on_delete=models.CASCADE, related_name='images')
    image       = models.ImageField(upload_to='ticket_images/')
    uploaded_at = models.DateTimeField(auto_now_add=True)


# ── Accounting: Suppliers & Purchase Invoices ─────────────────────────────────

# ── Accounts Payable chart-of-accounts layout ────────────────────────────────
# Each vendor owns a liability sub-account nested under a single AP control
# account, which itself sits under the Liabilities head. Three levels:
#   2000000 Liabilities (head)
#     └ 2100000 Utang Usaha / Accounts Payable (control, is_system)
#         └ 2100001..2199999 per-vendor payables (is_system, auto-managed)
LIABILITIES_HEAD_NUMBER = 2000000
AP_CONTROL_NUMBER       = 2100000
AP_VENDOR_RANGE         = (2100001, 2199999)


class Supplier(models.Model):
    name         = models.CharField(max_length=100)
    contact_name = models.CharField(max_length=100, blank=True)
    phone        = models.CharField(max_length=50, blank=True)
    email        = models.EmailField(blank=True)
    address      = models.TextField(blank=True)
    is_active    = models.BooleanField(default=True)
    ap_account   = models.OneToOneField(
        'ChartOfAccounts',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='supplier_ap',
    )

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    # ── Accounts-Payable account provisioning ────────────────────────────────

    @staticmethod
    def _ensure_ap_control_account():
        """Return the single AP control account (2100000), creating it — and the
        Liabilities head it hangs under — if either is missing. Idempotent."""
        control = ChartOfAccounts.objects.filter(account_number=AP_CONTROL_NUMBER).first()
        if control:
            return control
        head = ChartOfAccounts.objects.filter(account_number=LIABILITIES_HEAD_NUMBER).first()
        if not head:
            head = ChartOfAccounts.objects.create(
                account_number=LIABILITIES_HEAD_NUMBER,
                name='Liabilities',
                account_type='liability',
                is_head=True,
                is_system=False,
            )
        return ChartOfAccounts.objects.create(
            account_number=AP_CONTROL_NUMBER,
            name='Utang Usaha (Accounts Payable)',
            account_type='liability',
            is_system=True,
            is_head=False,
            parent=head,
        )

    @staticmethod
    def _next_ap_account_number():
        lo, hi = AP_VENDOR_RANGE
        max_num = (
            ChartOfAccounts.objects
            .filter(account_number__gte=lo, account_number__lte=hi)
            .aggregate(m=Max('account_number'))['m']
        )
        nxt = (max_num + 1) if max_num is not None else lo
        if nxt > hi:
            raise ValueError(f'AP account range {lo}–{hi} is exhausted.')
        return nxt

    def ensure_ap_account(self):
        """Create this vendor's AP sub-account if missing. Safe to call on
        existing suppliers; persists via a direct update so it does not
        re-enter save(). Returns the account (created or existing)."""
        if self.ap_account_id:
            return self.ap_account
        control = self._ensure_ap_control_account()
        self.ap_account = ChartOfAccounts.objects.create(
            account_number=self._next_ap_account_number(),
            name=f'Utang Usaha – {self.name}',
            account_type='liability',
            is_system=True,
            is_head=False,
            parent=control,
        )
        if self.pk:
            Supplier.objects.filter(pk=self.pk).update(ap_account_id=self.ap_account_id)
        return self.ap_account

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        if is_new and not self.ap_account_id:
            control = self._ensure_ap_control_account()
            self.ap_account = ChartOfAccounts.objects.create(
                account_number=self._next_ap_account_number(),
                name=f'Utang Usaha – {self.name}',
                account_type='liability',
                is_system=True,
                is_head=False,
                parent=control,
            )
        elif not is_new and self.ap_account_id:
            ChartOfAccounts.objects.filter(pk=self.ap_account_id).update(
                name=f'Utang Usaha – {self.name}',
            )
        super().save(*args, **kwargs)


class PurchaseInvoice(models.Model):
    STATUS_CHOICES = [
        ('unpaid',  'Belum Dibayar'),
        ('partial', 'Sebagian Dibayar'),
        ('paid',    'Lunas'),
    ]

    internal_id          = models.CharField(max_length=30, unique=True, blank=True)
    external_invoice_no  = models.CharField(max_length=100, blank=True)
    supplier             = models.ForeignKey('Supplier', on_delete=models.PROTECT, related_name='purchase_invoices')
    # Legacy indirection kept for back-compat and for old rows. New records set
    # payment_account directly.
    payment_method       = models.ForeignKey(
        'PaymentMethod',
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name='purchase_invoices',
    )
    # The cash/bank COA the invoice was (last) settled from. Left empty while
    # the invoice is unpaid — a purchase on credit has no payment account yet;
    # it is chosen when a payment is recorded (see PurchasePayment).
    payment_account      = models.ForeignKey(
        'ChartOfAccounts',
        on_delete=models.PROTECT,
        null=True, blank=True,
        limit_choices_to={'account_type': 'asset'},
        related_name='purchase_invoices_paid_from',
        help_text='Cash/bank account the invoice was last settled from. Must be '
                  'one of services.cash_accounts.cash_bank_account_ids().',
    )
    purchase_date  = models.DateField()
    due_date       = models.DateField(null=True, blank=True)
    status         = models.CharField(max_length=10, choices=STATUS_CHOICES, default='unpaid')
    notes          = models.TextField(blank=True)
    total_amount   = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    amount_paid    = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    invoice_image  = models.ImageField(upload_to='purchase_invoices/', null=True, blank=True)
    warehouse      = models.ForeignKey('Warehouse', on_delete=models.SET_NULL, null=True, blank=True, related_name='purchase_invoices')
    created_at     = models.DateTimeField(auto_now_add=True)
    created_by     = models.ForeignKey('AppUser', on_delete=models.SET_NULL, null=True, blank=True, related_name='purchase_invoices')
    is_voided      = models.BooleanField(default=False)
    voided_at      = models.DateTimeField(null=True, blank=True)
    voided_by      = models.ForeignKey('AppUser', on_delete=models.SET_NULL, null=True, blank=True, related_name='voided_purchase_invoices')
    posting_status = models.CharField(max_length=10, choices=POSTING_STATUS_CHOICES, default='unposted')

    class Meta:
        ordering = ['-purchase_date', '-created_at']

    def __str__(self):
        return self.internal_id or f'PO-{self.id}'

    def save(self, *args, **kwargs):
        if not self.internal_id:
            from django.utils import timezone as tz
            today = tz.now().strftime('%Y%m%d')
            base  = f'PO-{today}'
            count = PurchaseInvoice.objects.filter(internal_id__startswith=base).count()
            self.internal_id = f'{base}-{count + 1}'
        super().save(*args, **kwargs)

    def refresh_status(self):
        if self.amount_paid <= 0:
            self.status = 'unpaid'
        elif self.amount_paid >= self.total_amount:
            self.status = 'paid'
        else:
            self.status = 'partial'


class PurchaseInvoiceItem(models.Model):
    LINE_TYPE_CHOICES = [('stock', 'Stok'), ('expense', 'Beban')]

    invoice          = models.ForeignKey(PurchaseInvoice, on_delete=models.CASCADE, related_name='items')
    line_type        = models.CharField(max_length=10, choices=LINE_TYPE_CHOICES, default='stock')
    item             = models.ForeignKey('InventoryItem', on_delete=models.PROTECT, null=True, blank=True, related_name='purchase_items')
    item_name        = models.CharField(max_length=255)
    quantity         = models.DecimalField(max_digits=14, decimal_places=3)
    unit             = models.CharField(max_length=50, blank=True)
    unit_cost        = models.DecimalField(max_digits=14, decimal_places=2)
    total_discount   = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    actual_unit_cost = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    expense_account  = models.ForeignKey(
        'ChartOfAccounts',
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name='purchase_expense_items',
    )
    warehouse        = models.ForeignKey(
        'Warehouse',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='purchase_items',
    )

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f'{self.invoice.internal_id} – {self.item_name}'


class PurchaseAdditionalCost(models.Model):
    MODIFIER_CHOICES    = [('add', 'Tambah'), ('subtract', 'Kurang')]
    AMOUNT_TYPE_CHOICES = [('cash', 'Nominal'), ('percent', 'Persen')]

    invoice     = models.ForeignKey(PurchaseInvoice, on_delete=models.CASCADE, related_name='additional_costs')
    name        = models.CharField(max_length=255)
    modifier    = models.CharField(max_length=10, choices=MODIFIER_CHOICES, default='add')
    amount_type = models.CharField(max_length=10, choices=AMOUNT_TYPE_CHOICES, default='cash')
    amount      = models.DecimalField(max_digits=14, decimal_places=2)
    sort_order  = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'id']

    def __str__(self):
        sign = '+' if self.modifier == 'add' else '-'
        suffix = '%' if self.amount_type == 'percent' else ''
        return f'{self.invoice.internal_id} {sign}{self.amount}{suffix} ({self.name})'


class PurchasePayment(models.Model):
    """One settlement against a purchase invoice.

    A purchase can be paid in full at creation or in instalments later, each
    from a different bank/cash account — so the date and the account belong to
    the payment, not to the invoice. ``PurchaseInvoice.amount_paid`` stays as
    the running total these rows sum to, and ``PurchaseInvoice.payment_account``
    mirrors the most recent payment's account for list/detail display.
    """
    invoice        = models.ForeignKey(PurchaseInvoice, on_delete=models.CASCADE, related_name='payments')
    payment_date   = models.DateField()
    # Legacy indirection kept for back-compat; new rows only set payment_account.
    payment_method = models.ForeignKey(
        'PaymentMethod',
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name='purchase_payments',
    )
    # The cash/bank COA the money actually left from — the credit leg of
    # Dr Accounts Payable / Cr <this account>.
    payment_account = models.ForeignKey(
        'ChartOfAccounts',
        on_delete=models.PROTECT,
        null=True, blank=True,
        limit_choices_to={'account_type': 'asset'},
        related_name='purchase_payments_from',
        help_text='Cash/bank account credited by this payment. Must be one of '
                  'services.cash_accounts.cash_bank_account_ids().',
    )
    amount         = models.DecimalField(max_digits=14, decimal_places=2)
    notes          = models.CharField(max_length=255, blank=True)
    created_at     = models.DateTimeField(auto_now_add=True)
    created_by     = models.ForeignKey('AppUser', on_delete=models.SET_NULL, null=True, blank=True, related_name='purchase_payments')

    class Meta:
        ordering = ['payment_date', 'id']

    def __str__(self):
        return f'{self.invoice.internal_id} — {self.payment_date} {self.amount}'


# ── Accounting: Account Transfers & Manual Adjustments ────────────────────────

class AccountTransfer(models.Model):
    transfer_date = models.DateField()
    from_account  = models.ForeignKey('ChartOfAccounts', on_delete=models.PROTECT, related_name='transfers_out')
    to_account    = models.ForeignKey('ChartOfAccounts', on_delete=models.PROTECT, related_name='transfers_in')
    amount        = models.DecimalField(max_digits=14, decimal_places=2)
    description   = models.CharField(max_length=255)
    reference     = models.CharField(max_length=100, blank=True)
    created_at    = models.DateTimeField(auto_now_add=True)
    created_by    = models.ForeignKey('AppUser', on_delete=models.SET_NULL, null=True, blank=True, related_name='account_transfers')
    posting_status = models.CharField(max_length=10, choices=POSTING_STATUS_CHOICES, default='unposted')

    class Meta:
        ordering = ['-transfer_date', '-created_at']

    def __str__(self):
        return f'{self.transfer_date} | {self.from_account} → {self.to_account}'


# ── Accounting: Operating Expenses ─────────────────────────────────────────────

class ExpenseAlias(models.Model):
    """A friendly, staff-facing name for an expense GL account.

    Lets a beautician log 'Beli kapas' without knowing that it debits
    5200003 Perlengkapan Klinik. Purely a naming layer: the Expense rows
    written through it are indistinguishable from hand-entered ones apart
    from Expense.source. Chosen over a separate posting model specifically
    so this feature never has to touch the journal engine — the posting
    code stays exactly as dangerous, and exactly as untouched, as it was.
    """
    SCOPE_CHOICES = [('beautician', 'Beautician'), ('general', 'General')]

    name       = models.CharField(max_length=120)
    account    = models.ForeignKey(
        'ChartOfAccounts', on_delete=models.PROTECT,
        limit_choices_to={'account_type__in': ['expense', 'cogs']},
        related_name='expense_aliases',
    )
    scope      = models.CharField(max_length=20, choices=SCOPE_CHOICES, default='beautician')
    is_active  = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)
    notes      = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        ordering = ['sort_order', 'name']
        constraints = [
            models.UniqueConstraint(fields=['name', 'scope'], name='uniq_expense_alias_name_scope'),
        ]

    def __str__(self):
        return self.name


class Expense(models.Model):
    """One operating expense, read as a single journal entry.

    One credit leg out of ``payment_account`` (the named cash/bank COA the
    money leaves from) balanced by N debit legs, one per ``ExpenseItem``.
    Every leg carries its own memo: ``payment_memo`` for the credit side and
    ``ExpenseItem.description`` for each debit side, with a blank line memo
    inheriting ``payment_memo``. Resolve every memo through
    ``services.journal_engine.expense_leg_memo`` — never re-derive the chain.
    """

    STATUS_CHOICES = [
        ('unpaid',  'Belum Dibayar'),
        ('partial', 'Sebagian Dibayar'),
        ('paid',    'Lunas'),
    ]
    # Who wrote this expense, not how it was paid. 'beautician' rows come
    # through the alias-picking petty-cash flow (services/expense_create.py)
    # instead of the full GL-account expense form; kept as a plain field
    # rather than inferred from ExpenseItem.alias so a row is filterable even
    # after every one of its items has had its alias SET_NULL out from under it.
    SOURCE_CHOICES = [('general', 'General'), ('beautician', 'Beautician')]

    expense_date   = models.DateField()
    payment_method = models.ForeignKey(
        'PaymentMethod',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='expenses',
        help_text='Legacy indirection kept for back-compat. New records set '
                  'payment_account directly; this stays populated only so old '
                  'rows and any UI that still reasons in payment-method terms '
                  'keep working.',
    )
    # The cash/bank COA the money actually leaves from. Replaces the
    # payment_method indirection for new records. Nullable because an
    # accrual-only expense (booked to AP, paid later) may not have one yet.
    payment_account = models.ForeignKey(
        'ChartOfAccounts',
        on_delete=models.PROTECT,
        null=True, blank=True,
        limit_choices_to={'account_type': 'asset'},
        related_name='expenses_paid_from',
        help_text='Cash/bank account credited by this expense. Must be one of '
                  'services.cash_accounts.cash_bank_account_ids().',
    )
    # Journal memo for the credit leg, and the fallback memo for any expense
    # line whose own memo is left blank.
    payment_memo   = models.CharField(
        max_length=255, blank=True,
        help_text='Journal memo for the cash/bank (credit) leg. Also the '
                  'fallback memo for any expense line left without one.',
    )
    status         = models.CharField(max_length=10, choices=STATUS_CHOICES, default='unpaid')
    source         = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='general', db_index=True)
    total_amount   = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    amount_paid    = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    notes          = models.TextField(blank=True)
    posting_status = models.CharField(max_length=10, choices=POSTING_STATUS_CHOICES, default='unposted')
    created_by     = models.ForeignKey('AppUser', on_delete=models.SET_NULL, null=True, blank=True, related_name='expenses')
    created_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-expense_date', '-created_at']

    def __str__(self):
        return f'Expense #{self.pk}'

    def refresh_status(self):
        if self.amount_paid <= 0:
            self.status = 'unpaid'
        elif self.amount_paid >= self.total_amount:
            self.status = 'paid'
        else:
            self.status = 'partial'


class ExpenseItem(models.Model):
    """One debit leg of an expense journal entry.

    ``description`` *is* this leg's journal memo — there is deliberately no
    second ``memo`` column, because two free-text fields on the same row is
    exactly how users end up filling in the wrong one. Leave it blank and the
    leg inherits ``Expense.payment_memo``.
    """
    expense     = models.ForeignKey(Expense, on_delete=models.CASCADE, related_name='items')
    account     = models.ForeignKey(
        'ChartOfAccounts',
        on_delete=models.PROTECT,
        limit_choices_to={'account_type__in': ['expense', 'cogs']},
        related_name='expense_items',
    )
    description = models.CharField(
        max_length=255, blank=True,
        help_text="This leg's journal memo. Blank inherits Expense.payment_memo.",
    )
    amount      = models.DecimalField(max_digits=14, decimal_places=2)
    # SET_NULL, not PROTECT: deleting a retired alias must not be blocked by,
    # or destroy, a year of posted expense history. account and description
    # above are the record of truth for what was posted; alias is only
    # provenance ("which friendly name did the beautician pick"), so losing
    # it on delete is acceptable where losing a posted line never would be.
    alias       = models.ForeignKey(
        'ExpenseAlias', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='expense_items',
    )

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f'{self.expense_id} – {self.account} – {self.amount}'


class JournalBatch(models.Model):
    """One run of POST /api/accounting/journal/run/.

    ``requested_range`` is what the caller asked for (start is always the
    lowest transaction date among unposted documents at call time — i.e. the
    day the previous sweep left off, or the earliest ever-unposted document if
    none has run yet); ``swept_range`` is what was actually found and posted
    (may be narrower than requested if there was nothing to post, or wider if
    documents older than the nominal start were discovered)."""
    STATUS_CHOICES = [
        ('running',   'Running'),
        ('completed', 'Completed'),
        ('failed',    'Failed'),
    ]

    requested_range_start = models.DateField()
    requested_range_end   = models.DateField()
    swept_range_start     = models.DateField(null=True, blank=True)
    swept_range_end       = models.DateField(null=True, blank=True)
    status                = models.CharField(max_length=10, choices=STATUS_CHOICES, default='running')
    run_by                = models.ForeignKey('AppUser', on_delete=models.SET_NULL, null=True, blank=True, related_name='journal_batches')
    created_at             = models.DateTimeField(auto_now_add=True)
    summary                = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'JournalBatch #{self.pk} ({self.status}) {self.requested_range_start}..{self.requested_range_end}'


class JournalDayLog(models.Model):
    """Per-calendar-day posting tracker. A date with no row here has never been
    swept and reports must treat it as unposted."""
    date              = models.DateField(unique=True)
    is_posted         = models.BooleanField(default=False)
    posted_at         = models.DateTimeField(null=True, blank=True)
    posted_by         = models.ForeignKey('AppUser', on_delete=models.SET_NULL, null=True, blank=True, related_name='journal_day_logs')
    batch             = models.ForeignKey('JournalBatch', on_delete=models.SET_NULL, null=True, blank=True, related_name='day_logs')
    transaction_count = models.IntegerField(default=0)

    class Meta:
        ordering = ['-date']

    def __str__(self):
        return f'{self.date} — {"posted" if self.is_posted else "unposted"}'


class JournalEntry(models.Model):
    """One balanced journal document.

    Groups the ``LedgerEntry`` lines written together for a single source
    document, manual adjustment, reversal or correction. Before Phase 4 the
    ledger was flat — lines were tagged with a source FK and nothing else — so
    there was no object a detail page could address or a correction could point
    at. This is that object.

    An entry is immutable once written. The only way to change its effect is a
    correction: an auto-generated ``reversal`` entry dated today that negates
    every line, plus a ``correction`` entry the operator composes. Both link
    back to the original, so the detail page can show the full chain.
    """

    SOURCE_TYPE_CHOICES = LedgerEntry.SOURCE_TYPE_CHOICES + [
        ('reversal',   'Reversal'),
        ('correction', 'Correction'),
    ]

    entry_number = models.CharField(max_length=24, unique=True)
    date         = models.DateField(db_index=True)
    memo         = models.CharField(max_length=255, blank=True, default='')
    source_type  = models.CharField(max_length=15, choices=SOURCE_TYPE_CHOICES, blank=True, default='')

    # Mirrors LedgerEntry's document FKs so an entry reaches its source in one hop.
    invoice          = models.ForeignKey('Invoice',         on_delete=models.SET_NULL, null=True, blank=True, related_name='journal_entries')
    purchase_invoice = models.ForeignKey('PurchaseInvoice', on_delete=models.SET_NULL, null=True, blank=True, related_name='journal_entries')
    transfer         = models.ForeignKey('AccountTransfer', on_delete=models.SET_NULL, null=True, blank=True, related_name='journal_entries')
    expense          = models.ForeignKey('Expense',         on_delete=models.SET_NULL, null=True, blank=True, related_name='journal_entries')
    stock_out_log    = models.ForeignKey('StockOutLog',     on_delete=models.SET_NULL, null=True, blank=True, related_name='journal_entries')

    batch = models.ForeignKey('JournalBatch', on_delete=models.SET_NULL, null=True, blank=True, related_name='entries')

    # Correction chain. Both FKs point at the ORIGINAL entry: ``reverses`` is set
    # on the auto-generated reversal, ``corrects`` on the operator's replacement.
    # That lets the original read ``self.reversed_by`` / ``self.corrections``.
    # PROTECT because deleting an entry someone corrected would orphan the audit
    # trail — posted journals are never deleted.
    reverses = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True, related_name='reversed_by')
    corrects = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True, related_name='corrections')

    total_debit  = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_credit = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    # False only for historic entries the backfill found already unbalanced.
    # Nothing written by write_legs can be unbalanced — it raises instead.
    is_balanced  = models.BooleanField(default=True)

    created_by = models.ForeignKey('AppUser', on_delete=models.SET_NULL, null=True, blank=True, related_name='journal_entries')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-id']
        indexes = [
            models.Index(fields=['date', 'source_type']),
            models.Index(fields=['batch']),
        ]
        verbose_name_plural = 'Journal Entries'

    def __str__(self):
        return f'{self.entry_number} — {self.date} ({self.source_type})'

    @property
    def source_label(self):
        """Human reference to the document this entry came from."""
        if self.invoice_id:
            return self.invoice.invoice_number
        if self.purchase_invoice_id:
            return self.purchase_invoice.internal_id
        if self.expense_id:
            return f'Beban #{self.expense_id}'
        if self.stock_out_log_id:
            return f'Koreksi stok #{self.stock_out_log_id}'
        if self.transfer_id:
            return f'Transfer #{self.transfer_id}'
        return ''

    @property
    def is_reversed(self):
        return self.reversed_by.exists()


class JournalEntrySequence(models.Model):
    """Per-year counter behind ``JournalEntry.entry_number``.

    A ``max(entry_number)+1`` scan is not safe: the commit path posts many
    entries inside one transaction while another request may be posting a
    correction. Callers take this row ``select_for_update()`` so numbers are
    gapless and unique under concurrency.
    """
    year        = models.IntegerField(unique=True)
    last_number = models.IntegerField(default=0)

    class Meta:
        ordering = ['-year']

    def __str__(self):
        return f'{self.year}: {self.last_number}'


# ── Journal staging (preview before commit) ───────────────────────────────────
# A journal run is two-phase from Phase 4 on: preview materialises everything
# the sweep *would* post into these tables, the operator reviews it, and commit
# turns staged rows into real JournalEntry/LedgerEntry rows. Staging is a real
# table rather than a JSON blob because a 90-day sweep is realistically tens of
# thousands of lines and the review UI pages over them server-side.

class JournalStagingBatch(models.Model):
    STATUS_CHOICES = [
        ('draft',      'Draft'),
        ('committing', 'Committing'),
        ('committed',  'Committed'),
        ('discarded',  'Discarded'),
        ('failed',     'Failed'),
    ]

    date_to    = models.DateField()
    status     = models.CharField(max_length=12, choices=STATUS_CHOICES, default='draft')
    created_by = models.ForeignKey('AppUser', on_delete=models.SET_NULL, null=True, blank=True, related_name='journal_staging_batches')
    created_at = models.DateTimeField(auto_now_add=True)
    # Drafts are shared clinic-wide and auto-expire; see services/journal_preview.py.
    expires_at = models.DateTimeField(db_index=True)

    entry_count    = models.IntegerField(default=0)
    document_count = models.IntegerField(default=0)
    total_debit    = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_credit   = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    # Set once the draft has been turned into real journal entries.
    committed_batch = models.ForeignKey('JournalBatch', on_delete=models.SET_NULL, null=True, blank=True, related_name='staging_batches')
    days_committed  = models.IntegerField(default=0)
    error_message   = models.TextField(blank=True, default='')
    # FIFO lines whose recomputed amount at commit differed from the preview.
    variance_notes  = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'Journal Staging Batches'

    def __str__(self):
        return f'StagingBatch #{self.pk} ({self.status}) → {self.date_to}'


class StagedJournalEntry(models.Model):
    """One previewed journal document, not yet in the ledger."""

    batch    = models.ForeignKey(JournalStagingBatch, on_delete=models.CASCADE, related_name='entries')
    date     = models.DateField(db_index=True)
    sequence = models.IntegerField(default=0)

    source_type  = models.CharField(max_length=15)
    source_model = models.CharField(max_length=32)   # invoice|purchase|transfer|expense
    source_id    = models.IntegerField()
    source_label = models.CharField(max_length=120, blank=True, default='')
    memo         = models.CharField(max_length=255, blank=True, default='')

    total_debit  = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_credit = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    is_balanced  = models.BooleanField(default=True)
    # True when any line is FIFO-derived. Those amounts are recomputed at commit
    # (stock can move between preview and commit), so the UI must label them.
    has_estimate = models.BooleanField(default=False)
    warnings     = models.JSONField(default=list, blank=True)

    # SHA-256 over the source document's identity/totals/updated_at. Re-checked
    # at commit; a mismatch means the document changed after review and the
    # whole commit is refused.
    source_fingerprint = models.CharField(max_length=64, blank=True, default='')

    class Meta:
        ordering = ['date', 'sequence', 'id']
        indexes = [
            models.Index(fields=['batch', 'date']),
            models.Index(fields=['batch', 'source_type']),
        ]
        verbose_name_plural = 'Staged Journal Entries'

    def __str__(self):
        return f'{self.date} {self.source_label or self.source_type} (draft)'


class StagedJournalLine(models.Model):
    entry    = models.ForeignKey(StagedJournalEntry, on_delete=models.CASCADE, related_name='lines')
    # Null when the account does not exist yet (a supplier's AP sub-account is
    # created lazily at posting time). Preview must not create COA rows, so it
    # records the label it *would* create instead.
    account  = models.ForeignKey('ChartOfAccounts', on_delete=models.PROTECT, null=True, blank=True, related_name='staged_journal_lines')
    pending_account_label = models.CharField(max_length=120, blank=True, default='')

    entry_type   = models.CharField(max_length=6, choices=LedgerEntry.ENTRY_TYPE_CHOICES)
    amount       = models.DecimalField(max_digits=18, decimal_places=2)
    description  = models.CharField(max_length=255)
    is_estimated = models.BooleanField(default=False)
    sequence     = models.IntegerField(default=0)

    class Meta:
        ordering = ['sequence', 'id']

    def __str__(self):
        label = self.account.name if self.account_id else self.pending_account_label
        return f'{self.entry_type.upper()} {self.amount} → {label}'


class ProductionRecipe(models.Model):
    name = models.CharField(max_length=120)
    output_item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT,
                                    related_name='recipes_as_output')
    output_quantity = models.DecimalField(max_digits=14, decimal_places=4)  # smallest unit
    notes = models.TextField(blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey('AppUser', on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name='recipes_created')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} → {self.output_item.name}'


class ProductionRecipeIngredient(models.Model):
    UNIT_CHOICES = [('small', 'Small'), ('medium', 'Medium'), ('large', 'Large')]
    recipe = models.ForeignKey(ProductionRecipe, on_delete=models.CASCADE,
                               related_name='ingredients')
    item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT,
                             related_name='recipe_ingredient_lines')
    quantity = models.DecimalField(max_digits=14, decimal_places=4)
    unit = models.CharField(max_length=10, choices=UNIT_CHOICES, default='small')
    ordering = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['ordering', 'id']
        unique_together = [('recipe', 'item')]


class ProductionRun(models.Model):
    recipe = models.ForeignKey(ProductionRecipe, on_delete=models.SET_NULL, null=True,
                               blank=True, related_name='runs')
    output_item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT,
                                    related_name='production_runs')
    output_warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT,
                                         related_name='production_runs')
    output_quantity = models.DecimalField(max_digits=14, decimal_places=4)  # smallest unit
    total_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    produced_batch = models.ForeignKey(InventoryBatch, on_delete=models.SET_NULL,
                                        null=True, blank=True,
                                        related_name='production_runs')
    notes = models.TextField(blank=True, default='')
    produced_by = models.ForeignKey('AppUser', on_delete=models.SET_NULL, null=True,
                                    blank=True, related_name='production_runs')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class ProductionRunIngredient(models.Model):
    run = models.ForeignKey(ProductionRun, on_delete=models.CASCADE,
                            related_name='ingredients')
    item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT,
                             related_name='production_consumed_lines')
    quantity_small = models.DecimalField(max_digits=14, decimal_places=4)
    cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    source_warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, null=True,
                                         blank=True,
                                         related_name='production_consumed_lines')

    class Meta:
        ordering = ['id']


class ColorPalette(models.Model):
    name = models.CharField(max_length=50)
    primary_color = models.CharField(max_length=7)    # hex e.g. #0284c7
    secondary_color = models.CharField(max_length=7)
    background_color = models.CharField(max_length=7)
    is_dark = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['sort_order', 'id']

    def __str__(self):
        return self.name


# ── Inventory: Pencacahan (Unit Conversion) ───────────────────────────────────

class PencacahanRecord(models.Model):
    """
    Converts a quantity of one inventory item into a quantity of another item
    without changing total inventory value. Used to break bulk forms (bottles,
    boxes) into production-ready units (ml, g, pcs).
    """
    pencacahan_no    = models.CharField(max_length=50, unique=True)  # PCA-YYYYMMDD-N
    date             = models.DateField()
    source_item      = models.ForeignKey(
        InventoryItem, on_delete=models.PROTECT,
        related_name='pencacahan_source_records',
    )
    source_warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT,
        related_name='pencacahan_source_records',
    )
    source_quantity  = models.DecimalField(max_digits=14, decimal_places=4)  # in source small unit
    target_item      = models.ForeignKey(
        InventoryItem, on_delete=models.PROTECT,
        related_name='pencacahan_target_records',
    )
    target_warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT,
        related_name='pencacahan_target_records',
    )
    target_quantity  = models.DecimalField(max_digits=14, decimal_places=4)  # in target small unit
    value_transferred = models.DecimalField(max_digits=14, decimal_places=2)  # cost moved from source to target
    notes            = models.TextField(blank=True, default='')
    created_by       = models.ForeignKey(
        AppUser, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='pencacahan_records',
    )
    created_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.pencacahan_no}: {self.source_item} → {self.target_item}'

    @classmethod
    def next_number(cls, date):
        prefix = f'PCA-{date.strftime("%Y%m%d")}-'
        last = (
            cls.objects.filter(pencacahan_no__startswith=prefix)
            .order_by('pencacahan_no')
            .values_list('pencacahan_no', flat=True)
            .last()
        )
        n = int(last.split('-')[-1]) + 1 if last else 1
        return f'{prefix}{n}'


# Scheduled appointments (SatuSehat / FHIR R4 Appointment)


class AppointmentLocation(models.Model):
    """A bookable room. Maps to FHIR Location."""
    name = models.CharField(max_length=100)
    room_code = models.CharField(max_length=30, blank=True)
    is_active = models.BooleanField(default=True)
    # Assigned when the room is registered as a Location in SatuSehat. Sync phase only.
    ihs_id = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Appointment(models.Model):
    """
    A future-dated booking. Distinct from ActivePatient, which is the walk-in
    queue for today. Checking an Appointment in creates an ActivePatient and
    links it here, which is the only point the two flows touch.

    Field names and the status enum mirror FHIR R4 Appointment so the sync phase
    is a mapping walk with no schema change. See
    docs/satusehat-appointment-page-design.md section 7.
    """

    # FHIR R4 appointment status value set, verbatim. Only booked/arrived/
    # fulfilled/cancelled/noshow are reachable from the UI today; the rest are
    # carried so a future sync never hits an unmappable value.
    STATUS_CHOICES = [
        ('proposed', 'Proposed'),
        ('pending', 'Pending'),
        ('booked', 'Booked'),
        ('arrived', 'Arrived'),
        ('checked-in', 'Checked In'),
        ('fulfilled', 'Fulfilled'),
        ('cancelled', 'Cancelled'),
        ('noshow', 'No Show'),
        ('waitlist', 'Waitlist'),
        ('entered-in-error', 'Entered In Error'),
    ]

    APPOINTMENT_TYPE_CHOICES = [
        ('routine', 'Routine'),
        ('follow_up', 'Follow Up'),
        ('walk_in', 'Walk In'),
    ]

    SYNC_STATUS_CHOICES = [
        ('not_synced', 'Not Synced'),
        ('synced', 'Synced'),
        ('error', 'Error'),
    ]

    appointment_no = models.CharField(max_length=20, unique=True, blank=True)
    patient = models.ForeignKey(
        Patient, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='appointments',
    )
    guest_name = models.CharField(max_length=100, null=True, blank=True)
    practitioner = models.ForeignKey(
        Doctors, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='appointments',
    )
    location = models.ForeignKey(
        AppointmentLocation, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='appointments',
    )
    service_category = models.CharField(max_length=100, blank=True)
    service_type = models.CharField(max_length=100, blank=True)
    appointment_type = models.CharField(
        max_length=20, choices=APPOINTMENT_TYPE_CHOICES, blank=True)
    reason = models.TextField(blank=True)
    start_at = models.DateTimeField()
    end_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='booked')
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    linked_active_patient = models.ForeignKey(
        ActivePatient, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='scheduled_appointments',
    )

    # Sync-reserved. Written by the Phase E sync service; untouched today.
    ihs_appointment_id = models.CharField(max_length=64, null=True, blank=True)
    synced_at = models.DateTimeField(null=True, blank=True)
    sync_status = models.CharField(
        max_length=20, choices=SYNC_STATUS_CHOICES, default='not_synced')
    sync_error = models.TextField(blank=True)

    class Meta:
        ordering = ['start_at']
        indexes = [models.Index(fields=['start_at', 'status'])]

    def __str__(self):
        return f'{self.appointment_no}: {self.display_name}'

    @property
    def display_name(self):
        if self.patient_id:
            return self.patient.name
        return self.guest_name or 'Guest'

    @classmethod
    def next_number(cls, year):
        prefix = f'APT-{year}-'
        last = (
            cls.objects.filter(appointment_no__startswith=prefix)
            .order_by('appointment_no')
            .values_list('appointment_no', flat=True)
            .last()
        )
        n = int(last.split('-')[-1]) + 1 if last else 1
        return f'{prefix}{n:06d}'

    def save(self, *args, **kwargs):
        if not self.appointment_no:
            # Number by the Jakarta-local year: an appointment booked at 08:00 on
            # 1 Jan WIB is still 31 Dec in UTC, and should not get last year's prefix.
            year = timezone.localtime(
                self.start_at or timezone.now(), JAKARTA_TZ).year
            self.appointment_no = Appointment.next_number(year)
        super().save(*args, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# Taxation
#
# A tax rule is *data*, not code: the operator builds it on /accounting/tax by
# picking the accounts that form the base and choosing how a rate applies. The
# engine in services/tax_engine.py evaluates it against the LedgerEntry journal
# for a requested period, exactly like the financial reports do.
#
# Nothing here writes to the ledger — rules are a reporting overlay.
# ─────────────────────────────────────────────────────────────────────────────


class TaxRule(models.Model):
    """One computed tax line, e.g. 'PPN Keluaran' or 'PPh Badan'.

    ``rate_mode`` decides how the base (Dasar Pengenaan Pajak) becomes a tax
    amount. Four modes exist because Indonesian rates genuinely take four
    shapes and no single percentage field covers them:

    ``flat``      base x rate_percent. PPN, PPh Final UMKM.
    ``bracket``   progressive layers, see TaxRuleBracket. PPh 21.
    ``facility``  the Pasal 31E shape: income attributable to the first
                  ``facility_turnover_cap`` of turnover is taxed at
                  rate x ``facility_factor``, the remainder at the full rate.
    ``none``      no rate at all — the base *is* the answer. Used by netting
                  rules such as PPN Kurang Bayar (keluaran - masukan).

    Every threshold and rate is a field rather than a constant, so a change in
    the law is an edit on the page, not a deployment.
    """

    BASIS_CHOICES = [
        ('period', 'Periode Terpilih'),
        ('ytd',    'Akumulasi Tahun Berjalan'),
    ]
    RATE_MODE_CHOICES = [
        ('flat',     'Tarif Tunggal'),
        ('bracket',  'Tarif Progresif'),
        ('facility', 'Tarif Fasilitas (Pasal 31E)'),
        ('none',     'Tanpa Tarif'),
    ]
    ROUNDING_CHOICES = [
        ('none',     'Tanpa Pembulatan'),
        ('rupiah',   'Bulat Rupiah (ke bawah)'),
        ('thousand', 'Bulat Ribuan (ke bawah)'),
    ]

    code        = models.SlugField(max_length=40, unique=True)
    name        = models.CharField(max_length=120)
    description = models.CharField(max_length=300, blank=True, default='')

    # 'ytd' widens the window to 1 January of date_to's year regardless of the
    # requested date_from. Annual tests (the Rp 4,8bn omzet ceiling) are
    # meaningless against a single month.
    basis     = models.CharField(max_length=10, choices=BASIS_CHOICES, default='period')
    rate_mode = models.CharField(max_length=10, choices=RATE_MODE_CHOICES, default='flat')

    # flat / facility modes. Percent, not fraction: 11 means 11%.
    rate_percent = models.DecimalField(max_digits=7, decimal_places=4, default=0)

    # Subtracted from the base before any rate applies (PTKP, and any other
    # allowance that is a flat figure rather than a ledger balance).
    deduction_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    # ── facility mode (Pasal 31E) ────────────────────────────────────────────
    # The turnover tested against the caps comes from another rule, not from
    # this rule's own base: 31E tests *peredaran bruto* while taxing
    # *penghasilan kena pajak*.
    facility_turnover_rule = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='facility_dependents',
        help_text='Aturan yang menyediakan angka peredaran bruto untuk diuji.',
    )
    facility_turnover_cap = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='Batas peredaran bruto yang mendapat fasilitas (Rp 4,8 M).',
    )
    facility_full_rate_cap = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='Di atas batas ini fasilitas hilang sepenuhnya (Rp 50 M).',
    )
    facility_factor = models.DecimalField(
        max_digits=5, decimal_places=4, default=Decimal('0.5'),
        help_text='Pengali tarif untuk bagian yang mendapat fasilitas.',
    )

    rounding      = models.CharField(max_length=10, choices=ROUNDING_CHOICES, default='rupiah')
    display_order = models.IntegerField(default=0)
    is_active     = models.BooleanField(default=True)

    # A rule outside its effective window is skipped, so a rate change is
    # modelled as two rules rather than by editing history.
    effective_from = models.DateField(null=True, blank=True)
    effective_to   = models.DateField(null=True, blank=True)

    notes      = models.CharField(max_length=500, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['display_order', 'code']
        verbose_name = 'Tax Rule'
        verbose_name_plural = 'Tax Rules'

    def __str__(self):
        return f'{self.code} – {self.name}'

    def applies_on(self, day):
        """True when `day` falls inside the rule's effective window."""
        if self.effective_from and day < self.effective_from:
            return False
        if self.effective_to and day > self.effective_to:
            return False
        return True


class TaxRuleComponent(models.Model):
    """One signed term of a rule's base.

    The base is the sum of its components, each multiplied by ``sign``. That is
    the whole of the "formula" a user builds on the page: a netting rule such
    as PPN Kurang Bayar is a +keluaran and a -masukan component, and a DPP is
    one or more account selections.
    """

    SOURCE_CHOICES = [
        ('account', 'Akun'),
        ('subtree', 'Akun & Turunannya'),
        ('type',    'Jenis Akun'),
        ('rule',    'Hasil Aturan Lain'),
        ('fixed',   'Nilai Tetap'),
    ]

    rule   = models.ForeignKey(TaxRule, on_delete=models.CASCADE, related_name='components')
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default='account')
    # +1 adds the term to the base, -1 subtracts it.
    sign   = models.SmallIntegerField(default=1)

    # source='account' | 'subtree'
    account = models.ForeignKey(
        'ChartOfAccounts', null=True, blank=True,
        on_delete=models.PROTECT, related_name='tax_components',
    )
    # source='type'
    account_type = models.CharField(max_length=20, blank=True, default='')
    # source='rule' — reads the referenced rule's *result*.
    source_rule = models.ForeignKey(
        TaxRule, null=True, blank=True,
        on_delete=models.PROTECT, related_name='referenced_by',
    )
    # source='fixed'
    fixed_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    label         = models.CharField(max_length=120, blank=True, default='')
    display_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['display_order', 'id']

    def __str__(self):
        return f'{self.rule.code}: {self.sign:+d} {self.source}'


class TaxRuleBracket(models.Model):
    """One progressive layer of a ``rate_mode='bracket'`` rule (PPh 21).

    ``upper_bound`` is the top of the layer, inclusive; NULL means the layer
    runs to infinity. Layers are applied in ``upper_bound`` order and only the
    portion of the base falling inside a layer is taxed at that layer's rate.
    """

    rule = models.ForeignKey(TaxRule, on_delete=models.CASCADE, related_name='brackets')
    upper_bound = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='Batas atas lapisan; kosongkan untuk lapisan terakhir.',
    )
    rate_percent  = models.DecimalField(max_digits=7, decimal_places=4, default=0)
    display_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['display_order', 'id']

    def __str__(self):
        cap = self.upper_bound if self.upper_bound is not None else '∞'
        return f'{self.rule.code}: <={cap} @ {self.rate_percent}%'


# ── Monthly / weekly operational inputs (planning only) ──────────────────────


class OperationalInputTemplate(models.Model):
    """A recurring operational cost the manager is expected to record each period.

    Deliberately **planning-only**: recording a period's figure writes an
    ``OperationalInputEntry`` and nothing else. No ``Expense``, no
    ``LedgerEntry``, no ``posting_status`` — the journal engine never sees this
    model. That is a product decision, not an oversight: these rows exist so
    the operator can see the shape of monthly operating cost (and be nagged
    about the months they have not filled in) without a second, competing path
    into the general ledger. The books stay the books.

    ``account`` is therefore advisory. It records *which* expense account the
    real spend is expected to land in, so the operational-cost report can be
    read next to the P&L, but nothing reconciles the two automatically.
    """

    FREQ_MONTHLY = 'monthly'
    FREQ_WEEKLY = 'weekly'
    FREQUENCY_CHOICES = [(FREQ_MONTHLY, 'Bulanan'), (FREQ_WEEKLY, 'Mingguan')]

    name     = models.CharField(max_length=120)
    # Free-text grouping for the report's subtotals (e.g. 'Sewa & Utilitas').
    # A CharField rather than an FK because the groupings are a reporting
    # preference the operator reshuffles, not a controlled vocabulary.
    category = models.CharField(max_length=80, blank=True, default='')
    account  = models.ForeignKey(
        'ChartOfAccounts', on_delete=models.SET_NULL, null=True, blank=True,
        limit_choices_to={'account_type__in': ['expense', 'cogs']},
        related_name='operational_input_templates',
        help_text='Akun beban yang diharapkan menampung biaya ini. Hanya untuk pelaporan.',
    )
    frequency = models.CharField(max_length=10, choices=FREQUENCY_CHOICES, default=FREQ_MONTHLY)
    # Monthly: day of month, clamped to the month's length so 31 still resolves
    # in February. Weekly: ISO weekday, 1=Monday .. 7=Sunday.
    due_day   = models.PositiveSmallIntegerField(
        default=1,
        help_text='Bulanan: tanggal 1–31. Mingguan: hari 1 (Senin) – 7 (Minggu).',
    )
    expected_amount = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text='Perkiraan nominal; dipakai sebagai nilai awal saat input.',
    )
    is_active  = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)
    notes      = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['sort_order', 'name']
        constraints = [
            models.UniqueConstraint(fields=['name'], name='uniq_operational_input_template_name'),
        ]

    def __str__(self):
        return f'{self.name} ({self.get_frequency_display()})'


class OperationalInputEntry(models.Model):
    """One period's recorded figure for an ``OperationalInputTemplate``.

    ``period_key`` is the canonical identity of a period and the thing the
    uniqueness constraint is built on: ``'2026-08'`` for a monthly template,
    ``'2026-W34'`` for a weekly one. ``period_start`` is derived from it (first
    of the month / Monday of the ISO week) and stored so the reports can filter
    and order by date without parsing strings in SQL.

    Both are written by ``services.operational_inputs`` — never by hand, or the
    two will disagree about which week a Sunday belongs to.
    """

    template     = models.ForeignKey(
        OperationalInputTemplate, on_delete=models.CASCADE, related_name='entries',
    )
    period_key   = models.CharField(max_length=10)
    period_start = models.DateField()
    amount       = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    notes        = models.CharField(max_length=255, blank=True, default='')
    recorded_by  = models.ForeignKey(
        AppUser, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='operational_input_entries',
    )
    recorded_at  = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-period_start', 'template__sort_order']
        constraints = [
            models.UniqueConstraint(
                fields=['template', 'period_key'], name='uniq_operational_input_period',
            ),
        ]
        indexes = [
            models.Index(fields=['period_start'], name='idx_opinput_period_start'),
        ]

    def __str__(self):
        return f'{self.template.name} {self.period_key}: {self.amount}'


#####
# END#
#####
