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
    path('api/admin/treatments/import/', views.TreatmentImportView.as_view(), name='admin-treatments-import'),
    path('api/admin/treatments/<int:pk>/', views.TreatmentDetailAdminView.as_view(), name='admin-treatment-detail'),
    path('api/admin/beauticians/', views.BeauticianListCreateAdminView.as_view(), name='admin-beauticians'),
    path('api/admin/beauticians/<int:pk>/', views.BeauticianDetailAdminView.as_view(), name='admin-beautician-detail'),
    path('api/admin/users/', views.AppUserListCreateAdminView.as_view(), name='admin-users'),
    path('api/admin/users/<int:pk>/', views.AppUserDetailAdminView.as_view(), name='admin-user-detail'),
    path('api/admin/treatment-categories/', views.TreatmentCategoryListCreateView.as_view(), name='admin-treatment-categories'),
    path('api/admin/treatment-categories/<int:pk>/', views.TreatmentCategoryDetailView.as_view(), name='admin-treatment-category-detail'),
    path('api/admin/accounts/', views.ChartOfAccountsListCreateView.as_view(), name='admin-accounts'),
    path('api/admin/accounts/<int:pk>/', views.ChartOfAccountsDetailView.as_view(), name='admin-account-detail'),

    path("", views.homepage, name = 'homepage'),
    path("patient/", views.PatientListCreate.as_view(), name = "Patient-view-create"),
    path("api/patients/new/", views.PatientCreateWithActiveView.as_view(), name="patient-create-active"),
    path("api/appointments/add/", views.AppointmentAddView.as_view(), name="appointment-add"),
    path("api/appointments/general/", views.GeneralAppointmentCreateView.as_view(), name="appointment-general"),
    path("activepatient/", views.ActPatListCreate.as_view(), name = "ActPat-view-create"),
    path("doctors/", views.DoctorsListCreate.as_view(), name = "doctors-list"),
    path("beauticians/", views.BeauticiansListCreate.as_view(), name = "beauticians-list"),
    path("medicalrecord/", views.MedRecListCreate.as_view(), name = "medical-record"),
    path("api/medicalrecords/<str:patient_no_id>/", views.MedRecByPatientNoView.as_view(), name="medical-record-by-patient"),
    path("beautician/update/<str:patient_id>/", views.BeauticianUpdateActPat.as_view(), name = "beautician-update-actpat"),
    path('api/patients/search/', views.PatientSearchView.as_view(), name='patient-search'),
    path('api/activepatients/update-status/', views.ActivePatientUpdateStatusView.as_view(), name='activepatient-update-status'),
    path('api/activepatients/clear/', views.ActivePatientClearView.as_view(), name='activepatient-clear'),
    path('api/activepatients/treatment/', views.TreatmentQueueView.as_view(), name='treatment-queue'),
    path('api/patient-statuses/', views.patStatusListCreate.as_view(), name='patient-statuses'),
    path('api/treatments/', views.TreatmentListView.as_view(), name='treatment-list'),
    path('api/treatment-session/', views.TreatmentSessionCreateView.as_view(), name='treatment-session'),
    path('api/treatment-session/complete/', views.CompleteTreatmentView.as_view(), name='treatment-complete'),

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
    path('api/inventory/sync/items/', views.ItemSyncView.as_view(), name='inventory-sync-items'),

    # Reports
    path('api/reports/dashboard/', views.DashboardReportView.as_view(), name='reports-dashboard'),

    # Patient Notes
    path('api/patient-notes/', views.PatientNoteListCreateView.as_view(), name='patient-notes'),
    path('api/patient-notes/<int:pk>/', views.PatientNoteDetailView.as_view(), name='patient-note-detail'),

    # Assessment Codes (ICD-10)
    path('api/assessment-codes/', views.AssessmentCodeListCreateView.as_view(), name='assessment-codes'),
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

    # HR / Attendance
    path('api/hr/attendance/clock-in/',  views.ClockInView.as_view(),           name='hr-clock-in'),
    path('api/hr/attendance/clock-out/', views.ClockOutView.as_view(),           name='hr-clock-out'),
    path('api/hr/attendance/summary/',   views.AttendanceSummaryView.as_view(),  name='hr-attendance-summary'),
    path('api/hr/attendance/',           views.AttendanceListView.as_view(),     name='hr-attendance'),
    path('api/hr/shifts/',               views.ShiftListCreateView.as_view(),    name='hr-shifts'),
    path('api/hr/shifts/<int:pk>/',      views.ShiftDetailView.as_view(),        name='hr-shift-detail'),
    path('api/hr/schedules/',            views.StaffScheduleView.as_view(),      name='hr-schedules'),
]

