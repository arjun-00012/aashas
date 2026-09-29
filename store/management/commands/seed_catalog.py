import os
import shutil
from pathlib import Path
from django.core.management.base import BaseCommand
from django.core.management import call_command
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
            fixture_file = Path(settings.BASE_DIR) / 'store' / 'fixtures' / 'catalog.json'
            if fixture_file.exists():
                try:
                    self.stdout.write(f"Loading catalog records from {fixture_file}...")
                    call_command('loaddata', str(fixture_file))
                    self.stdout.write(self.style.SUCCESS("Successfully seeded catalog from fixture!"))
                    return
                except Exception as e:
                    self.stdout.write(self.style.WARNING(f"Could not load fixture: {e}. Falling back to default seed."))

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

            # Seed Products with full 4-photo galleries for carousel display
            Product.objects.get_or_create(
                name="Wolf Ring",
                defaults={
                    "category": cat_ring,
                    "image": "products/21-1-men-s-wolf-head-ring-vintage-animal-rings-for-men-ring-the-original-imahf_jCodmlE.webp",
                    "image_2": "products/ring_signet_macro.jpg",
                    "image_3_url": "https://res.cloudinary.com/dwk9pw2ol/image/upload/v1789014516/ashas/products/product_9.webp",
                    "image_4": "products/look_3.jpg",
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
                    "image_2": "products/shades_angle_shot.jpg",
                    "image_3": "products/shades_on_model.jpg",
                    "image_4": "products/look_1.jpg",
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
                    "image_2": "products/shades_angle_shot.jpg",
                    "image_3": "products/shades_on_model.jpg",
                    "image_4": "products/look_7.jpg",
                    "price": 250.00,
                    "discount_price": 199.00,
                    "description": "Futuristic Series Wraparound Y2K Sunglasses For Men & Women | UV Protected | Full Rim Trending & Stylish Shades | Free Size (Silver-Black)",
                    "stock": 3
                }
            )

            Product.objects.get_or_create(
                name="ASHAS Obsidian Heavy Cuban Chain",
                defaults={
                    "category": cat_chains,
                    "image": "products/ashas_obsidian_cuban_chain.jpg",
                    "image_2": "products/chain_neck_model.jpg",
                    "image_3": "products/look_6.jpg",
                    "image_4": "products/look_2.jpg",
                    "price": 1499.00,
                    "discount_price": 1199.00,
                    "description": "Heavy Cuban link chain and brutalist curb necklace engineered with rust-proof durability and polished finish.",
                    "stock": 5
                }
            )

            self.stdout.write(self.style.SUCCESS("Successfully seeded initial categories and products!"))
        else:
            self.stdout.write("Catalog already contains categories. Skipping seed.")
