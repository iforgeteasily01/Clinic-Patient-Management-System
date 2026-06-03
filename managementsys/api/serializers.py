from decimal import Decimal

from rest_framework import serializers

from ..models import (
    ActivePatient, AppUser, AssessmentCode, AttendanceRecord, Beauticians, ChartOfAccounts,
    Doctors, InventoryBatch, InventoryItem, Invoice, InvoiceItem, MedRec, Patient,
    PatientCRMProfile, PatientNote, PatientPackage, PatientPackageRedemption, PatientPhoto,
    PatientTier, Promotion, SiteConfig, SoapTemplate, StaffSchedule, Treatment, TreatmentCategory,
    TreatmentPackage, TreatmentPackageItem, TreatmentSession, Warehouse, WorkShift,
    patientStatus,
)


class PatientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Patient
        fields = ["patient_no", "name", "address", "phone_number", "NIK"]


class ActivePatientSerializer(serializers.ModelSerializer):
    patient_name = serializers.SerializerMethodField()
    current_beautician_name = serializers.SerializerMethodField()
    current_beautician_id = serializers.SerializerMethodField()
    treatment_session_ids = serializers.SerializerMethodField()
    current_treatments = serializers.SerializerMethodField()
    soap_treatment = serializers.SerializerMethodField()

    class Meta:
        model = ActivePatient
        fields = [
            "id", "patient_no", "patient_name", "guest_name",
            "status", "consult_status", "visit_time",
            "current_beautician_name", "current_beautician_id",
            "medrec_id",
            "treatment_session_ids",
            "current_treatments",
            "soap_treatment",
        ]

    def get_patient_name(self, obj):
        if obj.patient_no_id:
            return obj.patient_no.name
        return obj.guest_name

    def _current_session(self, obj):
        session = TreatmentSession.objects.filter(active_patient=obj).order_by('-session_time').first()
        if not session and obj.patient_no_id:
            session = TreatmentSession.objects.filter(patient_no=obj.patient_no).order_by('-session_time').first()
        return session

    def get_current_beautician_name(self, obj):
        session = self._current_session(obj)
        if session and session.beautician:
            return session.beautician.beautician_name
        return None

    def get_current_beautician_id(self, obj):
        session = self._current_session(obj)
        return session.beautician_id if session and session.beautician_id else None

    def get_treatment_session_ids(self, obj):
        return list(obj.treatmentsession_set.values_list('id', flat=True))

    def get_current_treatments(self, obj):
        result = []
        for session in obj.treatmentsession_set.prefetch_related('treatments').all():
            for t in session.treatments.all():
                result.append({'id': t.id, 'name': t.name, 'price': str(t.price)})
        return result

    def get_soap_treatment(self, obj):
        if obj.medrec_id and obj.medrec and obj.medrec.treatment:
            return obj.medrec.treatment
        return None


class DoctorsSerializer(serializers.ModelSerializer):
    class Meta:
        model = Doctors
        fields = ["id", "doctor_name"]


class BeauticiansSerializer(serializers.ModelSerializer):
    class Meta:
        model = Beauticians
        fields = ["id", "beautician_name", "bphone_number", "available"]


class MedRecSerializer(serializers.ModelSerializer):
    visit_date = serializers.DateField(write_only=True, required=False, allow_null=True)

    class Meta:
        model = MedRec
        fields = ["medrec_id", "doctor_id", "patient_no", "subjective", "objective",
                  "assessment", "assessment_codes", "plan", "sabun_pagi",
                  "sabun_malam", "toner_pagi", "toner_malam", "obat1_pagi", "obat2_pagi",
                  "obat1_malam", "obat2_malam", "treatment", "visit_date"]
        extra_kwargs = {
            'medrec_id':   {'required': False, 'allow_blank': True},
            'subjective':  {'allow_blank': True},
            'objective':   {'allow_blank': True},
            'assessment':  {'allow_blank': True},
            'plan':        {'allow_blank': True},
            'sabun_pagi':  {'required': False, 'allow_blank': True, 'allow_null': True},
            'sabun_malam': {'required': False, 'allow_blank': True, 'allow_null': True},
            'toner_pagi':  {'required': False, 'allow_blank': True, 'allow_null': True},
            'toner_malam': {'required': False, 'allow_blank': True, 'allow_null': True},
            'obat1_pagi':  {'required': False, 'allow_blank': True, 'allow_null': True},
            'obat2_pagi':  {'required': False, 'allow_blank': True, 'allow_null': True},
            'obat1_malam': {'required': False, 'allow_blank': True, 'allow_null': True},
            'obat2_malam': {'required': False, 'allow_blank': True, 'allow_null': True},
            'treatment':   {'required': False, 'allow_blank': True, 'allow_null': True},
        }

    def create(self, validated_data):
        visit_date = validated_data.pop('visit_date', None)
        date_str = visit_date.strftime('%Y-%m-%d') if visit_date else None
        instance = MedRec(**validated_data)
        instance.save(visit_date=date_str)
        return instance


class PatStatSerializer(serializers.ModelSerializer):
    class Meta:
        model = patientStatus
        fields = ["id", "status_name"]


class TreatmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Treatment
        fields = ["id", "code", "name", "category", "price", "active", "catalog_item_id"]
        read_only_fields = ["catalog_item_id"]


class AppUserPublicSerializer(serializers.ModelSerializer):
    profile_picture_url = serializers.SerializerMethodField()

    class Meta:
        model = AppUser
        fields = ["id", "display_name", "role", "avatar_color", "profile_picture_url",
                  "theme_primary", "theme_secondary", "theme_background"]

    def get_profile_picture_url(self, obj):
        if not obj.profile_picture:
            return None
        request = self.context.get('request')
        url = obj.profile_picture.url
        return request.build_absolute_uri(url) if request else url


# ── Assessment Codes ──────────────────────────────────────────────────────

class AssessmentCodeSerializer(serializers.ModelSerializer):
    class Meta:
        model = AssessmentCode
        fields = ['id', 'code', 'description', 'active', 'category']


# ── SOAP Templates ────────────────────────────────────────────────────────

class SoapTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = SoapTemplate
        fields = ['id', 'field', 'title', 'body', 'sort_order']


# ── Billing ────────────────────────────────────────────────────────────────

class BillingTreatmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Treatment
        fields = ["id", "code", "name", "price"]


class BillingSessionSerializer(serializers.ModelSerializer):
    treatments = BillingTreatmentSerializer(many=True, read_only=True)
    beautician_name = serializers.SerializerMethodField()

    class Meta:
        model = TreatmentSession
        fields = ["id", "beautician_name", "session_time", "treatments"]

    def get_beautician_name(self, obj):
        return obj.beautician.beautician_name if obj.beautician_id else None


class BillingMedRecSerializer(serializers.ModelSerializer):
    class Meta:
        model = MedRec
        fields = [
            'treatment',
            'sabun_pagi', 'sabun_malam',
            'toner_pagi', 'toner_malam',
            'obat1_pagi', 'obat2_pagi',
            'obat1_malam', 'obat2_malam',
        ]


class BillingPatientSerializer(serializers.ModelSerializer):
    patient_name = serializers.SerializerMethodField()
    sessions = serializers.SerializerMethodField()
    total = serializers.SerializerMethodField()
    medrec = BillingMedRecSerializer(read_only=True)

    class Meta:
        model = ActivePatient
        fields = ["id", "patient_no", "patient_name", "guest_name", "visit_time", "sessions", "total", "medrec"]

    def get_sessions(self, obj):
        return BillingSessionSerializer(
            obj.treatmentsession_set.all(), many=True, context=self.context,
        ).data

    def get_patient_name(self, obj):
        if obj.patient_no_id:
            return obj.patient_no.name
        return obj.guest_name

    def get_total(self, obj):
        total = Decimal('0')
        for session in obj.treatmentsession_set.all():
            for treatment in session.treatments.all():
                total += treatment.price
        return str(total)


# ── Inventory ──────────────────────────────────────────────────────────────

class InventoryItemSerializer(serializers.ModelSerializer):
    total_stock = serializers.IntegerField(read_only=True, default=0)
    created_by_name = serializers.SerializerMethodField()
    item_category_id = serializers.PrimaryKeyRelatedField(
        source='item_category',
        queryset=TreatmentCategory.objects.all(),
        allow_null=True, required=False,
    )
    item_category_name = serializers.CharField(
        source='item_category.name', read_only=True, allow_null=True
    )

    class Meta:
        model = InventoryItem
        fields = [
            'id', 'code', 'name', 'selling_price',
            'unit_small', 'unit_medium', 'unit_medium_qty',
            'unit_large', 'unit_large_qty',
            'category', 'item_category_id', 'item_category_name', 'legal_code',
            'is_active', 'is_service', 'min_stock',
            'created_by_name', 'created_at', 'updated_at', 'total_stock',
        ]
        read_only_fields = [
            'id', 'created_at', 'updated_at', 'created_by_name',
            'total_stock', 'is_service', 'item_category_name',
        ]

    def get_created_by_name(self, obj):
        return obj.created_by.display_name if obj.created_by_id else None


class PatientSyncSerializer(serializers.ModelSerializer):
    class Meta:
        model = Patient
        fields = ['patient_no', 'name', 'address', 'phone_number', 'NIK', 'updated_at']
        read_only_fields = fields


class ItemSyncSerializer(serializers.ModelSerializer):
    class Meta:
        model = InventoryItem
        fields = [
            'id', 'code', 'name', 'selling_price',
            'unit_small', 'unit_medium', 'unit_medium_qty',
            'unit_large', 'unit_large_qty',
            'is_active', 'is_service', 'min_stock',
            'created_at', 'updated_at',
        ]
        read_only_fields = fields


class WarehouseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Warehouse
        fields = ['id', 'code', 'name', 'is_active']


class InventoryBatchSerializer(serializers.ModelSerializer):
    item_code = serializers.CharField(source='item.code', read_only=True)
    item_name = serializers.CharField(source='item.name', read_only=True)
    item_unit_small = serializers.CharField(source='item.unit_small', read_only=True)
    warehouse_name = serializers.CharField(source='warehouse.name', read_only=True)
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = InventoryBatch
        fields = [
            'id', 'item_id', 'item_code', 'item_name', 'item_unit_small',
            'warehouse_id', 'warehouse_name',
            'input_date', 'quantity_initial', 'quantity_remaining', 'value',
            'created_by_name', 'created_at',
        ]
        read_only_fields = [
            'id', 'created_at',
            'item_code', 'item_name', 'item_unit_small',
            'warehouse_name', 'created_by_name',
        ]

    def get_created_by_name(self, obj):
        return obj.created_by.display_name if obj.created_by_id else None


# ── Patient Photos ─────────────────────────────────────────────────────────

class PatientPhotoSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = PatientPhoto
        fields = ['id', 'patient_no', 'photo_date', 'body_area', 'image_url', 'uploaded_at']
        read_only_fields = ['id', 'uploaded_at', 'image_url']

    def get_image_url(self, obj):
        request = self.context.get('request')
        if obj.image and request:
            return request.build_absolute_uri(obj.image.url)
        return None


# ── Medical Record History ─────────────────────────────────────────────────

class MedRecHistorySerializer(serializers.ModelSerializer):
    doctor_name = serializers.SerializerMethodField()
    patient_name = serializers.SerializerMethodField()
    visit_date = serializers.SerializerMethodField()

    class Meta:
        model = MedRec
        fields = [
            'medrec_id', 'patient_no', 'patient_name', 'doctor_id', 'doctor_name', 'visit_date',
            'subjective', 'objective', 'assessment', 'plan',
            'sabun_pagi', 'sabun_malam', 'toner_pagi', 'toner_malam',
            'obat1_pagi', 'obat2_pagi', 'obat1_malam', 'obat2_malam', 'treatment',
        ]

    def get_doctor_name(self, obj):
        return obj.doctor_id.doctor_name if obj.doctor_id_id else '—'

    def get_patient_name(self, obj):
        return obj.patient_no.name if obj.patient_no_id else '—'

    def get_visit_date(self, obj):
        # medrec_id format: MR-{patient_no}-{YYYYMMDD}-{N}
        for part in obj.medrec_id.split('-'):
            if len(part) == 8 and part.isdigit():
                return f"{part[:4]}-{part[4:6]}-{part[6:8]}"
        return None


# ── Invoice ────────────────────────────────────────────────────────────────

class InvoiceItemInputSerializer(serializers.Serializer):
    item_id      = serializers.IntegerField(required=False, allow_null=True)
    item_name    = serializers.CharField(required=False, allow_blank=True, default='')
    quantity     = serializers.DecimalField(max_digits=14, decimal_places=3)
    price        = serializers.DecimalField(max_digits=14, decimal_places=2)
    discount_pct = serializers.DecimalField(max_digits=5, decimal_places=2, default=0)
    # Package redemption: when set, this line is a Rp 0 redemption of one
    # session from the given PatientPackage (validated against treatment_id).
    redeem_patient_package_id = serializers.IntegerField(required=False, allow_null=True)
    treatment_id              = serializers.IntegerField(required=False, allow_null=True)


class InvoiceCreateSerializer(serializers.Serializer):
    datetime           = serializers.DateTimeField(required=False)
    patient_no         = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    payment_method_id  = serializers.IntegerField(required=False, allow_null=True)
    discount           = serializers.DecimalField(max_digits=14, decimal_places=2, default=0)
    cashier_id         = serializers.IntegerField(required=False, allow_null=True)
    warehouse_id       = serializers.IntegerField(required=False, allow_null=True)
    tax                = serializers.DecimalField(max_digits=14, decimal_places=2, default=0)
    additional_charges = serializers.DecimalField(max_digits=14, decimal_places=2, default=0)
    grand_total        = serializers.DecimalField(max_digits=14, decimal_places=2)
    notes              = serializers.CharField(required=False, allow_blank=True, default='')
    promotion_code     = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    items              = InvoiceItemInputSerializer(many=True)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("At least one item is required.")
        return value


class InvoiceUpdateSerializer(serializers.Serializer):
    datetime           = serializers.DateTimeField(required=False)
    patient_no         = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    payment_method_id  = serializers.IntegerField(required=False, allow_null=True)
    discount           = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    cashier_id         = serializers.IntegerField(required=False, allow_null=True)
    warehouse_id       = serializers.IntegerField(required=False, allow_null=True)
    tax                = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    additional_charges = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    grand_total        = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    items              = InvoiceItemInputSerializer(many=True, required=False)

    def validate_items(self, value):
        if value is not None and len(value) == 0:
            raise serializers.ValidationError("At least one item is required.")
        return value


class InvoiceItemReadSerializer(serializers.ModelSerializer):
    item_code    = serializers.SerializerMethodField()
    item_name    = serializers.SerializerMethodField()
    discount_pct = serializers.DecimalField(max_digits=5, decimal_places=2, read_only=True)

    def get_item_code(self, obj):
        return obj.item.code if obj.item_id else ''

    def get_item_name(self, obj):
        return obj.item.name if obj.item_id else obj.item_name

    class Meta:
        model = InvoiceItem
        fields = ['id', 'item_id', 'item_code', 'item_name', 'quantity', 'price', 'discount_pct']


class InvoiceReadSerializer(serializers.ModelSerializer):
    items                = InvoiceItemReadSerializer(many=True, read_only=True)
    patient_name         = serializers.SerializerMethodField()
    cashier_name         = serializers.SerializerMethodField()
    warehouse_name       = serializers.SerializerMethodField()
    payment_method_name  = serializers.SerializerMethodField()

    class Meta:
        model = Invoice
        fields = [
            'id', 'invoice_number', 'datetime',
            'patient_no_id', 'patient_name',
            'payment_method_id', 'payment_method_name',
            'discount', 'tax', 'additional_charges', 'grand_total',
            'cashier_id', 'cashier_name',
            'warehouse_id', 'warehouse_name',
            'items',
        ]

    def get_patient_name(self, obj):
        return obj.patient_no.name if obj.patient_no_id else None

    def get_cashier_name(self, obj):
        return obj.cashier.display_name if obj.cashier_id else None

    def get_warehouse_name(self, obj):
        return obj.warehouse.name if obj.warehouse_id else None

    def get_payment_method_name(self, obj):
        return obj.payment_method.name if obj.payment_method_id else None


# ── Chart of Accounts ──────────────────────────────────────────────────────

ACCOUNT_RANGES = {
    'asset':         (1000000, 1999999),
    'liability':     (2000000, 2999999),
    'equity':        (3000000, 3999999),
    'revenue':       (4000000, 4999999),
    'cogs':          (5000000, 5999999),
    'expense':       (6000000, 6999999),
    'other_income':  (7000000, 7999999),
    'other_expense': (8000000, 8999999),
}


class ChartOfAccountsSerializer(serializers.ModelSerializer):
    account_type_display = serializers.CharField(
        source='get_account_type_display', read_only=True
    )
    parent_name = serializers.CharField(
        source='parent.name', read_only=True, allow_null=True
    )
    parent_number = serializers.IntegerField(
        source='parent.account_number', read_only=True, allow_null=True
    )

    class Meta:
        model = ChartOfAccounts
        fields = [
            'id', 'account_number', 'name',
            'account_type', 'account_type_display',
            'balance', 'is_system', 'is_head',
            'parent', 'parent_name', 'parent_number',
        ]
        read_only_fields = ['id', 'balance', 'account_type_display', 'is_system', 'is_head']

    def validate(self, data):
        account_type = data.get(
            'account_type',
            getattr(self.instance, 'account_type', None),
        )
        account_number = data.get(
            'account_number',
            getattr(self.instance, 'account_number', None),
        )
        if account_type and account_number is not None:
            low, high = ACCOUNT_RANGES[account_type]
            if not (low <= account_number <= high):
                label = dict(ChartOfAccounts.ACCOUNT_TYPE_CHOICES).get(account_type, account_type)
                raise serializers.ValidationError({
                    'account_number': (
                        f'{label} accounts must be numbered '
                        f'{low:,}–{high:,}.'.replace(',', '.')
                    )
                })
        # Sub-accounts must have a parent; head accounts must not.
        is_head = getattr(self.instance, 'is_head', False)
        parent = data.get('parent', getattr(self.instance, 'parent', None))
        if not is_head and not parent:
            raise serializers.ValidationError({'parent': 'Sub-accounts must be assigned to a head account.'})
        return data


# ── Treatment Categories ───────────────────────────────────────────────────

class TreatmentCategorySerializer(serializers.ModelSerializer):
    revenue_account_number = serializers.IntegerField(
        source='revenue_account.account_number', read_only=True, allow_null=True
    )
    cogs_account_number = serializers.IntegerField(
        source='cogs_account.account_number', read_only=True, allow_null=True
    )

    class Meta:
        model = TreatmentCategory
        fields = ['id', 'name', 'revenue_account_number', 'cogs_account_number']
        read_only_fields = ['id', 'revenue_account_number', 'cogs_account_number']


# ── AppUser Admin ──────────────────────────────────────────────────────────

class AppUserAdminSerializer(serializers.ModelSerializer):
    pin = serializers.CharField(write_only=True, required=False, allow_blank=True)
    profile_picture_url = serializers.SerializerMethodField()

    class Meta:
        model = AppUser
        fields = ['id', 'display_name', 'role', 'avatar_color', 'is_active', 'pin', 'profile_picture_url']

    def get_profile_picture_url(self, obj):
        if not obj.profile_picture:
            return None
        request = self.context.get('request')
        url = obj.profile_picture.url
        return request.build_absolute_uri(url) if request else url

    def validate(self, data):
        if not self.instance and not data.get('pin', '').strip():
            raise serializers.ValidationError({'pin': 'PIN is required when creating a user.'})
        pin = data.get('pin', '').strip()
        if pin and (len(pin) != 6 or not pin.isdigit()):
            raise serializers.ValidationError({'pin': 'PIN must be exactly 6 digits.'})
        return data

    def create(self, validated_data):
        pin = validated_data.pop('pin')
        user = AppUser(**validated_data)
        user.set_pin(pin)
        user.save()
        return user

    def update(self, instance, validated_data):
        pin = validated_data.pop('pin', '').strip()
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if pin:
            instance.set_pin(pin)
        instance.save()
        return instance


# ── Patient Notes ──────────────────────────────────────────────────────────

class PatientNoteSerializer(serializers.ModelSerializer):
    patient_no = serializers.CharField(source='patient_no_id', read_only=True)

    class Meta:
        model = PatientNote
        fields = ['id', 'patient_no', 'date', 'content', 'author', 'created_at', 'updated_at']
        read_only_fields = ['id', 'patient_no', 'created_at', 'updated_at']


# ── CRM / Promotions ───────────────────────────────────────────────────────

class PatientTierSerializer(serializers.ModelSerializer):
    class Meta:
        model = PatientTier
        fields = ['id', 'name', 'min_visit_count', 'min_total_spend', 'color_hex', 'sort_order']


class PromotionListSerializer(serializers.ModelSerializer):
    usage_count = serializers.IntegerField(read_only=True)
    min_tier_name = serializers.CharField(source='min_tier.name', read_only=True, allow_null=True)
    created_by_name = serializers.CharField(source='created_by.display_name', read_only=True, allow_null=True)

    class Meta:
        model = Promotion
        fields = [
            'id', 'code', 'name', 'description',
            'discount_type', 'discount_value',
            'scope', 'valid_from', 'valid_until',
            'max_uses', 'max_uses_per_patient',
            'min_tier_id', 'min_tier_name',
            'is_auto', 'is_active',
            'created_by_id', 'created_by_name', 'created_at',
            'usage_count',
        ]


class PromotionDetailSerializer(serializers.ModelSerializer):
    usage_count = serializers.IntegerField(read_only=True)
    min_tier_name = serializers.CharField(source='min_tier.name', read_only=True, allow_null=True)
    created_by_name = serializers.CharField(source='created_by.display_name', read_only=True, allow_null=True)
    applicable_category_ids = serializers.PrimaryKeyRelatedField(
        source='applicable_categories', many=True, read_only=True
    )
    applicable_item_ids = serializers.PrimaryKeyRelatedField(
        source='applicable_items', many=True, read_only=True
    )

    class Meta:
        model = Promotion
        fields = [
            'id', 'code', 'name', 'description',
            'discount_type', 'discount_value',
            'scope', 'applicable_category_ids', 'applicable_item_ids',
            'valid_from', 'valid_until',
            'max_uses', 'max_uses_per_patient',
            'min_tier_id', 'min_tier_name',
            'is_auto', 'is_active',
            'created_by_id', 'created_by_name', 'created_at',
            'usage_count',
        ]


class PatientCRMSerializer(serializers.ModelSerializer):
    tier = serializers.SerializerMethodField()
    total_spend = serializers.DecimalField(
        source='crm_profile.total_spend', max_digits=18, decimal_places=2,
        read_only=True, default='0.00',
    )
    total_visits = serializers.IntegerField(source='crm_profile.total_visits', read_only=True, default=0)
    last_visit_date = serializers.DateField(source='crm_profile.last_visit_date', read_only=True, allow_null=True)

    class Meta:
        model = Patient
        fields = ['patient_no', 'name', 'phone_number', 'tier', 'total_spend', 'total_visits', 'last_visit_date']

    def get_tier(self, obj):
        crm = getattr(obj, 'crm_profile', None)
        if crm is None or crm.tier_id is None:
            return None
        return {'name': crm.tier.name, 'color_hex': crm.tier.color_hex}


# ── HR / Attendance ────────────────────────────────────────────────────────

class WorkShiftSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkShift
        fields = ['id', 'name', 'expected_start', 'expected_end', 'color_hex']


class StaffScheduleSerializer(serializers.ModelSerializer):
    shift = WorkShiftSerializer(read_only=True)

    class Meta:
        model = StaffSchedule
        fields = ['id', 'staff', 'date', 'shift', 'notes']


class AttendanceRecordSerializer(serializers.ModelSerializer):
    total_hours = serializers.ReadOnlyField()
    late_minutes = serializers.ReadOnlyField()
    display_name = serializers.CharField(source='staff.display_name', read_only=True)
    role = serializers.CharField(source='staff.role', read_only=True)

    class Meta:
        model = AttendanceRecord
        fields = [
            'id', 'staff_id', 'display_name', 'role',
            'date', 'clock_in', 'clock_out', 'status',
            'total_hours', 'late_minutes', 'notes',
        ]


class AttendanceSummarySerializer(serializers.Serializer):
    staff_id = serializers.IntegerField()
    display_name = serializers.CharField()
    role = serializers.CharField()
    days_present = serializers.IntegerField()
    days_late = serializers.IntegerField()
    days_absent = serializers.IntegerField()
    total_hours = serializers.FloatField()


# ── Treatment Packages ─────────────────────────────────────────────────────

class TreatmentPackageItemSerializer(serializers.ModelSerializer):
    treatment_code = serializers.CharField(source='treatment.code', read_only=True)
    treatment_name = serializers.CharField(source='treatment.name', read_only=True)

    class Meta:
        model = TreatmentPackageItem
        fields = ['id', 'treatment', 'treatment_code', 'treatment_name', 'sessions']


class TreatmentPackageSerializer(serializers.ModelSerializer):
    items = TreatmentPackageItemSerializer(many=True)
    total_sessions = serializers.IntegerField(read_only=True)
    catalog_item_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = TreatmentPackage
        fields = [
            'id', 'code', 'name', 'description', 'price', 'active',
            'catalog_item_id', 'total_sessions', 'items',
        ]

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError('A package must include at least one treatment.')
        seen = set()
        for item in value:
            tid = item['treatment'].id
            if tid in seen:
                raise serializers.ValidationError('Duplicate treatment in package.')
            seen.add(tid)
            if item['sessions'] < 1:
                raise serializers.ValidationError('Sessions must be at least 1 per treatment.')
        return value

    def create(self, validated_data):
        items_data = validated_data.pop('items')
        package = TreatmentPackage.objects.create(**validated_data)
        TreatmentPackageItem.objects.bulk_create([
            TreatmentPackageItem(package=package, treatment=i['treatment'], sessions=i['sessions'])
            for i in items_data
        ])
        return package

    def update(self, instance, validated_data):
        items_data = validated_data.pop('items', None)
        for attr, val in validated_data.items():
            setattr(instance, attr, val)
        instance.save()
        if items_data is not None:
            instance.items.all().delete()
            TreatmentPackageItem.objects.bulk_create([
                TreatmentPackageItem(package=instance, treatment=i['treatment'], sessions=i['sessions'])
                for i in items_data
            ])
        return instance


class PatientPackageRedemptionSerializer(serializers.ModelSerializer):
    treatment_name = serializers.CharField(source='treatment.name', read_only=True)
    invoice_number = serializers.CharField(source='invoice.invoice_number', read_only=True, allow_null=True)

    class Meta:
        model = PatientPackageRedemption
        fields = ['id', 'treatment', 'treatment_name', 'invoice_number', 'redeemed_at']


class PatientPackageSerializer(serializers.ModelSerializer):
    package_code = serializers.CharField(source='package.code', read_only=True)
    package_name = serializers.CharField(source='package.name', read_only=True)
    total_sessions = serializers.SerializerMethodField()
    remaining = serializers.SerializerMethodField()
    per_treatment = serializers.SerializerMethodField()
    redemptions = PatientPackageRedemptionSerializer(many=True, read_only=True)
    purchased_invoice_number = serializers.CharField(source='purchased_invoice.invoice_number', read_only=True, allow_null=True)

    class Meta:
        model = PatientPackage
        fields = [
            'id', 'patient', 'package', 'package_code', 'package_name',
            'purchased_invoice', 'purchased_invoice_number', 'purchased_at',
            'status', 'total_sessions', 'remaining', 'per_treatment', 'redemptions',
        ]

    def get_total_sessions(self, obj):
        return obj.package.total_sessions

    def get_remaining(self, obj):
        return obj.total_remaining()

    def get_per_treatment(self, obj):
        return [
            {
                'treatment_id': item.treatment_id,
                'treatment_name': item.treatment.name,
                'entitled': item.sessions,
                'remaining': obj.remaining_for(item.treatment_id),
            }
            for item in obj.package.items.select_related('treatment').all()
        ]




class SiteConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = SiteConfig
        fields = ["clinic_name", "address_line1", "address_line2", "phone_fax",
                  "receipt_header_extra", "receipt_footer"]
