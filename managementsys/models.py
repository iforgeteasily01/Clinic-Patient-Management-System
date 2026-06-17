import secrets
from datetime import date

from django.contrib.auth.hashers import check_password, make_password
from django.db import models
from django.db.models import Max, Min, Avg, Sum, Count
from django.utils import timezone


class Patient(models.Model):
    patient_no = models.CharField(
        max_length=10, unique=True, primary_key=True, blank=True)
    name = models.CharField(max_length=100, null=True)
    address = models.CharField(max_length=100, null=True, blank=True)
    phone_number = models.CharField(max_length=15, null=True, blank=True)
    NIK = models.CharField(max_length=16, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # Only generate if not already set
        if not self.patient_no:
            if not self.name:
                raise ValueError("Patient name is required to generate patient_no.")
            prefix = self.name[0].upper()

            maxnumber = Patient.objects.filter(
                patient_no__startswith=prefix
            ).aggregate(
                lastnumber=models.Max('patient_no')
            )["lastnumber"]

            if maxnumber:
                max_number = int(maxnumber[1:])
                new_number = max_number + 1
            else:
                new_number = 1

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
    sabun_pagi = models.TextField(default="", null=True)
    sabun_malam = models.TextField(default="", null=True)
    toner_pagi = models.TextField(default="", null=True)
    toner_malam = models.TextField(default="", null=True)
    obat1_pagi = models.TextField(default="", null=True)
    obat2_pagi = models.TextField(default="", null=True)
    obat1_malam = models.TextField(default="", null=True)
    obat2_malam = models.TextField(default="", null=True)
    treatment = models.TextField(default="", null=True)

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
        if is_new or not self.catalog_item_id:
            item = InventoryItem.objects.create(
                code=self.code,
                name=self.name,
                selling_price=self.price,
                unit_small='session',
                is_service=True,
                is_active=self.active,
                min_stock=0,
            )
            Treatment.objects.filter(pk=self.pk).update(catalog_item=item)
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


class TreatmentMaterial(models.Model):
    """Stock items consumed during a treatment (e.g. 5 ml of cleanser per facial)."""
    treatment = models.ForeignKey(
        Treatment,
        on_delete=models.CASCADE,
        related_name='materials',
    )
    item = models.ForeignKey(
        'InventoryItem',
        on_delete=models.PROTECT,
        related_name='treatment_materials',
        limit_choices_to={'is_service': False},
    )
    quantity_small = models.DecimalField(max_digits=10, decimal_places=4)

    class Meta:
        unique_together = ('treatment', 'item')
        ordering = ['id']

    def __str__(self):
        return f'{self.treatment.name} — {self.quantity_small} {self.item.unit_small} of {self.item.name}'


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

    class Meta:
        ordering = ['account_number']
        verbose_name        = 'Chart of Account'
        verbose_name_plural = 'Chart of Accounts'

    def __str__(self):
        return f'{self.account_number} – {self.name}'


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


class Invoice(models.Model):
    invoice_number     = models.CharField(max_length=30, unique=True, blank=True)
    datetime           = models.DateTimeField()
    patient_no         = models.ForeignKey(Patient, on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')
    payment_method     = models.ForeignKey('ChartOfAccounts', on_delete=models.PROTECT, null=True, blank=True, related_name='invoices', limit_choices_to={'account_type': 'asset'})
    discount           = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    cashier            = models.ForeignKey('AppUser', on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices_as_cashier')
    warehouse          = models.ForeignKey('Warehouse', on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')
    tax                = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    additional_charges = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    grand_total        = models.DecimalField(max_digits=14, decimal_places=2)
    promotion          = models.ForeignKey('Promotion', on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')
    notes              = models.CharField(max_length=500, blank=True, default='')
    promotion_code     = models.CharField(max_length=50, blank=True, default='')

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
    ]

    account          = models.ForeignKey('ChartOfAccounts', on_delete=models.PROTECT, related_name='ledger_entries')
    date             = models.DateField()
    description      = models.CharField(max_length=255)
    entry_type       = models.CharField(max_length=6, choices=ENTRY_TYPE_CHOICES)
    amount           = models.DecimalField(max_digits=18, decimal_places=2)
    source_type      = models.CharField(max_length=15, choices=SOURCE_TYPE_CHOICES, blank=True, default='')
    invoice          = models.ForeignKey('Invoice', on_delete=models.SET_NULL, null=True, blank=True, related_name='ledger_entries')
    purchase_invoice = models.ForeignKey('PurchaseInvoice', on_delete=models.SET_NULL, null=True, blank=True, related_name='ledger_entries')
    transfer         = models.ForeignKey('AccountTransfer', on_delete=models.SET_NULL, null=True, blank=True, related_name='ledger_entries')
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
    cogs_account = models.OneToOneField(
        'ChartOfAccounts',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='cogs_category',
    )
    expense_account = models.OneToOneField(
        'ChartOfAccounts',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='expense_category',
    )

    class Meta:
        ordering = ['sort_order', 'name']
        verbose_name_plural = 'Treatment Categories'

    def __str__(self):
        return self.name

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
        if not self.cogs_account_id:
            self.cogs_account = ChartOfAccounts.objects.create(
                account_number=self._next_account_number(5400000, 5999999),
                name=f'COGS – {self.name}',
                account_type='cogs',
            )
            created['cogs_account'] = str(self.cogs_account)
        if not self.expense_account_id:
            self.expense_account = ChartOfAccounts.objects.create(
                account_number=self._next_account_number(6900000, 6999999),
                name=f'Expense – {self.name}',
                account_type='expense',
            )
            created['expense_account'] = str(self.expense_account)
        if created:
            TreatmentCategory.objects.filter(pk=self.pk).update(
                revenue_account_id=self.revenue_account_id,
                cogs_account_id=self.cogs_account_id,
                expense_account_id=self.expense_account_id,
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
            if not self.cogs_account_id:
                self.cogs_account = ChartOfAccounts.objects.create(
                    account_number=self._next_account_number(5400000, 5999999),
                    name=f'COGS – {self.name}',
                    account_type='cogs',
                )
            if not self.expense_account_id:
                self.expense_account = ChartOfAccounts.objects.create(
                    account_number=self._next_account_number(6900000, 6999999),
                    name=f'Expense – {self.name}',
                    account_type='expense',
                )
        else:
            if self.revenue_account_id:
                ChartOfAccounts.objects.filter(pk=self.revenue_account_id).update(
                    name=f'Treatment Revenue – {self.name}',
                )
            if self.cogs_account_id:
                ChartOfAccounts.objects.filter(pk=self.cogs_account_id).update(
                    name=f'COGS – {self.name}',
                )
            if self.expense_account_id:
                ChartOfAccounts.objects.filter(pk=self.expense_account_id).update(
                    name=f'Expense – {self.name}',
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
    patient_no = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name='notes')
    date = models.DateField()
    content = models.TextField()
    author = models.CharField(max_length=100, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date', '-created_at']

    def __str__(self):
        return f'Note for {self.patient_no_id} on {self.date}'


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
    item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT, related_name='stock_out_logs')
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name='stock_out_logs')
    out_date = models.DateField()
    quantity = models.DecimalField(max_digits=14, decimal_places=4)
    notes = models.TextField(blank=True, default='')
    created_by = models.ForeignKey(AppUser, null=True, blank=True, on_delete=models.SET_NULL, related_name='stock_out_logs')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


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

    class Meta:
        verbose_name = 'Site Configuration'

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

class Supplier(models.Model):
    name         = models.CharField(max_length=100)
    contact_name = models.CharField(max_length=100, blank=True)
    phone        = models.CharField(max_length=50, blank=True)
    email        = models.EmailField(blank=True)
    address      = models.TextField(blank=True)
    is_active    = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class PurchaseInvoice(models.Model):
    STATUS_CHOICES = [
        ('unpaid',  'Belum Dibayar'),
        ('partial', 'Sebagian Dibayar'),
        ('paid',    'Lunas'),
    ]

    internal_id          = models.CharField(max_length=30, unique=True, blank=True)
    external_invoice_no  = models.CharField(max_length=100, blank=True)
    supplier             = models.ForeignKey('Supplier', on_delete=models.PROTECT, related_name='purchase_invoices')
    payment_account      = models.ForeignKey(
        'ChartOfAccounts',
        on_delete=models.PROTECT,
        related_name='purchase_invoices',
        limit_choices_to={'account_type': 'asset'},
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

    class Meta:
        ordering = ['-transfer_date', '-created_at']

    def __str__(self):
        return f'{self.transfer_date} | {self.from_account} → {self.to_account}'


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


#####
# END#
#####
