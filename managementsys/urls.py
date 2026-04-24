from django.urls import path
from .views import views

urlpatterns = [
    path("", views.homepage, name = 'homepage'),
    path("patient/", views.PatientListCreate.as_view(), name = "Patient-view-create"),
    path("api/patients/new/", views.PatientCreateWithActiveView.as_view(), name="patient-create-active"),
    path("activepatient/", views.ActPatListCreate.as_view(), name = "ActPat-view-create"),
    path("doctors/", views.DoctorsListCreate.as_view(), name = "doctors-list"),
    path("beauticians/", views.BeauticiansListCreate.as_view(), name = "beauticians-list"),
    path("medicalrecord/", views.MedRecListCreate.as_view(), name = "medical-record"),
    path("api/medicalrecords/<str:patient_no_id>/", views.MedRecByPatientNoView.as_view(), name="medical-record-by-patient"),
    path("beautician/update/<str:patient_id>/", views.BeauticianUpdateActPat.as_view(), name = "beautician-update-actpat"),
    path('api/patients/search/', views.PatientSearchView.as_view(), name='patient-search'),
    path('api/activepatients/update-status/', views.ActivePatientUpdateStatusView.as_view(), name='activepatient-update-status'),
]

