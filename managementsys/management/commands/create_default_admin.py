from django.core.management.base import BaseCommand

from managementsys.models import AppUser


class Command(BaseCommand):
    help = 'Create a default superuser (display_name=Admin, PIN=000000) if none exists.'

    def handle(self, *args, **options):
        if AppUser.objects.filter(role='superuser').exists():
            self.stdout.write(self.style.WARNING(
                'A superuser already exists — skipping. Use create_app_user to add more.'
            ))
            return

        user = AppUser(display_name='Admin', role='superuser', avatar_color='#0284c7')
        user.set_pin('000000')
        user.save()

        self.stdout.write(self.style.SUCCESS(
            f"Default admin created (id={user.id}, PIN=000000). Change the PIN after first login."
        ))
