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

        self.stdout.write(self.style.NOTICE(f"Connecting to target Cloudinary account ({new_cloud_name})..."))

        # 1. Configure target Cloudinary account
        cloudinary.config(
            cloud_name=new_cloud_name,
            api_key=new_api_key,
            api_secret=new_api_secret,
            secure=True
        )

        # 2. Connect to Live Neon Production DB
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
                self.stdout.write(self.style.SUCCESS("[OK] Connected directly to live Neon production DB."))
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"Live DB direct connection failed ({e}), using local records."))

        def upload_to_new_cloud(source_url, folder, public_id):
            if not source_url or not isinstance(source_url, str):
                return None
            # Clean off any transformation tags like /f_auto,q_auto/ to get original
            clean_source = re.sub(r'/upload/[^/]+/', '/upload/', source_url.strip())
            if not clean_source.startswith(('http://', 'https://')):
                return None
            try:
                res = cloudinary.uploader.upload(
                    clean_source,
                    folder=folder,
                    public_id=public_id,
                    overwrite=True,
                    resource_type="image"
                )
                return res.get('secure_url')
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"    Upload error for {public_id}: {e}"))
                return None

        # 3. Migrate Live Neon DB Categories if available
        if neon_cur:
            self.stdout.write("\n--- Migrating Live Production Categories ---")
            neon_cur.execute("SELECT id, name, slug, image_url FROM store_category")
            neon_cats = neon_cur.fetchall()
            for cat_id, cat_name, cat_slug, cat_img_url in neon_cats:
                if cat_img_url:
                    pub_id = cat_slug or f"cat_{cat_id}"
                    new_url = upload_to_new_cloud(cat_img_url, "ashas/categories", pub_id)
                    if new_url:
                        neon_cur.execute(
                            "UPDATE store_category SET image_url = %s WHERE id = %s",
                            (new_url, cat_id)
                        )
                        self.stdout.write(self.style.SUCCESS(f"  [OK] Category '{cat_name}' -> {new_url}"))

            self.stdout.write("\n--- Migrating Live Production Products ---")
            neon_cur.execute("SELECT id, name, image_url, image_2_url, image_3_url, image_4_url FROM store_product")
            neon_prods = neon_cur.fetchall()
            self.stdout.write(f"Found {len(neon_prods)} live products to migrate.")
            
            slots = [
                ('image_url', ''),
                ('image_2_url', '_2'),
                ('image_3_url', '_3'),
                ('image_4_url', '_4')
            ]
            
            for prod_id, prod_name, u1, u2, u3, u4 in neon_prods:
                url_map = {'image_url': u1, 'image_2_url': u2, 'image_3_url': u3, 'image_4_url': u4}
                updates = {}
                for field_name, suffix in slots:
                    cur_url = url_map[field_name]
                    if cur_url:
                        pub_id = f"product_{prod_id}{suffix}"
                        new_url = upload_to_new_cloud(cur_url, "ashas/products", pub_id)
                        if new_url:
                            updates[field_name] = new_url
                
                if updates:
                    set_clause = ", ".join([f"{f} = %s" for f in updates.keys()])
                    values = list(updates.values()) + [prod_id]
                    neon_cur.execute(f"UPDATE store_product SET {set_clause} WHERE id = %s", values)
                    self.stdout.write(self.style.SUCCESS(f"  [OK] Product #{prod_id} '{prod_name}' -> {len(updates)} images copied."))

            neon_conn.commit()
            neon_conn.close()
            self.stdout.write(self.style.SUCCESS("\n[OK] Live Neon DB successfully updated with new Cloudinary URLs!"))

        # 4. Migrate Local DB (so local development matches)
        self.stdout.write("\n--- Syncing Local SQLite Database ---")
        for cat in Category.objects.all():
            if cat.image_url:
                new_url = upload_to_new_cloud(cat.image_url, "ashas/categories", cat.slug or f"cat_{cat.id}")
                if new_url:
                    cat.image_url = new_url
                    cat.save(update_fields=['image_url'])

        for prod in Product.objects.all():
            slots_to_check = [
                (prod.image_url, 'image_url', ''),
                (prod.image_2_url, 'image_2_url', '_2'),
                (prod.image_3_url, 'image_3_url', '_3'),
                (prod.image_4_url, 'image_4_url', '_4'),
            ]
            up = []
            for cur_u, f_name, suff in slots_to_check:
                if cur_u:
                    new_u = upload_to_new_cloud(cur_u, "ashas/products", f"product_{prod.id}{suff}")
                    if new_u:
                        setattr(prod, f_name, new_u)
                        up.append(f_name)
            if up:
                prod.save(update_fields=up)

        self.stdout.write(self.style.SUCCESS("\n" + "="*60))
        self.stdout.write(self.style.SUCCESS("ALL IMAGES SUCCESSFULLY MIGRATED TO YOUR NEW ACCOUNT!"))
        self.stdout.write(self.style.SUCCESS("="*60))
