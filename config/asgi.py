import os
from django.core.asgi import get_asgi_application

# Default to PRODUCTION settings: a server that forgot DJANGO_SETTINGS_MODULE must not run DEBUG=True.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

application = get_asgi_application()
