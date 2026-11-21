import pytest

from managementsys.models import AppUser


@pytest.mark.django_db
class TestAppUserAuth:
    def test_pin_is_hashed_not_plaintext(self):
        u = AppUser(display_name="Dr A", role="doctor")
        u.set_pin("123456")
        assert u.pin_hash != "123456"
        assert u.pin_hash.startswith("pbkdf2_")

    def test_check_pin_accepts_correct_rejects_wrong(self):
        u = AppUser(display_name="Dr A")
        u.set_pin("123456")
        assert u.check_pin("123456") is True
        assert u.check_pin("000000") is False
        assert u.check_pin(123456) is True

    def test_generate_token_rotates_and_persists(self):
        u = AppUser.objects.create(display_name="Dr A")
        u.set_pin("123456")
        u.save()

        first = u.generate_token()
        second = u.generate_token()

        assert len(first) == 64
        assert first != second
        assert AppUser.objects.get(pk=u.pk).auth_token == second

    def test_clear_token_revokes_access(self):
        u = AppUser.objects.create(display_name="Dr A")
        u.generate_token()
        assert u.auth_token != ""

        u.clear_token()
        assert u.auth_token == ""
        assert AppUser.objects.get(pk=u.pk).auth_token == ""
