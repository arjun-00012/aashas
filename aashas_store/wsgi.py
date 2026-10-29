"""
WSGI config for aashas_store project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.1/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'aashas_store.settings')

application = get_wsgi_application()
app = application

# If running on Vercel and using SQLite, ensure catalog is loaded if database was empty
if os.environ.get('VERCEL'):
    try:
        from store.models import Category
        if not Category.objects.exists():
            from django.core.management import call_command
            call_command('migrate', verbosity=0)
            call_command('seed_catalog', verbosity=0)
    except Exception:
        pass

