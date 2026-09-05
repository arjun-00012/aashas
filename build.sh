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

# Auto-create superuser (admin / admin@123) if not exists
python manage.py shell -c "from django.contrib.auth.models import User; User.objects.filter(username='admin').exists() or User.objects.create_superuser('admin', 'admin@aashas.com', 'admin@123')"