web: python manage.py migrate --noinput && python manage.py seed_demo && gunicorn config.wsgi:application --workers 1 --threads 4
