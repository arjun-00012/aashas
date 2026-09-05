import os
import shutil
from pathlib import Path
from django.core.management.base import BaseCommand
from django.core.management import call_command
from django.conf import settings
from store.models import Category, Product

class Command(BaseCommand):
    help = "Exports all categories, products, and media assets into store/fixtures and store/seed_assets for Git tracking and deployment persistence"

    def handle(self, *args, **options):
        base_dir = Path(settings.BASE_DIR)
        fixtures_dir = base_dir / 'store' / 'fixtures'
        fixtures_dir.mkdir(parents=True, exist_ok=True)
        fixture_file = fixtures_dir / 'catalog.json'

        # 1. Dump database models to JSON fixture
        with open(fixture_file, 'w', encoding='utf-8') as f:
            call_command('dumpdata', 'store.Category', 'store.Product', indent=2, stdout=f)
        self.stdout.write(self.style.SUCCESS(f"Exported catalog database records to {fixture_file}"))

        # 2. Copy any local media assets to seed_assets for Git persistence
        media_root = Path(settings.MEDIA_ROOT)
        seed_root = base_dir / 'store' / 'seed_assets'
        seed_categories = seed_root / 'categories'
        seed_products = seed_root / 'products'
        seed_categories.mkdir(parents=True, exist_ok=True)
        seed_products.mkdir(parents=True, exist_ok=True)

        copied_count = 0
        if media_root.exists():
            for sub in ['categories', 'products']:
                src_dir = media_root / sub
                dest_dir = seed_root / sub
                if src_dir.exists():
                    for item in src_dir.iterdir():
                        if item.is_file():
                            target = dest_dir / item.name
                            if not target.exists() or item.stat().st_mtime > target.stat().st_mtime:
                                shutil.copy2(item, target)
                                copied_count += 1

        self.stdout.write(self.style.SUCCESS(f"Copied {copied_count} media assets to store/seed_assets/ for Git bundling."))
        self.stdout.write(self.style.SUCCESS("Export complete! You can now git add, commit, and push to deploy all products to live."))
