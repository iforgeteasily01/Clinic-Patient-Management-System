from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from ..api.serializers import AppUserPublicSerializer
from ..models import AppUser, AuditLog


class UserListView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        users = AppUser.objects.filter(is_active=True).order_by('display_name')
        serializer = AppUserPublicSerializer(users, many=True, context={'request': request})
        return Response(serializer.data)


class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        user_id = request.data.get('user_id')
        pin = request.data.get('pin')

        if not user_id or not pin:
            return Response(
                {"error": "user_id and pin are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            user = AppUser.objects.get(id=user_id, is_active=True)
        except AppUser.DoesNotExist:
            return Response({"error": "Invalid credentials."}, status=status.HTTP_401_UNAUTHORIZED)

        if not user.check_pin(str(pin)):
            return Response({"error": "Incorrect PIN."}, status=status.HTTP_401_UNAUTHORIZED)

        AuditLog.objects.create(
            performed_by=user,
            action='LOGIN',
            entity_type='AppUser',
            entity_id=str(user.id),
            description=f'{user.display_name} logged in',
        )
        token = user.generate_token()
        serializer = AppUserPublicSerializer(user, context={'request': request})
        return Response({"token": token, "user": serializer.data}, status=status.HTTP_200_OK)


class ProfileUpdateView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def _get_user(self, request):
        token = request.headers.get('Authorization', '').replace('Bearer ', '').strip()
        if not token:
            return None
        try:
            return AppUser.objects.get(auth_token=token, is_active=True)
        except AppUser.DoesNotExist:
            return None

    def get(self, request):
        user = self._get_user(request)
        if not user:
            return Response({'error': 'Authentication required.'}, status=status.HTTP_401_UNAUTHORIZED)
        serializer = AppUserPublicSerializer(user, context={'request': request})
        return Response(serializer.data)

    def patch(self, request):
        user = self._get_user(request)
        if not user:
            return Response({'error': 'Authentication required.'}, status=status.HTTP_401_UNAUTHORIZED)

        display_name = request.data.get('display_name')
        if display_name is not None:
            display_name = str(display_name).strip()
            if not display_name:
                return Response({'error': 'Name cannot be empty.'}, status=status.HTTP_400_BAD_REQUEST)
            user.display_name = display_name

        if 'profile_picture' in request.FILES:
            user.profile_picture = request.FILES['profile_picture']

        new_pin = request.data.get('new_pin')
        confirm_pin = request.data.get('confirm_pin')
        if new_pin is not None or confirm_pin is not None:
            new_pin_str = str(new_pin or '')
            confirm_pin_str = str(confirm_pin or '')
            if len(new_pin_str) != 6 or not new_pin_str.isdigit():
                return Response({'error': 'PIN must be exactly 6 digits.'}, status=status.HTTP_400_BAD_REQUEST)
            if new_pin_str != confirm_pin_str:
                return Response({'error': 'PINs do not match.'}, status=status.HTTP_400_BAD_REQUEST)
            user.set_pin(new_pin_str)

        user.save()
        serializer = AppUserPublicSerializer(user, context={'request': request})
        return Response(serializer.data)


class LogoutView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        token = request.headers.get('Authorization', '').replace('Bearer ', '').strip()
        if token:
            try:
                user = AppUser.objects.get(auth_token=token)
                AuditLog.objects.create(
                    performed_by=user,
                    action='LOGOUT',
                    entity_type='AppUser',
                    entity_id=str(user.id),
                    description=f'{user.display_name} logged out',
                )
                user.clear_token()
            except AppUser.DoesNotExist:
                pass
        return Response({"ok": True})
