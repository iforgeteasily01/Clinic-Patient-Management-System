from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import BasePermission

from .models import AppUser


class AppUserAuthentication(BaseAuthentication):
    def authenticate(self, request):
        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            return None
        token = auth[7:].strip()
        if not token:
            return None
        try:
            user = AppUser.objects.get(auth_token=token)
            return (user, token)
        except AppUser.DoesNotExist:
            return None

    def authenticate_header(self, request):
        return 'Bearer'


class IsAppAuthenticated(BasePermission):
    def has_permission(self, request, view):
        return isinstance(request.user, AppUser)
