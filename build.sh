#!/usr/bin/env bash
# Exit on error
set -o errexit

pip install -r requirements.txt

# Ensure media directory structure exists
mkdir -p media/categories media/products

# Populate initial seed assets if media is empty
if [ -d "store/seed_assets" ]; then
    cp -rn store/seed_assets/* media/ 2>/dev/null || cp -r store/seed_assets/* media/ 2>/dev/null || true
fi

python manage.py collectstatic --no-input
python manage.py migrate

# Seed initial store catalog if database is empty
python manage.py seed_catalog

# Ensure superuser (admin / admin@123) exists with verified password
python manage.py shell -c "from django.contrib.auth.models import User; u, _ = User.objects.get_or_create(username='admin', defaults={'email': 'admin@aashas.com', 'is_staff': True, 'is_superuser': True}); u.is_staff = True; u.is_superuser = True; u.set_password('admin@123'); u.save()"