from django.apps import AppConfig
from django.db.backends.signals import connection_created


def enable_sqlite_concurrency(sender, connection, **kwargs):
    if connection.vendor == 'sqlite':
        with connection.cursor() as cursor:
            cursor.execute('PRAGMA journal_mode = WAL;')
            cursor.execute('PRAGMA busy_timeout = 30000;')


class StoreConfig(AppConfig):
    name = 'store'

    def ready(self):
        connection_created.connect(enable_sqlite_concurrency)
