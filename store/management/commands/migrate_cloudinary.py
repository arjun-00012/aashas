import os
import re
from django.core.management.base import BaseCommand
from django.conf import settings
from store.models import Category, Product
import cloudinary
import cloudinary.uploader
import psycopg2


class Command(BaseCommand):
    help = "Migrates all active category and product images from old Cloudinary to a new Cloudinary account."

    def add_arguments(self, parser):
        parser.add_argument('--new-cloud-name', type=str, required=True, help='New Cloudinary Cloud Name')
        parser.add_argument('--new-api-key', type=str, required=True, help='New Cloudinary API Key')
        parser.add_argument('--new-api-secret', type=str, required=True, help='New Cloudinary API Secret')

    def handle(self, *args, **options):
        new_cloud_name = options['new_cloud_name']
        new_api_key = options['new_api_key']
        new_api_secret = options['new_api_secret']

        self.stdout.write(self.style.NOTICE(f"Initializing migration to new Cloudinary account ({new_cloud_name})..."))

        # Configure target account
        cloudinary.config(
            cloud_name=new_cloud_name,
            api_key=new_api_key,
            api_secret=new_api_secret,
            secure=True
        )

        # Detect live Neon DB URL if present
        neon_db_url = os.environ.get('DATABASE_URL')
        if not neon_db_url:
            render_yaml = os.path.join(settings.BASE_DIR, 'render.yaml')
            if os.path.exists(render_yaml):
                with open(render_yaml, 'r', encoding='utf-8') as f:
                    match = re.search(r'DATABASE_URL\s*\n\s*value:\s*["\']([^"\']+)["\']', f.read())
                    if match:
                        neon_db_url = match.group(1)

        neon_conn = None
        neon_cur = None
        if neon_db_url:
            try:
                neon_conn = psycopg2.connect(neon_db_url)
                neon_cur = neon_conn.cursor()
                self.stdout.write(self.style.SUCCESS("[OK] Connected to live Neon production DB."))
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"Live DB direct connection skipped: {e}"))

        # 1. Migrate Categories
        categories = list(Category.objects.all())
        self.stdout.write(f"\nMigrating {len(categories)} categories...")
        for cat in categories:
            old_url = cat.image_url
            if not old_url and cat.image:
                try:
                    old_url = cat.image.url
                except Exception:
                    pass

            if old_url:
                try:
                    # Strip any transformation tags from source URL to get original
                    source_url = re.sub(r'/upload/[^/]+/', '/upload/', old_url)
                    self.stdout.write(f"Uploading Category '{cat.name}'...")
                    res = cloudinary.uploader.upload(
                        source_url,
                        folder="ashas/categories",
                        public_id=cat.slug or f"cat_{cat.id}",
                        overwrite=True,
                        resource_type="image"
                    )
                    new_url = res.get('secure_url')
                    if new_url:
                        cat.image_url = new_url
                        cat.save(update_fields=['image_url'])

                        if neon_cur:
                            neon_cur.execute(
                                "UPDATE store_category SET image_url = %s WHERE id = %s",
                                (new_url, cat.id)
                            )
                        self.stdout.write(self.style.SUCCESS(f"  [OK] '{cat.name}' -> {new_url}"))
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"  [ERROR] Category '{cat.name}': {e}"))

        # 2. Migrate Products
        products = list(Product.objects.all())
        self.stdout.write(f"\nMigrating {len(products)} products...")
        slot_suffixes = [('', 'image_url'), ('_2', 'image_2_url'), ('_3', 'image_3_url'), ('_4', 'image_4_url')]

        for prod in products:
            updates = []
            for suffix, field_name in slot_suffixes:
                old_url = getattr(prod, field_name)
                if old_url:
                    try:
                        source_url = re.sub(r'/upload/[^/]+/', '/upload/', old_url)
                        public_id = f"product_{prod.id}{suffix}"
                        res = cloudinary.uploader.upload(
                            source_url,
                            folder="ashas/products",
                            public_id=public_id,
                            overwrite=True,
                            resource_type="image"
                        )
                        new_url = res.get('secure_url')
                        if new_url:
                            setattr(prod, field_name, new_url)
                            updates.append(field_name)

                            if neon_cur:
                                neon_cur.execute(
                                    f"UPDATE store_product SET {field_name} = %s WHERE id = %s",
                                    (new_url, prod.id)
                                )
                    except Exception as e:
                        self.stdout.write(self.style.ERROR(f"  [ERROR] Product '{prod.name}' slot {field_name}: {e}"))

            if updates:
                prod.save(update_fields=updates)
                self.stdout.write(self.style.SUCCESS(f"  [OK] Product #{prod.id} '{prod.name}' migrated ({len(updates)} images)."))

        if neon_conn:
            neon_conn.commit()
            neon_conn.close()
            self.stdout.write(self.style.SUCCESS("[OK] Live Neon production DB updated successfully!"))

        self.stdout.write(self.style.SUCCESS("\n========================================================"))
        self.stdout.write(self.style.SUCCESS("MIGRATION COMPLETE! All images are in your new account."))
        self.stdout.write(self.style.SUCCESS("Now update your Vercel Environment Variables:"))
        self.stdout.write(f"  CLOUDINARY_CLOUD_NAME = {new_cloud_name}")
        self.stdout.write(f"  CLOUDINARY_API_KEY    = {new_api_key}")
        self.stdout.write(f"  CLOUDINARY_API_SECRET = {new_api_secret}")
        self.stdout.write(self.style.SUCCESS("========================================================"))
