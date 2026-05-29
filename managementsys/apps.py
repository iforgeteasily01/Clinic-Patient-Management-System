from django.apps import AppConfig


class ManagementsysConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'managementsys'

    def ready(self):
        import managementsys.signals  # noqa: F401 — registers signal handlers
