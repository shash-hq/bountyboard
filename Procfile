web: gunicorn config.wsgi:application --preload --workers 2 --threads 4
release: python manage.py migrate --noinput && python manage.py collectstatic --noinput
