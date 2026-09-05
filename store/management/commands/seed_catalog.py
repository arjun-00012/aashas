import os
import shutil
from pathlib import Path
from django.core.management.base import BaseCommand
from django.conf import settings
from store.models import Category, Product

class Command(BaseCommand):
    help = "Seeds initial categories, products, and media assets if database catalog is empty"

    def handle(self, *args, **options):
        # 1. Ensure media directories exist
        media_root = Path(settings.MEDIA_ROOT)
        media_categories = media_root / 'categories'
        media_products = media_root / 'products'
        media_categories.mkdir(parents=True, exist_ok=True)
        media_products.mkdir(parents=True, exist_ok=True)

        # 2. Copy seed assets to MEDIA_ROOT if missing
        seed_root = Path(settings.BASE_DIR) / 'store' / 'seed_assets'
        if seed_root.exists():
            for sub in ['categories', 'products']:
                src_dir = seed_root / sub
                dest_dir = media_root / sub
                if src_dir.exists():
                    for item in src_dir.iterdir():
                        if item.is_file():
                            target = dest_dir / item.name
                            if not target.exists():
                                shutil.copy2(item, target)
                                self.stdout.write(f"Copied asset {item.name} to {dest_dir}")

        # 3. Seed Categories if empty
        if not Category.objects.exists():
            self.stdout.write("Catalog is empty. Seeding initial categories and products...")

            cat_ring, _ = Category.objects.get_or_create(
                name="Rings",
                defaults={
                    "slug": "rings",
                    "image": "categories/shopping.webp",
                    "description": "Bold, symbolic accessories that traditionally represent strength, loyalty, and freedom."
                }
            )

            cat_shades, _ = Category.objects.get_or_create(
                name="Shades",
                defaults={
                    "slug": "shades",
                    "image": "categories/images_1.jpg",
                    "description": "Modern UV protected trending eyewear and futuristic sunglasses."
                }
            )

            # Additional standard categories matching mannequin hotspots
            cat_caps, _ = Category.objects.get_or_create(
                name="Caps",
                defaults={
                    "slug": "caps",
                    "image": "categories/shopping.webp",
                    "description": "Vintage streetwear caps and aesthetic headwear."
                }
            )

            cat_chains, _ = Category.objects.get_or_create(
                name="Chains",
                defaults={
                    "slug": "chains",
                    "image": "categories/shopping.webp",
                    "description": "Pendant neck chains crafted for subtle elegance."
                }
            )

            cat_bracelets, _ = Category.objects.get_or_create(
                name="Bracelets",
                defaults={
                    "slug": "bracelets",
                    "image": "categories/shopping.webp",
                    "description": "Beaded stone and wrist accents."
                }
            )

            # Seed Products
            Product.objects.get_or_create(
                name="Wolf Ring",
                defaults={
                    "category": cat_ring,
                    "image": "products/21-1-men-s-wolf-head-ring-vintage-animal-rings-for-men-ring-the-original-imahf_jCodmlE.webp",
                    "price": 1000.00,
                    "discount_price": 899.00,
                    "description": "Wolf rings are bold, symbolic accessories that traditionally represent strength, loyalty, and freedom.",
                    "stock": 5
                }
            )

            Product.objects.get_or_create(
                name="Dark WOST Mc Stan Rimless Sunglasses",
                defaults={
                    "category": cat_shades,
                    "image": "products/shopping_1.webp",
                    "price": 400.00,
                    "discount_price": 250.00,
                    "description": "Iconic rimless streetwear sunglasses designed for bold everyday looks.",
                    "stock": 3
                }
            )

            Product.objects.get_or_create(
                name="ARZONAI Futuristic Series Wraparound Y2K Sunglasses",
                defaults={
                    "category": cat_shades,
                    "image": "products/shopping_2.webp",
                    "price": 250.00,
                    "discount_price": 199.00,
                    "description": "Futuristic Series Wraparound Y2K Sunglasses For Men & Women | UV Protected | Full Rim Trending & Stylish Shades | Free Size (Silver-Black)",
                    "stock": 3
                }
            )

            self.stdout.write(self.style.SUCCESS("Successfully seeded initial categories and products!"))
        else:
            self.stdout.write("Catalog already contains categories. Skipping seed.")
