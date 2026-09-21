from django.db import models
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils.text import slugify

class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    phone_number = models.CharField(max_length=20, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    pincode = models.CharField(max_length=10, blank=True, null=True, help_text="Postal PIN Code")
    city = models.CharField(max_length=100, blank=True, null=True, help_text="City / District")
    state = models.CharField(max_length=100, blank=True, null=True, help_text="State")

    def __str__(self):
        return f"{self.user.username}'s Profile"

@receiver(post_save, sender=User)
def create_or_update_user_profile(sender, instance, created, **kwargs):
    if created:
        Profile.objects.create(user=instance)
    else:
        if hasattr(instance, 'profile'):
            instance.profile.save()


def optimize_image_file(image_field, max_dimension=1200):
    """
    Downscale oversized uploaded images to max_dimension (maintaining aspect ratio)
    and compress cleanly using Pillow to protect server performance and speed up loading.
    """
    if not image_field:
        return
    try:
        import os
        from PIL import Image
        filepath = None
        try:
            filepath = getattr(image_field, 'path', None)
        except (NotImplementedError, AttributeError, ValueError):
            filepath = None

        if filepath and os.path.exists(filepath):
            with Image.open(filepath) as img:
                w, h = img.size
                if w > max_dimension or h > max_dimension:
                    img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
                    save_fmt = img.format or ('PNG' if img.mode in ('RGBA', 'LA', 'P') else 'JPEG')
                    img.save(filepath, format=save_fmt, quality=85, optimize=True)
    except Exception:
        pass


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=120, unique=True, blank=True)
    image = models.ImageField(upload_to='categories/', blank=True, null=True)
    image_url = models.URLField(max_length=1000, blank=True, null=True, help_text="Direct link to category image (optional)")
    description = models.TextField(blank=True, null=True)

    class Meta:
        verbose_name_plural = 'Categories'

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name) or 'category'
            slug = base_slug
            counter = 1
            while Category.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
            self.slug = slug

        # Reset image file pointer if present before saving
        if self.image:
            try:
                if hasattr(self.image, 'seek'):
                    self.image.seek(0)
            except Exception:
                pass
            try:
                if hasattr(self.image, 'file') and hasattr(self.image.file, 'seek'):
                    self.image.file.seek(0)
            except Exception:
                pass

        super().save(*args, **kwargs)

        # Optimize image dimensions if uploaded locally
        if self.image:
            optimize_image_file(self.image, max_dimension=1000)

        # Automatically record Cloudinary secure URL if uploaded to Cloudinary
        if self.image and not self.image_url:
            try:
                img_url = ''
                try:
                    img_url = str(self.image.url)
                except Exception:
                    img_url = ''
                if img_url and img_url.startswith(('http://', 'https://')):
                    self.image_url = img_url
                    super().save(update_fields=['image_url'])
                else:
                    import os
                    import cloudinary.uploader
                    filepath = None
                    try:
                        filepath = self.image.path
                    except (NotImplementedError, AttributeError, ValueError):
                        filepath = None

                    if filepath and os.path.exists(filepath):
                        res = cloudinary.uploader.upload(
                            filepath,
                            folder="ashas/categories",
                            public_id=self.slug,
                            overwrite=True
                        )
                        secure_url = res.get('secure_url')
                        if secure_url:
                            self.image_url = secure_url
                            super().save(update_fields=['image_url'])
                    else:
                        try:
                            if hasattr(self.image, 'file') and hasattr(self.image.file, 'seek'):
                                self.image.file.seek(0)
                                res = cloudinary.uploader.upload(
                                    self.image.file,
                                    folder="ashas/categories",
                                    public_id=self.slug,
                                    overwrite=True
                                )
                                secure_url = res.get('secure_url')
                                if secure_url:
                                    self.image_url = secure_url
                                    super().save(update_fields=['image_url'])
                        except Exception:
                            pass
            except Exception:
                pass

    @property
    def display_image(self):
        # Prioritize direct cloud link if available
        if self.image_url:
            return self.image_url
        if self.image:
            try:
                return self.image.url
            except Exception:
                pass
        return ''

    def __str__(self):
        return self.name


class Product(models.Model):
    category = models.ForeignKey(Category, related_name='products', on_delete=models.CASCADE)
    name = models.CharField(max_length=200)
    image = models.ImageField(upload_to='products/', blank=True, null=True)
    image_url = models.URLField(max_length=1000, blank=True, null=True, help_text="Direct link to product image (optional)")
    price = models.DecimalField(max_digits=10, decimal_places=2)
    discount_price = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    stock = models.PositiveIntegerField(default=1)
    is_trending = models.BooleanField(default=False, db_index=True, help_text="Showcase this product in the Trending section")
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        # Reset image file pointer if present before saving
        if self.image:
            try:
                if hasattr(self.image, 'seek'):
                    self.image.seek(0)
            except Exception:
                pass
            try:
                if hasattr(self.image, 'file') and hasattr(self.image.file, 'seek'):
                    self.image.file.seek(0)
            except Exception:
                pass

        super().save(*args, **kwargs)

        # Optimize image dimensions if uploaded locally
        if self.image:
            optimize_image_file(self.image, max_dimension=1200)

        # Automatically record Cloudinary secure URL if uploaded to Cloudinary
        if self.image and not self.image_url:
            try:
                img_url = ''
                try:
                    img_url = str(self.image.url)
                except Exception:
                    img_url = ''
                if img_url and img_url.startswith(('http://', 'https://')):
                    self.image_url = img_url
                    super().save(update_fields=['image_url'])
                else:
                    import os
                    import cloudinary.uploader
                    filepath = None
                    try:
                        filepath = self.image.path
                    except (NotImplementedError, AttributeError, ValueError):
                        filepath = None

                    if filepath and os.path.exists(filepath):
                        res = cloudinary.uploader.upload(
                            filepath,
                            folder="ashas/products",
                            public_id=f"product_{self.id}",
                            overwrite=True
                        )
                        secure_url = res.get('secure_url')
                        if secure_url:
                            self.image_url = secure_url
                            super().save(update_fields=['image_url'])
                    else:
                        try:
                            if hasattr(self.image, 'file') and hasattr(self.image.file, 'seek'):
                                self.image.file.seek(0)
                                res = cloudinary.uploader.upload(
                                    self.image.file,
                                    folder="ashas/products",
                                    public_id=f"product_{self.id}",
                                    overwrite=True
                                )
                                secure_url = res.get('secure_url')
                                if secure_url:
                                    self.image_url = secure_url
                                    super().save(update_fields=['image_url'])
                        except Exception:
                            pass
            except Exception:
                pass

    @property
    def current_price(self):
        return self.discount_price if self.discount_price else self.price

    @property
    def display_image(self):
        # Prioritize direct cloud link if available
        if self.image_url:
            return self.image_url
        if self.image:
            try:
                return self.image.url
            except Exception:
                pass
        return ''

    def __str__(self):
        return self.name


class Order(models.Model):
    STATUS_CHOICES = (
        ('Pending', 'Pending'),
        ('Completed', 'Completed'),
        ('Failed', 'Failed'),
    )
    SHIPPING_STATUS_CHOICES = (
        ('Processing', 'Processing / Packed'),
        ('Dispatched', 'Dispatched via Post Office'),
        ('In Transit', 'In Transit'),
        ('Out for Delivery', 'Out for Delivery'),
        ('Delivered', 'Delivered'),
    )
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='orders')
    full_name = models.CharField(max_length=200)
    phone_number = models.CharField(max_length=20)
    shipping_address = models.TextField()
    pincode = models.CharField(max_length=10, blank=True, null=True, help_text="Postal PIN Code")
    city = models.CharField(max_length=100, blank=True, null=True, help_text="City / District")
    state = models.CharField(max_length=100, blank=True, null=True, help_text="State")
    delivery_region = models.CharField(max_length=50, blank=True, null=True, default='kerala', help_text="Inside Kerala vs Outside Kerala")
    total_price = models.DecimalField(max_digits=10, decimal_places=2)
    shipping_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, help_text="Delivery charge based on destination and products")
    razorpay_order_id = models.CharField(max_length=100, blank=True, null=True)
    razorpay_payment_id = models.CharField(max_length=100, blank=True, null=True)
    payment_status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Pending')

    # Tracking Details (Updated manually by Admin)
    tracking_id = models.CharField(max_length=100, blank=True, null=True, help_text="India Post / Speed Post consignment tracking number")
    carrier = models.CharField(max_length=100, default='India Post', blank=True, help_text="Courier service or Post Office branch")
    shipping_status = models.CharField(max_length=50, choices=SHIPPING_STATUS_CHOICES, default='Processing', blank=True)
    tracking_notes = models.CharField(max_length=255, blank=True, null=True, help_text="Dispatch notes (e.g. Dispatched from Karuvissery Post Office)")
    tracking_updated_at = models.DateTimeField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def tracking_url(self):
        """Official India Post tracking URL"""
        if self.tracking_id:
            return "https://www.indiapost.gov.in/_layouts/15/dpt.cept.tracking/trackconsignment.aspx"
        return ""

    @property
    def whatsapp_notification_url(self):
        """Generates pre-formatted WhatsApp message link for admin to notify customer in 1 click"""
        if not self.phone_number:
            return ""
        import urllib.parse
        import re
        digits = re.sub(r'\D', '', str(self.phone_number))
        if len(digits) == 11 and digits.startswith('0'):
            digits = '91' + digits[1:]
        elif len(digits) == 10:
            digits = '91' + digits
        
        tracking_info = self.tracking_id or 'Will be updated shortly'
        carrier_name = self.carrier or 'India Post (Speed Post)'
        status_name = self.get_shipping_status_display() if hasattr(self, 'get_shipping_status_display') else self.shipping_status

        msg = (
            f"Hello {self.full_name},\n\n"
            f"Your ASHAS order #{self.id} has an update!\n"
            f"📦 Carrier: {carrier_name}\n"
            f"🏷️ Tracking ID: {tracking_info}\n"
            f"🚚 Status: {status_name}\n\n"
            f"Track your parcel on India Post:\n"
            f"https://www.indiapost.gov.in/_layouts/15/dpt.cept.tracking/trackconsignment.aspx\n\n"
            f"Thank you for choosing ASHAS!\n"
            f"Boutique Helpline: +91 82814 51481"
        )
        return f"https://wa.me/{digits}?text={urllib.parse.quote(msg)}"

    def __str__(self):
        return f"Order #{self.id} - {self.full_name} ({self.payment_status})"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, related_name='items', on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    quantity = models.PositiveIntegerField(default=1)

    def __str__(self):
        return f"{self.quantity} x {self.product.name}"


class ContactMessage(models.Model):
    name = models.CharField(max_length=150)
    email = models.EmailField()
    subject = models.CharField(max_length=200)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} - {self.subject}"


class CartItem(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, related_name='cart_items')
    session_key = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='in_carts')
    quantity = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        owner = self.user.username if self.user else f"Guest ({self.session_key})"
        return f"{self.quantity}x {self.product.name} ({owner})"

    @property
    def subtotal(self):
        return float(self.product.current_price) * self.quantity