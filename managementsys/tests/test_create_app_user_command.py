"""``manage.py create_app_user``.

The point of this file is the first test. The command used to declare
``--role`` as ``default='admin', choices=['admin']``, and ``admin`` is not one
of ``AppUser.ROLE_CHOICES``. Django validates choices in forms, not in the
database, so the row saved fine and the user was then invisible to every
role check in the app — a lockout with no error anywhere.
"""
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from managementsys.models import AppUser

VALID_ROLES = {value for value, _ in AppUser.ROLE_CHOICES}


@pytest.mark.django_db
class TestCreateAppUser:
    def test_the_default_role_is_one_the_app_recognises(self):
        call_command('create_app_user', 'Owner', '123456', stdout=StringIO())

        user = AppUser.objects.get(display_name='Owner')
        assert user.role == 'superuser'
        assert user.role in VALID_ROLES

    def test_every_offered_role_is_a_real_role(self):
        """Guards the literal list from drifting away from the model again."""
        for role in VALID_ROLES:
            call_command('create_app_user', f'Staff {role}', '123456',
                         '--role', role, stdout=StringIO())
            assert AppUser.objects.get(display_name=f'Staff {role}').role == role

    def test_an_unknown_role_is_refused(self):
        with pytest.raises(CommandError):
            call_command('create_app_user', 'Nobody', '123456',
                         '--role', 'admin', stdout=StringIO(), stderr=StringIO())
        assert not AppUser.objects.filter(display_name='Nobody').exists()

    def test_the_pin_is_hashed_and_usable(self):
        call_command('create_app_user', 'Owner', '654321', stdout=StringIO())

        user = AppUser.objects.get(display_name='Owner')
        assert user.pin_hash != '654321'
        assert user.check_pin('654321')

    @pytest.mark.parametrize('pin', ['12345', '1234567', 'abcdef', ''])
    def test_a_bad_pin_creates_nothing(self, pin):
        call_command('create_app_user', 'Owner', pin, stdout=StringIO(), stderr=StringIO())
        assert not AppUser.objects.filter(display_name='Owner').exists()

    def test_a_blank_name_creates_nothing(self):
        call_command('create_app_user', '   ', '123456', stdout=StringIO(), stderr=StringIO())
        assert AppUser.objects.count() == 0
