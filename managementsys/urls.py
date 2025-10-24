from django.urls import path
from .views import views

urlpatterns = [
    path("", views.homepage, name = 'homepage'),
    path("patient/", views.PatientListCreate.as_view(), name = "Patient-view-create"),
    path("activepatient/", views.ActPatListCreate.as_view(), name = "ActPat-view-create"),
    path("doctors/", views.DoctorsListCreate.as_view(), name = "doctors-list"),
    path("beauticians/", views.BeauticiansListCreate.as_view(), name = "beauticians-list"),
    path("medicalrecord/", views.MedRecListCreate.as_view(), name = "medical-record")
    #i assume patient status doesnt need a url, because if yes, why?  
]

