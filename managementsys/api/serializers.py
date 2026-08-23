from decimal import Decimal

from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers

from ..models import (
    JAKARTA_TZ,
    AccountTransfer, ActivePatient, Appointment, AppointmentLocation, AppUser,
    AssessmentCode, AttendanceRecord, Beauticians, Branch,
    BankReconciliation, BankStatementLine,
    ChartOfAccounts, ColorPalette, Doctors, Expense, ExpenseAlias, ExpenseItem, InventoryBatch, InventoryItem, Invoice, InvoiceItem, InvoicePayment,
    IssueTicket, IssueTicketImage, JournalEntry, JournalStagingBatch, LedgerEntry,
    MedRec, OperationalInputEntry, OperationalInputTemplate, Patient, PatientCRMProfile,
    PatientNote, PatientPackage, PatientPackageRedemption, PatientPhoto, PatientTier,
    PaymentMethod,
    SalesReturn, SalesReturnItem,
    ProductionRecipe, ProductionRecipeIngredient, ProductionRun, ProductionRunIngredient,
    Promotion, PurchaseAdditionalCost, PurchaseInvoice, PurchaseInvoiceItem,
    PurchasePayment, ReportSettings, ReservationRequest, SiteConfig,
    SoapTemplate, StagedJournalEntry, StagedJournalLine, StaffSchedule, Supplier,
    Treatment, TreatmentCategory,
    TreatmentPackage, TreatmentPackageItem, TreatmentSession, Warehouse, WorkShift, patientStatus,
)


class BranchSerializer(serializers.ModelSerializer):
    """Read/write shape for the Branches settings page.

    ``is_default`` is writable: promoting a branch to default is the intended
    admin action, and Branch.save() demotes the previous default so the
    singleton can never be broken by two concurrent writes both setting True.
    """
    staff_count = serializers.SerializerMethodField()

    class Meta:
        model = Branch
        fields = ['id', 'code', 'name', 'address_line1', 'address_line2',
                  'phone_fax', 'is_active', 'is_default', 'sort_order',
                  'staff_count']

    def get_staff_count(self, obj):
        return obj.staff.filter(is_active=True).count()

    def validate_code(self, value):
        return value.strip().upper()


class PatientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Patient
        fields = ["patient_no", "name", "address", "phone_number", "NIK",
                  "birth_date", "gender", "wa_opt_in", "wa_opt_in_at"]
        # wa_opt_in is read-only here on purpose: consent is changed through
        # /api/patients/<no>/wa-opt-in/, which logs it. A stale admin form
        # PUT-ing the whole patient must not be able to flip it back.
        read_only_fields = ["wa_opt_in", "wa_opt_in_at"]
        extra_kwargs = {
            # name is nullable in DB but must be provided on input — the model's
            # save() method needs it to generate patient_no.
            'name': {'required': True, 'allow_null': False, 'allow_blank': False},
            # patient_no is optional on create: if provided it is used as-is,
            # otherwise the model's save() auto-generates one from the name initial.
            'patient_no': {'required': False, 'allow_blank': True},
        }


class ActivePatientSerializer(serializers.ModelSerializer):
    patient_name = serializers.SerializerMethodField()
    current_beautician_name = serializers.SerializerMethodField()
    current_beautician_id = serializers.SerializerMethodField()
    treatment_session_ids = serializers.SerializerMethodField()
    current_treatments = serializers.SerializerMethodField()
    medrec_code = serializers.SerializerMethodField()
    soap_treatment = serializers.SerializerMethodField()

    class Meta:
        model = ActivePatient
        fields = [
            "id", "patient_no", "patient_name", "guest_name",
            "status", "consult_status", "visit_time",
            "current_beautician_name", "current_beautician_id",
            "medrec_id",
            "medrec_code",
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
                result.append({'id': t.id, 'name': t.name, 'price': str(t.price), 'session_id': session.id})
        return result

    def get_medrec_code(self, obj):
        # medrec_id above is the MedRec table's integer PK; this is the
        # MR-... code the medical-record endpoints look records up by.
        if obj.medrec_id:
            return obj.medrec.medrec_id
        return None

    def get_soap_treatment(self, obj):
        if obj.medrec_id and obj.medrec and obj.medrec.treatment:
            return obj.medrec.treatment
        return None


class DoctorsSerializer(serializers.ModelSerializer):
    class Meta:
        model = Doctors
        fields = ["id", "doctor_name"]


class BeauticiansSerializer(serializers.ModelSerializer):
    current_patient = serializers.SerializerMethodField()

    def get_current_patient(self, obj):
        session = (
            TreatmentSession.objects
            .filter(beautician=obj, active_patient__isnull=False)
            .select_related('active_patient__patient_no')
            .order_by('-session_time')
            .first()
        )
        if not session:
            return None
        ap = session.active_patient
        name = ap.patient_no.name if ap.patient_no else ap.guest_name
        return {'active_patient_id': ap.id, 'patient_name': name}

    class Meta:
        model = Beauticians
        fields = ["id", "beautician_name", "bphone_number", "available", "current_patient"]


class BeauticianAdminStatusSerializer(serializers.ModelSerializer):
    """Extended serializer for admin view — includes the current/last session info."""
    current_session = serializers.SerializerMethodField()

    class Meta:
        model = Beauticians
        fields = ["id", "beautician_name", "bphone_number", "available", "current_session"]

    def get_current_session(self, obj):
        session = (
            TreatmentSession.objects
            .filter(beautician=obj)
            .select_related('active_patient', 'active_patient__patient_no', 'patient_no')
            .order_by('-session_time')
            .first()
        )
        if not session:
            return None

        if session.active_patient_id:
            ap = session.active_patient
            patient_name = ap.patient_no.name if ap.patient_no_id else (ap.guest_name or '—')
            ap_status = ap.status
            ap_id = ap.id
        elif session.patient_no_id:
            patient_name = session.patient_no.name
            ap_status = None
            ap_id = None
        else:
            return None

        # Stuck = beautician marked busy but patient is no longer in status 4
        is_stuck = (not obj.available) and (ap_status != 4)

        return {
            'session_id': session.id,
            'active_patient_id': ap_id,
            'patient_name': patient_name,
            'active_patient_status': ap_status,
            'session_time': session.session_time,
            'is_stuck': is_stuck,
        }


class MedRecSerializer(serializers.ModelSerializer):
    visit_date = serializers.DateField(write_only=True, required=False, allow_null=True)

    class Meta:
        model = MedRec
        fields = ["medrec_id", "doctor_id", "patient_no", "status", "subjective", "objective",
                  "assessment", "assessment_codes", "plan", "sabun", "toner",
                  "obat1_pagi", "obat1_pagi_detail", "obat1_malam", "obat1_malam_detail",
                  "obat2_pagi", "obat2_pagi_detail", "obat2_malam", "obat2_malam_detail", "treatment", "visit_date"]
        extra_kwargs = {
            'medrec_id':    {'required': False, 'allow_blank': True},
            'status':       {'required': False},
            'subjective':   {'allow_blank': True},
            'objective':    {'allow_blank': True},
            'assessment':   {'allow_blank': True},
            'plan':         {'allow_blank': True},
            'sabun':        {'required': False, 'allow_blank': True, 'allow_null': True},
            'toner':        {'required': False, 'allow_blank': True, 'allow_null': True},
            'obat1_pagi':         {'required': False, 'allow_blank': True, 'allow_null': True},
            'obat1_pagi_detail':  {'required': False, 'allow_blank': True, 'allow_null': True},
            'obat1_malam':        {'required': False, 'allow_blank': True, 'allow_null': True},
            'obat1_malam_detail': {'required': False, 'allow_blank': True, 'allow_null': True},
            'obat2_pagi':         {'required': False, 'allow_blank': True, 'allow_null': True},
            'obat2_pagi_detail':  {'required': False, 'allow_blank': True, 'allow_null': True},
            'obat2_malam':        {'required': False, 'allow_blank': True, 'allow_null': True},
            'obat2_malam_detail': {'required': False, 'allow_blank': True, 'allow_null': True},
            'treatment':    {'required': False, 'allow_blank': True, 'allow_null': True},
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
        fields = ["id", "code", "name", "category", "price", "active", "sort_order", "catalog_item_id"]
        read_only_fields = ["catalog_item_id"]


class AppUserPublicSerializer(serializers.ModelSerializer):
    profile_picture_url = serializers.SerializerMethodField()
    # The client needs all three to render the branch picker without a second
    # round trip: which branch to default to, what to label it, and whether the
    # picker is a control at all or a read-only badge.
    home_branch_name = serializers.CharField(source='home_branch.name', read_only=True, default=None)
    home_branch_code = serializers.CharField(source='home_branch.code', read_only=True, default=None)
    can_cross_branch = serializers.SerializerMethodField()

    class Meta:
        model = AppUser
        fields = ["id", "display_name", "role", "avatar_color", "profile_picture_url",
                  "theme_primary", "theme_secondary", "theme_background",
                  "home_branch", "home_branch_name", "home_branch_code",
                  "can_cross_branch"]

    def get_can_cross_branch(self, obj):
        from ..services.branches import can_cross_branch
        return can_cross_branch(obj)

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
            'sabun', 'toner',
            'obat1_pagi', 'obat1_pagi_detail', 'obat1_malam', 'obat1_malam_detail',
            'obat2_pagi', 'obat2_pagi_detail', 'obat2_malam', 'obat2_malam_detail',
        ]


class BillingPatientSerializer(serializers.ModelSerializer):
    patient_name = serializers.SerializerMethodField()
    sessions = serializers.SerializerMethodField()
    total = serializers.SerializerMethodField()
    medrec = BillingMedRecSerializer(read_only=True)
    notes = serializers.SerializerMethodField()

    class Meta:
        model = ActivePatient
        fields = ["id", "patient_no", "patient_name", "guest_name", "visit_time", "sessions", "total", "medrec", "notes"]

    def get_notes(self, obj):
        """Today's notes for this visit, oldest first.

        The caller (BillingQueueView) puts a prebuilt map in the context so the
        whole queue costs one query. The per-object fallback below only runs
        when this serializer is used somewhere that did not prefetch.
        """
        cached = self.context.get('notes_by_visit')
        if cached is not None:
            notes = cached.get(obj.id, [])
        else:
            subject = Q(active_patient_id=obj.id)
            if obj.patient_no_id:
                subject |= Q(patient_no_id=obj.patient_no_id)
            notes = (
                PatientNote.objects
                .filter(date=timezone.localdate())
                .filter(subject)
                .select_related('author_user')
                .order_by('created_at')
            )
        return PatientNoteSerializer(notes, many=True, context=self.context).data

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
        fields = ['patient_no', 'name', 'address', 'phone_number', 'NIK',
                  'birth_date', 'gender', 'updated_at']
        read_only_fields = fields


class ItemSyncSerializer(serializers.ModelSerializer):
    class Meta:
        model = InventoryItem
        fields = [
            'id', 'code', 'name', 'selling_price',
            'unit_small', 'unit_medium', 'unit_medium_qty',
            'unit_large', 'unit_large_qty',
            # The POS mirrors this payload into SQLite with an upsert that
            # overwrites every column it knows about. It has category and
            # legal_code columns, so leaving them out of the payload did not
            # merely skip them — each sync wiped the local values to NULL.
            'category', 'legal_code',
            'is_active', 'is_service', 'min_stock',
            'created_at', 'updated_at',
        ]
        read_only_fields = fields


class WarehouseSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source='branch.name', read_only=True, default=None)

    class Meta:
        model = Warehouse
        fields = ['id', 'code', 'name', 'is_active', 'branch', 'branch_name']


class InventoryBatchSerializer(serializers.ModelSerializer):
    item_code = serializers.CharField(source='item.code', read_only=True)
    item_name = serializers.CharField(source='item.name', read_only=True)
    item_unit_small = serializers.CharField(source='item.unit_small', read_only=True)
    warehouse_name = serializers.CharField(source='warehouse.name', read_only=True)
    created_by_name = serializers.SerializerMethodField()
    # A batch's value comes from exactly one place: the purchase invoice that
    # received it. Batches created by the inventory stock-in form carry no
    # invoice and no value, and the movement page says so rather than showing a
    # bare "Rp 0" the operator would read as a bug.
    purchase_invoice_no = serializers.CharField(
        source='purchase_invoice.internal_id', read_only=True, default=None)

    class Meta:
        model = InventoryBatch
        fields = [
            'id', 'item_id', 'item_code', 'item_name', 'item_unit_small',
            'warehouse_id', 'warehouse_name',
            'input_date', 'quantity_initial', 'quantity_remaining', 'value',
            'purchase_invoice', 'purchase_invoice_no',
            'created_by_name', 'created_at',
        ]
        read_only_fields = [
            'id', 'created_at',
            'item_code', 'item_name', 'item_unit_small',
            'warehouse_name', 'created_by_name',
        ]

    def get_created_by_name(self, obj):
        return obj.created_by.display_name if obj.created_by_id else None


# ── Item Production ──────────────────────────────────────────────────────────

class ProductionRecipeIngredientSerializer(serializers.ModelSerializer):
    item_code = serializers.CharField(source='item.code', read_only=True)
    item_name = serializers.CharField(source='item.name', read_only=True)
    item_unit_small = serializers.CharField(source='item.unit_small', read_only=True)

    class Meta:
        model = ProductionRecipeIngredient
        fields = ['id', 'item', 'item_code', 'item_name', 'item_unit_small',
                  'quantity', 'unit', 'ordering']


class ProductionRecipeSerializer(serializers.ModelSerializer):
    ingredients = ProductionRecipeIngredientSerializer(many=True)
    output_item_name = serializers.CharField(source='output_item.name', read_only=True)
    output_item_code = serializers.CharField(source='output_item.code', read_only=True)
    output_unit_small = serializers.CharField(source='output_item.unit_small',
                                              read_only=True)

    class Meta:
        model = ProductionRecipe
        fields = ['id', 'name', 'output_item', 'output_item_code', 'output_item_name',
                  'output_unit_small', 'output_quantity', 'notes', 'is_active',
                  'ingredients', 'created_at', 'updated_at']

    def create(self, validated_data):
        ingredients = validated_data.pop('ingredients', [])
        recipe = ProductionRecipe.objects.create(**validated_data)
        for idx, ing in enumerate(ingredients):
            ing.setdefault('ordering', idx)
            ProductionRecipeIngredient.objects.create(recipe=recipe, **ing)
        return recipe

    def update(self, instance, validated_data):
        ingredients = validated_data.pop('ingredients', None)
        for k, v in validated_data.items():
            setattr(instance, k, v)
        instance.save()
        if ingredients is not None:
            instance.ingredients.all().delete()      # replace-all strategy
            for idx, ing in enumerate(ingredients):
                ing.setdefault('ordering', idx)
                ProductionRecipeIngredient.objects.create(recipe=instance, **ing)
        return instance


class ProductionRunIngredientSerializer(serializers.ModelSerializer):
    item_code = serializers.CharField(source='item.code', read_only=True)
    item_name = serializers.CharField(source='item.name', read_only=True)
    source_warehouse_name = serializers.CharField(source='source_warehouse.name',
                                                  read_only=True)

    class Meta:
        model = ProductionRunIngredient
        fields = ['id', 'item', 'item_code', 'item_name', 'quantity_small', 'cost',
                  'source_warehouse', 'source_warehouse_name']


class ProductionRunSerializer(serializers.ModelSerializer):
    ingredients = ProductionRunIngredientSerializer(many=True, read_only=True)
    output_item_name = serializers.CharField(source='output_item.name', read_only=True)
    output_warehouse_name = serializers.CharField(source='output_warehouse.name',
                                                  read_only=True)

    class Meta:
        model = ProductionRun
        fields = ['id', 'recipe', 'output_item', 'output_item_name', 'output_warehouse',
                  'output_warehouse_name', 'output_quantity', 'total_cost',
                  'produced_batch', 'notes', 'created_at', 'ingredients']


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
            'medrec_id', 'status', 'patient_no', 'patient_name', 'doctor_id', 'doctor_name',
            'visit_date', 'clinician', 'subjective', 'objective', 'assessment', 'assessment_codes', 'plan',
            'sabun', 'toner',
            'obat1_pagi', 'obat1_pagi_detail', 'obat1_malam', 'obat1_malam_detail',
            'obat2_pagi', 'obat2_pagi_detail', 'obat2_malam', 'obat2_malam_detail', 'treatment',
        ]

    def get_doctor_name(self, obj):
        if obj.doctor_id_id:
            return obj.doctor_id.doctor_name
        return obj.clinician or '—'

    def get_patient_name(self, obj):
        return obj.patient_no.name if obj.patient_no_id else '—'

    def get_visit_date(self, obj):
        # medrec_id format: MR-{patient_no}-{YYYYMMDD}-{N}
        for part in obj.medrec_id.split('-'):
            if len(part) == 8 and part.isdigit():
                return f"{part[:4]}-{part[4:6]}-{part[6:8]}"
        return None


class MedRecPendingDraftSerializer(serializers.ModelSerializer):
    patient_name = serializers.SerializerMethodField()
    visit_date = serializers.SerializerMethodField()
    active_patient_id = serializers.SerializerMethodField()
    active_patient_status = serializers.SerializerMethodField()

    class Meta:
        model = MedRec
        fields = [
            'medrec_id', 'status', 'patient_no', 'patient_name',
            'visit_date', 'active_patient_id', 'active_patient_status',
            'subjective', 'objective', 'assessment', 'assessment_codes', 'plan',
            'sabun', 'toner',
            'obat1_pagi', 'obat1_pagi_detail', 'obat1_malam', 'obat1_malam_detail',
            'obat2_pagi', 'obat2_pagi_detail', 'obat2_malam', 'obat2_malam_detail', 'treatment',
        ]

    def get_patient_name(self, obj):
        return obj.patient_no.name if obj.patient_no_id else '—'

    def get_visit_date(self, obj):
        for part in obj.medrec_id.split('-'):
            if len(part) == 8 and part.isdigit():
                return f"{part[:4]}-{part[4:6]}-{part[6:8]}"
        return None

    def get_active_patient_id(self, obj):
        visit = getattr(obj, 'active_visit', None)
        return visit.id if visit else None

    def get_active_patient_status(self, obj):
        visit = getattr(obj, 'active_visit', None)
        return visit.status if visit else None


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


class InvoicePaymentInputSerializer(serializers.Serializer):
    """One tender of a split payment. Send at least one of the two ids.

    Amounts are what each method settles against the invoice — not what the
    patient handed over — so they must sum to ``grand_total``; change is the
    cashier's arithmetic, not the ledger's.
    """
    payment_method_id  = serializers.IntegerField(required=False, allow_null=True)
    payment_account_id = serializers.IntegerField(required=False, allow_null=True)
    amount             = serializers.DecimalField(max_digits=14, decimal_places=2)

    def validate(self, attrs):
        if not attrs.get('payment_method_id') and not attrs.get('payment_account_id'):
            raise serializers.ValidationError(
                'Each payment needs payment_method_id or payment_account_id.')
        return attrs


class InvoiceCreateSerializer(serializers.Serializer):
    datetime           = serializers.DateTimeField(required=False)
    patient_no         = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    payment_method_id  = serializers.IntegerField(required=False, allow_null=True)
    # The direct cash/bank COA reference (design doc §3). payment_method_id is
    # kept accepted for back-compat — Medya-Cashier POS still sends it — and
    # the view resolves payment_account from it when this is omitted.
    payment_account_id = serializers.IntegerField(required=False, allow_null=True)
    discount           = serializers.DecimalField(max_digits=14, decimal_places=2, default=0)
    cashier_id         = serializers.IntegerField(required=False, allow_null=True)
    warehouse_id       = serializers.IntegerField(required=False, allow_null=True)
    tax                = serializers.DecimalField(max_digits=14, decimal_places=2, default=0)
    additional_charges = serializers.DecimalField(max_digits=14, decimal_places=2, default=0)
    grand_total        = serializers.DecimalField(max_digits=14, decimal_places=2)
    notes              = serializers.CharField(required=False, allow_blank=True, default='')
    promotion_code     = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    items              = InvoiceItemInputSerializer(many=True)
    # Split payment. Omit for the ordinary one-method invoice — payment_method_id
    # / payment_account_id above still describe it on their own.
    payments           = InvoicePaymentInputSerializer(many=True, required=False)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("At least one item is required.")
        return value


class InvoiceUpdateSerializer(serializers.Serializer):
    datetime           = serializers.DateTimeField(required=False)
    patient_no         = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    payment_method_id  = serializers.IntegerField(required=False, allow_null=True)
    payment_account_id = serializers.IntegerField(required=False, allow_null=True)
    discount           = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    cashier_id         = serializers.IntegerField(required=False, allow_null=True)
    warehouse_id       = serializers.IntegerField(required=False, allow_null=True)
    tax                = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    additional_charges = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    grand_total        = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    items              = InvoiceItemInputSerializer(many=True, required=False)
    # Present = replace the split rows wholesale (an empty list clears them and
    # returns the invoice to a single-method payment). Absent = leave as they are.
    payments           = InvoicePaymentInputSerializer(many=True, required=False)

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


class InvoicePaymentReadSerializer(serializers.ModelSerializer):
    payment_method_name  = serializers.CharField(source='payment_method.name', read_only=True, allow_null=True)
    payment_account_name = serializers.CharField(source='payment_account.name', read_only=True, allow_null=True)
    payment_account_no   = serializers.IntegerField(source='payment_account.account_number', read_only=True, allow_null=True)

    class Meta:
        model = InvoicePayment
        fields = [
            'id', 'payment_method_id', 'payment_method_name',
            'payment_account_id', 'payment_account_name', 'payment_account_no',
            'amount', 'sort_order',
        ]


class InvoiceReadSerializer(serializers.ModelSerializer):
    items                 = InvoiceItemReadSerializer(many=True, read_only=True)
    # Empty for a single-method invoice — payment_method_* / payment_account_*
    # below describe those in full.
    payments              = InvoicePaymentReadSerializer(many=True, read_only=True)
    patient_name          = serializers.SerializerMethodField()
    cashier_name          = serializers.SerializerMethodField()
    warehouse_name        = serializers.SerializerMethodField()
    payment_method_name   = serializers.SerializerMethodField()
    # The direct cash/bank COA (design doc §3). payment_method_id/_name stay in
    # the payload unchanged — the Vercel push and the WinUI POS still read them.
    payment_account_name  = serializers.SerializerMethodField()
    payment_account_no    = serializers.SerializerMethodField()
    voided_by_name        = serializers.CharField(source='voided_by.display_name', read_only=True, allow_null=True)

    class Meta:
        model = Invoice
        fields = [
            'id', 'invoice_number', 'datetime',
            'patient_no_id', 'patient_name',
            'payment_method_id', 'payment_method_name',
            'payment_account_id', 'payment_account_name', 'payment_account_no',
            'discount', 'tax', 'additional_charges', 'grand_total',
            'cashier_id', 'cashier_name',
            'warehouse_id', 'warehouse_name',
            'is_voided', 'voided_at', 'voided_by_name',
            'items', 'payments',
        ]

    def get_patient_name(self, obj):
        return obj.patient_no.name if obj.patient_no_id else None

    def get_cashier_name(self, obj):
        return obj.cashier.display_name if obj.cashier_id else None

    def get_warehouse_name(self, obj):
        return obj.warehouse.name if obj.warehouse_id else None

    def get_payment_method_name(self, obj):
        return obj.payment_method.name if obj.payment_method_id else None

    def get_payment_account_name(self, obj):
        return obj.payment_account.name if obj.payment_account_id else None

    def get_payment_account_no(self, obj):
        return obj.payment_account.account_number if obj.payment_account_id else None


# ── Payment Methods ────────────────────────────────────────────────────────

class PaymentMethodSerializer(serializers.ModelSerializer):
    linked_account_name   = serializers.CharField(source='linked_account.name', read_only=True)
    linked_account_number = serializers.IntegerField(source='linked_account.account_number', read_only=True)

    class Meta:
        model = PaymentMethod
        fields = [
            'id', 'name', 'code',
            'linked_account', 'linked_account_name', 'linked_account_number',
            'is_active', 'is_system', 'sort_order',
        ]
        read_only_fields = ['id', 'is_system']


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
    # What this account is wired to — a treatment/item category, or a vendor.
    linked_kind       = serializers.SerializerMethodField()
    linked_name       = serializers.SerializerMethodField()
    linked_role       = serializers.SerializerMethodField()
    linked_treatments = serializers.SerializerMethodField()
    linked_items      = serializers.SerializerMethodField()

    class Meta:
        model = ChartOfAccounts
        fields = [
            'id', 'account_number', 'name',
            'account_type', 'account_type_display',
            'balance', 'is_system', 'is_head',
            'parent', 'parent_name', 'parent_number',
            'linked_kind', 'linked_name', 'linked_role',
            'linked_treatments', 'linked_items',
        ]
        read_only_fields = ['id', 'balance', 'account_type_display', 'is_system', 'is_head']

    # ── Linkage helpers ────────────────────────────────────────────────────
    # Reverse OneToOne accessors raise DoesNotExist rather than returning None,
    # so every lookup goes through _linked_category / _linked_supplier.

    @staticmethod
    def _linked_category(obj):
        """Return (TreatmentCategory, role) this account belongs to, or (None, None)."""
        for attr, role in (
            ('treatment_category', 'revenue'),
        ):
            category = getattr(obj, attr, None)
            if category is not None:
                return category, role
        return None, None

    @staticmethod
    def _linked_supplier(obj):
        return getattr(obj, 'supplier_ap', None)

    def get_linked_kind(self, obj):
        if self._linked_category(obj)[0] is not None:
            return 'treatment_category'
        if self._linked_supplier(obj) is not None:
            return 'supplier'
        return None

    def get_linked_name(self, obj):
        category, _ = self._linked_category(obj)
        if category is not None:
            return category.name
        supplier = self._linked_supplier(obj)
        return supplier.name if supplier is not None else None

    def get_linked_role(self, obj):
        _, role = self._linked_category(obj)
        if role is not None:
            return role
        return 'payable' if self._linked_supplier(obj) is not None else None

    def _category_counts(self, obj):
        """(treatment_count, item_count) for the category this account serves."""
        category, _ = self._linked_category(obj)
        if category is None:
            return None, None
        counts = self.context.get('category_counts') or {}
        treatments, items = counts.get(category.id, (None, None))
        if treatments is None:
            # No prefetched map (detail view) — fall back to a direct count.
            treatments = category.inventory_items.filter(is_service=True).count()
            items      = category.inventory_items.filter(is_service=False).count()
        return treatments, items

    def get_linked_treatments(self, obj):
        return self._category_counts(obj)[0]

    def get_linked_items(self, obj):
        return self._category_counts(obj)[1]

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
    revenue_account_id     = serializers.IntegerField(source='revenue_account.id',             read_only=True, allow_null=True)
    revenue_account_number = serializers.IntegerField(source='revenue_account.account_number', read_only=True, allow_null=True)
    revenue_account_name   = serializers.CharField(   source='revenue_account.name',           read_only=True, allow_null=True)

    class Meta:
        model = TreatmentCategory
        fields = [
            'id', 'name', 'show_to_beautician', 'sort_order',
            'revenue_account_id', 'revenue_account_number', 'revenue_account_name',
        ]
        read_only_fields = [
            'id',
            'revenue_account_id', 'revenue_account_number', 'revenue_account_name',
        ]


# ── Ledger ────────────────────────────────────────────────────────────────

class LedgerEntrySerializer(serializers.ModelSerializer):
    # invoice_id comes off the FK column itself (no extra query), and is what
    # the journal/ledger tables link to the invoice detail page with.
    invoice_id            = serializers.IntegerField(read_only=True, allow_null=True)
    invoice_number        = serializers.CharField(source='invoice.invoice_number', read_only=True, allow_null=True)
    purchase_invoice_id   = serializers.IntegerField(source='purchase_invoice.id', read_only=True, allow_null=True)
    purchase_internal_id  = serializers.CharField(source='purchase_invoice.internal_id', read_only=True, allow_null=True)
    transfer_id           = serializers.IntegerField(source='transfer.id', read_only=True, allow_null=True)
    account_id            = serializers.IntegerField(read_only=True)
    account_number        = serializers.IntegerField(source='account.account_number', read_only=True)
    account_name          = serializers.CharField(source='account.name', read_only=True)
    account_type          = serializers.CharField(source='account.account_type', read_only=True)

    # Phase 4: every line belongs to a JournalEntry. Surfacing the number here is
    # what lets the flat journal history link each row to its entry detail page.
    journal_entry_id = serializers.IntegerField(read_only=True, allow_null=True)
    entry_number     = serializers.CharField(source='journal_entry.entry_number', read_only=True, allow_null=True)

    class Meta:
        model = LedgerEntry
        fields = [
            'id', 'date', 'description', 'entry_type', 'amount', 'source_type',
            'invoice_id', 'invoice_number', 'purchase_invoice_id', 'purchase_internal_id',
            'transfer_id', 'account_id', 'account_number', 'account_name', 'account_type', 'created_at',
            'journal_entry_id', 'entry_number',
        ]


# ── Journal entries (Phase 4) ─────────────────────────────────────────────────

class JournalEntryLineSerializer(serializers.ModelSerializer):
    account_id     = serializers.IntegerField(read_only=True)
    account_number = serializers.IntegerField(source='account.account_number', read_only=True)
    account_name   = serializers.CharField(source='account.name', read_only=True)
    account_type   = serializers.CharField(source='account.account_type', read_only=True)

    class Meta:
        model = LedgerEntry
        fields = ['id', 'account_id', 'account_number', 'account_name', 'account_type',
                  'description', 'entry_type', 'amount']


class JournalEntryListSerializer(serializers.ModelSerializer):
    source_label = serializers.CharField(read_only=True)
    line_count   = serializers.IntegerField(read_only=True)
    is_reversed  = serializers.SerializerMethodField()

    class Meta:
        model = JournalEntry
        fields = ['id', 'entry_number', 'date', 'memo', 'source_type', 'source_label',
                  'total_debit', 'total_credit', 'is_balanced', 'line_count',
                  'is_reversed', 'created_at']

    def get_is_reversed(self, obj):
        # Annotated by the view to avoid a query per row; falls back for
        # single-object use where the annotation is absent.
        cached = getattr(obj, 'reversal_count', None)
        return bool(cached) if cached is not None else obj.reversed_by.exists()


class JournalEntryRefSerializer(serializers.ModelSerializer):
    """Just enough of an entry to render a link in a correction chain.

    Also the response shape for the two entries a correction creates.
    """

    class Meta:
        model = JournalEntry
        fields = ['id', 'entry_number', 'date', 'source_type', 'total_debit', 'total_credit']


class JournalEntryDetailSerializer(serializers.ModelSerializer):
    lines        = JournalEntryLineSerializer(many=True, read_only=True)
    source_label = serializers.CharField(read_only=True)
    created_by_name = serializers.CharField(source='created_by.display_name', read_only=True, allow_null=True)

    # The correction chain, both directions.
    reverses    = JournalEntryRefSerializer(read_only=True)
    corrects    = JournalEntryRefSerializer(read_only=True)
    reversed_by = JournalEntryRefSerializer(many=True, read_only=True)
    corrections = JournalEntryRefSerializer(many=True, read_only=True)

    # Where to send the user for the underlying document.
    source_ref = serializers.SerializerMethodField()
    can_correct = serializers.SerializerMethodField()

    class Meta:
        model = JournalEntry
        fields = ['id', 'entry_number', 'date', 'memo', 'source_type', 'source_label',
                  'total_debit', 'total_credit', 'is_balanced', 'created_at',
                  'created_by_name', 'batch_id', 'lines',
                  'reverses', 'corrects', 'reversed_by', 'corrections',
                  'source_ref', 'can_correct']

    def get_source_ref(self, obj):
        if obj.invoice_id:
            return {'kind': 'invoice', 'id': obj.invoice_id, 'label': obj.source_label}
        if obj.purchase_invoice_id:
            return {'kind': 'purchase', 'id': obj.purchase_invoice_id, 'label': obj.source_label}
        if obj.expense_id:
            return {'kind': 'expense', 'id': obj.expense_id, 'label': obj.source_label}
        if obj.transfer_id:
            return {'kind': 'transfer', 'id': obj.transfer_id, 'label': obj.source_label}
        return None

    def get_can_correct(self, obj):
        # A reversal is a correction artefact, not something to correct; and an
        # entry that is already reversed must not be reversed twice.
        return obj.source_type != 'reversal' and not obj.reversed_by.exists()


# ── Journal staging / preview (Phase 4) ───────────────────────────────────────

class StagedJournalLineSerializer(serializers.ModelSerializer):
    account_id     = serializers.IntegerField(read_only=True, allow_null=True)
    account_number = serializers.IntegerField(source='account.account_number', read_only=True, allow_null=True)
    account_name   = serializers.CharField(source='account.name', read_only=True, allow_null=True)
    account_type   = serializers.CharField(source='account.account_type', read_only=True, allow_null=True)

    class Meta:
        model = StagedJournalLine
        fields = ['id', 'account_id', 'account_number', 'account_name', 'account_type',
                  'pending_account_label', 'description', 'entry_type', 'amount',
                  'is_estimated']


class StagedJournalEntryListSerializer(serializers.ModelSerializer):
    class Meta:
        model = StagedJournalEntry
        fields = ['id', 'date', 'source_type', 'source_model', 'source_id', 'source_label',
                  'memo', 'total_debit', 'total_credit', 'is_balanced', 'has_estimate',
                  'warnings']


class StagedJournalEntryDetailSerializer(StagedJournalEntryListSerializer):
    lines = StagedJournalLineSerializer(many=True, read_only=True)

    class Meta(StagedJournalEntryListSerializer.Meta):
        fields = StagedJournalEntryListSerializer.Meta.fields + ['lines']


class JournalStagingBatchSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source='created_by.display_name', read_only=True, allow_null=True)

    class Meta:
        model = JournalStagingBatch
        fields = ['id', 'date_to', 'status', 'created_at', 'created_by_name', 'expires_at',
                  'entry_count', 'document_count', 'total_debit', 'total_credit',
                  'days_committed', 'error_message', 'variance_notes', 'committed_batch_id']


# ── AppUser Admin ──────────────────────────────────────────────────────────

class AppUserAdminSerializer(serializers.ModelSerializer):
    pin = serializers.CharField(write_only=True, required=False, allow_blank=True)
    profile_picture_url = serializers.SerializerMethodField()
    home_branch_name = serializers.CharField(source='home_branch.name', read_only=True, default=None)

    class Meta:
        model = AppUser
        fields = ['id', 'display_name', 'role', 'avatar_color', 'is_active', 'pin',
                  'profile_picture_url', 'home_branch', 'home_branch_name']

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


# ── Bank Reconciliation ────────────────────────────────────────────────────

class BankStatementLineSerializer(serializers.ModelSerializer):
    """One statement row plus a flattened view of whatever it is matched to.

    The matched entry is inlined rather than nested: the matching screen renders
    a table, and a nested object per row means the template reaches through a
    possibly-null relation on every cell.
    """
    is_matched = serializers.BooleanField(read_only=True)
    matched_date = serializers.DateField(source='ledger_entry.date', read_only=True, default=None)
    matched_description = serializers.CharField(source='ledger_entry.description',
                                                read_only=True, default=None)
    matched_amount = serializers.SerializerMethodField()

    class Meta:
        model = BankStatementLine
        fields = ['id', 'date', 'description', 'reference', 'amount',
                  'ledger_entry', 'match_type', 'is_ignored', 'is_matched',
                  'matched_date', 'matched_description', 'matched_amount']

    def get_matched_amount(self, obj):
        """The matched row's effect on the account, signed like the statement.

        Signed here rather than left as a debit/credit pair so the screen can
        put the two numbers side by side and a mismatch is visible without the
        reader having to remember which side raises an asset.
        """
        if not obj.ledger_entry_id:
            return None
        entry = obj.ledger_entry
        return str(entry.amount if entry.entry_type == 'debit' else -entry.amount)


class BankReconciliationListSerializer(serializers.ModelSerializer):
    account_name = serializers.CharField(source='account.name', read_only=True)
    account_number = serializers.IntegerField(source='account.account_number', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True, default=None)
    created_by_name = serializers.CharField(source='created_by.display_name',
                                            read_only=True, default=None)
    completed_by_name = serializers.CharField(source='completed_by.display_name',
                                              read_only=True, default=None)
    line_count = serializers.SerializerMethodField()
    matched_count = serializers.SerializerMethodField()

    class Meta:
        model = BankReconciliation
        fields = ['id', 'account', 'account_name', 'account_number',
                  'branch', 'branch_name', 'statement_start', 'statement_end',
                  'opening_balance', 'closing_balance', 'status', 'notes',
                  'created_by_name', 'completed_by_name', 'created_at',
                  'completed_at', 'line_count', 'matched_count']

    def get_line_count(self, obj):
        return obj.lines.count()

    def get_matched_count(self, obj):
        return obj.lines.filter(ledger_entry__isnull=False).count()


class BankReconciliationDetailSerializer(BankReconciliationListSerializer):
    """Adds the live figures.

    ``summary`` is computed on read rather than stored, because every number in
    it is derived from rows that change under the operator's hands. A stored
    copy would be stale the moment a match is made, and a stale difference on a
    reconciliation screen is worse than no difference at all.
    """
    summary = serializers.SerializerMethodField()
    is_locked = serializers.BooleanField(read_only=True)

    class Meta(BankReconciliationListSerializer.Meta):
        fields = BankReconciliationListSerializer.Meta.fields + ['summary', 'is_locked']

    def get_summary(self, obj):
        from ..services.bank_reconciliation import summary as compute_summary
        return {
            k: (str(v) if isinstance(v, Decimal) else v)
            for k, v in compute_summary(obj).items()
        }


# ── Sales Returns ──────────────────────────────────────────────────────────

class SalesReturnItemSerializer(serializers.ModelSerializer):
    item_code = serializers.CharField(source='item.code', read_only=True, default=None)
    line_refund = serializers.SerializerMethodField()

    class Meta:
        model = SalesReturnItem
        fields = ['id', 'invoice_item', 'item', 'item_code', 'item_name',
                  'quantity', 'price', 'discount_pct', 'restock',
                  'cogs_reversed', 'line_refund']

    def get_line_refund(self, obj):
        """The line's own net value — before the invoice-level apportionment.

        Deliberately *not* the line's share of ``total_refund``: the invoice
        discount, tax and charges are apportioned once against the return as a
        whole (see services/sales_returns.compute_refund), so there is no
        per-line share to report without inventing one. The header carries the
        number that was actually paid out.
        """
        return str(obj.net.quantize(Decimal('0.01')))


class SalesReturnListSerializer(serializers.ModelSerializer):
    invoice_number = serializers.CharField(source='invoice.invoice_number', read_only=True)
    patient_name = serializers.CharField(source='invoice.patient_no.name',
                                         read_only=True, default=None)
    refund_method_name = serializers.CharField(source='refund_method.name',
                                               read_only=True, default=None)
    refund_account_name = serializers.CharField(source='refund_account.name',
                                                read_only=True, default=None)
    processed_by_name = serializers.CharField(source='processed_by.display_name',
                                              read_only=True, default=None)
    branch_name = serializers.CharField(source='branch.name', read_only=True, default=None)
    reason_label = serializers.CharField(source='get_reason_display', read_only=True)
    item_count = serializers.SerializerMethodField()

    class Meta:
        model = SalesReturn
        fields = ['id', 'return_number', 'invoice', 'invoice_number', 'patient_name',
                  'datetime', 'reason', 'reason_label', 'notes', 'total_refund',
                  'refund_method_name', 'refund_account_name', 'processed_by_name',
                  'branch', 'branch_name', 'posting_status', 'is_voided', 'voided_at',
                  'item_count']

    def get_item_count(self, obj):
        return obj.items.count()


class SalesReturnDetailSerializer(SalesReturnListSerializer):
    items = SalesReturnItemSerializer(many=True, read_only=True)
    warehouse_name = serializers.CharField(source='warehouse.name', read_only=True, default=None)
    voided_by_name = serializers.CharField(source='voided_by.display_name',
                                           read_only=True, default=None)

    class Meta(SalesReturnListSerializer.Meta):
        fields = SalesReturnListSerializer.Meta.fields + [
            'items', 'warehouse', 'warehouse_name', 'refund_method',
            'refund_account', 'voided_by_name', 'created_at',
        ]


# ── Issue Tickets ──────────────────────────────────────────────────────────

class IssueTicketImageSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = IssueTicketImage
        fields = ['id', 'image_url', 'uploaded_at']
        read_only_fields = ['id', 'image_url', 'uploaded_at']

    def get_image_url(self, obj):
        request = self.context.get('request')
        url = obj.image.url
        return request.build_absolute_uri(url) if request else url


class IssueTicketSerializer(serializers.ModelSerializer):
    submitted_by_username = serializers.CharField(source='submitted_by.display_name', read_only=True)
    images = IssueTicketImageSerializer(many=True, read_only=True)
    status_display   = serializers.CharField(source='get_status_display', read_only=True)
    category_display = serializers.CharField(source='get_category_display', read_only=True)

    class Meta:
        model = IssueTicket
        fields = [
            'id', 'ticket_no', 'submitted_by', 'submitted_by_username',
            'category', 'category_display',
            'title', 'description', 'status', 'status_display',
            'images', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'ticket_no', 'submitted_by_username', 'category_display', 'images', 'status_display', 'created_at', 'updated_at']


# ── Patient Notes ──────────────────────────────────────────────────────────

MANAGING_ROLES = ('manager', 'superuser')


class PatientNoteSerializer(serializers.ModelSerializer):
    patient_no = serializers.CharField(source='patient_no_id', read_only=True)
    active_patient_id = serializers.IntegerField(read_only=True)
    author_display = serializers.SerializerMethodField()
    can_edit = serializers.SerializerMethodField()
    # Defaulted in PatientNoteListCreateView.create() when the client omits it,
    # so the POS/webapp can just post content.
    date = serializers.DateField(required=False)

    class Meta:
        model = PatientNote
        fields = [
            'id', 'patient_no', 'active_patient_id', 'date', 'content',
            'author', 'author_display', 'author_role',
            'created_at', 'updated_at', 'can_edit',
        ]
        # author_role is a server-owned snapshot — a client must not be able to
        # claim a role it does not have.
        read_only_fields = [
            'id', 'patient_no', 'active_patient_id', 'author_display',
            'author_role', 'can_edit', 'created_at', 'updated_at',
        ]

    def get_author_display(self, obj):
        if obj.author_user_id and obj.author_user:
            name = (obj.author_user.display_name or '').strip()
            if name:
                return name
        return (obj.author or '').strip() or '—'

    def get_can_edit(self, obj):
        """True when the requesting user may PATCH/DELETE this note.

        Mirrors the check enforced in PatientNoteDetailView — keep the two in
        step or the webapp will offer buttons the server rejects. Anonymous
        callers (the POS billing queue is AllowAny) always get False, which is
        what makes the POS panel read-only.
        """
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if not isinstance(user, AppUser):
            return False
        if obj.author_user_id and obj.author_user_id == user.id:
            return True
        return user.role in MANAGING_ROLES


def todays_notes_by_visit(active_patients):
    """Today's notes for a whole billing queue, in one query.

    Returns ``{active_patient_id: [PatientNote, ...]}`` ordered by created_at.
    A note counts for a visit when it points at the visit itself OR at the
    Patient behind it (beauticians writing from the webapp attach to the
    patient; guests can only attach to the visit).

    Without this, BillingPatientSerializer.get_notes runs one query per queue
    entry — an N+1 across the whole queue on an endpoint the POS polls.
    """
    active_patients = list(active_patients)
    if not active_patients:
        return {}

    visit_ids = [ap.id for ap in active_patients]
    visits_by_patient = {}
    for ap in active_patients:
        if ap.patient_no_id:
            visits_by_patient.setdefault(ap.patient_no_id, []).append(ap.id)

    subject = Q(active_patient_id__in=visit_ids)
    if visits_by_patient:
        subject |= Q(patient_no_id__in=list(visits_by_patient))

    notes = (
        PatientNote.objects
        .filter(date=timezone.localdate())
        .filter(subject)
        .select_related('author_user')
        .order_by('created_at')
    )

    by_visit = {vid: [] for vid in visit_ids}
    for note in notes:
        targets = set(visits_by_patient.get(note.patient_no_id, ()))
        if note.active_patient_id in by_visit:
            targets.add(note.active_patient_id)
        for vid in targets:
            by_visit[vid].append(note)
    return by_visit


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
                  "receipt_header_extra", "receipt_footer", "logo"]


class ReportSettingsSerializer(serializers.ModelSerializer):
    """Validates the tunable classification windows as a set, not field-by-field.

    Each threshold is meaningless alone — a dead-months <= slow-months window
    silently zeroes out the slow bucket, and a fast window as long as (or
    longer than) the slow window lets one item land in both buckets at once.
    Rejecting the whole payload with a field-keyed error, rather than
    clamping, is deliberate: a manager who fat-fingers one of these numbers
    should see exactly why, not have the report quietly reinterpret what they
    typed. See docs/stock-movement-patient-activity-design.md §1.
    """
    class Meta:
        model = ReportSettings
        fields = [
            'stock_fast_window_days', 'stock_fast_top_percent',
            'stock_slow_months', 'stock_dead_months',
            'patient_active_months', 'patient_inactive_months',
            'updated_at',
        ]
        read_only_fields = ['updated_at']

    _INT_FIELDS = (
        'stock_fast_window_days', 'stock_fast_top_percent',
        'stock_slow_months', 'stock_dead_months',
        'patient_active_months', 'patient_inactive_months',
    )

    def validate(self, attrs):
        # PATCH only carries the changed fields — merge onto the existing
        # instance so the cross-field checks below see the values the row
        # would actually have *after* saving, not just what this request sent.
        merged = {
            f: attrs.get(f, getattr(self.instance, f, None))
            for f in self._INT_FIELDS
        }

        errors = {}
        for f in self._INT_FIELDS:
            if merged[f] is None:
                errors[f] = 'Wajib diisi.'
            elif merged[f] < 1:
                errors[f] = 'Harus bernilai minimal 1.'
        if 'stock_fast_top_percent' not in errors and merged['stock_fast_top_percent'] > 100:
            errors['stock_fast_top_percent'] = 'Tidak boleh lebih dari 100.'

        if not errors:
            if merged['stock_dead_months'] <= merged['stock_slow_months']:
                errors['stock_dead_months'] = (
                    'Harus lebih besar dari stock_slow_months — jika tidak, kategori '
                    'lambat (slow) tidak akan pernah terisi.'
                )
            if merged['patient_inactive_months'] <= merged['patient_active_months']:
                errors['patient_inactive_months'] = (
                    'Harus lebih besar dari patient_active_months — jika tidak, kategori '
                    'kurang aktif (lapsing) tidak akan pernah terisi.'
                )
            if merged['stock_fast_window_days'] >= merged['stock_slow_months'] * 30:
                errors['stock_fast_window_days'] = (
                    'Harus lebih pendek dari stock_slow_months * 30 hari — jika tidak, '
                    'sebuah item bisa masuk kategori cepat (fast) dan lambat (slow) sekaligus.'
                )

        if errors:
            raise serializers.ValidationError(errors)
        return attrs


# ── Accounting serializers ─────────────────────────────────────────────────────

class SupplierSerializer(serializers.ModelSerializer):
    ap_account_id     = serializers.IntegerField(source='ap_account.id',             read_only=True, allow_null=True)
    ap_account_number = serializers.IntegerField(source='ap_account.account_number', read_only=True, allow_null=True)
    ap_account_name   = serializers.CharField(   source='ap_account.name',           read_only=True, allow_null=True)
    ap_balance        = serializers.DecimalField(source='ap_account.balance', max_digits=18, decimal_places=2,
                                                 read_only=True, allow_null=True)

    class Meta:
        model = Supplier
        fields = [
            'id', 'name', 'contact_name', 'phone', 'email', 'address', 'is_active',
            'ap_account_id', 'ap_account_number', 'ap_account_name', 'ap_balance',
        ]
        read_only_fields = ['ap_account_id', 'ap_account_number', 'ap_account_name', 'ap_balance']


class PurchaseAdditionalCostSerializer(serializers.ModelSerializer):
    class Meta:
        model = PurchaseAdditionalCost
        fields = ['id', 'name', 'modifier', 'amount_type', 'amount', 'sort_order']


class PurchaseInvoiceItemSerializer(serializers.ModelSerializer):
    item_code            = serializers.CharField(source='item.code', read_only=True, allow_null=True)
    expense_account_name = serializers.CharField(source='expense_account.name', read_only=True, allow_null=True)
    warehouse_name       = serializers.CharField(source='warehouse.name', read_only=True, allow_null=True)
    subtotal             = serializers.SerializerMethodField()
    adjusted_subtotal    = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseInvoiceItem
        fields = [
            'id', 'line_type', 'item', 'item_code', 'item_name',
            'quantity', 'unit', 'unit_cost', 'total_discount',
            'actual_unit_cost', 'subtotal', 'adjusted_subtotal',
            'expense_account', 'expense_account_name',
            'warehouse', 'warehouse_name',
        ]

    def get_subtotal(self, obj):
        return str(obj.quantity * obj.unit_cost)

    def get_adjusted_subtotal(self, obj):
        return str(obj.quantity * obj.unit_cost - obj.total_discount)


def _purchase_cash_account(obj):
    """The cash/bank COA a purchase invoice or payment settled from.

    ``payment_account`` for anything written since the cash-account picker
    landed; the legacy method's ``linked_account`` for older rows the backfill
    could not resolve.
    """
    if obj.payment_account_id:
        return obj.payment_account
    if obj.payment_method_id:
        return obj.payment_method.linked_account
    return None


class PurchaseCashAccountMixin:
    """Getters for the three cash-account fields shared by the purchase invoice
    and purchase payment serializers.

    ``payment_account_name``/``payment_account_no`` name the *account* the money
    moved from — they used to name the PaymentMethod, which stopped being the
    source of truth once purchases started paying from a cash account directly.
    ``cash_account`` is that account's pk, for the picker.

    Only the methods live here: DRF's metaclass collects declared fields from a
    class's own attrs and from serializer bases, so a plain mixin's field
    declarations would be silently dropped. Each serializer declares its own.
    """

    def get_cash_account(self, obj):
        acct = _purchase_cash_account(obj)
        return acct.id if acct else None

    def get_payment_account_name(self, obj):
        acct = _purchase_cash_account(obj)
        return acct.name if acct else None

    def get_payment_account_no(self, obj):
        acct = _purchase_cash_account(obj)
        return acct.account_number if acct else None


class PurchasePaymentSerializer(PurchaseCashAccountMixin, serializers.ModelSerializer):
    cash_account         = serializers.SerializerMethodField()
    payment_account_name = serializers.SerializerMethodField()
    payment_account_no   = serializers.SerializerMethodField()
    payment_method_name  = serializers.CharField(source='payment_method.name', read_only=True, allow_null=True)
    created_by_name      = serializers.CharField(source='created_by.display_name', read_only=True, allow_null=True)

    class Meta:
        model = PurchasePayment
        fields = [
            'id', 'payment_date', 'payment_method', 'payment_method_name',
            'cash_account', 'payment_account_name', 'payment_account_no',
            'amount', 'notes', 'created_at', 'created_by_name',
        ]


class PurchaseInvoiceListSerializer(PurchaseCashAccountMixin, serializers.ModelSerializer):
    supplier_name        = serializers.CharField(source='supplier.name', read_only=True)
    cash_account         = serializers.SerializerMethodField()
    payment_account_name = serializers.SerializerMethodField()
    payment_account_no   = serializers.SerializerMethodField()
    warehouse_name       = serializers.CharField(source='warehouse.name', read_only=True, allow_null=True)
    balance_due          = serializers.SerializerMethodField()
    last_payment_date    = serializers.SerializerMethodField()
    invoice_image_url    = serializers.SerializerMethodField()
    voided_by_name       = serializers.CharField(source='voided_by.display_name', read_only=True, allow_null=True)

    class Meta:
        model = PurchaseInvoice
        fields = [
            'id', 'internal_id', 'external_invoice_no', 'supplier', 'supplier_name',
            'payment_method', 'cash_account', 'payment_account_name', 'payment_account_no',
            'warehouse', 'warehouse_name',
            'purchase_date', 'due_date', 'status', 'total_amount', 'amount_paid',
            'balance_due', 'last_payment_date', 'notes', 'invoice_image_url', 'created_at',
            'is_voided', 'voided_at', 'voided_by_name',
        ]

    def get_last_payment_date(self, obj):
        # Read through .all() so a prefetched list is reused instead of firing
        # one query per invoice in the list view.
        dates = [p.payment_date for p in obj.payments.all()]
        return max(dates) if dates else None

    def get_balance_due(self, obj):
        return str(obj.total_amount - obj.amount_paid)

    def get_invoice_image_url(self, obj):
        if not obj.invoice_image:
            return None
        request = self.context.get('request')
        if request:
            return request.build_absolute_uri(obj.invoice_image.url)
        return obj.invoice_image.url


class PurchaseInvoiceDetailSerializer(PurchaseInvoiceListSerializer):
    items            = PurchaseInvoiceItemSerializer(many=True, read_only=True)
    additional_costs = PurchaseAdditionalCostSerializer(many=True, read_only=True)
    payments         = PurchasePaymentSerializer(many=True, read_only=True)

    class Meta(PurchaseInvoiceListSerializer.Meta):
        fields = PurchaseInvoiceListSerializer.Meta.fields + ['items', 'additional_costs', 'payments']


class ExpenseAliasSerializer(serializers.ModelSerializer):
    """A friendly, staff-facing name for an expense GL account (design §4).

    ``account_name``/``account_number`` are read-only lookups for display; the
    manager admin picks ``account`` itself from the expense/cogs COA range
    (enforced by the model's ``limit_choices_to``, not repeated here).
    """
    account_name   = serializers.CharField(source='account.name', read_only=True)
    account_number = serializers.IntegerField(source='account.account_number', read_only=True)

    class Meta:
        model = ExpenseAlias
        fields = [
            'id', 'name', 'account', 'account_name', 'account_number',
            'scope', 'is_active', 'sort_order', 'notes',
        ]


class ExpenseItemSerializer(serializers.ModelSerializer):
    account_name   = serializers.CharField(source='account.name', read_only=True, allow_null=True)
    account_number = serializers.IntegerField(source='account.account_number', read_only=True, allow_null=True)
    # Provenance only (Expense.alias is SET_NULL) — account/description above
    # remain the record of truth even after an alias is retired.
    alias_id       = serializers.SerializerMethodField()
    alias_name     = serializers.SerializerMethodField()

    class Meta:
        model = ExpenseItem
        fields = ['id', 'account', 'account_name', 'account_number', 'description', 'amount',
                  'alias_id', 'alias_name']

    def get_alias_id(self, obj):
        return obj.alias_id

    def get_alias_name(self, obj):
        return obj.alias.name if obj.alias_id else None


class ExpenseSerializer(serializers.ModelSerializer):
    """An expense serialized as a journal document.

    ``payment_method_name`` is the *payment method*'s name (it was called
    ``payment_account_name`` until the cash-account picker landed, which made
    the name actively misleading). The ``cash_account_*`` fields are the real
    credit-side COA. ``resolved_legs`` is the journal preview — the same memo
    strings the posting path will write, built with ``expense_leg_memo`` so the
    fallback chain lives in exactly one place.
    """
    items                 = ExpenseItemSerializer(many=True, read_only=True)
    payment_method_name   = serializers.CharField(source='payment_method.name', read_only=True, allow_null=True)
    payment_account_no    = serializers.IntegerField(source='payment_method.linked_account.account_number', read_only=True, allow_null=True)
    cash_account          = serializers.PrimaryKeyRelatedField(
        source='payment_account',
        queryset=ChartOfAccounts.objects.all(),
        required=False, allow_null=True,
    )
    cash_account_name     = serializers.CharField(source='payment_account.name', read_only=True, allow_null=True)
    cash_account_number   = serializers.IntegerField(source='payment_account.account_number', read_only=True, allow_null=True)
    payment_memo          = serializers.CharField(required=False, allow_blank=True, max_length=255)
    resolved_legs         = serializers.SerializerMethodField()
    balance_due           = serializers.SerializerMethodField()
    created_by_name       = serializers.CharField(source='created_by.display_name', read_only=True, allow_null=True)

    class Meta:
        model = Expense
        fields = [
            'id', 'expense_date', 'payment_method',
            'payment_method_name', 'payment_account_no',
            'cash_account', 'cash_account_name', 'cash_account_number', 'payment_memo',
            'status', 'source', 'total_amount', 'amount_paid', 'balance_due',
            'notes', 'posting_status', 'created_at', 'created_by_name', 'items',
            'resolved_legs',
        ]

    def get_balance_due(self, obj):
        return str(obj.total_amount - obj.amount_paid)

    def get_resolved_legs(self, obj):
        """Journal preview: one credit leg + one debit leg per expense item.

        ``inherited`` marks a debit memo that was borrowed from the cash side
        (the line's own memo was blank) so the UI can render it muted.
        """
        from ..models import AP_CONTROL_NUMBER
        from ..services.journal_engine import expense_credit_account, expense_leg_memo

        legs = []

        total = obj.total_amount or 0
        if total:
            fully_paid = (obj.amount_paid or 0) >= total
            credit_account = expense_credit_account(obj) if fully_paid else None
            if credit_account is None:
                # Mirrors _post_expense_accrual: an unpaid expense (or one with
                # no cash account named) credits the AP control account. Looked
                # up read-only here — a preview must never provision an account.
                credit_account = ChartOfAccounts.objects.filter(
                    account_number=AP_CONTROL_NUMBER,
                ).first()
            legs.append({
                'side':         'credit',
                'account_id':   credit_account.id if credit_account else None,
                'account_name': credit_account.name if credit_account else None,
                'memo':         expense_leg_memo(obj),
                'amount':       str(total),
                'inherited':    False,
            })

        payment_memo = (obj.payment_memo or '').strip()
        for it in obj.items.all():
            memo = expense_leg_memo(obj, it)
            legs.append({
                'side':         'debit',
                'account_id':   it.account_id,
                'account_name': it.account.name if it.account_id else None,
                'memo':         memo,
                'amount':       str(it.amount),
                'inherited':    not (it.description or '').strip() and bool(payment_memo),
            })

        return legs


class BeauticianExpenseItemInputSerializer(serializers.Serializer):
    """One petty-cash line: an alias pick + amount, nothing that names a GL
    account — see views.beautician_expense_page for the alias→account resolve."""
    alias_id    = serializers.IntegerField()
    amount      = serializers.DecimalField(max_digits=14, decimal_places=2)
    description = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError('Jumlah harus lebih besar dari 0.')
        return value


class BeauticianExpenseCreateSerializer(serializers.Serializer):
    expense_date       = serializers.DateField()
    payment_account_id = serializers.IntegerField()
    payment_memo       = serializers.CharField(required=False, allow_blank=True, default='')
    notes              = serializers.CharField(required=False, allow_blank=True, default='')
    items               = BeauticianExpenseItemInputSerializer(many=True)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError('Minimal satu baris pengeluaran diperlukan.')
        return value


class AccountTransferSerializer(serializers.ModelSerializer):
    from_account_name   = serializers.CharField(source='from_account.name', read_only=True)
    from_account_number = serializers.IntegerField(source='from_account.account_number', read_only=True)
    to_account_name     = serializers.CharField(source='to_account.name', read_only=True)
    to_account_number   = serializers.IntegerField(source='to_account.account_number', read_only=True)
    created_by_name     = serializers.CharField(source='created_by.display_name', read_only=True, allow_null=True)

    class Meta:
        model = AccountTransfer
        fields = [
            'id', 'transfer_date', 'from_account', 'from_account_name', 'from_account_number',
            'to_account', 'to_account_name', 'to_account_number',
            'amount', 'description', 'reference', 'created_at', 'created_by_name',
        ]


class ColorPaletteSerializer(serializers.ModelSerializer):
    class Meta:
        model = ColorPalette
        fields = ['id', 'name', 'primary_color', 'secondary_color', 'background_color', 'is_dark', 'sort_order']


class AppointmentLocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = AppointmentLocation
        fields = ['id', 'name', 'room_code', 'is_active']


class AppointmentSerializer(serializers.ModelSerializer):
    # settings.TIME_ZONE is UTC, so these would render as 'Z' by default.
    # default_timezone pins both directions to Jakarta: output carries +07:00
    # (what SatuSehat expects and what the clinic reads), and a naive input is
    # read as clinic-local rather than UTC. An explicit offset on input is
    # always honoured as-is.
    start_at = serializers.DateTimeField(default_timezone=JAKARTA_TZ)
    end_at = serializers.DateTimeField(
        default_timezone=JAKARTA_TZ, required=False, allow_null=True)
    created_at = serializers.DateTimeField(
        default_timezone=JAKARTA_TZ, read_only=True)
    updated_at = serializers.DateTimeField(
        default_timezone=JAKARTA_TZ, read_only=True)

    patient_name = serializers.CharField(
        source='patient.name', read_only=True, allow_null=True)
    patient_nik = serializers.CharField(
        source='patient.NIK', read_only=True, allow_null=True)
    practitioner_name = serializers.CharField(
        source='practitioner.doctor_name', read_only=True, allow_null=True)
    location_name = serializers.CharField(
        source='location.name', read_only=True, allow_null=True)
    display_name = serializers.CharField(read_only=True)
    satusehat_readiness = serializers.SerializerMethodField()

    class Meta:
        model = Appointment
        fields = [
            'id', 'appointment_no',
            'patient', 'patient_name', 'patient_nik', 'guest_name', 'display_name',
            'practitioner', 'practitioner_name',
            'location', 'location_name',
            'service_category', 'service_type', 'appointment_type', 'reason',
            'source', 'contact_phone',
            'start_at', 'end_at', 'status', 'note',
            'created_at', 'updated_at',
            'linked_active_patient',
            'sync_status', 'synced_at', 'ihs_appointment_id',
            'satusehat_readiness',
        ]
        read_only_fields = [
            'appointment_no', 'linked_active_patient',
            'sync_status', 'synced_at', 'ihs_appointment_id',
            # Origin is a fact about how the row was created, never a client
            # choice: a staff booking must not be able to pose as an online one.
            'source',
        ]

    def get_satusehat_readiness(self, obj):
        """
        Which references a future FHIR push would still be missing. Informational
        only — nothing here blocks a booking, and no SatuSehat call is made.
        """
        gaps = []
        if not obj.patient_id:
            gaps.append('guest booking — no patient record')
        elif not (obj.patient.NIK or '').strip():
            gaps.append('patient has no NIK')
        if not obj.practitioner_id:
            gaps.append('no practitioner')
        elif not (obj.practitioner.nik or '').strip():
            gaps.append('practitioner has no NIK')
        if not obj.location_id:
            gaps.append('no location')
        return {'ready': not gaps, 'gaps': gaps}

    def validate(self, attrs):
        def resolved(field):
            if field in attrs:
                return attrs[field]
            return getattr(self.instance, field, None)

        errors = {}
        is_create = self.instance is None

        patient = resolved('patient')
        guest_name = (resolved('guest_name') or '').strip()
        if not patient and not guest_name:
            # Mirrors GeneralAppointmentCreateView: a booking is either against a
            # patient record or a named guest.
            errors['guest_name'] = 'Provide a patient or a guest name.'

        start_at = resolved('start_at')
        end_at = resolved('end_at')
        status_value = resolved('status') or 'booked'

        if start_at and end_at and end_at <= start_at:
            errors['end_at'] = 'End time must be after the start time.'

        if status_value == 'booked' and not end_at:
            # FHIR invariant app-2/app-3: a booked slot must carry start and end.
            # Enforced here so the record is pushable the moment sync is switched on.
            errors['end_at'] = 'A booked appointment requires an end time.'

        # Only on create — edits may legitimately correct a past appointment.
        if is_create and start_at and start_at <= timezone.now():
            errors['start_at'] = 'Start time must be in the future.'

        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class ReservationRequestSerializer(serializers.ModelSerializer):
    """One online booking, as the reservations inbox reads it."""

    reserved_at = serializers.DateTimeField(
        default_timezone=JAKARTA_TZ, read_only=True)
    pulled_at = serializers.DateTimeField(
        default_timezone=JAKARTA_TZ, read_only=True)
    acknowledged_at = serializers.DateTimeField(
        default_timezone=JAKARTA_TZ, read_only=True)

    matched_patient_name = serializers.CharField(
        source='matched_patient.name', read_only=True, allow_null=True)
    acknowledged_by_name = serializers.CharField(
        source='acknowledged_by.display_name', read_only=True, allow_null=True)
    appointment_no = serializers.CharField(
        source='appointment.appointment_no', read_only=True, allow_null=True)
    appointment_status = serializers.CharField(
        source='appointment.status', read_only=True, allow_null=True)
    # Whether the booking has already been pulled into today's queue. The one
    # thing reception needs that the inbox row itself does not carry.
    checked_in = serializers.SerializerMethodField()
    needs_attention = serializers.BooleanField(read_only=True)
    candidates = serializers.SerializerMethodField()

    class Meta:
        model = ReservationRequest
        fields = [
            'id', 'external_id', 'name', 'phone', 'reserved_at',
            'service_name', 'service_id',
            'match_status', 'matched_patient', 'matched_patient_name',
            'candidate_patient_nos', 'candidates',
            'appointment', 'appointment_no', 'appointment_status', 'checked_in',
            'acknowledged_at', 'acknowledged_by', 'acknowledged_by_name',
            'pulled_at', 'needs_attention',
        ]
        read_only_fields = fields

    def get_checked_in(self, obj):
        return bool(
            obj.appointment_id and obj.appointment.linked_active_patient_id)

    def get_candidates(self, obj):
        """The ambiguous matches, named. A list of patient numbers is not
        something a receptionist can choose between."""
        numbers = obj.candidate_patient_nos or []
        if not numbers:
            return []
        rows = Patient.objects.filter(patient_no__in=numbers).values(
            'patient_no', 'name', 'phone_number')
        return [
            {
                'patient_no': r['patient_no'],
                'name': r['name'],
                'phone_number': r['phone_number'],
            }
            for r in rows
        ]


# ── Operational inputs (planning only — never posts to the GL) ───────────────


class OperationalInputTemplateSerializer(serializers.ModelSerializer):
    """One recurring cost the operator is expected to record each period."""

    account_name   = serializers.CharField(source='account.name', read_only=True, allow_null=True)
    account_number = serializers.IntegerField(source='account.account_number', read_only=True, allow_null=True)
    # A method field, not IntegerField(read_only=True): the create/update views
    # serialise the instance they just saved, which carries no `entry_count`
    # annotation, and a plain field would raise AttributeError on it.
    entry_count    = serializers.SerializerMethodField()

    class Meta:
        model = OperationalInputTemplate
        fields = [
            'id', 'name', 'category', 'account', 'account_name', 'account_number',
            'frequency', 'due_day', 'expected_amount', 'is_active', 'sort_order',
            'notes', 'entry_count',
        ]

    def get_entry_count(self, obj):
        # Prefer the list view's annotation; fall back to a count for the
        # single-instance responses.
        annotated = getattr(obj, 'entry_count', None)
        return annotated if annotated is not None else obj.entries.count()

    def validate_name(self, value):
        # The uniqueness rule is a Meta.constraints UniqueConstraint rather than
        # unique=True on the field, so it is checked here explicitly: without
        # this, a duplicate name reaches the database and surfaces as a 500
        # IntegrityError instead of a field error the form can show.
        name = (value or '').strip()
        clash = OperationalInputTemplate.objects.filter(name__iexact=name)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError('Nama template sudah dipakai.')
        return name

    def validate(self, attrs):
        # due_day means two different things depending on frequency, so the
        # range it must fall in is validated together with it rather than as a
        # field-level rule that cannot see the other value. On PATCH, fall back
        # to the instance for whichever half was not sent.
        frequency = attrs.get('frequency') or getattr(self.instance, 'frequency', None) or 'monthly'
        due_day = attrs.get('due_day', getattr(self.instance, 'due_day', 1))
        if due_day is not None:
            upper = 7 if frequency == 'weekly' else 31
            if not 1 <= int(due_day) <= upper:
                raise serializers.ValidationError({
                    'due_day': (
                        'Hari 1–7 (Senin–Minggu) untuk template mingguan.'
                        if frequency == 'weekly'
                        else 'Tanggal 1–31 untuk template bulanan.'
                    ),
                })
        return attrs

    def validate_account(self, value):
        if value is not None and value.account_type not in ('expense', 'cogs'):
            raise serializers.ValidationError('Pilih akun beban atau HPP.')
        return value


class OperationalInputEntrySerializer(serializers.ModelSerializer):
    """A recorded period figure. Read-only shape; writes go through the view.

    ``period_key``/``period_start`` are derived server-side from the template's
    frequency (see ``services.operational_inputs``), so they are read-only here
    even though a client sends a period on create — the view resolves it.
    """

    template_name = serializers.CharField(source='template.name', read_only=True)
    category      = serializers.CharField(source='template.category', read_only=True)
    frequency     = serializers.CharField(source='template.frequency', read_only=True)
    recorded_by_name = serializers.SerializerMethodField()

    class Meta:
        model = OperationalInputEntry
        fields = [
            'id', 'template', 'template_name', 'category', 'frequency',
            'period_key', 'period_start', 'amount', 'notes',
            'recorded_by_name', 'recorded_at', 'updated_at',
        ]
        read_only_fields = ['period_key', 'period_start', 'recorded_at', 'updated_at']

    def get_recorded_by_name(self, obj):
        return obj.recorded_by.display_name if obj.recorded_by_id else None


class OperationalInputEntryWriteSerializer(serializers.Serializer):
    """Create/replace one period's figure.

    ``period`` is a period *key* ('2026-08' / '2026-W34'), validated against the
    template's frequency by the view rather than here — the template has to be
    fetched to know the frequency at all, and doing it twice invites drift.
    """

    template = serializers.IntegerField()
    period   = serializers.CharField(max_length=10)
    amount   = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=Decimal('0'))
    notes    = serializers.CharField(max_length=255, required=False, allow_blank=True, default='')
