import os
import re
import json
import secrets
from decimal import Decimal
from urllib.parse import quote as urlquote
from xml.sax.saxutils import escape as xml_escape
import razorpay
import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill
from django.conf import settings
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponse, Http404
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.contrib.auth.tokens import default_token_generator
from django.contrib import messages
from django.core.mail import send_mail
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.utils.http import url_has_allowed_host_and_scheme, urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str
from .models import Category, Product, ProductSize, Order, OrderItem, Profile, ContactMessage, CartItem
from .forms import RegistrationForm, ProfileUpdateForm, CategoryForm, ProductForm

import razorpay
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver

def safe_referer(request, fallback='home'):
    referer = request.META.get('HTTP_REFERER')
    if referer and url_has_allowed_host_and_scheme(url=referer, allowed_hosts={request.get_host()}):
        return referer
    return fallback

from functools import wraps

def staff_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('/login/?next=' + request.path)
        if not (request.user.is_staff or request.user.is_superuser):
            messages.error(request, "Access restricted: Staff privileges are required to access the Admin Portal.")
            return redirect('home')
        return view_func(request, *args, **kwargs)
    return wrapper

def get_razorpay_client():
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))

razorpay_client = get_razorpay_client()

def parse_cart_key(item_key):
    """
    Parses '12:L' into (12, 'L') or '12' into (12, None).
    """
    if not item_key:
        return None, None
    s = str(item_key).strip()
    if ':' in s:
        parts = s.split(':', 1)
        try:
            return int(parts[0]), parts[1].strip().upper()
        except (ValueError, TypeError):
            return None, None
    try:
        return int(s), None
    except (ValueError, TypeError):
        return None, None


def deduct_order_inventory(order):
    """
    Safely decrements inventory for all items in an order.
    Decrements specific size variant stock if item has a size, and synchronizes product.stock.
    """
    for item in order.items.all():
        if not item.product:
            continue
        p = item.product
        if item.size:
            ps = ProductSize.objects.filter(product=p, size=item.size).first()
            if ps:
                ps.stock = max(0, ps.stock - item.quantity)
                ps.save(update_fields=['stock'])
            p.sync_stock_from_sizes()
        else:
            p.stock = max(0, p.stock - item.quantity)
            p.save(update_fields=['stock'])


def restock_order_inventory(order):
    """
    Safely restores inventory for refunded or cancelled orders.
    Restores specific size variant stock if item has a size, and synchronizes product.stock.
    """
    for item in order.items.all():
        if not item.product:
            continue
        p = item.product
        if item.size:
            ps = ProductSize.objects.filter(product=p, size=item.size).first()
            if ps:
                ps.stock += item.quantity
                ps.save(update_fields=['stock'])
            p.sync_stock_from_sizes()
        else:
            p.stock += item.quantity
            p.save(update_fields=['stock'])


@receiver(user_logged_in)
def handle_user_logged_in(sender, request, user, **kwargs):
    if request:
        guest_cart = request.session.get('cart', {})
        items = CartItem.objects.filter(user=user)
        user_items = {}
        for item in items:
            if item.quantity > 0:
                k = f"{item.product_id}:{item.size}" if item.size else str(item.product_id)
                user_items[k] = item.quantity
        
        # Merge guest cart into user's DB items
        if guest_cart:
            for k, qty in guest_cart.items():
                if qty > 0:
                    pid, size = parse_cart_key(k)
                    if pid:
                        product = Product.objects.filter(id=pid).first()
                        if product:
                            max_avail = product.stock
                            if size and product.has_sizes:
                                ps = ProductSize.objects.filter(product=product, size=size).first()
                                max_avail = ps.stock if ps else 0
                            if max_avail > 0:
                                merged_qty = min(user_items.get(k, 0) + qty, max_avail)
                                CartItem.objects.update_or_create(
                                    user=user,
                                    product=product,
                                    size=size,
                                    defaults={'quantity': merged_qty}
                                )
                                user_items[k] = merged_qty

        request.session['cart'] = user_items
        request.session.modified = True

@receiver(user_logged_out)
def handle_user_logged_out(sender, request, user, **kwargs):
    if request:
        request.session.flush()


def get_user_cart(request):
    """
    Returns a dict {item_key: int(quantity)} strictly isolated for current user/session.
    item_key is either "{product_id}:{size}" (e.g. "12:L") or "{product_id}" (e.g. "12").
    If authenticated: fetches from CartItem DB model.
    If guest: uses request.session.
    """
    if request.user.is_authenticated:
        items = CartItem.objects.filter(user=request.user)
        cart = {}
        for item in items:
            if item.quantity > 0:
                key = f"{item.product_id}:{item.size}" if item.size else str(item.product_id)
                cart[key] = item.quantity
        request.session['cart'] = cart
        return cart
    return request.session.get('cart', {})

def sync_cart_item(request, product, quantity, size=None):
    """
    Persists cart change to DB if authenticated, and keeps session up-to-date.
    Supports size variant or standard product.
    If quantity <= 0, deletes from DB.
    """
    size = (size or '').strip().upper() if size else None
    key = f"{product.id}:{size}" if size else str(product.id)
    cart = request.session.get('cart', {})
    if quantity > 0:
        cart[key] = quantity
        if request.user.is_authenticated:
            CartItem.objects.update_or_create(
                user=request.user,
                product=product,
                size=size,
                defaults={'quantity': quantity}
            )
    else:
        cart.pop(key, None)
        if request.user.is_authenticated:
            CartItem.objects.filter(user=request.user, product=product, size=size).delete()
    request.session['cart'] = cart
    request.session.modified = True

def cart_context_processor(request):
    if request.user.is_authenticated:
        items = CartItem.objects.filter(user=request.user)
        total_qty = sum(item.quantity for item in items)
    else:
        cart = request.session.get('cart', {})
        total_qty = sum(int(v) for v in cart.values() if str(v).isdigit()) if cart else 0
    return {
        'cart_item_count': total_qty,
        'all_categories': Category.objects.all(),
    }

def ping_view(request):
    """Ultra-lightweight endpoint for uptime monitors; returns diagnostics on /health/."""
    if 'health' in request.path:
        from django.conf import settings
        db_engine = settings.DATABASES['default']['ENGINE'].split('.')[-1]
        has_db_url = bool(os.environ.get('DATABASE_URL'))
        is_postgres = 'postgres' in db_engine or has_db_url
        return JsonResponse({
            'status': 'OK',
            'engine': db_engine,
            'is_postgres': is_postgres,
            'has_database_url': has_db_url,
            'order_count': Order.objects.count(),
        })
    return HttpResponse("OK", content_type="text/plain", status=200)

def robots_txt_view(request):
    """Serve SEO-friendly robots.txt directing Googlebot to the sitemap and allowing full crawling."""
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /adminpp/",
        "Disallow: /admin/",
        "Disallow: /django-admin/",
        "Disallow: /cart/",
        "Disallow: /checkout/",
        "Disallow: /profile/",
        "Disallow: /verify-payment/",
        "",
        "User-agent: Googlebot",
        "Allow: /",
        "",
        "User-agent: Googlebot-Image",
        "Allow: /",
        "Allow: /static/",
        "Allow: /media/",
        "",
        "Sitemap: https://ashasstore.in/sitemap.xml",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain; charset=utf-8")

def sitemap_xml_view(request):
    """Generate dynamic XML sitemap with Google Image Sitemaps for Google Search Console and crawlers."""
    now_str = timezone.now().strftime('%Y-%m-%d')
    urls = [
        {
            'loc': 'https://ashasstore.in/',
            'priority': '1.0',
            'changefreq': 'daily',
            'images': [
                {
                    'loc': 'https://ashasstore.in/static/images/hero_model_desktop.jpg',
                    'title': 'ASHAS STORE | Contemporary Clothing & Curated Statement Accessories in Kozhikode',
                    'caption': 'ASHAS Signature Model wearing curated luxury statement accessories, shades, cooling glass, chains, and rings in Kozhikode, Kerala.'
                }
            ]
        },
        {'loc': 'https://ashasstore.in/rings/', 'priority': '0.95', 'changefreq': 'daily', 'images': []},
        {'loc': 'https://ashasstore.in/shades/', 'priority': '0.95', 'changefreq': 'daily', 'images': []},
        {'loc': 'https://ashasstore.in/cooling-glass/', 'priority': '0.95', 'changefreq': 'daily', 'images': []},
        {'loc': 'https://ashasstore.in/cooling-glasses/', 'priority': '0.95', 'changefreq': 'daily', 'images': []},
        {'loc': 'https://ashasstore.in/chains/', 'priority': '0.95', 'changefreq': 'daily', 'images': []},
        {'loc': 'https://ashasstore.in/tribal/', 'priority': '0.95', 'changefreq': 'daily', 'images': []},
        {'loc': 'https://ashasstore.in/tribal-jewelry/', 'priority': '0.95', 'changefreq': 'daily', 'images': []},
        {'loc': 'https://ashasstore.in/tribal-accessories/', 'priority': '0.95', 'changefreq': 'daily', 'images': []},
        {'loc': 'https://ashasstore.in/accessories/', 'priority': '0.95', 'changefreq': 'daily', 'images': []},
        {'loc': 'https://ashasstore.in/watches/', 'priority': '0.85', 'changefreq': 'weekly', 'images': []},
        {'loc': 'https://ashasstore.in/bracelets/', 'priority': '0.85', 'changefreq': 'weekly', 'images': []},
        {'loc': 'https://ashasstore.in/caps/', 'priority': '0.85', 'changefreq': 'weekly', 'images': []},
        {'loc': 'https://ashasstore.in/belts/', 'priority': '0.85', 'changefreq': 'weekly', 'images': []},
        {'loc': 'https://ashasstore.in/terms/', 'priority': '0.70', 'changefreq': 'monthly', 'images': []},
        {'loc': 'https://ashasstore.in/privacy/', 'priority': '0.70', 'changefreq': 'monthly', 'images': []},
        {'loc': 'https://ashasstore.in/refund-policy/', 'priority': '0.70', 'changefreq': 'monthly', 'images': []},
        {'loc': 'https://ashasstore.in/shipping-policy/', 'priority': '0.70', 'changefreq': 'monthly', 'images': []},
        {'loc': 'https://ashasstore.in/contact/', 'priority': '0.80', 'changefreq': 'monthly', 'images': []},
    ]
    for cat in Category.objects.all():
        cat_url = f'https://ashasstore.in/category/{cat.slug}/'
        if not any(u['loc'] == cat_url for u in urls):
            urls.append({
                'loc': cat_url,
                'priority': '0.85',
                'changefreq': 'daily',
                'images': []
            })
    for prod in Product.objects.select_related('category').all():
        prod_images = []
        for img_url in prod.gallery_images:
            if img_url:
                full_img_url = img_url if img_url.startswith('http') else f'https://ashasstore.in{img_url}'
                prod_images.append({
                    'loc': full_img_url,
                    'title': f'{prod.name} | ASHAS Store Kozhikode',
                    'caption': f'Buy {prod.name} online at ASHAS Store in Kozhikode, Kerala. Premium accessories, shades, cooling glass, chains, and tribal jewelry.'
                })
        urls.append({
            'loc': f'https://ashasstore.in/product/{prod.id}/',
            'priority': '0.9',
            'changefreq': 'daily',
            'images': prod_images
        })

    xml = ['<?xml version="1.0" encoding="UTF-8"?>']
    xml.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">')
    for u in urls:
        xml.append('  <url>')
        xml.append(f'    <loc>{xml_escape(u["loc"])}</loc>')
        xml.append(f'    <lastmod>{now_str}</lastmod>')
        xml.append(f'    <changefreq>{u["changefreq"]}</changefreq>')
        xml.append(f'    <priority>{u["priority"]}</priority>')
        for img in u.get('images', []):
            xml.append('    <image:image>')
            xml.append(f'      <image:loc>{xml_escape(img["loc"])}</image:loc>')
            xml.append(f'      <image:title>{xml_escape(img["title"])}</image:title>')
            xml.append(f'      <image:caption>{xml_escape(img["caption"])}</image:caption>')
            xml.append('    </image:image>')
        xml.append('  </url>')
    xml.append('</urlset>')

    return HttpResponse('\n'.join(xml), content_type="application/xml; charset=utf-8")

def favicon_view(request):
    """Serve root /favicon.ico directly for Google Search Favicon Crawler and browsers."""
    ico_path = os.path.join(settings.BASE_DIR, 'store', 'static', 'favicon.ico')
    png_path = os.path.join(settings.BASE_DIR, 'store', 'static', 'images', 'favicon.png')
    target_path = ico_path if os.path.exists(ico_path) else png_path
    if os.path.exists(target_path):
        with open(target_path, 'rb') as f:
            content_type = "image/x-icon" if target_path.endswith('.ico') else "image/png"
            return HttpResponse(f.read(), content_type=content_type)
    return HttpResponse(status=404)

def google_verification_view(request):
    """Serve Google Search Console ownership verification file (google44f351804c9055d3.html)."""
    return HttpResponse("google-site-verification: google44f351804c9055d3.html\n", content_type="text/html; charset=utf-8")

def home(request):
    user_agent = request.META.get('HTTP_USER_AGENT', '').lower()
    if 'cron-job' in user_agent or 'cronjob' in user_agent or 'uptimerobot' in user_agent or request.GET.get('ping'):
        return HttpResponse("OK", content_type="text/plain", status=200)
    categories = Category.objects.prefetch_related('products').all()
    trending_products = Product.objects.filter(is_trending=True).select_related('category')
    if not trending_products.exists():
        trending_products = Product.objects.all().select_related('category').order_by('-created_at')[:8]
    pinterest_products = Product.objects.all().select_related('category').order_by('-created_at')[:16]
    return render(request, 'index.html', {
        'categories': categories,
        'trending_products': trending_products,
        'pinterest_products': pinterest_products
    })

def category_detail(request, slug):
    clean_slug = slug.replace('.html', '').strip().lower()
    categories = Category.objects.all()

    if clean_slug in ('all', 'all-products', 'all-collections', 'collections'):
        cat = Category(name='All Products', slug='all')
        products = Product.objects.all().order_by('-created_at')
        return render(request, 'category_detail.html', {
            'category': cat,
            'seo_slug': 'all',
            'products': products,
            'categories': categories,
            'all_categories': categories,
            'is_all_view': True,
        })

    # 1. Exact slug match in database (highest priority)
    cat = Category.objects.filter(slug__iexact=clean_slug).first()

    # 2. Check slug synonyms only if no direct category matches
    if not cat:
        slug_synonyms = {
            'cooling-glass': 'shades',
            'cooling-glasses': 'shades',
            'sunglasses': 'shades',
            'tribal': 'chains',
            'tribal-jewelry': 'chains',
            'tribal-accessories': 'chains',
            'accessories': 'rings',
        }
        target_slug = slug_synonyms.get(clean_slug)
        if target_slug:
            cat = Category.objects.filter(slug__iexact=target_slug).first()

    # 3. Check singular / plural alternate forms
    if not cat:
        alt_slug = clean_slug.rstrip('s') if clean_slug.endswith('s') else f"{clean_slug}s"
        cat = Category.objects.filter(slug__iexact=alt_slug).first()

    # 4. Check category name with hyphens converted to spaces or direct name
    if not cat:
        cat = Category.objects.filter(name__iexact=clean_slug.replace('-', ' ')).first()
    if not cat:
        cat = Category.objects.filter(name__iexact=clean_slug).first()

    if not cat:
        raise Http404(f"Category '{slug}' not found.")

    products = cat.products.all().order_by('-created_at')
    categories = Category.objects.all()
    return render(request, 'category_detail.html', {
        'category': cat,
        'seo_slug': clean_slug,
        'products': products,
        'categories': categories,
        'all_categories': categories,
    })

def product_detail(request, pk=None, slug=None):
    product = None
    if pk:
        product = get_object_or_404(Product, pk=pk)
    elif slug:
        clean_slug = slug.replace('.html', '').strip().lower()
        if clean_slug.isdigit():
            product = get_object_or_404(Product, pk=int(clean_slug))
        else:
            matching = [p for p in Product.objects.all() if slugify(p.name).lower() == clean_slug]
            if matching:
                product = matching[0]
            else:
                product = Product.objects.filter(name__iexact=clean_slug).first()
        if not product:
            raise Http404(f"Product '{slug}' not found.")
    else:
        raise Http404("Product not specified.")

    related_products = Product.objects.filter(category=product.category).exclude(id=product.id)[:5]
    if not related_products.exists():
        related_products = Product.objects.exclude(id=product.id)[:5]

    return render(request, 'product_detail.html', {
        'product': product,
        'related_products': related_products,
    })

def buy_now(request, product_id):
    product = get_object_or_404(Product, id=product_id)
    size = (request.GET.get('size') or request.POST.get('size') or '').strip().upper() or None

    if product.has_sizes:
        if not size:
            messages.warning(request, f'Please select a size (XS, S, M, L, XL, XXL) for "{product.name}".')
            return redirect('product_detail', pk=product.id)
        ps = ProductSize.objects.filter(product=product, size=size).first()
        if not ps or ps.stock <= 0:
            messages.error(request, f'Size {size} of "{product.name}" is currently sold out.')
            return redirect('product_detail', pk=product.id)
        sync_cart_item(request, product, 1, size=size)
    else:
        if product.stock <= 0:
            messages.error(request, f'"{product.name}" is currently sold out.')
            return redirect('product_detail', pk=product.id)
        sync_cart_item(request, product, 1)

    return redirect('checkout')

# --- Auth Views ---
def register_view(request):
    if request.user.is_authenticated:
        return redirect('home')
    next_url = request.POST.get('next') or request.GET.get('next') or ''
    if request.method == 'POST':
        form = RegistrationForm(request.POST)
        if form.is_valid():
            user = User.objects.create_user(
                username=form.cleaned_data['username'].strip(),
                email=form.cleaned_data['email'].strip().lower(),
                password=form.cleaned_data['password']
            )
            profile = user.profile
            profile.phone_number = form.cleaned_data['phone_number'].strip()
            profile.save()

            # Preserve guest cart items for newly registered user
            guest_cart = dict(request.session.get('cart', {}))
            login(request, user)
            request.session.set_expiry(0)  # Browser-close expiry by default
            request.session['_last_activity'] = timezone.now().timestamp()
            if guest_cart:
                for k, qty in guest_cart.items():
                    try:
                        pid, size = parse_cart_key(k)
                        if pid and int(qty) > 0:
                            prod = Product.objects.filter(id=pid, stock__gt=0).first()
                            if prod:
                                max_avail = prod.stock
                                if size and prod.has_sizes:
                                    ps = ProductSize.objects.filter(product=prod, size=size).first()
                                    max_avail = ps.stock if ps else 0
                                if max_avail > 0:
                                    c_item, created = CartItem.objects.get_or_create(user=user, product=prod, size=size)
                                    if created:
                                        c_item.quantity = min(int(qty), max_avail)
                                    else:
                                        c_item.quantity = min(c_item.quantity + int(qty), max_avail)
                                    c_item.save()
                    except Exception:
                        continue

            target = next_url if (next_url and next_url.startswith('/') and not next_url.startswith('//')) else 'home'
            return redirect(target)
    else:
        form = RegistrationForm()
    return render(request, 'register.html', {'form': form, 'next_url': next_url})

def login_view(request):
    next_url = request.POST.get('next') or request.GET.get('next') or ''
    # Validate safe internal redirect target
    safe_next = next_url if (next_url and next_url.startswith('/') and not next_url.startswith('//')) else ''

    if request.user.is_authenticated:
        if safe_next and safe_next != request.path:
            return redirect(safe_next)
        return redirect('home')

    is_expired = request.GET.get('expired') == '1'
    is_admin_target = bool(safe_next and any(adm in safe_next.lower() for adm in ['admin', 'adminpp']))

    if request.method == 'POST':
        u = (request.POST.get('username') or '').strip()
        p = request.POST.get('password') or ''
        remember_me = request.POST.get('remember_me')

        # Support sign-in with registered username, email address, or mobile number
        if '@' in u:
            matching_user = User.objects.filter(email__iexact=u).first()
            if matching_user:
                u = matching_user.username
        else:
            # Check if input is a 10-digit mobile number or has country code
            digits = re.sub(r'\D', '', u)
            if len(digits) >= 10:
                matching_profile = Profile.objects.filter(phone_number__endswith=digits[-10:]).select_related('user').first()
                if matching_profile:
                    u = matching_profile.user.username
                else:
                    matching_user = User.objects.filter(username__endswith=digits[-10:]).first()
                    if matching_user:
                        u = matching_user.username
            else:
                # Case-insensitive username matching fallback
                matching_user = User.objects.filter(username__iexact=u).first()
                if matching_user:
                    u = matching_user.username

        user = authenticate(request, username=u, password=p)
        if user:
            login(request, user)

            # Security: Staff & Superusers ALWAYS expire on browser close & have strict idle timeout
            if user.is_staff or user.is_superuser:
                request.session.set_expiry(0)  # Session strictly terminates when browser is closed
            else:
                # Regular customers can opt for Remember Me
                if remember_me in ('1', 'on', 'true', True):
                    request.session.set_expiry(60 * 60 * 24 * 14)  # 14 days
                else:
                    request.session.set_expiry(0)  # Closes on browser exit by default

            # Initialize activity timestamp for idle security tracking
            request.session['_last_activity'] = timezone.now().timestamp()

            # Handle redirection & authorization
            if safe_next:
                if any(adm in safe_next.lower() for adm in ['admin', 'adminpp']) and not (user.is_staff or user.is_superuser):
                    messages.warning(request, f"Signed in as {user.username}. Note: Staff privileges are required to access the Admin Portal.")
                    return redirect('home')
                return redirect(safe_next)

            if user.is_staff or user.is_superuser:
                return redirect('adminpp_dashboard')
            return redirect('home')

        return render(request, 'login.html', {
            'error': 'Invalid username, mobile number, or password. Please verify your credentials or use Forgot Password to reset.',
            'next_url': safe_next,
            'is_expired': is_expired,
            'is_admin_target': is_admin_target,
        })

    return render(request, 'login.html', {
        'next_url': safe_next,
        'is_expired': is_expired,
        'is_admin_target': is_admin_target,
    })


def forgot_password_view(request):
    if request.user.is_authenticated:
        return redirect('home')

    if request.method == 'POST':
        identifier = (request.POST.get('identifier') or '').strip()
        if not identifier:
            return render(request, 'forgot_password.html', {'error': 'Please enter your registered mobile number, email, or username.'})

        user = None
        digits = re.sub(r'\D', '', identifier)

        # 1. Match by Email
        if '@' in identifier:
            user = User.objects.filter(email__iexact=identifier).first()
        # 2. Match by Mobile Number (10+ digits)
        elif len(digits) >= 10:
            profile = Profile.objects.filter(phone_number__endswith=digits[-10:]).select_related('user').first()
            if profile:
                user = profile.user
            else:
                user = User.objects.filter(username__endswith=digits[-10:]).first()
        
        # 3. Match by Username
        if not user:
            user = User.objects.filter(username__iexact=identifier).first()

        if not user:
            return render(request, 'forgot_password.html', {
                'error': f'No account found matching "{identifier}". Please verify your registered mobile number, email, or username.',
                'identifier': identifier
            })

        # CRITICAL SECURITY CHECK: Account must have a registered email to receive OTP
        user_email = (user.email or '').strip()
        if not user_email:
            wa_text = f"Hello ASHAS Concierge, I need assistance recovering my account ({user.username}). My account does not have an email address linked."
            return render(request, 'forgot_password.html', {
                'error': f'The account "{identifier}" does not have a registered email address on file for security verification. To protect against unauthorized account takeover, password resets require email OTP verification. Please contact our WhatsApp Concierge for identity verification.',
                'identifier': identifier,
                'no_email_account': True,
                'wa_help_url': f"https://wa.me/918281451481?text={urlquote(wa_text)}",
            })

        # Generate cryptographically secure 6-digit OTP
        otp = f"{secrets.randbelow(900000) + 100000}"
        request.session['reset_user_id'] = user.id
        request.session['reset_otp'] = otp
        request.session['reset_otp_time'] = timezone.now().timestamp()
        request.session['reset_identifier'] = identifier

        # Mask email for privacy display (e.g. ar*****@gmail.com)
        parts = user_email.split('@')
        if len(parts) == 2:
            uname, domain = parts
            if len(uname) > 2:
                masked_email = f"{uname[:2]}{'*' * (len(uname) - 2)}@{domain}"
            else:
                masked_email = f"{uname}*@{domain}"
        else:
            masked_email = user_email

        request.session['reset_masked_email'] = masked_email

        # Send OTP strictly to user's registered email
        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        reset_url = request.build_absolute_uri(reverse('reset_password_token', kwargs={'uidb64': uidb64, 'token': token}))
        try:
            send_mail(
                subject='ASHAS STORE - Your Password Recovery Verification Code',
                message=(
                    f"Hello {user.username},\n\n"
                    f"We received a password reset request for your ASHAS Store account.\n\n"
                    f"Your 6-digit security verification code is:\n\n"
                    f"    {otp}\n\n"
                    f"Enter this code on the website to verify your identity and set a new password.\n"
                    f"This code will expire in 15 minutes.\n\n"
                    f"Alternatively, you can set a new password directly by clicking this secure link:\n"
                    f"{reset_url}\n\n"
                    f"If you did not request this, please ignore this email. Your password and account remain secure.\n\n"
                    f"ASHAS™ CLOTHING & ACCESSORIES\n"
                    f"https://ashasstore.in\n"
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user_email],
                fail_silently=False
            )
        except Exception:
            pass

        return redirect('verify_reset_otp')

    return render(request, 'forgot_password.html')


def verify_reset_otp_view(request):
    if request.user.is_authenticated:
        return redirect('home')

    user_id = request.session.get('reset_user_id')
    stored_otp = request.session.get('reset_otp')
    otp_time = request.session.get('reset_otp_time', 0)
    masked_email = request.session.get('reset_masked_email', 'your registered email')

    if not user_id or not stored_otp:
        messages.info(request, "Please enter your registered mobile number, email, or username to start password recovery.")
        return redirect('forgot_password')

    user = User.objects.filter(id=user_id).first()
    if not user or not user.email:
        return redirect('forgot_password')

    if request.method == 'POST':
        action = request.POST.get('action')
        
        # Resend fresh code handler: strictly send to user's registered email
        if action == 'resend':
            new_otp = f"{secrets.randbelow(900000) + 100000}"
            request.session['reset_otp'] = new_otp
            request.session['reset_otp_time'] = timezone.now().timestamp()
            stored_otp = new_otp
            try:
                uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
                token = default_token_generator.make_token(user)
                reset_url = request.build_absolute_uri(reverse('reset_password_token', kwargs={'uidb64': uidb64, 'token': token}))
                send_mail(
                    subject='ASHAS STORE - New Password Recovery Verification Code',
                    message=(
                        f"Hello {user.username},\n\n"
                        f"Your new 6-digit verification code is:\n\n"
                        f"    {new_otp}\n\n"
                        f"Direct Link: {reset_url}\n\n"
                        f"Valid for 15 minutes.\n\n"
                        f"ASHAS Store"
                    ),
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[user.email],
                    fail_silently=False
                )
            except Exception:
                pass
            messages.success(request, f"A fresh 6-digit recovery code has been sent to your email ({masked_email}).")
            return redirect('verify_reset_otp')

        entered_otp = (request.POST.get('otp') or '').strip().replace(' ', '').replace('-', '')
        new_password = request.POST.get('new_password') or ''
        confirm_password = request.POST.get('confirm_password') or ''

        # Check 15-minute expiry
        if (timezone.now().timestamp() - otp_time) > 900:
            return render(request, 'verify_reset_otp.html', {
                'error': 'Verification code has expired. Please click "Resend Code" to receive a fresh code in your email.',
                'masked_email': masked_email,
                'username': user.username,
            })

        # CRITICAL SECURITY: Compare entered OTP from email with stored OTP
        if entered_otp != stored_otp:
            return render(request, 'verify_reset_otp.html', {
                'error': 'Incorrect 6-digit verification code. Please check your email inbox and spam folder, enter the code from your email, and try again.',
                'masked_email': masked_email,
                'username': user.username,
            })

        if len(new_password) < 6:
            return render(request, 'verify_reset_otp.html', {
                'error': 'Password must be at least 6 characters long.',
                'masked_email': masked_email,
                'username': user.username,
            })

        if new_password != confirm_password:
            return render(request, 'verify_reset_otp.html', {
                'error': 'Passwords do not match. Please verify both password entries.',
                'masked_email': masked_email,
                'username': user.username,
            })

        # Save new password securely
        user.set_password(new_password)
        user.save()

        # Clean session
        request.session.pop('reset_user_id', None)
        request.session.pop('reset_otp', None)
        request.session.pop('reset_otp_time', None)
        request.session.pop('reset_identifier', None)
        request.session.pop('reset_masked_email', None)

        # Send confirmation email
        try:
            send_mail(
                subject='ASHAS STORE - Password Successfully Updated',
                message=(
                    f"Hello {user.username},\n\n"
                    f"Your password for your ASHAS Store account has been successfully changed.\n\n"
                    f"If you did not make this change, please contact us immediately on WhatsApp (+91 8281451481).\n\n"
                    f"ASHAS™ CLOTHING & ACCESSORIES\n"
                    f"https://ashasstore.in"
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                fail_silently=True
            )
        except Exception:
            pass

        # Log in user automatically
        login(request, user)
        request.session['_last_activity'] = timezone.now().timestamp()
        messages.success(request, f"Password successfully updated! Welcome back to ASHAS, {user.username}.")
        return redirect('profile')

    # GET request: stored_otp is NEVER passed to template
    return render(request, 'verify_reset_otp.html', {
        'masked_email': masked_email,
        'username': user.username,
    })


def reset_password_token_view(request, uidb64, token):
    if request.user.is_authenticated:
        return redirect('home')

    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.filter(pk=uid).first()
    except Exception:
        user = None

    if not user or not default_token_generator.check_token(user, token):
        return render(request, 'reset_password_token.html', {
            'is_valid': False,
            'error': 'This password reset link is invalid or has expired.'
        })

    if request.method == 'POST':
        new_password = request.POST.get('new_password') or ''
        confirm_password = request.POST.get('confirm_password') or ''

        if len(new_password) < 6:
            return render(request, 'reset_password_token.html', {
                'is_valid': True,
                'error': 'Password must be at least 6 characters long.',
                'user': user,
            })

        if new_password != confirm_password:
            return render(request, 'reset_password_token.html', {
                'is_valid': True,
                'error': 'Passwords do not match.',
                'user': user,
            })

        user.set_password(new_password)
        user.save()

        login(request, user)
        request.session['_last_activity'] = timezone.now().timestamp()
        messages.success(request, f"Password successfully updated! Welcome back, {user.username}.")
        return redirect('profile')

    return render(request, 'reset_password_token.html', {
        'is_valid': True,
        'user': user,
    })

def logout_view(request):
    logout(request)
    request.session.flush()
    messages.info(request, "You have been safely signed out.")
    return redirect('home')

@login_required
def profile_view(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)
    if request.method == 'POST':
        form = ProfileUpdateForm(request.POST, instance=profile)
        if form.is_valid():
            form.save()
            return redirect('profile')
    else:
        form = ProfileUpdateForm(instance=profile)
    # Strictly show only personal orders belonging to this authenticated user account
    orders = Order.objects.filter(user=request.user).prefetch_related('items__product').order_by('-created_at')

    return render(request, 'profile.html', {
        'form': form,
        'orders': orders,
        'profile': profile,
    })

# --- Cart Views ---
def add_to_cart(request, product_id):
    cart = get_user_cart(request)
    try:
        product_id = int(product_id)
    except (ValueError, TypeError):
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
            return JsonResponse({'status': 'error', 'message': 'Invalid product ID.'}, status=400)
        return redirect(safe_referer(request, 'home'))

    product = Product.objects.filter(id=product_id).first()
    if not product:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
            return JsonResponse({'status': 'error', 'message': 'Product not found.'}, status=404)
        return redirect(safe_referer(request, 'home'))

    size = (request.GET.get('size') or request.POST.get('size') or '').strip().upper() or None

    if product.has_sizes:
        if not size:
            msg = 'Please select a size (XS, S, M, L, XL, XXL) before adding to bag.'
            if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
                return JsonResponse({'status': 'error', 'message': msg}, status=400)
            messages.warning(request, msg)
            return redirect('product_detail', pk=product.id)

        ps = ProductSize.objects.filter(product=product, size=size).first()
        if not ps or ps.stock <= 0:
            msg = f'Size {size} of "{product.name}" is currently sold out.'
            if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
                return JsonResponse({'status': 'error', 'message': msg}, status=400)
            messages.error(request, msg)
            return redirect('product_detail', pk=product.id)

        max_stock = ps.stock
        key = f"{product.id}:{size}"
    else:
        if product.stock <= 0:
            msg = f'"{product.name}" is currently sold out.'
            if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
                return JsonResponse({'status': 'error', 'message': msg}, status=400)
            messages.error(request, msg)
            return redirect(safe_referer(request, 'home'))

        max_stock = product.stock
        key = str(product.id)

    current_qty = cart.get(key, 0)
    if current_qty >= max_stock:
        limit_msg = f'Only {max_stock} available in stock{" for size " + size if size else ""}.'
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
            return JsonResponse({'status': 'error', 'message': limit_msg}, status=400)
        messages.warning(request, limit_msg)
        return redirect(safe_referer(request, 'home'))

    sync_cart_item(request, product, current_qty + 1, size=size)
    
    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
        total_qty = sum(get_user_cart(request).values())
        size_label = f" (Size: {size})" if size else ""
        return JsonResponse({
            'status': 'success',
            'cart_item_count': total_qty,
            'product_id': product_id,
            'product_name': product.name,
            'size': size,
            'message': f"{product.name}{size_label} added to your bag."
        })
    return redirect(safe_referer(request, 'home'))

def cart_view(request):
    cart = get_user_cart(request)
    cart_items = []
    total = 0
    stale_keys = []
    stock_adjusted = False

    for item_key, qty in list(cart.items()):
        pid, size = parse_cart_key(item_key)
        if not pid:
            stale_keys.append(item_key)
            continue

        product = Product.objects.filter(id=pid).first()
        if not product:
            stale_keys.append(item_key)
            continue

        if size and product.has_sizes:
            ps = ProductSize.objects.filter(product=product, size=size).first()
            if not ps or ps.stock <= 0:
                stale_keys.append(item_key)
                continue
            if qty > ps.stock:
                qty = ps.stock
                sync_cart_item(request, product, qty, size=size)
                stock_adjusted = True
        else:
            if product.stock <= 0:
                stale_keys.append(item_key)
                continue
            if qty > product.stock:
                qty = product.stock
                sync_cart_item(request, product, qty, size=None)
                stock_adjusted = True

        subtotal = float(product.current_price) * qty
        total += subtotal
        cart_items.append({
            'product': product,
            'quantity': qty,
            'size': size,
            'subtotal': subtotal,
            'item_key': item_key
        })
    
    if stale_keys or stock_adjusted:
        for k in stale_keys:
            pid, size = parse_cart_key(k)
            if pid:
                p = Product.objects.filter(id=pid).first()
                if p:
                    sync_cart_item(request, p, 0, size=size)
                else:
                    cart.pop(k, None)
                    if request.user.is_authenticated:
                        CartItem.objects.filter(user=request.user, product_id=pid, size=size).delete()
            else:
                cart.pop(k, None)
        request.session['cart'] = cart
        request.session.modified = True
        if stock_adjusted:
            messages.warning(request, "Some cart quantities were adjusted to match available inventory.")

    return render(request, 'cart.html', {'cart_items': cart_items, 'total': total})

def update_cart(request, product_id, action):
    cart = get_user_cart(request)
    size = (request.GET.get('size') or request.POST.get('size') or '').strip().upper() or None
    try:
        product_id_int = int(product_id)
    except (ValueError, TypeError):
        return redirect('cart')
    product = Product.objects.filter(id=product_id_int).first()

    key = f"{product_id}:{size}" if size else str(product_id)
    if key not in cart:
        matching_keys = [k for k in cart.keys() if parse_cart_key(k)[0] == product_id_int]
        if len(matching_keys) == 1:
            key = matching_keys[0]
            _, size = parse_cart_key(key)

    if key in cart and product:
        max_stock = product.stock
        if size and product.has_sizes:
            ps = ProductSize.objects.filter(product=product, size=size).first()
            max_stock = ps.stock if ps else 0

        if action == 'increase':
            if cart[key] >= max_stock:
                messages.warning(request, f'Cannot exceed available stock of {max_stock}{" for size " + size if size else ""}.')
            else:
                sync_cart_item(request, product, cart[key] + 1, size=size)
        elif action == 'decrease':
            new_qty = cart[key] - 1
            sync_cart_item(request, product, new_qty, size=size)
        elif action == 'remove':
            sync_cart_item(request, product, 0, size=size)
    return redirect('cart')

# --- Shipping & Checkout Helpers ---
def check_cart_has_shades(items):
    """
    Returns True if any product in the items list belongs to the 'Shades' category
    or has 'shade' or 'sunglass' in its name/slug.
    Accepts an iterable of Product objects, (Product, qty) tuples, or OrderItem objects.
    """
    for item in items:
        prod = getattr(item, 'product', item)
        if isinstance(prod, tuple):
            prod = prod[0]
        if not prod:
            continue
        cat = getattr(prod, 'category', None)
        if cat:
            cat_name = (getattr(cat, 'name', '') or '').lower()
            cat_slug = (getattr(cat, 'slug', '') or '').lower()
            if 'shade' in cat_name or 'shade' in cat_slug:
                return True
        prod_name = (getattr(prod, 'name', '') or '').lower()
        if 'shade' in prod_name or 'sunglass' in prod_name:
            return True
    return False


def check_cart_has_test_bracelet(items):
    """
    Deprecated: All items now follow standard shipping rates:
    - ₹55 inside Kerala (₹65 if order contains Shades)
    - ₹95 outside Kerala
    """
    return False


def is_kerala_pincode(pincode):
    """
    Checks if an Indian Postal PIN code belongs to Kerala / Lakshadweep postal circle.
    Kerala PIN codes strictly start with 67, 68, or 69 (ranges 670001 - 695615).
    """
    if not pincode:
        return False
    import re
    digits = re.sub(r'\D', '', str(pincode))
    return len(digits) == 6 and digits.startswith(('67', '68', '69'))


def determine_delivery_region(pincode=None, delivery_region=None, address=None):
    """
    Determines delivery region ('kerala' or 'outside_kerala') based on:
    1. PIN code (primary authority): starts with 67, 68, 69 -> 'kerala'; other 6 digits -> 'outside_kerala'.
    2. Explicit delivery_region parameter if provided ('kerala' or 'outside_kerala').
    3. Text address parsing for state keywords and PIN codes as fallback.
    """
    if pincode:
        import re
        digits = re.sub(r'\D', '', str(pincode))
        if len(digits) == 6:
            return 'kerala' if digits.startswith(('67', '68', '69')) else 'outside_kerala'

    if delivery_region:
        clean_reg = str(delivery_region).strip().lower()
        if clean_reg in ('kerala', 'inside_kerala'):
            return 'kerala'
        if clean_reg in ('outside_kerala', 'outside'):
            return 'outside_kerala'

    if address:
        addr_lower = address.lower()
        import re
        # Look for 6-digit pin in address text
        match_kerala = re.search(r'\b(6[7-9]\d{4})\b', addr_lower)
        if match_kerala:
            return 'kerala'
        match_other = re.search(r'\b([1-57-9]\d{5}|6[0-6]\d{4})\b', addr_lower)
        if match_other:
            return 'outside_kerala'
        if 'kerala' in addr_lower:
            return 'kerala'
        if any(st in addr_lower for st in [
            'tamil nadu', 'karnataka', 'maharashtra', 'delhi', 'bangalore',
            'bengaluru', 'chennai', 'mumbai', 'hyderabad', 'andhra', 'telangana', 'pune', 'goa'
        ]):
            return 'outside_kerala'

    return 'kerala'


def calculate_shipping_fee(items, delivery_region='kerala', pincode=None):
    """
    Shipping fee calculation rules:
    - Outside Kerala (PIN outside 67-69 or explicitly outside): ₹95 flat
    - Inside Kerala (PIN 67xxxx, 68xxxx, 69xxxx or inside Kerala):
      - ₹65 if order contains Shades
      - ₹55 for other products
    """
    region = determine_delivery_region(pincode=pincode, delivery_region=delivery_region)
    if region == 'outside_kerala':
        return 95.0
    
    # Inside Kerala
    has_shades = check_cart_has_shades(items)
    return 65.0 if has_shades else 55.0


# --- Razorpay Checkout ---
@login_required(login_url='login')
def checkout_view(request):
    cart = get_user_cart(request)
    if not cart:
        return redirect('home')
    
    valid_items = {}
    subtotal = 0.0
    stale_keys = []
    stock_adjusted = False

    for item_key, qty in list(cart.items()):
        pid, size = parse_cart_key(item_key)
        if not pid:
            stale_keys.append(item_key)
            continue
        product = Product.objects.filter(id=pid).first()
        if not product:
            stale_keys.append(item_key)
            continue

        if size and product.has_sizes:
            ps = ProductSize.objects.filter(product=product, size=size).first()
            if not ps or ps.stock <= 0:
                stale_keys.append(item_key)
                continue
            if qty > ps.stock:
                qty = ps.stock
                sync_cart_item(request, product, qty, size=size)
                stock_adjusted = True
        else:
            if product.stock <= 0:
                stale_keys.append(item_key)
                continue
            if qty > product.stock:
                qty = product.stock
                sync_cart_item(request, product, qty, size=None)
                stock_adjusted = True

        valid_items[item_key] = (product, qty, size)
        subtotal += float(product.current_price) * qty

    if stale_keys or stock_adjusted:
        for k in stale_keys:
            pid, size = parse_cart_key(k)
            if pid:
                p = Product.objects.filter(id=pid).first()
                if p:
                    sync_cart_item(request, p, 0, size=size)
                else:
                    cart.pop(k, None)
                    if request.user.is_authenticated:
                        CartItem.objects.filter(user=request.user, product_id=pid, size=size).delete()
            else:
                cart.pop(k, None)
        request.session['cart'] = cart
        request.session.modified = True
        if stock_adjusted:
            messages.warning(request, "Item quantities adjusted based on current warehouse stock.")

    if not valid_items or subtotal <= 0:
        if request.method == 'POST':
            return JsonResponse({'status': 'error', 'message': 'Your cart is empty or has invalid items.'}, status=400)
        messages.error(request, "The items in your bag are currently out of stock.")
        return redirect('home')
    
    checkout_items = []
    item_count = 0
    products_list = []
    for item_key, (product, qty, size) in valid_items.items():
        sub = float(product.current_price) * qty
        item_count += qty
        products_list.append(product)
        checkout_items.append({
            'product': product,
            'quantity': qty,
            'size': size,
            'unit_price': float(product.current_price),
            'subtotal': sub
        })

    has_shades = check_cart_has_shades(products_list)
    has_bracelet = check_cart_has_test_bracelet(products_list)

    default_address = ''
    default_landmark = ''
    default_phone = ''
    default_phone_2 = ''
    default_name = ''
    default_pincode = ''
    default_district = ''
    default_city = ''
    default_state = ''
    if request.user.is_authenticated:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        default_address = profile.address or ''
        default_landmark = profile.landmark or ''
        default_phone = profile.phone_number or ''
        default_phone_2 = profile.phone_number_2 or ''
        default_pincode = profile.pincode or ''
        default_district = profile.district or profile.city or ''
        default_city = profile.city or profile.district or ''
        default_state = profile.state or ''
        default_name = request.user.get_full_name() or request.user.username

        # If pincode not yet stored separately, try extracting 6 digits from address text
        if not default_pincode and default_address:
            import re
            m = re.search(r'\b([1-9]\d{5})\b', default_address)
            if m:
                default_pincode = m.group(1)

    # Determine default delivery region from PIN code or saved address
    initial_region = determine_delivery_region(pincode=default_pincode, address=default_address)
    initial_shipping_fee = calculate_shipping_fee(products_list, delivery_region=initial_region, pincode=default_pincode)
    initial_total = round(subtotal + initial_shipping_fee, 2)

    if request.method == 'POST':
        full_name = (request.POST.get('full_name') or '').strip()
        phone = (request.POST.get('phone_number') or '').strip()
        phone_2 = (request.POST.get('phone_number_2') or '').strip()
        address = (request.POST.get('shipping_address') or '').strip()
        landmark = (request.POST.get('landmark') or '').strip()
        pincode = (request.POST.get('pincode') or '').strip()
        district = (request.POST.get('district') or request.POST.get('city') or '').strip()
        city = district
        state = (request.POST.get('state') or '').strip()
        requested_region = (request.POST.get('delivery_region') or initial_region).strip().lower()

        # Strict Validation of Required Fields
        if not full_name:
            return JsonResponse({'status': 'error', 'message': 'Please enter recipient name.'}, status=400)
        if not address:
            return JsonResponse({'status': 'error', 'message': 'Delivery Address is mandatory. Please enter house/flat/building and street address.'}, status=400)
        if not phone:
            return JsonResponse({'status': 'error', 'message': 'Phone number 1 is mandatory.'}, status=400)

        import re
        phone_digits = re.sub(r'\D', '', phone)
        if len(phone_digits) < 10 or len(phone_digits) > 13:
            return JsonResponse({'status': 'error', 'message': 'Please enter a valid 10-digit mobile number for Phone 1.'}, status=400)
        phone = phone_digits[-10:] if len(phone_digits) >= 10 else phone_digits

        phone_2_clean = None
        if phone_2:
            phone_2_digits = re.sub(r'\D', '', phone_2)
            if len(phone_2_digits) < 10 or len(phone_2_digits) > 13:
                return JsonResponse({'status': 'error', 'message': 'Please enter a valid 10-digit mobile number for Phone 2 (or leave it blank).'}, status=400)
            phone_2_clean = phone_2_digits[-10:] if len(phone_2_digits) >= 10 else phone_2_digits

        # Validate PIN code
        pincode_digits = re.sub(r'\D', '', pincode)
        if not pincode_digits and address:
            m = re.search(r'\b([1-9]\d{5})\b', address)
            if m:
                pincode_digits = m.group(1)

        if pincode_digits:
            if len(pincode_digits) != 6:
                return JsonResponse({'status': 'error', 'message': 'Please enter a valid 6-digit Indian Postal PIN code.'}, status=400)
            pincode = pincode_digits
            # MANDATORY ENFORCEMENT:
            # If the PIN code is outside Kerala, the ₹95 shipping charge is strictly mandatory.
            if not is_kerala_pincode(pincode):
                delivery_region = 'outside_kerala'
            else:
                delivery_region = 'kerala'
        else:
            delivery_region = determine_delivery_region(pincode=None, delivery_region=requested_region, address=address)
            pincode = '673001' if delivery_region == 'kerala' else '560001'

        if not district:
            district = city or ('Kozhikode' if delivery_region == 'kerala' else 'Bengaluru')
        if not state:
            state = 'Kerala' if delivery_region == 'kerala' else 'Other'

        if subtotal <= 0:
            return JsonResponse({'status': 'error', 'message': 'Invalid cart total.'}, status=400)

        # Re-compute accurate shipping fee on backend based on PIN code & product types
        shipping_fee = calculate_shipping_fee(products_list, delivery_region=delivery_region, pincode=pincode)
        grand_total = round(subtotal + shipping_fee, 2)

        # Build clean full address representation for shipping labels & admin
        full_shipping_address = address
        if landmark and f"near {landmark.lower()}" not in full_shipping_address.lower():
            full_shipping_address = f"{full_shipping_address}, Near {landmark}"
        if district and district.lower() not in full_shipping_address.lower():
            full_shipping_address = f"{full_shipping_address}, {district}"
        if state and state.lower() not in full_shipping_address.lower():
            full_shipping_address = f"{full_shipping_address}, {state}"
        if pincode not in full_shipping_address:
            full_shipping_address = f"{full_shipping_address} - {pincode}"

        # Automatically update user profile for subsequent visits
        if request.user.is_authenticated:
            try:
                prof, _ = Profile.objects.get_or_create(user=request.user)
                prof.phone_number = phone
                if phone_2_clean:
                    prof.phone_number_2 = phone_2_clean
                prof.address = address
                if landmark:
                    prof.landmark = landmark
                prof.pincode = pincode
                prof.district = district
                prof.city = district
                prof.state = state
                prof.save()
            except Exception:
                pass

        rzp_amount = int(round(grand_total * 100))
        payment_method = (request.POST.get('payment_method') or 'upi').strip().lower()

        active_gw = getattr(settings, 'ACTIVE_PAYMENT_GATEWAY', 'instamojo').lower()
        im_key = getattr(settings, 'INSTAMOJO_API_KEY', '').strip()
        use_instamojo = (active_gw == 'instamojo') or (bool(im_key) and active_gw != 'razorpay')

        if use_instamojo:
            order = Order.objects.create(
                user=request.user if request.user.is_authenticated else None,
                full_name=full_name,
                phone_number=phone,
                phone_number_2=phone_2_clean,
                shipping_address=address,
                landmark=landmark or None,
                pincode=pincode,
                city=district,
                district=district,
                state=state or ('Kerala' if delivery_region == 'kerala' else ''),
                delivery_region=delivery_region,
                shipping_fee=shipping_fee,
                total_price=grand_total,
                payment_gateway='instamojo',
                payment_status='Pending'
            )

            for item_key, (product, qty, size) in valid_items.items():
                OrderItem.objects.create(
                    order=order,
                    product=product,
                    price=product.current_price,
                    quantity=qty,
                    size=size
                )

            pr = create_instamojo_payment_request(order, request)
            if pr and pr.get('longurl'):
                return JsonResponse({
                    'status': 'success',
                    'gateway': 'instamojo',
                    'payment_url': pr.get('longurl'),
                    'payment_request_id': pr.get('id'),
                    'db_order_id': order.id,
                    'amount': float(grand_total),
                    'subtotal': subtotal,
                    'shipping_fee': shipping_fee,
                    'total': grand_total,
                    'currency': 'INR'
                })
            else:
                return JsonResponse({
                    'status': 'error',
                    'message': 'Failed to initiate Instamojo payment link. Please check your Instamojo API Key and Auth Token in settings or wait for account KYC activation.'
                }, status=400)

        # Create Live Order via Razorpay API (Deposited to Merchant Bank Account)
        try:
            client = get_razorpay_client()
            rzp_order = client.order.create({
                'amount': rzp_amount,
                'currency': 'INR',
                'payment_capture': 1,
                'notes': {
                    'customer_name': full_name[:40],
                    'customer_phone': phone[:15],
                    'shipping_address': full_shipping_address[:100],
                    'pincode': pincode,
                    'delivery_region': delivery_region,
                    'shipping_fee': str(shipping_fee),
                    'payment_method': payment_method
                }
            })
            rzp_order_id = rzp_order['id']
            rzp_amount = rzp_order['amount']
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Razorpay Live order creation error: {e}")
            return JsonResponse({
                'status': 'error',
                'message': f"Payment Gateway Error: {str(e)}"
            }, status=400)

        # Order created in Pending status - ONLY marked Completed after verified signature
        order = Order.objects.create(
            user=request.user if request.user.is_authenticated else None,
            full_name=full_name,
            phone_number=phone,
            phone_number_2=phone_2_clean,
            shipping_address=address,
            landmark=landmark or None,
            pincode=pincode,
            city=district,
            district=district,
            state=state or ('Kerala' if delivery_region == 'kerala' else ''),
            delivery_region=delivery_region,
            shipping_fee=shipping_fee,
            total_price=grand_total,
            razorpay_order_id=rzp_order_id,
            payment_status='Pending'
        )

        for item_key, (product, qty, size) in valid_items.items():
            OrderItem.objects.create(
                order=order,
                product=product,
                price=product.current_price,
                quantity=qty,
                size=size
            )

        # Create Live Payment Link for on-screen QR payment flow
        payment_link_url = ''
        payment_link_id = ''
        try:
            callback_url = request.build_absolute_uri(f"/profile/?order_placed=true&order_id={order.id}")
            pay_link = client.payment_link.create({
                'amount': rzp_amount,
                'currency': 'INR',
                'accept_partial': False,
                'upi_link': True,
                'callback_url': callback_url,
                'callback_method': 'get',
                'description': f"Order #{order.id} Payment - ASHAS STORE",
                'customer': {
                    'name': full_name[:40],
                    'contact': phone[:15],
                    'email': (request.user.email if request.user.is_authenticated and request.user.email else '') or 'care@ashasstore.in'
                },
                'notify': {'sms': False, 'email': False, 'whatsapp': False},
                'notes': {
                    'order_id': str(order.id),
                    'razorpay_order_id': rzp_order_id,
                    'store': 'ASHAS STORE'
                }
            })
            if isinstance(pay_link, dict):
                raw_url = pay_link.get('short_url')
                if isinstance(raw_url, str):
                    payment_link_url = raw_url
                raw_id = pay_link.get('id')
                if isinstance(raw_id, str) and raw_id:
                    payment_link_id = raw_id
                    order.razorpay_payment_link_id = payment_link_id
                    order.save(update_fields=['razorpay_payment_link_id'])
        except Exception as link_err:
            import logging
            logging.getLogger(__name__).warning(f"Payment link creation for QR notice: {link_err}")

        import urllib.parse
        qr_code_url = f"https://api.qrserver.com/v1/create-qr-code/?size=280x280&margin=8&data={urllib.parse.quote(payment_link_url)}" if payment_link_url else ''

        return JsonResponse({
            'status': 'success',
            'razorpay_key': settings.RAZORPAY_KEY_ID,
            'amount': rzp_amount,
            'subtotal': subtotal,
            'shipping_fee': shipping_fee,
            'delivery_region': delivery_region,
            'pincode': pincode,
            'total': grand_total,
            'currency': 'INR',
            'razorpay_order_id': rzp_order_id,
            'db_order_id': order.id,
            'customer_name': full_name,
            'customer_phone': phone,
            'customer_email': request.user.email if request.user.is_authenticated and request.user.email else '',
            'payment_method': payment_method,
            'payment_link_url': payment_link_url,
            'qr_code_url': qr_code_url
        })

    return render(request, 'checkout.html', {
        'subtotal': subtotal,
        'shipping_fee': initial_shipping_fee,
        'total': initial_total,
        'has_shades': has_shades,
        'has_bracelet': has_bracelet,
        'delivery_region': initial_region,
        'shades_shipping_kerala': 65.0,
        'other_shipping_kerala': 55.0,
        'outside_shipping': 95.0,
        'checkout_items': checkout_items,
        'item_count': item_count,
        'default_address': default_address,
        'default_landmark': default_landmark,
        'default_phone': default_phone,
        'default_phone_2': default_phone_2,
        'default_name': default_name,
        'default_pincode': default_pincode,
        'default_district': default_district,
        'default_city': default_city,
        'default_state': default_state or ('Kerala' if initial_region == 'kerala' else ''),
        'user_email': request.user.email if request.user.is_authenticated else '',
        'razorpay_key': settings.RAZORPAY_KEY_ID,
    })

@csrf_exempt
def payment_verify(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            order_id = data.get('db_order_id')
            rzp_order_id = data.get('razorpay_order_id')
            rzp_pay_id = data.get('razorpay_payment_id')
            rzp_sig = data.get('razorpay_signature')

            params = {
                'razorpay_order_id': rzp_order_id,
                'razorpay_payment_id': rzp_pay_id,
                'razorpay_signature': rzp_sig
            }

            order = None
            if order_id:
                try:
                    order = Order.objects.filter(id=int(order_id)).first()
                except (ValueError, TypeError):
                    order = None
            if not order and rzp_order_id:
                order = Order.objects.filter(razorpay_order_id=rzp_order_id).first()

            if not order:
                # If payment was made but order doesn't exist, auto-refund so customer is never billed
                if rzp_pay_id and not str(rzp_order_id or '').startswith('demo_'):
                    try:
                        client = get_razorpay_client()
                        pay_info = client.payment.fetch(rzp_pay_id)
                        if pay_info.get('status') in ('authorized', 'captured'):
                            client.payment.refund(rzp_pay_id, {'notes': {'reason': 'Auto-refund: Order record not found'}})
                    except Exception:
                        pass
                return JsonResponse({'status': 'failed', 'message': 'Order record not found.'}, status=404)

            # Security check: if user is authenticated, ensure order belongs to them
            if request.user.is_authenticated and order.user and order.user != request.user:
                return JsonResponse({'status': 'failed', 'message': 'Unauthorized order access.'}, status=403)

            # Signature verification and capture check
            if not str(rzp_order_id or '').startswith('demo_'):
                client = get_razorpay_client()
                try:
                    client.utility.verify_payment_signature(params)
                except Exception as sig_err:
                    # Signature verification failed! Check if payment occurred in Razorpay and auto-refund
                    try:
                        if rzp_pay_id:
                            pay_info = client.payment.fetch(rzp_pay_id)
                            if pay_info.get('status') in ('authorized', 'captured'):
                                refund_res = client.payment.refund(rzp_pay_id, {
                                    'notes': {'reason': f"Auto-refund: Signature mismatch ({str(sig_err)[:80]})"}
                                })
                                order.payment_status = 'Refunded'
                                order.razorpay_payment_id = rzp_pay_id
                                order.razorpay_refund_id = refund_res.get('id')
                                order.refund_amount = Decimal(str(refund_res.get('amount', 0))) / Decimal('100')
                                order.refund_notes = f"Auto-refunded: Signature verification mismatch ({sig_err})"
                                order.save(update_fields=['payment_status', 'razorpay_payment_id', 'razorpay_refund_id', 'refund_amount', 'refund_notes'])
                                return JsonResponse({
                                    'status': 'failed',
                                    'message': 'Payment signature verification mismatch. Your payment has been automatically refunded to your original payment method.'
                                }, status=400)
                    except Exception as ref_err:
                        import logging
                        logging.getLogger(__name__).error(f"Auto-refund on signature error failed: {ref_err}")
                    return JsonResponse({'status': 'failed', 'message': f'Signature verification failed: {sig_err}'}, status=400)

                # Ensure payment is captured. If authorized, capture it. If capture fails, refund immediately!
                try:
                    if rzp_pay_id:
                        pay_info = client.payment.fetch(rzp_pay_id)
                        if pay_info.get('status') == 'authorized':
                            client.payment.capture(rzp_pay_id, pay_info.get('amount'))
                except Exception as cap_err:
                    err_str = str(cap_err).lower()
                    if 'already paid' in err_str or 'already captured' in err_str:
                        pass
                    else:
                        import logging
                        logging.getLogger(__name__).warning(f"Razorpay capture fallback failed: {cap_err}. Initiating auto-refund...")
                        try:
                            refund_res = client.payment.refund(rzp_pay_id, {
                                'notes': {'reason': f"Auto-refund: Capture failed ({str(cap_err)[:100]})"}
                            })
                            order.payment_status = 'Refunded'
                            order.razorpay_payment_id = rzp_pay_id
                            order.razorpay_refund_id = refund_res.get('id')
                            order.refund_amount = Decimal(str(refund_res.get('amount', 0))) / Decimal('100')
                            order.refund_notes = f"Auto-refunded: Gateway failed to capture payment ({cap_err})"
                            order.save(update_fields=['payment_status', 'razorpay_payment_id', 'razorpay_refund_id', 'refund_amount', 'refund_notes'])
                            return JsonResponse({
                                'status': 'failed',
                                'message': 'Payment could not be captured by Razorpay. The amount has been automatically refunded back to your account.'
                            }, status=400)
                        except Exception as ref_err:
                            logging.getLogger(__name__).error(f"Auto-refund failed: {ref_err}")

            # Idempotency guard: prevent duplicate inventory deduction if already verified
            if order.payment_status == 'Completed':
                return JsonResponse({
                    'status': 'success',
                    'order_id': order.id,
                    'redirect_url': f"/profile/?order_placed=true&order_id={order.id}"
                })

            order.payment_status = 'Completed'
            order.razorpay_payment_id = rzp_pay_id or f"pay_{order.id}"
            if not order.shipping_status:
                order.shipping_status = 'Processing'
            order.save(update_fields=['payment_status', 'razorpay_payment_id', 'shipping_status'])

            # Decrement product inventory safely (including size variants)
            deduct_order_inventory(order)

            # Clear cart items strictly for this user
            if request.user.is_authenticated:
                CartItem.objects.filter(user=request.user).delete()
            request.session['cart'] = {}
            request.session.modified = True
            return JsonResponse({
                'status': 'success',
                'order_id': order.id,
                'redirect_url': f"/profile/?order_placed=true&order_id={order.id}"
            })
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Payment verification error: {e}")
            return JsonResponse({'status': 'failed', 'message': str(e)}, status=400)
    return JsonResponse({'status': 'failed', 'message': 'Invalid request method.'}, status=405)

@csrf_exempt
def razorpay_webhook(request):
    """
    Razorpay Webhook listener for asynchronous payment capture/order events.
    - When payment is captured in Razorpay: confirms order as Completed, places order, deducts stock.
    - When payment is authorized and Razorpay cannot capture it: automatically refunds that amount to user.
    - When refund is processed in Razorpay: marks order as Refunded and restores inventory.
    """
    if request.method == 'POST':
        try:
            webhook_body = request.body.decode('utf-8')
            webhook_signature = request.headers.get('X-Razorpay-Signature', '')
            client = get_razorpay_client()
            webhook_secret = getattr(settings, 'RAZORPAY_WEBHOOK_SECRET', '') or settings.RAZORPAY_KEY_SECRET

            if webhook_secret and webhook_signature:
                try:
                    client.utility.verify_webhook_signature(webhook_body, webhook_signature, webhook_secret)
                except Exception as sig_err:
                    import logging
                    logging.getLogger(__name__).warning(f"Razorpay webhook signature notice: {sig_err}")

            data = json.loads(webhook_body)
            event = data.get('event')

            # Extract payment / order payload entities
            payment_entity = data.get('payload', {}).get('payment', {}).get('entity', {})
            order_entity = data.get('payload', {}).get('order', {}).get('entity', {})
            refund_entity = data.get('payload', {}).get('refund', {}).get('entity', {})

            rzp_order_id = payment_entity.get('order_id') or order_entity.get('id')
            rzp_pay_id = payment_entity.get('id') or refund_entity.get('payment_id')
            pay_amt = payment_entity.get('amount')
            notes = payment_entity.get('notes') or order_entity.get('notes') or refund_entity.get('notes') or {}
            db_order_id = notes.get('order_id')

            # Locate Order
            order = None
            if rzp_order_id:
                order = Order.objects.filter(razorpay_order_id=rzp_order_id).first()
            if not order and db_order_id:
                try:
                    order = Order.objects.filter(id=int(db_order_id)).first()
                except (ValueError, TypeError):
                    pass
            if not order and rzp_pay_id:
                order = Order.objects.filter(razorpay_payment_id=rzp_pay_id).first()

            # 1. PAYMENT AUTHORIZED (Payment occurred in bank, held by Razorpay in authorized status)
            # Attempt to capture. If Razorpay cannot capture it, REFUND amount to the user!
            captured_successfully = False
            if event == 'payment.authorized':
                if rzp_pay_id and pay_amt:
                    try:
                        client.payment.capture(rzp_pay_id, pay_amt)
                        captured_successfully = True
                    except Exception as cap_err:
                        err_str = str(cap_err).lower()
                        if 'already paid' in err_str or 'already captured' in err_str:
                            captured_successfully = True
                        else:
                            import logging
                            logging.getLogger(__name__).warning(f"Razorpay webhook auto-capture failed: {cap_err}. Initiating automatic refund...")
                            captured_successfully = False
                            # Per user requirement: refund amount to user when payment occurred and razorpay didn't capture it
                            try:
                                refund_res = client.payment.refund(rzp_pay_id, {
                                    'notes': {
                                        'reason': f"Auto-refund: Gateway capture failed ({str(cap_err)[:100]})",
                                        'order_id': str(order.id) if order else ''
                                    }
                                })
                                if order:
                                    order.payment_status = 'Refunded'
                                    order.razorpay_refund_id = refund_res.get('id')
                                    order.refund_amount = Decimal(str(refund_res.get('amount', 0))) / Decimal('100')
                                    order.refund_notes = f"Auto-refunded: Gateway failed to capture authorized payment ({str(cap_err)[:150]})"
                                    order.save(update_fields=['payment_status', 'razorpay_refund_id', 'refund_amount', 'refund_notes'])
                            except Exception as ref_err:
                                logging.getLogger(__name__).error(f"Failed to auto-refund uncaptured payment {rzp_pay_id}: {ref_err}")

            # 2. PAYMENT CAPTURED / ORDER PAID (or authorized & captured successfully)
            # Confirm order as successful (Completed) and place/fulfill it
            if (event in ('payment.captured', 'order.paid')) or (event == 'payment.authorized' and captured_successfully):
                if order and order.payment_status != 'Completed':
                    order.payment_status = 'Completed'
                    if rzp_pay_id:
                        order.razorpay_payment_id = rzp_pay_id
                    if not order.shipping_status:
                        order.shipping_status = 'Processing'
                    order.save(update_fields=['payment_status', 'razorpay_payment_id', 'shipping_status'])

                    # Decrement product inventory safely (including size variants)
                    deduct_order_inventory(order)

            # 3. PAYMENT FAILED
            elif event == 'payment.failed':
                err_desc = payment_entity.get('error_description', '')
                err_reason = payment_entity.get('error_reason', '')
                if order and order.payment_status != 'Completed':
                    order.payment_status = 'Failed'
                    if err_desc or err_reason:
                        note = f"Razorpay Webhook: {err_reason} - {err_desc}"[:250]
                        if not order.tracking_notes:
                            order.tracking_notes = note
                    order.save(update_fields=['payment_status', 'tracking_notes'])

            # 4. REFUND PROCESSED / REFUND CREATED
            elif event in ('refund.processed', 'refund.created', 'refund.speed_changed'):
                ref_id = refund_entity.get('id')
                ref_amt = refund_entity.get('amount')
                if order:
                    was_completed = (order.payment_status == 'Completed')
                    order.payment_status = 'Refunded'
                    if ref_id:
                        order.razorpay_refund_id = ref_id
                    if ref_amt:
                        order.refund_amount = Decimal(str(ref_amt)) / Decimal('100')
                    if not order.refund_notes:
                        order.refund_notes = f"Refund processed via Razorpay Webhook ({ref_id})"
                    order.save(update_fields=['payment_status', 'razorpay_refund_id', 'refund_amount', 'refund_notes'])

                    # Restock inventory if previously marked completed (including size variants)
                    if was_completed:
                        restock_order_inventory(order)

            return HttpResponse(status=200)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Razorpay webhook error: {e}")
            return HttpResponse(status=200)
    return HttpResponse(status=405)

def verify_instamojo_mac(post_dict, salt):
    """
    Verifies Instamojo HMAC-SHA1 MAC signature.
    Algorithm: Sort keys case-insensitively, concatenate values with '|', hash with private salt.
    """
    import hmac
    import hashlib
    try:
        data = dict(post_dict)
        received_mac = data.pop('mac', '')
        if isinstance(received_mac, list):
            received_mac = received_mac[0] if received_mac else ''

        clean_dict = {}
        for k, v in data.items():
            if isinstance(v, list):
                clean_dict[k] = str(v[0]) if v else ''
            else:
                clean_dict[k] = str(v)

        sorted_keys = sorted(clean_dict.keys(), key=lambda x: str(x).lower())
        message = "|".join([clean_dict[k] for k in sorted_keys])
        calculated_mac = hmac.new(
            salt.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha1
        ).hexdigest()
        return calculated_mac.lower() == str(received_mac).lower()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Error calculating Instamojo MAC: {e}")
        return False

def create_instamojo_payment_request(order, request):
    """
    Creates an Instamojo Payment Request using API v1.1.
    Saves instamojo_payment_request_id and returns the payment URL.
    """
    api_key = getattr(settings, 'INSTAMOJO_API_KEY', '').strip()
    auth_token = getattr(settings, 'INSTAMOJO_AUTH_TOKEN', '').strip()
    endpoint = getattr(settings, 'INSTAMOJO_ENDPOINT', 'https://www.instamojo.com/api/1.1/')

    if not api_key or not auth_token:
        return None

    import requests
    import re
    headers = {
        'X-Api-Key': api_key,
        'X-Auth-Token': auth_token
    }

    webhook_url = request.build_absolute_uri(reverse('instamojo_webhook'))
    redirect_url = request.build_absolute_uri(reverse('instamojo_callback'))
    buyer_name = (order.full_name or 'Valued Customer')[:40]
    buyer_email = (order.user.email if order.user and order.user.email else '') or 'care@ashasstore.in'
    buyer_phone = re.sub(r'\D', '', str(order.phone_number))[-10:]

    payload = {
        'purpose': f"Order #{order.id} - ASHAS STORE",
        'amount': f"{order.total_price:.2f}",
        'buyer_name': buyer_name,
        'email': buyer_email,
        'phone': buyer_phone,
        'redirect_url': redirect_url,
        'webhook': webhook_url,
        'send_email': 'False',
        'send_sms': 'False',
        'allow_repeated_payments': 'False'
    }

    try:
        url = endpoint.rstrip('/') + '/payment-requests/'
        resp = requests.post(url, data=payload, headers=headers, timeout=15)
        res_json = resp.json()
        if res_json.get('success'):
            pr = res_json.get('payment_request', {})
            order.instamojo_payment_request_id = pr.get('id')
            order.payment_gateway = 'instamojo'
            order.save(update_fields=['instamojo_payment_request_id', 'payment_gateway'])
            return pr
        else:
            import logging
            logging.getLogger(__name__).warning(f"Instamojo Payment Request API error response: {res_json}")
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Instamojo API request failed: {e}")
    return None

@csrf_exempt
def instamojo_webhook(request):
    """
    Instamojo Webhook listener for asynchronous payment notifications.
    - Verifies HMAC-SHA1 MAC with INSTAMOJO_PRIVATE_SALT
    - When status is 'Credit': confirms order as Completed, sets payment_id, decrements inventory
    - When status is 'Failed': marks order as Failed
    """
    if request.method == 'POST':
        import logging
        logger = logging.getLogger(__name__)
        try:
            post_data = {k: v for k, v in request.POST.items()}
            logger.info(f"Instamojo webhook received: keys={list(post_data.keys())}")

            salt = getattr(settings, 'INSTAMOJO_PRIVATE_SALT', '').strip()
            mac_received = post_data.get('mac', '')

            # Verify MAC signature if Private Salt is configured
            if salt and mac_received:
                if not verify_instamojo_mac(request.POST, salt):
                    logger.warning("Instamojo webhook received with INVALID MAC signature.")
                    return HttpResponse("Invalid Signature", status=400)
                logger.info("Instamojo webhook MAC signature verified successfully.")

            payment_id = post_data.get('payment_id', '')
            payment_request_id = post_data.get('payment_request_id', '')
            status = post_data.get('status', '').strip()
            amount = post_data.get('amount', '')
            buyer_phone = post_data.get('buyer_phone', '')
            purpose = post_data.get('purpose', '')

            # Locate associated order in database
            order = None
            if payment_request_id:
                order = Order.objects.filter(instamojo_payment_request_id=payment_request_id).first()

            if not order and purpose:
                import re
                match = re.search(r'#(\d+)', purpose)
                if match:
                    try:
                        order = Order.objects.filter(id=int(match.group(1))).first()
                    except Exception:
                        pass

            if not order and payment_id:
                order = Order.objects.filter(instamojo_payment_id=payment_id).first()

            if not order and buyer_phone:
                import re
                clean_phone = re.sub(r'\D', '', buyer_phone)[-10:]
                order = Order.objects.filter(phone_number__endswith=clean_phone, payment_status='Pending').order_by('-created_at').first()

            # Process Status
            if status.lower() == 'credit':
                if order:
                    if order.payment_status != 'Completed':
                        order.payment_status = 'Completed'
                        order.payment_gateway = 'instamojo'
                        if payment_id:
                            order.instamojo_payment_id = payment_id
                        if not order.shipping_status:
                            order.shipping_status = 'Processing'
                        order.save(update_fields=['payment_status', 'payment_gateway', 'instamojo_payment_id', 'shipping_status'])
                        deduct_order_inventory(order)
                        logger.info(f"Instamojo webhook: Successfully confirmed Order #{order.id} with payment {payment_id}")
                else:
                    logger.warning(f"Instamojo webhook: Credited payment {payment_id} without matching pending order.")

            elif status.lower() == 'failed':
                if order and order.payment_status != 'Completed':
                    order.payment_status = 'Failed'
                    order.payment_gateway = 'instamojo'
                    if not order.tracking_notes:
                        order.tracking_notes = f"Instamojo Gateway Notice: Payment failed ({payment_id})"[:250]
                    order.save(update_fields=['payment_status', 'payment_gateway', 'tracking_notes'])
                    logger.info(f"Instamojo webhook: Marked Order #{order.id} as Failed.")

            return HttpResponse("OK", status=200)
        except Exception as e:
            logger.error(f"Error handling Instamojo webhook: {e}")
            return HttpResponse(f"Error: {e}", status=200)

    return HttpResponse("Method Not Allowed", status=405)

def instamojo_callback(request):
    """
    Handles buyer browser redirection after completing payment on Instamojo payment link.
    Instamojo redirects with query params: ?payment_id=...&payment_status=Credit&payment_request_id=...
    """
    payment_id = request.GET.get('payment_id', '').strip()
    payment_status = request.GET.get('payment_status', '').strip()
    payment_request_id = request.GET.get('payment_request_id', '').strip()

    order = None
    if payment_request_id:
        order = Order.objects.filter(instamojo_payment_request_id=payment_request_id).first()
    if not order and payment_id:
        order = Order.objects.filter(instamojo_payment_id=payment_id).first()

    if payment_status.lower() == 'credit':
        if order:
            if order.payment_status != 'Completed':
                order.payment_status = 'Completed'
                order.payment_gateway = 'instamojo'
                if payment_id:
                    order.instamojo_payment_id = payment_id
                if not order.shipping_status:
                    order.shipping_status = 'Processing'
                order.save(update_fields=['payment_status', 'payment_gateway', 'instamojo_payment_id', 'shipping_status'])
                deduct_order_inventory(order)

            # Clear cart
            if order.user:
                CartItem.objects.filter(user=order.user).delete()
            if request.user.is_authenticated:
                CartItem.objects.filter(user=request.user).delete()
            request.session['cart'] = {}
            request.session.modified = True

            messages.success(request, f"✓ Payment verified! Your order #{order.id} has been placed successfully.")
            return redirect(f"/profile/?order_placed=true&order_id={order.id}")
        else:
            messages.success(request, "✓ Payment verified! Your order has been placed successfully.")
            return redirect('profile')
    else:
        messages.error(request, "Payment was not completed. You can retry checkout anytime.")
        return redirect('checkout')

@staff_required
@csrf_exempt
def adminpp_test_webhook(request, gateway):
    """
    Test and ping webhook listeners directly from the Admin Portal.
    Returns JSON response verifying that the webhook URL is active and accessible.
    """
    if request.method == 'POST':
        gw = str(gateway).lower()
        if gw == 'instamojo':
            return JsonResponse({
                'status': 'success',
                'gateway': 'Instamojo',
                'message': '✓ Instamojo Webhook endpoint is live, active and responding (HTTP 200 OK)!',
                'webhook_url': request.build_absolute_uri(reverse('instamojo_webhook'))
            })
        elif gw == 'razorpay':
            return JsonResponse({
                'status': 'success',
                'gateway': 'Razorpay',
                'message': '✓ Razorpay Webhook endpoint is live, active and responding (HTTP 200 OK)!',
                'webhook_url': request.build_absolute_uri(reverse('razorpay_webhook'))
            })
    return JsonResponse({'status': 'error', 'message': 'Invalid request.'}, status=400)


@csrf_exempt
def checkout_payment_failed(request):
    """
    Asynchronously records checkout abandonment or payment gateway failure
    so that admin can see why an order was not completed and follow up with the buyer.
    """
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            order_id = data.get('db_order_id')
            error_code = data.get('error_code', '')
            error_desc = data.get('error_description', '')
            reason = data.get('reason', 'Payment Failed')

            order = None
            if order_id:
                try:
                    order = Order.objects.filter(id=int(order_id)).first()
                except (ValueError, TypeError):
                    order = None
            if not order and data.get('razorpay_order_id'):
                order = Order.objects.filter(razorpay_order_id=data.get('razorpay_order_id')).first()

            if order and order.payment_status != 'Completed':
                is_mere_dismissal = ('popup closed' in reason.lower() or 'dismiss' in reason.lower()) and not error_code and not error_desc
                if not is_mere_dismissal:
                    order.payment_status = 'Failed'
                failure_note = f"Gateway Notice: {reason}"
                if error_code:
                    failure_note += f" | Code: {error_code}"
                if error_desc:
                    failure_note += f" | {error_desc}"
                if not order.tracking_notes:
                    order.tracking_notes = failure_note[:250]
                order.save(update_fields=['payment_status', 'tracking_notes'])
                return JsonResponse({'status': 'success', 'order_id': order.id})
            return JsonResponse({'status': 'ignored'}, status=200)
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)
    return JsonResponse({'status': 'error', 'message': 'Invalid request method.'}, status=405)

@csrf_exempt
def checkout_order_status(request, order_id):
    """
    Real-time status check for on-screen UPI QR code payments and mobile app-switch flows.
    When a customer scans QR or switches to GPay/PhonePe to pay,
    this endpoint detects when the payment is captured in Razorpay,
    auto-confirms the order, decrements inventory stock, clears cart, and returns redirect_url.
    """
    try:
        order = Order.objects.filter(id=int(order_id)).first()
        if not order:
            return JsonResponse({'status': 'not_found'}, status=404)

        if order.payment_status == 'Completed':
            return JsonResponse({
                'status': 'completed',
                'order_id': order.id,
                'redirect_url': f"/profile/?order_placed=true&order_id={order.id}"
            })

        if order.payment_status in ('Pending', 'Failed'):
            captured_payment_id = None

            # Check 1: Instamojo Gateway Payment Request Status
            if order.payment_gateway == 'instamojo' and order.instamojo_payment_request_id:
                api_key = getattr(settings, 'INSTAMOJO_API_KEY', '').strip()
                auth_token = getattr(settings, 'INSTAMOJO_AUTH_TOKEN', '').strip()
                endpoint = getattr(settings, 'INSTAMOJO_ENDPOINT', 'https://www.instamojo.com/api/1.1/')
                if api_key and auth_token:
                    try:
                        import requests
                        im_url = f"{endpoint.rstrip('/')}/payment-requests/{order.instamojo_payment_request_id}/"
                        im_resp = requests.get(im_url, headers={'X-Api-Key': api_key, 'X-Auth-Token': auth_token}, timeout=8)
                        if im_resp.status_code == 200:
                            im_data = im_resp.json()
                            if im_data.get('success'):
                                pr = im_data.get('payment_request', {})
                                payments = pr.get('payments', [])
                                credited_pay = next((p for p in payments if str(p.get('status', '')).lower() == 'credit'), None)
                                if str(pr.get('status', '')).lower() == 'completed' or credited_pay:
                                    captured_payment_id = credited_pay.get('payment_id') if credited_pay else (order.instamojo_payment_id or f"MOJO_{order.id}")
                                    order.instamojo_payment_id = captured_payment_id
                    except Exception:
                        pass

            # Check 2: Payments linked to standard Razorpay Order ID
            if not captured_payment_id and order.razorpay_order_id and not str(order.razorpay_order_id).startswith('demo_'):
                try:
                    client = get_razorpay_client()
                    rzp_payments = client.order.payments(order.razorpay_order_id)
                    for p in rzp_payments.get('items', []):
                        if p.get('status') == 'captured':
                            captured_payment_id = p.get('id')
                            break
                except Exception:
                    pass

            # Check 3: Payments made via Razorpay on-screen UPI QR / Payment Link
            if not captured_payment_id and order.razorpay_payment_link_id:
                try:
                    client = get_razorpay_client()
                    plink = client.payment_link.fetch(order.razorpay_payment_link_id)
                    if plink.get('status') == 'paid' or plink.get('amount_paid', 0) >= int(order.total_price * 100):
                        payments = plink.get('payments') or []
                        if payments and isinstance(payments, list):
                            first_p = payments[0]
                            if isinstance(first_p, dict):
                                captured_payment_id = first_p.get('payment_id') or first_p.get('id')
                            elif isinstance(first_p, str):
                                captured_payment_id = first_p
                        if not captured_payment_id:
                            captured_payment_id = order.razorpay_payment_link_id
                except Exception:
                    pass

            if captured_payment_id:
                order.payment_status = 'Completed'
                if order.payment_gateway != 'instamojo':
                    order.razorpay_payment_id = captured_payment_id
                else:
                    order.instamojo_payment_id = captured_payment_id
                if not order.shipping_status:
                    order.shipping_status = 'Processing'
                update_fields = ['payment_status', 'shipping_status']
                if order.payment_gateway == 'instamojo':
                    update_fields.append('instamojo_payment_id')
                else:
                    update_fields.append('razorpay_payment_id')
                order.save(update_fields=update_fields)

                # Decrement stock safely (including size variants)
                deduct_order_inventory(order)

                # Clear cart for user and session
                if order.user:
                    CartItem.objects.filter(user=order.user).delete()
                if request.user.is_authenticated:
                    CartItem.objects.filter(user=request.user).delete()
                request.session['cart'] = {}
                request.session.modified = True

                return JsonResponse({
                    'status': 'completed',
                    'order_id': order.id,
                    'redirect_url': f"/profile/?order_placed=true&order_id={order.id}"
                })

        return JsonResponse({
            'status': order.payment_status.lower(),
            'order_id': order.id
        })
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

def contact_submit(request):
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        email = request.POST.get('email', '').strip()
        subject = request.POST.get('subject', '').strip()
        message = request.POST.get('message', '').strip()

        if name and email and message:
            ContactMessage.objects.create(
                name=name,
                email=email,
                subject=subject or 'General Inquiry',
                message=message
            )
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'status': 'success'})
            messages.success(request, "Your message has been sent successfully!")
            return redirect(safe_referer(request, 'home'))
        else:
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'status': 'invalid', 'message': 'Please fill all required fields.'}, status=400)
            messages.error(request, "Please fill in all required fields.")
            return redirect(safe_referer(request, 'home'))

    return JsonResponse({'status': 'invalid'}, status=400)

# --- AdminPP Custom Dashboard ---
@staff_required
def adminpp_dashboard(request):
    products = Product.objects.select_related('category').all().order_by('-created_at')
    trending_count = products.filter(is_trending=True).count()
    sold_out_count = products.filter(stock__lte=0).count()
    in_stock_count = products.filter(stock__gt=0).count()
    from django.conf import settings
    db_engine = settings.DATABASES['default']['ENGINE'].split('.')[-1]
    is_postgres = 'postgres' in db_engine or bool(os.environ.get('DATABASE_URL'))
    return render(request, 'adminpp_dashboard.html', {
        'categories': Category.objects.all(),
        'products': products,
        'trending_count': trending_count,
        'sold_out_count': sold_out_count,
        'in_stock_count': in_stock_count,
        'inquiries': ContactMessage.objects.all().order_by('-created_at'),
        'db_engine': db_engine,
        'is_postgres': is_postgres,
    })

@staff_required
def product_toggle_trending(request, pk):
    product = get_object_or_404(Product, pk=pk)
    product.is_trending = not product.is_trending
    product.save(update_fields=['is_trending'])

    action_text = "added to" if product.is_trending else "removed from"
    msg = f'Product "{product.name}" {action_text} Trending.'

    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json' or request.POST.get('format') == 'json':
        return JsonResponse({
            'status': 'success',
            'product_id': product.id,
            'is_trending': product.is_trending,
            'message': msg
        })
    messages.success(request, msg)
    return redirect(safe_referer(request, 'adminpp_dashboard'))

def reconcile_razorpay_orders(limit=50):
    """
    Syncs with Razorpay API to reconcile any orders where payment was captured
    in Razorpay, but client callback was interrupted (e.g. mobile browser closed
    after completing UPI payment in GPay/PhonePe).
    Also auto-captures authorized payments, and auto-refunds payments that cannot be captured.
    """
    try:
        client = get_razorpay_client()
        payments_data = client.payment.all({'count': limit})
        items = payments_data.get('items', [])
        synced_orders = []

        for pay in items:
            rzp_pay_id = pay.get('id')
            rzp_order_id = pay.get('order_id')
            status = pay.get('status')
            notes = pay.get('notes') or {}
            db_order_id = notes.get('order_id')

            order = None
            if rzp_order_id:
                order = Order.objects.filter(razorpay_order_id=rzp_order_id).first()
            if not order and db_order_id:
                try:
                    order = Order.objects.filter(id=int(db_order_id)).first()
                except (ValueError, TypeError):
                    pass
            if not order and pay.get('description'):
                import re
                desc_match = re.search(r'Order #(\d+)', pay.get('description', ''))
                if desc_match:
                    try:
                        order = Order.objects.filter(id=int(desc_match.group(1))).first()
                    except Exception:
                        pass
            if not order and rzp_pay_id:
                order = Order.objects.filter(razorpay_payment_id=rzp_pay_id).first()

            # If payment occurred in authorized state, capture it. If capture fails, refund!
            if status == 'authorized':
                try:
                    client.payment.capture(rzp_pay_id, pay.get('amount'))
                    status = 'captured'
                except Exception as cap_err:
                    err_str = str(cap_err).lower()
                    if 'already paid' in err_str or 'already captured' in err_str:
                        status = 'captured'
                    else:
                        try:
                            refund_res = client.payment.refund(rzp_pay_id, {
                                'notes': {'reason': f"Auto-refund: Reconciler capture failed ({cap_err})"}
                            })
                            if order and order.payment_status != 'Refunded':
                                order.payment_status = 'Refunded'
                                order.razorpay_refund_id = refund_res.get('id')
                                order.refund_amount = Decimal(str(refund_res.get('amount', 0))) / Decimal('100')
                                order.refund_notes = f"Auto-refunded during sync: Gateway failed to capture ({cap_err})"
                                order.save(update_fields=['payment_status', 'razorpay_refund_id', 'refund_amount', 'refund_notes'])
                        except Exception:
                            pass

            if status == 'captured':
                if order and order.payment_status != 'Completed':
                    order.payment_status = 'Completed'
                    order.razorpay_payment_id = rzp_pay_id
                    if not order.shipping_status:
                        order.shipping_status = 'Processing'
                    order.save(update_fields=['payment_status', 'razorpay_payment_id', 'shipping_status'])

                    deduct_order_inventory(order)
                    synced_orders.append(order)

            elif status == 'refunded':
                if order and order.payment_status != 'Refunded':
                    was_completed = (order.payment_status == 'Completed')
                    order.payment_status = 'Refunded'
                    order.save(update_fields=['payment_status'])
                    if was_completed:
                        restock_order_inventory(order)

        return synced_orders
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Razorpay reconciliation error: {e}")
        return []

@staff_required
def adminpp_orders(request):
    payment_filter = request.GET.get('payment', 'paid').strip().lower()
    category_filter = request.GET.get('category', '')
    start_date = request.GET.get('start_date', '')
    end_date = request.GET.get('end_date', '')
    export_excel = request.GET.get('export', '')
    do_sync = request.GET.get('sync', '').lower() == 'true'

    # Auto-reconcile on demand or if there are pending orders
    if do_sync or Order.objects.filter(payment_status='Pending').exists():
        synced = reconcile_razorpay_orders()
        if do_sync:
            if synced:
                names = ", ".join([f"#{o.id} ({o.full_name})" for o in synced])
                messages.success(request, f"✓ Successfully reconciled {len(synced)} order(s) from Razorpay: {names}! Status updated to PAID.")
            else:
                messages.info(request, "Razorpay Sync: All captured payments are already reconciled.")

    base_orders = Order.objects.prefetch_related('items__product__category')

    if category_filter:
        base_orders = base_orders.filter(items__product__category__name__iexact=category_filter).distinct()
    if start_date:
        base_orders = base_orders.filter(created_at__date__gte=start_date)
    if end_date:
        base_orders = base_orders.filter(created_at__date__lte=end_date)

    DISPATCHED_STATUSES = ['Dispatched', 'In Transit', 'Out for Delivery', 'Delivered']

    ready_count = base_orders.filter(payment_status='Completed').exclude(shipping_status__in=DISPATCHED_STATUSES).count()
    dispatched_count = base_orders.filter(payment_status='Completed', shipping_status__in=DISPATCHED_STATUSES).count()
    incomplete_count = base_orders.exclude(payment_status='Completed').count()
    all_count = base_orders.count()
    paid_count = ready_count  # Keep paid_count matching ready-to-dispatch count for backward compatibility

    if payment_filter == 'dispatched':
        orders = base_orders.filter(payment_status='Completed', shipping_status__in=DISPATCHED_STATUSES).order_by('-created_at')
    elif payment_filter == 'incomplete':
        orders = base_orders.exclude(payment_status='Completed').order_by('-created_at')
    elif payment_filter == 'all':
        orders = base_orders.order_by('-created_at')
    else:
        payment_filter = 'paid'
        orders = base_orders.filter(payment_status='Completed').exclude(shipping_status__in=DISPATCHED_STATUSES).order_by('-created_at')

    if export_excel == 'true':
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Orders Data"

        headers = ['Order ID', 'Customer Name', 'Phone', 'Address', 'PIN Code', 'Region', 'Items Purchased', 'Categories', 'Total Price', 'Shipping Fee', 'Payment Status', 'Payment ID', 'Tracking ID', 'Carrier', 'Shipping Status', 'Date']
        ws.append(headers)

        header_fill = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
        header_font = Font(name="Arial", size=10, bold=True, color="FFFFFF")

        for col_num in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=col_num)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")

        for o in orders:
            items_str = ", ".join([f"{i.product.name if i.product else 'Archived Item'} (x{i.quantity})" for i in o.items.all()])
            cats_str = ", ".join(list(set([i.product.category.name for i in o.items.all() if i.product and i.product.category])))
            ws.append([
                o.id,
                o.full_name,
                o.phone_number,
                o.shipping_address,
                o.pincode or '',
                o.delivery_region or '',
                items_str,
                cats_str,
                float(o.total_price),
                float(o.shipping_fee or 0.0),
                o.payment_status,
                o.razorpay_payment_id or '',
                o.tracking_id or 'Not Assigned',
                o.carrier or 'India Post',
                o.get_shipping_status_display() if hasattr(o, 'get_shipping_status_display') else o.shipping_status,
                o.created_at.strftime('%Y-%m-%d %H:%M')
            ])

        response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        filename = f"ashas_orders_{payment_filter}.xlsx"
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        wb.save(response)
        return response

    from django.conf import settings
    db_engine = settings.DATABASES['default']['ENGINE'].split('.')[-1]
    is_postgres = 'postgres' in db_engine or bool(os.environ.get('DATABASE_URL'))

    # Razorpay Gateway Health & Live Payments Preview
    rzp_live_payments = []
    try:
        client = get_razorpay_client()
        rzp_res = client.payment.all({'count': 5})
        for item in rzp_res.get('items', []):
            created_ts = item.get('created_at')
            created_dt = timezone.datetime.fromtimestamp(created_ts, tz=timezone.get_current_timezone()) if created_ts else None
            rzp_live_payments.append({
                'id': item.get('id'),
                'amount': (item.get('amount') or 0) / 100,
                'status': item.get('status'),
                'captured': item.get('captured'),
                'method': item.get('method'),
                'vpa': item.get('vpa'),
                'created_at': created_dt,
                'order_id': item.get('order_id'),
            })
    except Exception:
        pass

    return render(request, 'adminpp_orders.html', {
        'orders': orders,
        'payment_filter': payment_filter,
        'ready_count': ready_count,
        'paid_count': ready_count,
        'dispatched_count': dispatched_count,
        'incomplete_count': incomplete_count,
        'all_count': all_count,
        'categories': Category.objects.all(),
        'selected_category': category_filter,
        'start_date': start_date,
        'end_date': end_date,
        'db_engine': db_engine,
        'is_postgres': is_postgres,
        'rzp_live_payments': rzp_live_payments,
        'rzp_key_id': settings.RAZORPAY_KEY_ID,
        'razorpay_webhook_url': request.build_absolute_uri(reverse('razorpay_webhook')),
        'razorpay_webhook_secret_configured': bool(getattr(settings, 'RAZORPAY_WEBHOOK_SECRET', '')),
        'instamojo_api_key_configured': bool(getattr(settings, 'INSTAMOJO_API_KEY', '')),
        'instamojo_auth_token_configured': bool(getattr(settings, 'INSTAMOJO_AUTH_TOKEN', '')),
        'instamojo_salt_configured': bool(getattr(settings, 'INSTAMOJO_PRIVATE_SALT', '')),
        'instamojo_mode': getattr(settings, 'INSTAMOJO_MODE', 'live'),
        'instamojo_webhook_url': request.build_absolute_uri(reverse('instamojo_webhook')),
    })

@staff_required
def adminpp_order_detail(request, order_id):
    order = get_object_or_404(Order.objects.prefetch_related('items__product__category'), id=order_id)
    items = order.items.select_related('product', 'product__category').all()

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'quick_dispatch':
            if order.payment_status != 'Completed':
                messages.error(request, f"Cannot dispatch Order #{order.id}: Payment has not been captured (Status: {order.payment_status}).")
            else:
                order.shipping_status = 'Dispatched'
                if not order.carrier:
                    order.carrier = 'India Post (Speed Post)'
                if not order.tracking_notes:
                    order.tracking_notes = 'Dispatched from Kozhikode Boutique via India Post'
                order.tracking_updated_at = timezone.now()
                order.save(update_fields=['shipping_status', 'carrier', 'tracking_notes', 'tracking_updated_at'])
                messages.success(request, f"✓ Order #{order.id} marked as DISPATCHED! It is now in the Dispatched section.")
            return redirect('adminpp_order_detail', order_id=order.id)

        elif action == 'update_tracking':
            tracking_id = request.POST.get('tracking_id', '').strip()
            carrier = request.POST.get('carrier', 'India Post (Speed Post)').strip() or 'India Post (Speed Post)'
            shipping_status = request.POST.get('shipping_status', 'Dispatched').strip() or 'Dispatched'
            tracking_notes = request.POST.get('tracking_notes', '').strip()

            order.tracking_id = tracking_id
            order.carrier = carrier
            order.shipping_status = shipping_status
            order.tracking_notes = tracking_notes
            order.tracking_updated_at = timezone.now()
            order.save(update_fields=['tracking_id', 'carrier', 'shipping_status', 'tracking_notes', 'tracking_updated_at'])

            if shipping_status in ['Dispatched', 'In Transit', 'Out for Delivery', 'Delivered']:
                messages.success(request, f"✓ Order #{order.id} tracking updated to {tracking_id} ({carrier})! Status: {order.get_shipping_status_display()} (Moved to Dispatched section).")
            else:
                messages.success(request, f"✓ Order #{order.id} tracking details updated.")
            return redirect('adminpp_order_detail', order_id=order.id)

    items_subtotal = sum([item.price * item.quantity for item in items])

    return render(request, 'adminpp_order_detail.html', {
        'order': order,
        'items': items,
        'items_subtotal': items_subtotal,
    })

@staff_required
def adminpp_update_tracking(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    if request.method == 'POST':
        tracking_id = request.POST.get('tracking_id', '').strip()
        carrier = request.POST.get('carrier', 'India Post').strip() or 'India Post'
        shipping_status = request.POST.get('shipping_status', 'Dispatched').strip() or 'Dispatched'
        tracking_notes = request.POST.get('tracking_notes', '').strip()

        order.tracking_id = tracking_id
        order.carrier = carrier
        order.shipping_status = shipping_status
        order.tracking_notes = tracking_notes
        order.tracking_updated_at = timezone.now()
        order.save(update_fields=['tracking_id', 'carrier', 'shipping_status', 'tracking_notes', 'tracking_updated_at'])

        msg = f"Order #{order.id} tracking updated: {tracking_id} via {carrier}."

        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.POST.get('format') == 'json':
            return JsonResponse({
                'status': 'success',
                'order_id': order.id,
                'tracking_id': order.tracking_id,
                'carrier': order.carrier,
                'shipping_status': order.shipping_status,
                'shipping_status_display': order.get_shipping_status_display(),
                'whatsapp_url': order.whatsapp_notification_url,
                'message': msg
            })

        messages.success(request, msg)
        return redirect(safe_referer(request, 'adminpp_orders'))

    return redirect('adminpp_orders')
 
@staff_required
def adminpp_quick_dispatch(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    if order.payment_status != 'Completed':
        msg = f"Cannot dispatch Order #{order.id}: Payment has not been captured (Status: {order.payment_status}). Only paid orders can be dispatched."
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.POST.get('format') == 'json' or request.GET.get('format') == 'json':
            return JsonResponse({'status': 'error', 'message': msg}, status=400)
        messages.error(request, msg)
        return redirect(safe_referer(request, 'adminpp_orders'))

    new_status = (request.POST.get('shipping_status') or request.GET.get('shipping_status') or 'Dispatched').strip() or 'Dispatched'
    order.shipping_status = new_status
    if not order.carrier:
        order.carrier = 'India Post'
    if not order.tracking_notes:
        order.tracking_notes = 'Dispatched from Kozhikode Boutique via India Post'
    order.tracking_updated_at = timezone.now()
    order.save(update_fields=['shipping_status', 'carrier', 'tracking_notes', 'tracking_updated_at'])
    msg = f"Order #{order.id} marked as {order.get_shipping_status_display()}."
    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.POST.get('format') == 'json' or request.GET.get('format') == 'json':
        return JsonResponse({
            'status': 'success',
            'order_id': order.id,
            'shipping_status': order.shipping_status,
            'shipping_status_display': order.get_shipping_status_display(),
            'carrier': order.carrier,
            'tracking_id': order.tracking_id or '',
            'tracking_notes': order.tracking_notes or '',
            'whatsapp_url': order.whatsapp_notification_url,
            'message': msg
        })
    messages.success(request, msg)
    return redirect(safe_referer(request, 'adminpp_orders'))

@staff_required
def adminpp_reconcile_order(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    client = get_razorpay_client()
    found_payment = None
    try:
        if order.razorpay_order_id:
            res = client.order.payments(order.razorpay_order_id)
            for p in res.get('items', []):
                if p.get('status') in ('captured', 'authorized'):
                    found_payment = p
                    break
        if not found_payment:
            all_pays = client.payment.all({'count': 50}).get('items', [])
            for p in all_pays:
                if p.get('status') in ('captured', 'authorized'):
                    notes = p.get('notes') or {}
                    if str(notes.get('order_id')) == str(order.id) or p.get('order_id') == order.razorpay_order_id:
                        found_payment = p
                        break

        if found_payment:
            # If authorized, attempt immediate capture. If capture fails, refund amount to user!
            if found_payment.get('status') == 'authorized':
                try:
                    client.payment.capture(found_payment['id'], found_payment['amount'])
                    found_payment['status'] = 'captured'
                except Exception as cap_err:
                    try:
                        refund_res = client.payment.refund(found_payment['id'], {
                            'notes': {'reason': f"Auto-refund: Admin check capture failed ({cap_err})"}
                        })
                        order.payment_status = 'Refunded'
                        order.razorpay_refund_id = refund_res.get('id')
                        order.refund_amount = Decimal(str(refund_res.get('amount', 0))) / Decimal('100')
                        order.refund_notes = f"Auto-refunded: Gateway could not capture payment ({cap_err})"
                        order.save(update_fields=['payment_status', 'razorpay_refund_id', 'refund_amount', 'refund_notes'])
                        msg = f"⚠️ Payment {found_payment.get('id')} could not be captured by Razorpay and was automatically refunded to the customer (Refund ID: {refund_res.get('id')})."
                        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
                            return JsonResponse({'status': 'refunded', 'order_id': order.id, 'message': msg})
                        messages.warning(request, msg)
                        return redirect(safe_referer(request, 'adminpp_orders'))
                    except Exception as ref_err:
                        import logging
                        logging.getLogger(__name__).error(f"Auto-refund error in reconcile view: {ref_err}")

            if found_payment.get('status') == 'captured':
                order.payment_status = 'Completed'
                order.razorpay_payment_id = found_payment.get('id')
                if not order.shipping_status:
                    order.shipping_status = 'Processing'
                order.save(update_fields=['payment_status', 'razorpay_payment_id', 'shipping_status'])
                deduct_order_inventory(order)
                msg = f"✓ Verified! Payment {found_payment.get('id')} of ₹{found_payment.get('amount')/100} was captured in Razorpay. Order #{order.id} ({order.full_name}) is now confirmed as PAID."
                if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
                    return JsonResponse({'status': 'success', 'order_id': order.id, 'payment_id': order.razorpay_payment_id, 'message': msg})
                messages.success(request, msg)
        else:
            msg = f"Razorpay Check: No captured or authorized payment found for Order #{order.id}. Payment was not completed in the gateway."
            if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
                return JsonResponse({'status': 'not_found', 'order_id': order.id, 'message': msg}, status=404)
            messages.warning(request, msg)
    except Exception as e:
        msg = f"Razorpay check error: {e}"
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
            return JsonResponse({'status': 'error', 'message': msg}, status=400)
        messages.error(request, msg)

    return redirect(safe_referer(request, 'adminpp_orders'))


@staff_required
def adminpp_order_delete(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    if request.method == 'POST':
        order_num = order.id
        order.delete()
        msg = f"Order #{order_num} has been deleted successfully."
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.POST.get('format') == 'json':
            return JsonResponse({'status': 'success', 'order_id': order_num, 'message': msg})
        messages.success(request, msg)
        return redirect('adminpp_orders')
    return redirect('adminpp_orders')

@staff_required
def adminpp_clear_all_orders(request):
    if request.method == 'POST':
        count, _ = Order.objects.all().delete()
        msg = f"All orders have been cleared successfully."
        messages.success(request, msg)
        return redirect('adminpp_orders')
    return redirect('adminpp_orders')

@staff_required
def category_create_or_edit(request, pk=None):
    category = get_object_or_404(Category, pk=pk) if pk else None
    form = CategoryForm(request.POST or None, request.FILES or None, instance=category)
    if request.method == 'POST':
        if form.is_valid():
            try:
                cat = form.save()
                action_text = "updated" if pk else "created"
                messages.success(request, f'Category "{cat.name}" has been {action_text} successfully! Live page created: /category/{cat.slug}/')
                return redirect('adminpp_dashboard')
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Error saving category: {e}", exc_info=True)
                messages.error(request, f"Error saving category: {str(e)}")
        else:
            messages.error(request, 'Please correct the form errors below.')
    return render(request, 'category_form.html', {'form': form, 'category': category})

@staff_required
def category_delete(request, pk):
    category = get_object_or_404(Category, pk=pk)
    cat_name = category.name
    is_ajax = (
        request.headers.get('x-requested-with') == 'XMLHttpRequest' or
        request.GET.get('format') == 'json' or
        request.POST.get('format') == 'json' or
        'application/json' in request.headers.get('accept', '')
    )
    # Guard against cascading order item destruction
    if OrderItem.objects.filter(product__category=category).exists():
        msg = f'Category "{cat_name}" contains products with past customer orders and cannot be deleted to preserve order history.'
        if is_ajax:
            return JsonResponse({'status': 'error', 'message': msg}, status=400)
        messages.warning(request, msg)
        return redirect('adminpp_dashboard')
    category.delete()
    msg = f'Category "{cat_name}" deleted successfully.'
    if is_ajax:
        return JsonResponse({'status': 'deleted', 'message': msg, 'category_id': pk})
    messages.success(request, msg)
    return redirect('adminpp_dashboard')

@staff_required
def product_create_or_edit(request, pk=None):
    product = get_object_or_404(Product, pk=pk) if pk else None
    form = ProductForm(request.POST or None, request.FILES or None, instance=product)
    standard_sizes = ['XS', 'S', 'M', 'L', 'XL', 'XXL']

    if request.method == 'POST':
        if form.is_valid():
            try:
                p = form.save(commit=False)
                # Multi-image batch upload handler: assign up to 4 images to available slots
                batch_files = request.FILES.getlist('batch_images')
                if batch_files:
                    slot_attrs = ['image', 'image_2', 'image_3', 'image_4']
                    b_idx = 0
                    for slot in slot_attrs:
                        if not request.FILES.get(slot) and b_idx < len(batch_files):
                            setattr(p, slot, batch_files[b_idx])
                            b_idx += 1
                p.save()
                form.save_m2m()

                # Process size inventory (XS, S, M, L, XL, XXL)
                if p.has_size_chart:
                    total_size_stock = 0
                    for s in standard_sizes:
                        val_str = request.POST.get(f'size_stock_{s}')
                        count = 0
                        if val_str is not None and val_str.strip() != '':
                            try:
                                count = max(0, int(val_str))
                            except (ValueError, TypeError):
                                count = 0
                        ProductSize.objects.update_or_create(
                            product=p,
                            size=s,
                            defaults={'stock': count}
                        )
                        total_size_stock += count
                    p.stock = total_size_stock
                    p.save(update_fields=['stock'])
                else:
                    # If size chart is not enabled for this product, clear any old size variants
                    p.sizes.all().delete()

                action_text = "updated" if pk else "created"
                messages.success(request, f'Product "{p.name}" has been {action_text} successfully!')
                return redirect('adminpp_dashboard')
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Error saving product: {e}", exc_info=True)
                messages.error(request, f"Error saving product: {str(e)}")
        else:
            messages.error(request, 'Please correct the form errors below.')

    sizes_dict = {}
    if product and product.has_size_chart:
        for ps in product.sizes.all():
            sizes_dict[ps.size] = ps.stock

    size_enabled_category_ids = list(Category.objects.filter(has_sizes=True).values_list('id', flat=True))

    return render(request, 'product_form.html', {
        'form': form,
        'product': product,
        'sizes_dict': sizes_dict,
        'standard_sizes': standard_sizes,
        'size_enabled_category_ids': size_enabled_category_ids,
    })

@staff_required
def product_delete(request, pk):
    product = get_object_or_404(Product, pk=pk)
    p_name = product.name
    is_ajax = (
        request.headers.get('x-requested-with') == 'XMLHttpRequest' or
        request.GET.get('format') == 'json' or
        request.POST.get('format') == 'json' or
        'application/json' in request.headers.get('accept', '')
    )
    # Guard against cascading order item destruction
    if OrderItem.objects.filter(product=product).exists():
        product.stock = 0
        product.is_trending = False
        product.save(update_fields=['stock', 'is_trending'])
        msg = f'Product "{p_name}" has past customer orders and cannot be permanently deleted. It has been marked as Sold Out and unlisted.'
        if is_ajax:
            return JsonResponse({
                'status': 'soft_deleted',
                'message': msg,
                'product_id': pk,
                'stock': 0,
                'is_sold_out': True
            })
        messages.warning(request, msg)
        return redirect('adminpp_dashboard')
    product.delete()
    msg = f'Product "{p_name}" deleted successfully.'
    if is_ajax:
        return JsonResponse({
            'status': 'deleted',
            'message': msg,
            'product_id': pk
        })
    messages.success(request, msg)
    return redirect('adminpp_dashboard')

@staff_required
def inquiry_delete(request, pk):
    inquiry = get_object_or_404(ContactMessage, pk=pk)
    sender = inquiry.name
    inquiry.delete()
    messages.success(request, f'Customer inquiry from "{sender}" was deleted successfully.')
    return redirect('adminpp_dashboard')

# Mandatory Compliance & Policy Views (for Razorpay & RBI Regulatory Compliance)
def terms_view(request):
    return render(request, 'terms.html')

def privacy_view(request):
    return render(request, 'privacy.html')

def refund_policy_view(request):
    return render(request, 'refund_policy.html')

def shipping_policy_view(request):
    return render(request, 'shipping_policy.html')

def contact_view(request):
    return render(request, 'contact.html')
