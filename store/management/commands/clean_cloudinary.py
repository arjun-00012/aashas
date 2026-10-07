import os
import re
import json
from django.core.management.base import BaseCommand
from django.conf import settings
from store.models import Category, Product
import cloudinary
import cloudinary.api
import psycopg2


class Command(BaseCommand):
    help = "Audits and safely deletes unused/orphaned images in Cloudinary without affecting active store images."

    def add_arguments(self, parser):
        parser.add_argument(
            '--delete',
            action='store_true',
            help='Actually delete unused images from Cloudinary. Without this flag, runs in safe preview (dry-run) mode.'
        )

    def handle(self, *args, **options):
        is_delete = options['delete']

        cloud_name = getattr(settings, 'CLOUDINARY_CLOUD_NAME', 'dwk9pw2ol')
        api_key = getattr(settings, 'CLOUDINARY_API_KEY', '934762869273294')
        api_secret = getattr(settings, 'CLOUDINARY_API_SECRET', 'bu1cNNgmT6U29O9uPTIM2yyAqFs')

        cloudinary.config(
            cloud_name=cloud_name,
            api_key=api_key,
            api_secret=api_secret,
            secure=True
        )

        self.stdout.write(self.style.NOTICE(f"Connecting to Cloudinary ({cloud_name})..."))

        protected_urls = set()
        protected_identifiers = set()

        def register_target(val):
            if not val or not isinstance(val, str):
                return
            clean = val.strip()
            if not clean:
                return
            protected_urls.add(clean)
            if 'cloudinary.com' in clean and '/upload/' in clean:
                part = clean.split('/upload/')[-1]
                tokens = part.split('/')
                if tokens[0].startswith('v') and tokens[0][1:].isdigit():
                    tokens = tokens[1:]
                raw_path = '/'.join(tokens)
                pub_id_clean = os.path.splitext(raw_path)[0]
                protected_identifiers.add(pub_id_clean)
                protected_identifiers.add(tokens[-1].split('.')[0])
            else:
                base = os.path.basename(clean).split('.')[0]
                if base:
                    protected_identifiers.add(base)

        # 1. Collect from Current Django DB (Categories + Products)
        for cat in Category.objects.all():
            register_target(cat.image_url)
            if cat.image:
                try:
                    register_target(cat.image.url)
                except Exception:
                    pass
                register_target(getattr(cat.image, 'name', ''))

        for p in Product.objects.all():
            for u in [p.image_url, p.image_2_url, p.image_3_url, p.image_4_url]:
                register_target(u)
            for f in [p.image, p.image_2, p.image_3, p.image_4]:
                if f:
                    try:
                        register_target(f.url)
                    except Exception:
                        pass
                    register_target(getattr(f, 'name', ''))

        # 2. Collect from Live Production Neon DB (from render.yaml or env)
        neon_db_url = os.environ.get('DATABASE_URL')
        if not neon_db_url:
            render_yaml = os.path.join(settings.BASE_DIR, 'render.yaml')
            if os.path.exists(render_yaml):
                with open(render_yaml, 'r', encoding='utf-8') as f:
                    match = re.search(r'DATABASE_URL\s*\n\s*value:\s*["\']([^"\']+)["\']', f.read())
                    if match:
                        neon_db_url = match.group(1)

        if neon_db_url:
            try:
                self.stdout.write("Connecting to Live Neon Production DB to guarantee 100% protection...")
                conn = psycopg2.connect(neon_db_url)
                cur = conn.cursor()
                cur.execute("SELECT image_url, image_2_url, image_3_url, image_4_url, image, image_2, image_3, image_4 FROM store_product")
                for row in cur.fetchall():
                    for item in row:
                        register_target(item)
                cur.execute("SELECT image_url, image FROM store_category")
                for row in cur.fetchall():
                    for item in row:
                        register_target(item)
                conn.close()
                self.stdout.write(self.style.SUCCESS("[OK] Successfully verified live production database records."))
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"Could not connect to live DB ({e}), using local records."))

        # 3. Collect from Fixtures Catalog
        catalog_path = os.path.join(settings.BASE_DIR, 'store', 'fixtures', 'catalog.json')
        if os.path.exists(catalog_path):
            try:
                with open(catalog_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for item in data:
                        for k, v in item.get('fields', {}).items():
                            if 'image' in k and isinstance(v, str):
                                register_target(v)
            except Exception:
                pass

        self.stdout.write(f"Gathered {len(protected_urls)} protected active URLs and {len(protected_identifiers)} identifiers.")

        # 4. Fetch all resources from Cloudinary
        self.stdout.write("Fetching asset list from Cloudinary via Admin API...")
        all_resources = []
        next_cursor = None
        while True:
            params = {'max_results': 500}
            if next_cursor:
                params['next_cursor'] = next_cursor
            res = cloudinary.api.resources(**params)
            all_resources.extend(res.get('resources', []))
            next_cursor = res.get('next_cursor')
            if not next_cursor:
                break

        self.stdout.write(f"Total assets currently in Cloudinary: {len(all_resources)}")

        # 5. Segregate active vs unused
        active_assets = []
        unused_assets = []
        unused_bytes = 0

        for item in all_resources:
            pub_id = item['public_id']
            sec_url = item.get('secure_url', '')
            plain_url = item.get('url', '')
            base_name = pub_id.split('/')[-1]

            is_used = False
            if pub_id in protected_identifiers or base_name in protected_identifiers:
                is_used = True
            elif sec_url in protected_urls or plain_url in protected_urls:
                is_used = True
            else:
                for target in protected_urls:
                    if pub_id in target or base_name in target:
                        is_used = True
                        break

            if is_used:
                active_assets.append(item)
            else:
                unused_assets.append(item)
                unused_bytes += item.get('bytes', 0)

        total_bytes = sum(item.get('bytes', 0) for item in all_resources)
        active_bytes = total_bytes - unused_bytes

        self.stdout.write("=" * 65)
        self.stdout.write(self.style.SUCCESS(f"ACTIVE / IN-USE IMAGES:  {len(active_assets)} ({active_bytes / (1024*1024):.2f} MB) - PRESERVED"))
        self.stdout.write(self.style.WARNING(f"UNUSED / ORPHANED ASSETS: {len(unused_assets)} ({unused_bytes / (1024*1024):.2f} MB)"))
        self.stdout.write("=" * 65)

        if not unused_assets:
            self.stdout.write(self.style.SUCCESS("No unused assets found! Cloudinary is clean."))
            return

        self.stdout.write("\nSample unused assets found:")
        for u in sorted(unused_assets, key=lambda x: x.get('bytes', 0), reverse=True)[:15]:
            self.stdout.write(f"  - {u['public_id']}.{u.get('format')} ({u.get('bytes', 0) / (1024*1024):.2f} MB)")

        if not is_delete:
            self.stdout.write(self.style.NOTICE(
                f"\n[DRY-RUN COMPLETE] Found {len(unused_assets)} unused images ({unused_bytes / (1024*1024):.2f} MB).\n"
                f"No files were deleted.\n"
                f"To safely delete these {len(unused_assets)} unused assets, run:\n"
                f"  python manage.py clean_cloudinary --delete"
            ))
            return

        # 6. Delete unused assets
        self.stdout.write(self.style.WARNING(f"\nProceeding to delete {len(unused_assets)} unused assets from Cloudinary..."))
        public_ids_to_delete = [u['public_id'] for u in unused_assets]

        chunk_size = 100
        deleted_count = 0
        for i in range(0, len(public_ids_to_delete), chunk_size):
            chunk = public_ids_to_delete[i:i + chunk_size]
            try:
                res = cloudinary.api.delete_resources(chunk)
                deleted_dict = res.get('deleted', {})
                for pid, status in deleted_dict.items():
                    if status == 'deleted':
                        deleted_count += 1
                self.stdout.write(f"Deleted batch {i//chunk_size + 1}: {len(chunk)} items.")
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"Error deleting batch: {e}"))

        self.stdout.write(self.style.SUCCESS(
            f"\nSUCCESS: Cleaned up {deleted_count} unused files! "
            f"Freed {unused_bytes / (1024*1024):.2f} MB from Cloudinary."
        ))
