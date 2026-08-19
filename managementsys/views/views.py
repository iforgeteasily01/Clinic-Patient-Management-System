from django.shortcuts import render, HttpResponse
from rest_framework import generics, filters
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from ..models import *
from ..api.serializers import *
from .beautician_page import *
from .patient_page import PatientCreateWithActiveView, PatientSearchView, PatientSyncView, PatientCountView, PatientNextNoView, ActivePatientUpdateStatusView, ActivePatientClearView, TreatmentQueueView, TreatmentListView, TreatmentSessionCreateView, AppointmentAddView, CompleteTreatmentView, GeneralAppointmentCreateView, TreatmentRemoveView
from .billing_page import BillingQueueView, BillingCompleteView
from .soap_templates_page import SoapTemplateListCreateView, SoapTemplateDetailView, SoapTemplateImportView, SoapTemplateExportView, SoapTemplateTemplateDownloadView
from .inventory_page import (
    InventoryItemListCreateView, InventoryItemDetailView,
    WarehouseListCreateView, WarehouseDetailView,
    StockLevelView, InventoryDashboardView, InventoryBatchListView,
    StockInView, StockOutView, StockOutBatchListView, StockOutReasonsView,
    ItemSyncView,
    InventoryItemImportPreviewView,
    InventoryItemImportView,
    InventoryItemTemplateView,
    PencacahanListCreateView, PencacahanDetailView,
)
from .production_page import (
    RecipeListCreateView, RecipeDetailView, RecipeCostView,
    RecipeExportView, RecipeTemplateView, RecipeImportView,
    ProductionPreviewView, ProductionRunListCreateView, ProductionRunDetailView,
)
from .auth_views import UserListView, LoginView, LogoutView, ProfileUpdateView, ThemeUpdateView
from .admin_views import (
    DoctorListCreateAdminView, DoctorDetailAdminView,
    PatientListCreateAdminView, PatientDetailAdminView,
    TreatmentListCreateAdminView, TreatmentDetailAdminView,
    TreatmentImportView, TreatmentTemplateView,
    TreatmentPackageListCreateAdminView, TreatmentPackageDetailAdminView, TreatmentPackageSyncView,
    BeauticianListCreateAdminView, BeauticianDetailAdminView, BeauticianReleaseView,
    AppUserListCreateAdminView, AppUserDetailAdminView,
    TreatmentCategoryListCreateView, TreatmentCategoryDetailView,
    TreatmentCategoryAuditAccountsView, TreatmentCategoryProvisionAccountsView,
    ChartOfAccountsListCreateView, ChartOfAccountsDetailView, AccountLedgerView,
    AccountLedgerPrintView,
    PaymentMethodListCreateView, PaymentMethodDetailView,
    SiteConfigView,
    ColorPaletteListCreateView, ColorPaletteDetailView,
    ReportSettingsView,
    ExpenseAliasListCreateView, ExpenseAliasDetailView,
)
from .stock_movement_report import StockMovementReportView, StockMovementExportView
from .patient_activity_report import PatientActivityReportView, PatientActivityExportView
from .beautician_expense_page import (
    BeauticianExpenseAliasListView, BeauticianExpenseListCreateView, BeauticianExpenseDetailView,
)
from .appointments_scheduled import (
    ScheduledAppointmentListCreateView,
    ScheduledAppointmentDetailView,
    AppointmentCheckInView,
    AppointmentLocationListView,
)
from .package_page import PatientPackagesView
from .medical_record_page import MedRecByPatientNoView
from .photo_page import PatientPhotoUploadView, PatientPhotoListView
from .medical_record_history import MedRecHistoryListView, MedRecHistoryDetailView
from .medical_record_draft import MedRecDraftCreateView, MedRecUpdateView, MedRecFinalizeView, MedRecPendingDraftsView
from .invoice_page import InvoiceCreateView, InvoiceListView, InvoiceDetailView, InvoiceExportView, InvoiceImportView
from .reports_page import DashboardReportView, GenerateReportView, SalesRangeReportView, SalesItemsBreakdownView
from .patient_notes_page import PatientNoteListCreateView, PatientNoteDetailView
from .assessment_codes_page import (
    AssessmentCodeListCreateView,
    AssessmentCodeDetailView,
    AssessmentCodeTemplateDownloadView,
    AssessmentCodeImportPreviewView,
    AssessmentCodeImportConfirmView,
)
from .promotion_page import PromotionListCreateView, PromotionDetailView, PromotionValidateView
from .crm_page import PatientCRMListView, PatientCRMDetailView, PatientTierListCreateView, PatientTierDetailView
from .whatsapp_page import (
    PatientWhatsAppOptInView,
    WhatsAppBlastCancelView,
    WhatsAppBlastDetailView,
    WhatsAppBlastListCreateView,
    WhatsAppBlastPreviewView,
    WhatsAppSegmentsView,
    WhatsAppSessionView,
    WhatsAppSettingsView,
    WhatsAppStatusView,
    WhatsAppTestMessageView,
)
from .crm_dashboard import (
    CRMDashboardView,
    CRMPatientProfileView,
    MessageTemplateDetailView,
    MessageTemplateListCreateView,
    MessageTemplateRenderView,
)
from .hr_attendance_page import (
    ClockInView, ClockOutView,
    AttendanceListView, AttendanceSummaryView,
    ShiftListCreateView, ShiftDetailView,
    StaffScheduleView,
)
from .hr_performance_page import StaffPerformanceView, StaffPerformanceDailyView
from .tickets_page import IssueTicketListCreateView, IssueTicketDetailView, IssueTicketImageUploadView
from .stock_opname_page import (
    StockOpnameSessionListCreateView,
    StockOpnameSessionDetailView,
    StockOpnameCompleteView,
    StockOpnameTemplateView,
    StockOpnameTemplateSampleView,
)
from .accounting_page import (
    AccountingDashboardView,
    DailySalesView,
    PaymentPlanPreviewView,
    PaymentPlanExportView,
    SupplierListCreateView,
    SupplierDetailView,
    SupplierAccountView,
    SupplierTemplateView,
    SupplierImportView,
    PurchaseInvoiceListCreateView,
    PurchaseInvoiceDetailView,
    PurchaseInvoicePayView,
    PurchaseInvoiceRestoreView,
    PurchaseLastPriceView,
    CashAccountListView,
    ExpenseListCreateView,
    ExpenseDetailView,
    ExpensePayView,
    AccountTransferListCreateView,
    AccountTransferDetailView,
    JournalAdjustmentView,
    JournalHistoryView,
    JournalRunView,
    JournalRunStreamView,
    JournalStatusView,
    JournalPreviewView,
    JournalPreviewEntriesView,
    JournalPreviewEntryDetailView,
    JournalPreviewCommitView,
    JournalPreviewDiscardView,
    JournalEntryListView,
    JournalEntryDetailView,
    JournalEntryCorrectionDraftView,
    JournalEntryCorrectView,
)
from .manual_journal_page import (
    ManualJournalMetaView,
    ManualJournalClassifyView,
    ManualJournalCreateView,
)
from .financial_reports_page import (
    TrialBalanceView,
    ProfitLossView,
    BalanceSheetView,
    GeneralLedgerView,
    CashFlowView,
)
from .tax_page import (
    TaxMetaView,
    TaxRuleListCreateView,
    TaxRuleDetailView,
    TaxComputeView,
)
from .admin_quick_expense_page import (
    QuickExpenseAliasListView,
    QuickExpenseListCreateView,
)
from .admin_operations_page import (
    AdminDashboardView,
    OperationalCostReportView,
    OperationalEntryDetailView,
    OperationalEntryListCreateView,
    OperationalTasksView,
    OperationalTemplateDetailView,
    OperationalTemplateListCreateView,
)

# Create your views here.
def homepage(request):
    return render(request, 'homepage.html')


#Create List for API
class PatientListCreate(generics.ListCreateAPIView):
    queryset = Patient.objects.all()
    serializer_class = PatientSerializer

class ActPatListCreate(generics.ListCreateAPIView):
    queryset = ActivePatient.objects.exclude(status=0)
    serializer_class = ActivePatientSerializer

class DoctorsListCreate(generics.ListCreateAPIView):
    queryset = Doctors.objects.all()
    serializer_class = DoctorsSerializer

class BeauticiansListCreate(generics.ListCreateAPIView):
    serializer_class = BeauticiansSerializer

    def get_queryset(self):
        if self.request.GET.get('available') == 'true':
            return Beauticians.objects.filter(available=True)
        return Beauticians.objects.all()

class MedRecListCreate(generics.ListCreateAPIView):
    queryset = MedRec.objects.all()
    serializer_class = MedRecSerializer

    def perform_create(self, serializer):
        instance = serializer.save()
        active = ActivePatient.objects.filter(
            patient_no=instance.patient_no,
            medrec__isnull=True,
        ).order_by('-visit_time').first()
        if active:
            active.medrec = instance
            active.status = 3
            active.save(update_fields=['medrec_id', 'status'])
        AuditLog.objects.create(
            performed_by=self.request.user if isinstance(self.request.user, AppUser) else None,
            action='CREATE',
            entity_type='MedRec',
            entity_id=str(instance.medrec_id),
            description=f'Medical record created: {instance.medrec_id}',
        )

class patStatusListCreate(generics.ListCreateAPIView):
    queryset = patientStatus.objects.all()
    serializer_class = PatStatSerializer

#to update the data based from functions that was mentioned before
class BeauticianUpdateActPat(generics.UpdateAPIView):
    queryset = ActivePatient.objects.all()
    serializer_class = ActivePatientSerializer
    lookup_field = 'patient_id'
    
    def perform_update(self, serializer):
        input_id = serializer.instance.patient_id #colorless doesnt mean wrong, i asked chatgpt. need to test it tho tbh
        target_status = self.request.data.get('target_status')

        if not target_status:
            raise ValidationError("target_status is needed, do not leave it empty")
        else:
            message = UpdatePatientTreatment(input_id, target_status)
        
        print(message)
        return super().perform_update(serializer)
    

