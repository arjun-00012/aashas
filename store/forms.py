from django import forms
from django.contrib.auth.models import User
from .models import Category, Product, Profile

class RegistrationForm(forms.ModelForm):
    email = forms.EmailField(required=True, label="Email Address")
    phone_number = forms.CharField(max_length=20, required=True)
    password = forms.CharField(widget=forms.PasswordInput)
    confirm_password = forms.CharField(widget=forms.PasswordInput)

    class Meta:
        model = User
        fields = ['username', 'email', 'phone_number', 'password']

    def clean_email(self):
        email = self.cleaned_data.get('email', '').strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email address already exists. Please sign in or use forgot password.")
        return email

    def clean_phone_number(self):
        phone = self.cleaned_data.get('phone_number', '').strip()
        import re
        digits = re.sub(r'\D', '', phone)
        if len(digits) < 10 or len(digits) > 13:
            raise forms.ValidationError("Please enter a valid 10-digit mobile number.")
        return phone

    def clean(self):
        cleaned_data = super().clean()
        p1 = cleaned_data.get("password")
        p2 = cleaned_data.get("confirm_password")
        if p1 and p2 and p1 != p2:
            raise forms.ValidationError("Passwords do not match.")
        return cleaned_data

class ProfileUpdateForm(forms.ModelForm):
    class Meta:
        model = Profile
        fields = ['phone_number', 'address', 'pincode', 'city', 'state']

    def clean_phone_number(self):
        phone = self.cleaned_data.get('phone_number', '').strip()
        if phone:
            import re
            digits = re.sub(r'\D', '', phone)
            if len(digits) < 10 or len(digits) > 13:
                raise forms.ValidationError("Please enter a valid 10-digit mobile number.")
        return phone

    def clean_pincode(self):
        pin = (self.cleaned_data.get('pincode') or '').strip()
        if pin:
            import re
            digits = re.sub(r'\D', '', pin)
            if len(digits) != 6:
                raise forms.ValidationError("Please enter a valid 6-digit Indian Postal PIN code.")
            return digits
        return pin

class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ['name', 'image', 'image_url', 'description']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Rings, Shades, Caps'}),
            'image': forms.FileInput(attrs={'class': 'form-control', 'accept': 'image/*'}),
            'image_url': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://example.com/image.webp (Optional direct URL)'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': 'Category description...'}),
        }
        help_texts = {
            'image': 'Recommended: 800 × 800 px (1:1 Square). Max 5 MB. Formats: WebP, PNG, JPG.',
            'image_url': 'Direct high-res link to category icon or photo (optional fallback).',
        }

    def clean_image(self):
        img = self.cleaned_data.get('image')
        if img:
            max_bytes = 5 * 1024 * 1024  # 5MB
            if hasattr(img, 'size') and img.size > max_bytes:
                size_mb = img.size / (1024 * 1024)
                raise forms.ValidationError(f"File size too large ({size_mb:.1f} MB). Maximum allowed size is 5 MB.")
            try:
                if hasattr(img, 'seek'):
                    img.seek(0)
                if hasattr(img, 'file') and hasattr(img.file, 'seek'):
                    try:
                        img.file.seek(0)
                    except Exception:
                        pass
                from PIL import Image
                trial_image = Image.open(img)
                trial_image.verify()
            except Exception:
                raise forms.ValidationError("Please upload a valid image file (WebP, PNG, JPG, JPEG).")
            finally:
                if hasattr(img, 'seek'):
                    img.seek(0)
                if hasattr(img, 'file') and hasattr(img.file, 'seek'):
                    try:
                        img.file.seek(0)
                    except Exception:
                        pass
        return img

class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = [
            'category', 'name', 'admin_code',
            'image', 'image_url', 
            'image_2', 'image_2_url', 
            'image_3', 'image_3_url', 
            'image_4', 'image_4_url', 
            'price', 'discount_price', 'description', 'stock', 'is_trending'
        ]
        widgets = {
            'category': forms.Select(attrs={'class': 'form-select'}),
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Wolf Ring, Noir Sunglasses'}),
            'admin_code': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. 100, 101 (Leave blank to auto-generate)'}),
            'image': forms.FileInput(attrs={'class': 'form-control', 'accept': 'image/*'}),
            'image_url': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://example.com/photo-1.webp (Optional direct URL)'}),
            'image_2': forms.FileInput(attrs={'class': 'form-control', 'accept': 'image/*'}),
            'image_2_url': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://example.com/photo-2.webp (Optional direct URL)'}),
            'image_3': forms.FileInput(attrs={'class': 'form-control', 'accept': 'image/*'}),
            'image_3_url': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://example.com/photo-3.webp (Optional direct URL)'}),
            'image_4': forms.FileInput(attrs={'class': 'form-control', 'accept': 'image/*'}),
            'image_4_url': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://example.com/photo-4.webp (Optional direct URL)'}),
            'price': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': '999'}),
            'discount_price': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': '799 (Leave empty if not on sale)'}),
            'stock': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': '10'}),
            'is_trending': forms.CheckboxInput(attrs={'class': 'form-check-input', 'role': 'switch'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': 'Product details, sizing, material...'}),
        }
        help_texts = {
            'admin_code': 'Internal shipping code visible ONLY to admin. Auto-assigned sequentially (100, 101, etc.) if left blank.',
            'image': 'Photo 1 (Cover / Main Photo) - Recommended: 1000 × 1000 px (1:1 Square). Max 5 MB.',
            'image_url': 'Direct high-res link to Photo 1 (e.g. Cloudinary, CDN, Imgur).',
            'image_2': 'Photo 2 (Side / Angle View) - Recommended: 1:1 Square. Max 5 MB.',
            'image_2_url': 'Direct link to Photo 2 (optional).',
            'image_3': 'Photo 3 (On-Model / Wearing View) - Recommended: 1:1 Square. Max 5 MB.',
            'image_3_url': 'Direct link to Photo 3 (optional).',
            'image_4': 'Photo 4 (Detail / Packaging View) - Recommended: 1:1 Square. Max 5 MB.',
            'image_4_url': 'Direct link to Photo 4 (optional).',
            'is_trending': 'Feature this item prominently in the homepage Trending Now section.',
        }

    def clean_admin_code(self):
        code = (self.cleaned_data.get('admin_code') or '').strip()
        if code:
            qs = Product.objects.filter(admin_code=code)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError(f"Admin shipping code '{code}' is already assigned to another product.")
        return code or None

    def _validate_image_field(self, field_name):
        img = self.cleaned_data.get(field_name)
        if img:
            max_bytes = 5 * 1024 * 1024  # 5MB
            if hasattr(img, 'size') and img.size > max_bytes:
                size_mb = img.size / (1024 * 1024)
                raise forms.ValidationError(f"File size too large ({size_mb:.1f} MB). Maximum allowed size is 5 MB.")
            try:
                if hasattr(img, 'seek'):
                    img.seek(0)
                if hasattr(img, 'file') and hasattr(img.file, 'seek'):
                    try:
                        img.file.seek(0)
                    except Exception:
                        pass
                from PIL import Image
                trial_image = Image.open(img)
                trial_image.verify()
            except Exception:
                raise forms.ValidationError("Please upload a valid image file (WebP, PNG, JPG, JPEG).")
            finally:
                if hasattr(img, 'seek'):
                    img.seek(0)
                if hasattr(img, 'file') and hasattr(img.file, 'seek'):
                    try:
                        img.file.seek(0)
                    except Exception:
                        pass
        return img

    def clean_image(self):
        return self._validate_image_field('image')

    def clean_image_2(self):
        return self._validate_image_field('image_2')

    def clean_image_3(self):
        return self._validate_image_field('image_3')

    def clean_image_4(self):
        return self._validate_image_field('image_4')

    def clean(self):
        cleaned_data = super().clean()
        price = cleaned_data.get('price')
        discount_price = cleaned_data.get('discount_price')

        if price is not None and price <= 0:
            self.add_error('price', 'Price must be greater than zero.')

        if discount_price is not None:
            if discount_price <= 0:
                self.add_error('discount_price', 'Discount price must be greater than zero.')
            elif price is not None and discount_price >= price:
                self.add_error('discount_price', 'Discount price must be lower than the original regular price.')

        return cleaned_data