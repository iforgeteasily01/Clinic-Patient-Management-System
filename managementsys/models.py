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
    address = models.CharField(max_length=100, null=True)
    phone_number = models.CharField(max_length=15, null=True)
    NIK = models.CharField(max_length=16, null=True)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # Only generate if not already set
        if not self.patient_no:
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
    medrec_id = models.CharField(max_length=30, unique=True, blank=True)
    # set null as in, if there is no doctor that takes care of this patient, then doctor_id is Null rather than deleted
    doctor_id = models.ForeignKey(
        Doctors, on_delete=models.SET_NULL, null=True)
    patient_no = models.ForeignKey(Patient, on_delete=models.CASCADE)
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


class Treatment(models.Model):
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    category = models.CharField(max_length=50)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    active = models.BooleanField(default=True)
    catalog_item = models.OneToOneField(
        'InventoryItem',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='treatment',
    )

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
    quantity_initial = models.PositiveIntegerField()    # in smallest unit
    quantity_remaining = models.PositiveIntegerField()  # in smallest unit; decremented FIFO
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
    invoice  = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='items')
    item     = models.ForeignKey('InventoryItem', on_delete=models.PROTECT, related_name='invoice_items')
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    price    = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f"{self.invoice.invoice_number} – {self.item.name} ×{self.quantity}"


class TreatmentCategory(models.Model):
    name = models.CharField(max_length=50, unique=True)
    revenue_account = models.OneToOneField(
        'ChartOfAccounts',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='treatment_category',
    )

    class Meta:
        ordering = ['name']
        verbose_name_plural = 'Treatment Categories'

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        if is_new and not self.revenue_account_id:
            max_num = (
                ChartOfAccounts.objects
                .filter(account_number__gte=4400000, account_number__lte=4999999)
                .aggregate(m=Max('account_number'))['m']
            )
            next_num = (max_num + 1000) if max_num is not None else 4400000
            if next_num > 4999999:
                raise ValueError('Revenue account range 4400000–4999999 is exhausted.')
            account = ChartOfAccounts.objects.create(
                account_number=next_num,
                name=f'Treatment Revenue – {self.name}',
                account_type='revenue',
            )
            self.revenue_account = account
        elif not is_new and self.revenue_account_id:
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


#####
# END#
#####
