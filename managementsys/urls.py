from django.urls import path
from .views import views

urlpatterns = [
    # Auth
    path('api/auth/users/', views.UserListView.as_view(), name='auth-users'),
    path('api/auth/login/', views.LoginView.as_view(), name='auth-login'),
    path('api/auth/logout/', views.LogoutView.as_view(), name='auth-logout'),
    path('api/auth/profile/', views.ProfileUpdateView.as_view(), name='auth-profile'),
    path('api/auth/profile/theme/', views.ThemeUpdateView.as_view(), name='auth-profile-theme'),

    # Admin CRUD
    path('api/admin/doctors/', views.DoctorListCreateAdminView.as_view(), name='admin-doctors'),
    path('api/admin/doctors/<int:pk>/', views.DoctorDetailAdminView.as_view(), name='admin-doctor-detail'),
    path('api/admin/patients/', views.PatientListCreateAdminView.as_view(), name='admin-patients'),
    path('api/admin/patients/<str:patient_no>/', views.PatientDetailAdminView.as_view(), name='admin-patient-detail'),
    path('api/admin/treatments/', views.TreatmentListCreateAdminView.as_view(), name='admin-treatments'),
    path('api/admin/treatments/template/', views.TreatmentTemplateView.as_view(), name='admin-treatments-template'),
    path('api/admin/treatments/import/', views.TreatmentImportView.as_view(), name='admin-treatments-import'),
    path('api/admin/treatments/<int:pk>/', views.TreatmentDetailAdminView.as_view(), name='admin-treatment-detail'),
    path('api/admin/treatment-packages/', views.TreatmentPackageListCreateAdminView.as_view(), name='admin-treatment-packages'),
    path('api/admin/treatment-packages/<int:pk>/', views.TreatmentPackageDetailAdminView.as_view(), name='admin-treatment-package-detail'),
    path('api/treatment-packages/', views.TreatmentPackageSyncView.as_view(), name='treatment-packages-sync'),
    path('api/patients/<str:patient_no>/packages/', views.PatientPackagesView.as_view(), name='patient-packages'),
    path('api/admin/beauticians/', views.BeauticianListCreateAdminView.as_view(), name='admin-beauticians'),
    path('api/admin/beauticians/<int:pk>/', views.BeauticianDetailAdminView.as_view(), name='admin-beautician-detail'),
    path('api/admin/beauticians/<int:pk>/release/', views.BeauticianReleaseView.as_view(), name='admin-beautician-release'),
    path('api/admin/users/', views.AppUserListCreateAdminView.as_view(), name='admin-users'),
    path('api/admin/users/<int:pk>/', views.AppUserDetailAdminView.as_view(), name='admin-user-detail'),
    path('api/admin/treatment-categories/', views.TreatmentCategoryListCreateView.as_view(), name='admin-treatment-categories'),
    path('api/admin/treatment-categories/audit-accounts/', views.TreatmentCategoryAuditAccountsView.as_view(), name='admin-treatment-categories-audit'),
    path('api/admin/treatment-categories/provision-accounts/', views.TreatmentCategoryProvisionAccountsView.as_view(), name='admin-treatment-categories-provision'),
    path('api/admin/treatment-categories/<int:pk>/', views.TreatmentCategoryDetailView.as_view(), name='admin-treatment-category-detail'),
    path('api/admin/accounts/', views.ChartOfAccountsListCreateView.as_view(), name='admin-accounts'),
    path('api/admin/accounts/<int:pk>/', views.ChartOfAccountsDetailView.as_view(), name='admin-account-detail'),
    path('api/admin/accounts/<int:pk>/ledger/', views.AccountLedgerView.as_view(), name='admin-account-ledger'),
    path('api/admin/accounts/<int:pk>/ledger/print/', views.AccountLedgerPrintView.as_view(), name='admin-account-ledger-print'),
    path('api/admin/payment-methods/', views.PaymentMethodListCreateView.as_view(), name='admin-payment-methods'),
    path('api/admin/payment-methods/<int:pk>/', views.PaymentMethodDetailView.as_view(), name='admin-payment-method-detail'),
    path('api/admin/site-config/', views.SiteConfigView.as_view(), name='admin-site-config'),
    path('api/admin/color-palettes/', views.ColorPaletteListCreateView.as_view(), name='admin-color-palettes'),
    path('api/admin/color-palettes/<int:pk>/', views.ColorPaletteDetailView.as_view(), name='admin-color-palette-detail'),
    path('api/admin/report-settings/', views.ReportSettingsView.as_view(), name='admin-report-settings'),
    path('api/admin/expense-aliases/', views.ExpenseAliasListCreateView.as_view(), name='admin-expense-aliases'),
    path('api/admin/expense-aliases/<int:pk>/', views.ExpenseAliasDetailView.as_view(), name='admin-expense-alias-detail'),

    # Admin module: operator dashboard + operational-cost inputs.
    # These inputs are planning records only — see views/admin_operations_page.py.
    path('api/admin/dashboard/', views.AdminDashboardView.as_view(), name='admin-dashboard'),
    path('api/admin/operations/templates/', views.OperationalTemplateListCreateView.as_view(), name='admin-operations-templates'),
    path('api/admin/operations/templates/<int:pk>/', views.OperationalTemplateDetailView.as_view(), name='admin-operations-template-detail'),
    path('api/admin/operations/entries/', views.OperationalEntryListCreateView.as_view(), name='admin-operations-entries'),
    path('api/admin/operations/entries/<int:pk>/', views.OperationalEntryDetailView.as_view(), name='admin-operations-entry-detail'),
    path('api/admin/operations/tasks/', views.OperationalTasksView.as_view(), name='admin-operations-tasks'),
    path('api/admin/operations/report/', views.OperationalCostReportView.as_view(), name='admin-operations-report'),

    # Simplified purchasing: alias-picked expenses, scope='general'.
    path('api/admin/quick-expenses/aliases/', views.QuickExpenseAliasListView.as_view(), name='admin-quick-expense-aliases'),
    path('api/admin/quick-expenses/', views.QuickExpenseListCreateView.as_view(), name='admin-quick-expenses'),

    # Beautician petty-cash expenses (design doc §4)
    path('api/beautician/expense-aliases/', views.BeauticianExpenseAliasListView.as_view(), name='beautician-expense-aliases'),
    path('api/beautician/expenses/', views.BeauticianExpenseListCreateView.as_view(), name='beautician-expenses'),
    path('api/beautician/expenses/<int:pk>/', views.BeauticianExpenseDetailView.as_view(), name='beautician-expense-detail'),

    path("", views.homepage, name = 'homepage'),
    path("patient/", views.PatientListCreate.as_view(), name = "Patient-view-create"),
    path("api/patients/new/", views.PatientCreateWithActiveView.as_view(), name="patient-create-active"),
    path("api/appointments/add/", views.AppointmentAddView.as_view(), name="appointment-add"),
    path("api/appointments/general/", views.GeneralAppointmentCreateView.as_view(), name="appointment-general"),

    # Scheduled appointments (SatuSehat-ready bookings) — separate from the walk-in queue above
    path("api/scheduled-appointments/", views.ScheduledAppointmentListCreateView.as_view(), name="scheduled-appointments"),
    path("api/scheduled-appointments/<int:pk>/", views.ScheduledAppointmentDetailView.as_view(), name="scheduled-appointment-detail"),
    path("api/scheduled-appointments/<int:pk>/check-in/", views.AppointmentCheckInView.as_view(), name="scheduled-appointment-check-in"),
    path("api/appointment-locations/", views.AppointmentLocationListView.as_view(), name="appointment-locations"),
    path("activepatient/", views.ActPatListCreate.as_view(), name = "ActPat-view-create"),
    path("doctors/", views.DoctorsListCreate.as_view(), name = "doctors-list"),
    path("beauticians/", views.BeauticiansListCreate.as_view(), name = "beauticians-list"),
    path("medicalrecord/", views.MedRecListCreate.as_view(), name = "medical-record"),
    path("api/medicalrecords/<str:patient_no_id>/", views.MedRecByPatientNoView.as_view(), name="medical-record-by-patient"),
    path("beautician/update/<str:patient_id>/", views.BeauticianUpdateActPat.as_view(), name = "beautician-update-actpat"),
    path('api/patients/search/', views.PatientSearchView.as_view(), name='patient-search'),
    path('api/patients/count/', views.PatientCountView.as_view(), name='patient-count'),
    path('api/patients/next-no/', views.PatientNextNoView.as_view(), name='patient-next-no'),
    path('api/patients/sync/', views.PatientSyncView.as_view(), name='patient-sync'),
    path('api/activepatients/update-status/', views.ActivePatientUpdateStatusView.as_view(), name='activepatient-update-status'),
    path('api/activepatients/clear/', views.ActivePatientClearView.as_view(), name='activepatient-clear'),
    path('api/activepatients/treatment/', views.TreatmentQueueView.as_view(), name='treatment-queue'),
    path('api/patient-statuses/', views.patStatusListCreate.as_view(), name='patient-statuses'),
    path('api/treatments/', views.TreatmentListView.as_view(), name='treatment-list'),
    path('api/treatment-session/', views.TreatmentSessionCreateView.as_view(), name='treatment-session'),
    path('api/treatment-session/complete/', views.CompleteTreatmentView.as_view(), name='treatment-complete'),
    path('api/treatment-session/<int:session_id>/treatment/<int:treatment_id>/', views.TreatmentRemoveView.as_view(), name='treatment-session-remove-treatment'),

    path('api/billing/', views.BillingQueueView.as_view(), name='billing-queue'),
    path('api/billing/<int:pk>/complete/', views.BillingCompleteView.as_view(), name='billing-complete'),

    path('api/soap-templates/', views.SoapTemplateListCreateView.as_view(), name='soap-template-list'),
    path('api/soap-templates/import/', views.SoapTemplateImportView.as_view(), name='soap-template-import'),
    path('api/soap-templates/export/', views.SoapTemplateExportView.as_view(), name='soap-template-export'),
    path('api/soap-templates/sample/', views.SoapTemplateTemplateDownloadView.as_view(), name='soap-template-sample'),
    path('api/soap-templates/<int:pk>/', views.SoapTemplateDetailView.as_view(), name='soap-template-detail'),

    # Photos
    path('api/photos/upload/', views.PatientPhotoUploadView.as_view(), name='photo-upload'),
    path('api/photos/', views.PatientPhotoListView.as_view(), name='photo-list'),

    # Medical Record History
    path('api/medical-records/history/', views.MedRecHistoryListView.as_view(), name='medrec-history'),
    path('api/medical-records/history/<str:medrec_id>/', views.MedRecHistoryDetailView.as_view(), name='medrec-history-detail'),

    # Medical Record Drafts
    path('api/medical-records/draft/', views.MedRecDraftCreateView.as_view(), name='medrec-draft-create'),
    path('api/medical-records/pending-drafts/', views.MedRecPendingDraftsView.as_view(), name='medrec-pending-drafts'),
    path('api/medical-records/<str:medrec_id>/', views.MedRecUpdateView.as_view(), name='medrec-update'),
    path('api/medical-records/<str:medrec_id>/finalize/', views.MedRecFinalizeView.as_view(), name='medrec-finalize'),

    # Invoice
    path('api/invoices/', views.InvoiceListView.as_view(), name='invoice-list'),
    path('api/invoices/create/', views.InvoiceCreateView.as_view(), name='invoice-create'),
    path('api/invoices/export/', views.InvoiceExportView.as_view(), name='invoice-export'),
    path('api/invoices/import/', views.InvoiceImportView.as_view(), name='invoice-import'),
    path('api/invoices/<int:pk>/', views.InvoiceDetailView.as_view(), name='invoice-detail'),

    # Inventory
    path('api/inventory/items/', views.InventoryItemListCreateView.as_view(), name='inventory-items'),
    path('api/inventory/items/template/', views.InventoryItemTemplateView.as_view(), name='inventory-items-template'),
    path('api/inventory/items/import/preview/', views.InventoryItemImportPreviewView.as_view(), name='inventory-items-import-preview'),
    path('api/inventory/items/import/', views.InventoryItemImportView.as_view(), name='inventory-items-import'),
    path('api/inventory/items/<int:pk>/', views.InventoryItemDetailView.as_view(), name='inventory-item-detail'),
    path('api/inventory/warehouses/', views.WarehouseListCreateView.as_view(), name='inventory-warehouses'),
    path('api/inventory/warehouses/<int:pk>/', views.WarehouseDetailView.as_view(), name='inventory-warehouse-detail'),
    path('api/inventory/stock/', views.StockLevelView.as_view(), name='inventory-stock'),
    path('api/inventory/batches/', views.InventoryBatchListView.as_view(), name='inventory-batches'),
    path('api/inventory/stock-in/', views.StockInView.as_view(), name='inventory-stock-in'),
    path('api/inventory/stock-out/', views.StockOutView.as_view(), name='inventory-stock-out'),
    path('api/inventory/stock-out/batches/', views.StockOutBatchListView.as_view(), name='inventory-stock-out-batches'),
    path('api/inventory/sync/items/', views.ItemSyncView.as_view(), name='inventory-sync-items'),
    path('api/inventory/production/recipes/', views.RecipeListCreateView.as_view(), name='production-recipes'),
    path('api/inventory/production/recipes/export/', views.RecipeExportView.as_view(), name='production-recipes-export'),
    path('api/inventory/production/recipes/template/', views.RecipeTemplateView.as_view(), name='production-recipes-template'),
    path('api/inventory/production/recipes/import/', views.RecipeImportView.as_view(), name='production-recipes-import'),
    path('api/inventory/production/recipes/<int:pk>/', views.RecipeDetailView.as_view(), name='production-recipe-detail'),
    path('api/inventory/production/recipes/<int:pk>/cost/', views.RecipeCostView.as_view(), name='production-recipe-cost'),
    path('api/inventory/production/preview/', views.ProductionPreviewView.as_view(), name='production-preview'),
    path('api/inventory/production/runs/', views.ProductionRunListCreateView.as_view(), name='production-runs'),
    path('api/inventory/production/runs/<int:pk>/', views.ProductionRunDetailView.as_view(), name='production-run-detail'),

    # Pencacahan (unit conversion)
    path('api/inventory/pencacahan/', views.PencacahanListCreateView.as_view(), name='inventory-pencacahan'),
    path('api/inventory/pencacahan/<int:pk>/', views.PencacahanDetailView.as_view(), name='inventory-pencacahan-detail'),

    # Stock Opname
    path('api/stock-opname/', views.StockOpnameSessionListCreateView.as_view(), name='stock-opname-list'),
    path('api/stock-opname/<int:pk>/', views.StockOpnameSessionDetailView.as_view(), name='stock-opname-detail'),
    path('api/stock-opname/<int:pk>/complete/', views.StockOpnameCompleteView.as_view(), name='stock-opname-complete'),
    path('api/stock-opname/template/', views.StockOpnameTemplateView.as_view(), name='stock-opname-template'),
    path('api/stock-opname/template/sample/', views.StockOpnameTemplateSampleView.as_view(), name='stock-opname-template-sample'),

    # Reports
    path('api/reports/dashboard/', views.DashboardReportView.as_view(), name='reports-dashboard'),
    path('api/reports/sales/', views.SalesRangeReportView.as_view(), name='reports-sales'),
    path('api/reports/sales-items/', views.SalesItemsBreakdownView.as_view(), name='reports-sales-items'),
    path('api/reports/generate/', views.GenerateReportView.as_view(), name='reports-generate'),
    path('api/reports/stock-movement/', views.StockMovementReportView.as_view(), name='reports-stock-movement'),
    path('api/reports/stock-movement/export/', views.StockMovementExportView.as_view(), name='reports-stock-movement-export'),
    path('api/reports/patient-activity/', views.PatientActivityReportView.as_view(), name='reports-patient-activity'),
    path('api/reports/patient-activity/export/', views.PatientActivityExportView.as_view(), name='reports-patient-activity-export'),

    # Patient Notes
    path('api/patient-notes/', views.PatientNoteListCreateView.as_view(), name='patient-notes'),
    path('api/patient-notes/<int:pk>/', views.PatientNoteDetailView.as_view(), name='patient-note-detail'),

    # Assessment Codes (ICD-10)
    path('api/assessment-codes/', views.AssessmentCodeListCreateView.as_view(), name='assessment-codes'),
    path('api/assessment-codes/template/', views.AssessmentCodeTemplateDownloadView.as_view(), name='assessment-codes-template'),
    path('api/assessment-codes/import/preview/', views.AssessmentCodeImportPreviewView.as_view(), name='assessment-codes-import-preview'),
    path('api/assessment-codes/import/confirm/', views.AssessmentCodeImportConfirmView.as_view(), name='assessment-codes-import-confirm'),
    path('api/assessment-codes/<int:pk>/', views.AssessmentCodeDetailView.as_view(), name='assessment-code-detail'),

    # Promotions
    path('api/promotions/', views.PromotionListCreateView.as_view(), name='promotion-list'),
    path('api/promotions/validate/', views.PromotionValidateView.as_view(), name='promotion-validate'),
    path('api/promotions/<int:pk>/', views.PromotionDetailView.as_view(), name='promotion-detail'),

    # CRM
    path('api/crm/patients/', views.PatientCRMListView.as_view(), name='crm-patients'),
    path('api/crm/patients/<str:patient_no>/', views.PatientCRMDetailView.as_view(), name='crm-patient-detail'),

    # Tiers
    path('api/admin/tiers/', views.PatientTierListCreateView.as_view(), name='admin-tiers'),
    path('api/admin/tiers/<int:pk>/', views.PatientTierDetailView.as_view(), name='admin-tier-detail'),

    # HR / Performance
    path('api/hr/performance/daily/',  views.StaffPerformanceDailyView.as_view(), name='hr-performance-daily'),
    path('api/hr/performance/',        views.StaffPerformanceView.as_view(),       name='hr-performance'),

    # Issue Tickets
    path('api/tickets/', views.IssueTicketListCreateView.as_view(), name='ticket-list'),
    path('api/tickets/<int:pk>/', views.IssueTicketDetailView.as_view(), name='ticket-detail'),
    path('api/tickets/<int:pk>/images/', views.IssueTicketImageUploadView.as_view(), name='ticket-image-upload'),

    # HR / Attendance
    path('api/hr/attendance/clock-in/',  views.ClockInView.as_view(),           name='hr-clock-in'),
    path('api/hr/attendance/clock-out/', views.ClockOutView.as_view(),           name='hr-clock-out'),
    path('api/hr/attendance/summary/',   views.AttendanceSummaryView.as_view(),  name='hr-attendance-summary'),
    path('api/hr/attendance/',           views.AttendanceListView.as_view(),     name='hr-attendance'),
    path('api/hr/shifts/',               views.ShiftListCreateView.as_view(),    name='hr-shifts'),
    path('api/hr/shifts/<int:pk>/',      views.ShiftDetailView.as_view(),        name='hr-shift-detail'),
    path('api/hr/schedules/',            views.StaffScheduleView.as_view(),      name='hr-schedules'),

    # Accounting
    path('api/accounting/dashboard/',              views.AccountingDashboardView.as_view(),      name='accounting-dashboard'),
    path('api/accounting/suppliers/',              views.SupplierListCreateView.as_view(),       name='accounting-suppliers'),
    path('api/accounting/suppliers/template/',    views.SupplierTemplateView.as_view(),          name='accounting-supplier-template'),
    path('api/accounting/suppliers/import/',      views.SupplierImportView.as_view(),            name='accounting-supplier-import'),
    path('api/accounting/suppliers/<int:pk>/',     views.SupplierDetailView.as_view(),           name='accounting-supplier-detail'),
    path('api/accounting/suppliers/<int:pk>/account/', views.SupplierAccountView.as_view(),       name='accounting-supplier-account'),
    path('api/accounting/purchases/',                   views.PurchaseInvoiceListCreateView.as_view(), name='accounting-purchases'),
    path('api/accounting/purchases/last-price/',        views.PurchaseLastPriceView.as_view(),          name='accounting-purchase-last-price'),
    path('api/accounting/purchases/<int:pk>/',          views.PurchaseInvoiceDetailView.as_view(),      name='accounting-purchase-detail'),
    path('api/accounting/purchases/<int:pk>/pay/',      views.PurchaseInvoicePayView.as_view(),         name='accounting-purchase-pay'),
    path('api/accounting/purchases/<int:pk>/restore/',  views.PurchaseInvoiceRestoreView.as_view(),     name='accounting-purchase-restore'),
    path('api/accounting/cash-accounts/',               views.CashAccountListView.as_view(),            name='accounting-cash-accounts'),
    path('api/accounting/expenses/',                    views.ExpenseListCreateView.as_view(),          name='accounting-expenses'),
    path('api/accounting/expenses/<int:pk>/',           views.ExpenseDetailView.as_view(),              name='accounting-expense-detail'),
    path('api/accounting/expenses/<int:pk>/pay/',       views.ExpensePayView.as_view(),                 name='accounting-expense-pay'),
    path('api/accounting/transfers/',              views.AccountTransferListCreateView.as_view(), name='accounting-transfers'),
    path('api/accounting/transfers/<int:pk>/',     views.AccountTransferDetailView.as_view(),    name='accounting-transfer-detail'),
    # Retired: single-sided adjustments unbalanced the ledger. Kept so an older
    # client gets a 410 with an explanation rather than a 404.
    path('api/accounting/adjustments/',            views.JournalAdjustmentView.as_view(),        name='accounting-adjustments'),
    path('api/accounting/manual-journal/',          views.ManualJournalCreateView.as_view(),      name='accounting-manual-journal'),
    path('api/accounting/manual-journal/meta/',     views.ManualJournalMetaView.as_view(),        name='accounting-manual-journal-meta'),
    path('api/accounting/manual-journal/classify/', views.ManualJournalClassifyView.as_view(),    name='accounting-manual-journal-classify'),
    path('api/accounting/journal/',                views.JournalHistoryView.as_view(),           name='accounting-journal'),
    path('api/accounting/journal/run/',            views.JournalRunView.as_view(),               name='accounting-journal-run'),
    path('api/accounting/journal/run/stream/',     views.JournalRunStreamView.as_view(),         name='accounting-journal-run-stream'),
    path('api/accounting/journal/status/',         views.JournalStatusView.as_view(),            name='accounting-journal-status'),
    # Phase 4 — two-phase run: stage into a draft, review, then commit.
    path('api/accounting/journal/preview/',                views.JournalPreviewView.as_view(),            name='accounting-journal-preview'),
    path('api/accounting/journal/preview/entries/',        views.JournalPreviewEntriesView.as_view(),     name='accounting-journal-preview-entries'),
    path('api/accounting/journal/preview/entries/<int:pk>/', views.JournalPreviewEntryDetailView.as_view(), name='accounting-journal-preview-entry'),
    path('api/accounting/journal/preview/commit/',         views.JournalPreviewCommitView.as_view(),      name='accounting-journal-preview-commit'),
    path('api/accounting/journal/preview/discard/',        views.JournalPreviewDiscardView.as_view(),     name='accounting-journal-preview-discard'),
    # Posted journal entries (headers) + correction journals.
    path('api/accounting/journal/entries/',                    views.JournalEntryListView.as_view(),             name='accounting-journal-entries'),
    path('api/accounting/journal/entries/<int:pk>/',           views.JournalEntryDetailView.as_view(),           name='accounting-journal-entry'),
    path('api/accounting/journal/entries/<int:pk>/correction-draft/', views.JournalEntryCorrectionDraftView.as_view(), name='accounting-journal-entry-correction-draft'),
    path('api/accounting/journal/entries/<int:pk>/correct/',   views.JournalEntryCorrectView.as_view(),          name='accounting-journal-entry-correct'),
    path('api/accounting/daily-sales/',            views.DailySalesView.as_view(),               name='accounting-daily-sales'),
    path('api/accounting/payment-plan/preview/',   views.PaymentPlanPreviewView.as_view(),       name='accounting-payment-plan-preview'),
    path('api/accounting/payment-plan/export/',    views.PaymentPlanExportView.as_view(),        name='accounting-payment-plan-export'),

    # Financial Reports (Laporan Keuangan)
    # ── Perpajakan ──────────────────────────────────────────────────────────
    path('api/accounting/tax/meta/',        views.TaxMetaView.as_view(),           name='tax-meta'),
    path('api/accounting/tax/rules/',       views.TaxRuleListCreateView.as_view(), name='tax-rules'),
    path('api/accounting/tax/rules/<int:pk>/', views.TaxRuleDetailView.as_view(),  name='tax-rule-detail'),
    path('api/accounting/tax/compute/',     views.TaxComputeView.as_view(),        name='tax-compute'),

    path('api/accounting/reports/trial-balance/',  views.TrialBalanceView.as_view(),   name='reports-trial-balance'),
    path('api/accounting/reports/profit-loss/',    views.ProfitLossView.as_view(),     name='reports-profit-loss'),
    path('api/accounting/reports/balance-sheet/',  views.BalanceSheetView.as_view(),   name='reports-balance-sheet'),
    path('api/accounting/reports/general-ledger/', views.GeneralLedgerView.as_view(),  name='reports-general-ledger'),
    path('api/accounting/reports/cash-flow/',      views.CashFlowView.as_view(),       name='reports-cash-flow'),
]

