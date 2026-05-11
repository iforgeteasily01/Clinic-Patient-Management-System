from django.core.management.base import BaseCommand

from managementsys.models import AppUser

AVATAR_COLORS = ['#0284c7', '#7c3aed', '#16a34a', '#dc2626', '#d97706', '#0891b2']


class Command(BaseCommand):
    help = 'Create an AppUser with a 6-digit PIN'

    def add_arguments(self, parser):
        parser.add_argument('display_name', type=str, help='Display name for the user')
        parser.add_argument('pin', type=str, help='6-digit numeric PIN')
        parser.add_argument('--role', type=str, default='admin', choices=['admin'])
        parser.add_argument('--color', type=str, default='', help='Avatar hex color (e.g. #0284c7)')

    def handle(self, *args, **options):
        name = options['display_name'].strip()
        pin = options['pin'].strip()

        if not pin.isdigit() or len(pin) != 6:
            self.stderr.write(self.style.ERROR('PIN must be exactly 6 digits (e.g. 123456).'))
            return

        color = options['color'] or AVATAR_COLORS[AppUser.objects.count() % len(AVATAR_COLORS)]

        user = AppUser(display_name=name, role=options['role'], avatar_color=color)
        user.set_pin(pin)
        user.save()

        self.stdout.write(
            self.style.SUCCESS(f"Created user '{name}' (id={user.id}, role={user.role}).")
        )
