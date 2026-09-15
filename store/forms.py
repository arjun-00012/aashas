from django import forms
from django.contrib.auth.models import User
from .models import Category, Product, Profile

class RegistrationForm(forms.ModelForm):
    phone_number = forms.CharField(max_length=20, required=True)
    password = forms.CharField(widget=forms.PasswordInput)
    confirm_password = forms.CharField(widget=forms.PasswordInput)

    class Meta:
        model = User
        fields = ['username', 'phone_number', 'password']

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
        fields = ['phone_number', 'address']

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
                from PIL import Image
                trial_image = Image.open(img)
                trial_image.verify()
            except Exception:
                raise forms.ValidationError("Please upload a valid image file (WebP, PNG, JPG, JPEG).")
        return img

class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = ['category', 'name', 'image', 'image_url', 'price', 'discount_price', 'description', 'stock', 'is_trending']
        widgets = {
            'category': forms.Select(attrs={'class': 'form-select'}),
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Wolf Ring, Noir Sunglasses'}),
            'image': forms.FileInput(attrs={'class': 'form-control', 'accept': 'image/*'}),
            'image_url': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://example.com/product.webp (Optional direct URL)'}),
            'price': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': '999'}),
            'discount_price': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': '799 (Leave empty if not on sale)'}),
            'stock': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': '10'}),
            'is_trending': forms.CheckboxInput(attrs={'class': 'form-check-input', 'role': 'switch'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': 'Product details, sizing, material...'}),
        }
        help_texts = {
            'image': 'Recommended: 1000 × 1000 px to 1200 × 1200 px (1:1 Square). Max 5 MB. Formats: WebP, PNG, JPG.',
            'image_url': 'Direct high-res link to product image (e.g. Cloudinary, CDN, Imgur).',
            'is_trending': 'Feature this item prominently in the homepage Trending Now section.',
        }

    def clean_image(self):
        img = self.cleaned_data.get('image')
        if img:
            max_bytes = 5 * 1024 * 1024  # 5MB
            if hasattr(img, 'size') and img.size > max_bytes:
                size_mb = img.size / (1024 * 1024)
                raise forms.ValidationError(f"File size too large ({size_mb:.1f} MB). Maximum allowed size is 5 MB.")
            try:
                from PIL import Image
                trial_image = Image.open(img)
                trial_image.verify()
            except Exception:
                raise forms.ValidationError("Please upload a valid image file (WebP, PNG, JPG, JPEG).")
        return img