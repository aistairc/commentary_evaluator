from django.conf import settings
from django.apps import AppConfig
from django.db.backends.signals import connection_created
from django.dispatch import receiver


class VideoEvalAppConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'video_eval_app'

    def ready(self):
        # Register the signal handler when the app is ready
        @receiver(connection_created)
        def set_busy_timeout(sender, connection, **kwargs):
            if connection.settings_dict['ENGINE'] == 'django.db.backends.sqlite3':
                connection.cursor().execute(f'PRAGMA busy_timeout = {settings.SQLITE3_BUSY_TIMEOUT}')
