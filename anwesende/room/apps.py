from django.apps import AppConfig


class UsersConfig(AppConfig):
    name = "anwesende.room"
    verbose_name = "Room"

    def ready(self):
        try:
            import anwesende.room.signals  # noqa F401
        except ImportError:
            pass
