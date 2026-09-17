import os
from pathlib import Path
from django.core.management.base import BaseCommand
from django.conf import settings
from store.models import Category, Product
import cloudinary
import cloudinary.uploader

class Command(BaseCommand):
    help = "Uploads media and category images to Cloudinary (folder 'ashas') and updates database image_url fields"

    def handle(self, *args, **options):
        cloud_name = getattr(settings, 'CLOUDINARY_CLOUD_NAME', 'dwk9pw2ol')
        api_key = getattr(settings, 'CLOUDINARY_API_KEY', '934762869273294')
        api_secret = getattr(settings, 'CLOUDINARY_API_SECRET', 'bu1cNNgmT6U29O9uPTIM2yyAqFs')

        cloudinary.config(
            cloud_name=cloud_name,
            api_key=api_key,
            api_secret=api_secret,
            secure=True
        )

        self.stdout.write(f"Connecting to Cloudinary ({cloud_name})...")

        # 1. Process Categories
        categories = Category.objects.all()
        self.stdout.write(f"Syncing {categories.count()} categories...")
        for cat in categories:
            local_path = None
            if cat.image and hasattr(cat.image, 'path') and os.path.exists(cat.image.path):
                local_path = cat.image.path
            else:
                static_alt = Path(settings.BASE_DIR) / 'store' / 'static' / 'images' / 'categories' / f"{cat.slug}.jpg"
                if static_alt.exists():
                    local_path = str(static_alt)

            if local_path:
                try:
                    self.stdout.write(f"Uploading category image for '{cat.name}' from {local_path}...")
                    res = cloudinary.uploader.upload(
                        local_path,
                        folder="ashas/categories",
                        public_id=cat.slug,
                        overwrite=True,
                        resource_type="image"
                    )
                    secure_url = res.get('secure_url')
                    cat.image_url = secure_url
                    cat.save(update_fields=['image_url'])
                    self.stdout.write(self.style.SUCCESS(f"[OK] '{cat.name}' -> {secure_url}"))
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"[ERROR] Failed uploading '{cat.name}': {e}"))
            else:
                self.stdout.write(self.style.WARNING(f"[SKIP] No local image found for '{cat.name}'"))

        # 2. Process Products
        products = Product.objects.all()
        self.stdout.write(f"Syncing {products.count()} products...")
        for prod in products:
            local_path = None
            if prod.image and hasattr(prod.image, 'path') and os.path.exists(prod.image.path):
                local_path = prod.image.path
            
            if local_path:
                try:
                    self.stdout.write(f"Uploading product image for '{prod.name}'...")
                    slug_id = f"product_{prod.id}"
                    res = cloudinary.uploader.upload(
                        local_path,
                        folder="ashas/products",
                        public_id=slug_id,
                        overwrite=True,
                        resource_type="image"
                    )
                    secure_url = res.get('secure_url')
                    prod.image_url = secure_url
                    prod.save(update_fields=['image_url'])
                    self.stdout.write(self.style.SUCCESS(f"[OK] '{prod.name}' -> {secure_url}"))
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"[ERROR] Failed uploading '{prod.name}': {e}"))
            else:
                self.stdout.write(self.style.WARNING(f"[SKIP] No local image found for product '{prod.name}'"))

        self.stdout.write(self.style.SUCCESS("Cloudinary sync process finished!"))
