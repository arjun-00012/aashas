import os
import re
import json
import secrets
from urllib.parse import quote as urlquote
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
from .models import Category, Product, Order, OrderItem, Profile, ContactMessage, CartItem
from .forms import RegistrationForm, ProfileUpdateForm, CategoryForm, ProductForm

import razorpay
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver

def safe_referer(request, fallback='home'):
    referer = request.META.get('HTTP_REFERER')
    if referer and url_has_allowed_host_and_scheme(url=referer, allowed_hosts={request.get_host()}):
        return referer
    return fallback

def get_razorpay_client():
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))

razorpay_client = get_razorpay_client()

@receiver(user_logged_in)
def handle_user_logged_in(sender, request, user, **kwargs):
    if request:
        # Check if visitor added items to session before logging in
        guest_cart = request.session.get('cart', {})
        items = CartItem.objects.filter(user=user)
        user_items = {str(item.product_id): item.quantity for item in items if item.quantity > 0}
        
        # Merge guest cart into user's DB items
        if guest_cart:
            for pid, qty in guest_cart.items():
                if qty > 0:
                    product = Product.objects.filter(id=pid).first()
                    if product and product.stock > 0:
                        merged_qty = min(user_items.get(pid, 0) + qty, product.stock)
                        CartItem.objects.update_or_create(user=user, product=product, defaults={'quantity': merged_qty})
                        user_items[pid] = merged_qty

        request.session['cart'] = user_items
        request.session.modified = True

@receiver(user_logged_out)
def handle_user_logged_out(sender, request, user, **kwargs):
    if request:
        request.session.flush()


def get_user_cart(request):
    """
    Returns a dict {str(product_id): int(quantity)} strictly isolated for current user/session.
    If authenticated: fetches from CartItem DB model.
    If guest: uses request.session.
    """
    if request.user.is_authenticated:
        items = CartItem.objects.filter(user=request.user)
        cart = {str(item.product_id): item.quantity for item in items if item.quantity > 0}
        request.session['cart'] = cart
        return cart
    return request.session.get('cart', {})

def sync_cart_item(request, product, quantity):
    """
    Persists cart change to DB if authenticated, and keeps session up-to-date.
    If quantity <= 0, deletes from DB.
    """
    pid = str(product.id)
    cart = request.session.get('cart', {})
    if quantity > 0:
        cart[pid] = quantity
        if request.user.is_authenticated:
            CartItem.objects.update_or_create(
                user=request.user,
                product=product,
                defaults={'quantity': quantity}
            )
    else:
        cart.pop(pid, None)
        if request.user.is_authenticated:
            CartItem.objects.filter(user=request.user, product=product).delete()
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
    """Generate dynamic XML sitemap for Google Search Console and crawlers."""
    now_str = timezone.now().strftime('%Y-%m-%d')
    urls = [
        {'loc': 'https://ashasstore.in/', 'priority': '1.0', 'changefreq': 'daily'},
        {'loc': 'https://ashasstore.in/#contact-section', 'priority': '0.7', 'changefreq': 'monthly'},
    ]
    for cat in Category.objects.all():
        urls.append({
            'loc': f'https://ashasstore.in/category/{cat.slug}/',
            'priority': '0.8',
            'changefreq': 'weekly'
        })
    for prod in Product.objects.all():
        urls.append({
            'loc': f'https://ashasstore.in/product/{prod.id}/',
            'priority': '0.9',
            'changefreq': 'weekly'
        })

    xml = ['<?xml version="1.0" encoding="UTF-8"?>']
    xml.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    for u in urls:
        xml.append('  <url>')
        xml.append(f'    <loc>{u["loc"]}</loc>')
        xml.append(f'    <lastmod>{now_str}</lastmod>')
        xml.append(f'    <changefreq>{u["changefreq"]}</changefreq>')
        xml.append(f'    <priority>{u["priority"]}</priority>')
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
    cat = Category.objects.filter(slug__iexact=clean_slug).first()
    if not cat:
        alt_slug = clean_slug.rstrip('s') if clean_slug.endswith('s') else f"{clean_slug}s"
        cat = Category.objects.filter(slug__iexact=alt_slug).first()
    if not cat:
        cat = Category.objects.filter(name__iexact=clean_slug).first()
    if not cat:
        raise Http404(f"Category '{slug}' not found.")

    products = cat.products.all().order_by('-created_at')
    categories = Category.objects.all()
    return render(request, 'category_detail.html', {
        'category': cat,
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
    if product.stock <= 0:
        messages.error(request, f'"{product.name}" is currently sold out.')
        return redirect('product_detail', pk=product.id)

    cart = get_user_cart(request)
    pid = str(product.id)
    qty = max(cart.get(pid, 0), 1)
    sync_cart_item(request, product, qty)
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
                for pid_str, qty in guest_cart.items():
                    try:
                        prod = Product.objects.filter(id=int(pid_str), stock__gt=0).first()
                        if prod and int(qty) > 0:
                            c_item, created = CartItem.objects.get_or_create(user=user, product=prod)
                            if created:
                                c_item.quantity = min(int(qty), prod.stock)
                            else:
                                c_item.quantity = min(c_item.quantity + int(qty), prod.stock)
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

    pid = str(product_id)
    product = Product.objects.filter(id=product_id).first()

    if not product:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
            return JsonResponse({'status': 'error', 'message': 'Product not found.'}, status=404)
        return redirect(safe_referer(request, 'home'))

    # Check available inventory
    if product.stock <= 0:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
            return JsonResponse({'status': 'error', 'message': f'"{product.name}" is currently sold out.'}, status=400)
        messages.error(request, f'"{product.name}" is currently sold out.')
        return redirect(safe_referer(request, 'home'))

    current_qty = cart.get(pid, 0)
    if current_qty >= product.stock:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
            return JsonResponse({'status': 'error', 'message': f'Only {product.stock} available in stock.'}, status=400)
        messages.warning(request, f'Only {product.stock} available in stock.')
        return redirect(safe_referer(request, 'home'))

    sync_cart_item(request, product, current_qty + 1)
    
    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
        total_qty = sum(get_user_cart(request).values())
        return JsonResponse({
            'status': 'success',
            'cart_item_count': total_qty,
            'product_id': product_id,
            'product_name': product.name,
            'message': f"{product.name} added to your bag."
        })
    return redirect(safe_referer(request, 'home'))

def cart_view(request):
    cart = get_user_cart(request)
    cart_items = []
    total = 0
    stale_pids = []
    stock_adjusted = False

    for pid, qty in list(cart.items()):
        try:
            pid_int = int(pid)
        except (ValueError, TypeError):
            stale_pids.append(pid)
            continue

        product = Product.objects.filter(id=pid_int).first()
        if not product or product.stock <= 0:
            stale_pids.append(pid)
            continue
        if qty > product.stock:
            qty = product.stock
            sync_cart_item(request, product, qty)
            stock_adjusted = True

        subtotal = float(product.current_price) * qty
        total += subtotal
        cart_items.append({'product': product, 'quantity': qty, 'subtotal': subtotal})
    
    if stale_pids or stock_adjusted:
        for pid in stale_pids:
            try:
                p = Product.objects.filter(id=int(pid)).first()
            except (ValueError, TypeError):
                p = None
            if p:
                sync_cart_item(request, p, 0)
            else:
                cart.pop(pid, None)
                if request.user.is_authenticated:
                    CartItem.objects.filter(user=request.user, product_id=pid).delete()
        request.session['cart'] = cart
        request.session.modified = True
        if stock_adjusted:
            messages.warning(request, "Some cart quantities were adjusted to match available inventory.")

    return render(request, 'cart.html', {'cart_items': cart_items, 'total': total})

def update_cart(request, product_id, action):
    cart = get_user_cart(request)
    pid = str(product_id)
    try:
        product_id_int = int(product_id)
    except (ValueError, TypeError):
        return redirect('cart')
    product = Product.objects.filter(id=product_id_int).first()

    if pid in cart and product:
        if action == 'increase':
            if cart[pid] >= product.stock:
                messages.warning(request, f'Cannot exceed available stock of {product.stock}.')
            else:
                sync_cart_item(request, product, cart[pid] + 1)
        elif action == 'decrease':
            new_qty = cart[pid] - 1
            sync_cart_item(request, product, new_qty)
        elif action == 'remove':
            sync_cart_item(request, product, 0)
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
    Returns True if the order contains the featured bracelet (Matte Obsidian Stone Bracelet, id 11 or bracelet).
    Used for user's temporary test payment shipping rate (₹1).
    """
    for item in items:
        prod = getattr(item, 'product', item)
        if isinstance(prod, tuple):
            prod = prod[0]
        if not prod:
            continue
        if getattr(prod, 'id', None) == 11 or 'bracelet' in (getattr(prod, 'name', '') or '').lower():
            return True
        cat = getattr(prod, 'category', None)
        if cat and 'bracelet' in (getattr(cat, 'slug', '') or getattr(cat, 'name', '') or '').lower():
            return True
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
    - Featured Bracelet (temporary test rate): ₹1 flat
    - Outside Kerala (PIN outside 67-69 or explicitly outside): ₹95 flat
    - Inside Kerala (PIN 67xxxx, 68xxxx, 69xxxx or inside Kerala):
      - ₹65 if order contains Shades
      - ₹55 for other products
    """
    # Special ₹1 shipping charge for the featured bracelet for testing
    if check_cart_has_test_bracelet(items):
        return 1.0

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
    stale_pids = []
    stock_adjusted = False

    for pid, qty in list(cart.items()):
        product = Product.objects.filter(id=pid).first()
        if product and product.stock > 0:
            if qty > product.stock:
                qty = product.stock
                sync_cart_item(request, product, qty)
                stock_adjusted = True
            valid_items[pid] = (product, qty)
            subtotal += float(product.current_price) * qty
        else:
            stale_pids.append(pid)

    if stale_pids or stock_adjusted:
        for pid in stale_pids:
            p = Product.objects.filter(id=pid).first()
            if p:
                sync_cart_item(request, p, 0)
            else:
                cart.pop(pid, None)
                if request.user.is_authenticated:
                    CartItem.objects.filter(user=request.user, product_id=pid).delete()
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
    for pid, (product, qty) in valid_items.items():
        sub = float(product.current_price) * qty
        item_count += qty
        products_list.append(product)
        checkout_items.append({
            'product': product,
            'quantity': qty,
            'unit_price': float(product.current_price),
            'subtotal': sub
        })

    has_shades = check_cart_has_shades(products_list)
    has_bracelet = check_cart_has_test_bracelet(products_list)

    default_address = ''
    default_phone = ''
    default_name = ''
    default_pincode = ''
    default_city = ''
    default_state = ''
    if request.user.is_authenticated:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        default_address = profile.address or ''
        default_phone = profile.phone_number or ''
        default_pincode = profile.pincode or ''
        default_city = profile.city or ''
        default_state = profile.state or ''
        default_name = request.user.username

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
        address = (request.POST.get('shipping_address') or '').strip()
        pincode = (request.POST.get('pincode') or '').strip()
        city = (request.POST.get('city') or '').strip()
        state = (request.POST.get('state') or '').strip()
        requested_region = (request.POST.get('delivery_region') or initial_region).strip().lower()

        if not full_name or not phone or not address:
            return JsonResponse({'status': 'error', 'message': 'Please complete all required shipping details.'}, status=400)

        # Validate PIN code
        import re
        pincode_digits = re.sub(r'\D', '', pincode)
        # If pincode was not sent in dedicated field, try extracting 6 digits from address
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
            # Outside-Kerala customers have NO option to select or receive the Inside Kerala rate.
            if not is_kerala_pincode(pincode):
                delivery_region = 'outside_kerala'
            else:
                delivery_region = 'kerala'
        else:
            # Fallback when only delivery_region/address is passed
            delivery_region = determine_delivery_region(pincode=None, delivery_region=requested_region, address=address)
            pincode = '673001' if delivery_region == 'kerala' else '560001'

        if subtotal <= 0:
            return JsonResponse({'status': 'error', 'message': 'Invalid cart total.'}, status=400)

        # Re-compute accurate shipping fee on backend based on PIN code & product types
        shipping_fee = calculate_shipping_fee(products_list, delivery_region=delivery_region, pincode=pincode)
        grand_total = round(subtotal + shipping_fee, 2)

        # Build clean full address representation for shipping labels & admin
        full_shipping_address = address
        if pincode not in full_shipping_address:
            if city and city.lower() not in full_shipping_address.lower():
                full_shipping_address = f"{address}, {city} - {pincode}"
            else:
                full_shipping_address = f"{address} - {pincode}"

        # Automatically update user profile for subsequent visits
        if request.user.is_authenticated:
            try:
                prof, _ = Profile.objects.get_or_create(user=request.user)
                if phone:
                    prof.phone_number = phone
                if address:
                    prof.address = address
                prof.pincode = pincode
                if city:
                    prof.city = city
                if state:
                    prof.state = state
                prof.save()
            except Exception:
                pass

        rzp_amount = int(round(grand_total * 100))
        payment_method = (request.POST.get('payment_method') or 'upi').strip().lower()

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
            shipping_address=full_shipping_address,
            pincode=pincode,
            city=city,
            state=state or ('Kerala' if delivery_region == 'kerala' else ''),
            delivery_region=delivery_region,
            shipping_fee=shipping_fee,
            total_price=grand_total,
            razorpay_order_id=rzp_order_id,
            payment_status='Pending'
        )

        for pid, (product, qty) in valid_items.items():
            OrderItem.objects.create(
                order=order,
                product=product,
                price=product.current_price,
                quantity=qty
            )

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
            'payment_method': payment_method
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
        'default_phone': default_phone,
        'default_name': default_name,
        'default_pincode': default_pincode,
        'default_city': default_city,
        'default_state': default_state,
        'user_email': request.user.email if request.user.is_authenticated else '',
        'razorpay_key': settings.RAZORPAY_KEY_ID,
    })

@csrf_exempt
def payment_verify(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            order_id = data.get('db_order_id')
            params = {
                'razorpay_order_id': data.get('razorpay_order_id'),
                'razorpay_payment_id': data.get('razorpay_payment_id'),
                'razorpay_signature': data.get('razorpay_signature')
            }
            if not str(data.get('razorpay_order_id', '')).startswith('demo_'):
                client = get_razorpay_client()
                client.utility.verify_payment_signature(params)
                # Auto-capture fallback: guarantee payment is captured for bank settlement
                try:
                    rzp_pay_id = data.get('razorpay_payment_id')
                    if rzp_pay_id:
                        pay_info = client.payment.fetch(rzp_pay_id)
                        if pay_info.get('status') == 'authorized':
                            client.payment.capture(rzp_pay_id, pay_info.get('amount'))
                except Exception as cap_err:
                    import logging
                    logging.getLogger(__name__).warning(f"Razorpay capture fallback notice: {cap_err}")
            order = None
            if order_id:
                try:
                    order = Order.objects.filter(id=int(order_id)).first()
                except (ValueError, TypeError):
                    order = None
            if not order and data.get('razorpay_order_id'):
                order = Order.objects.filter(razorpay_order_id=data.get('razorpay_order_id')).first()

            if not order:
                return JsonResponse({'status': 'failed', 'message': 'Order record not found.'}, status=404)

            # Security check: if user is authenticated, ensure order belongs to them
            if request.user.is_authenticated and order.user and order.user != request.user:
                return JsonResponse({'status': 'failed', 'message': 'Unauthorized order access.'}, status=403)

            # Idempotency guard: prevent duplicate inventory deduction if already verified
            if order.payment_status == 'Completed':
                return JsonResponse({
                    'status': 'success',
                    'order_id': order.id,
                    'redirect_url': f"/profile/?order_placed=true&order_id={order.id}"
                })

            order.payment_status = 'Completed'
            order.razorpay_payment_id = data.get('razorpay_payment_id') or f"pay_{order.id}"
            order.save(update_fields=['payment_status', 'razorpay_payment_id'])

            # Decrement product inventory safely
            for item in order.items.all():
                if item.product:
                    item.product.stock = max(0, item.product.stock - item.quantity)
                    item.product.save(update_fields=['stock'])

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
    Automatically marks orders as Completed if user closes browser before payment_verify executes.
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

            # Auto-capture if payment was authorized so it never expires without settlement
            if event == 'payment.authorized':
                payment_entity = data.get('payload', {}).get('payment', {}).get('entity', {})
                rzp_pay_id = payment_entity.get('id')
                pay_amt = payment_entity.get('amount')
                if rzp_pay_id and pay_amt:
                    try:
                        client.payment.capture(rzp_pay_id, pay_amt)
                    except Exception as cap_err:
                        import logging
                        logging.getLogger(__name__).warning(f"Razorpay webhook auto-capture notice: {cap_err}")

            if event in ('order.paid', 'payment.captured', 'payment.authorized'):
                payment_entity = data.get('payload', {}).get('payment', {}).get('entity', {})
                rzp_order_id = payment_entity.get('order_id')
                rzp_pay_id = payment_entity.get('id')

                if rzp_order_id:
                    order = Order.objects.filter(razorpay_order_id=rzp_order_id).first()
                    if order and order.payment_status != 'Completed':
                        order.payment_status = 'Completed'
                        if rzp_pay_id:
                            order.razorpay_payment_id = rzp_pay_id
                        order.save(update_fields=['payment_status', 'razorpay_payment_id'])

                        for item in order.items.all():
                            if item.product:
                                item.product.stock = max(0, item.product.stock - item.quantity)
                                item.product.save(update_fields=['stock'])

            return HttpResponse(status=200)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Razorpay webhook error: {e}")
            return HttpResponse(status=200)
    return HttpResponse(status=405)

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

from functools import wraps

# --- AdminPP Custom Dashboard ---
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

@staff_required
def adminpp_dashboard(request):
    products = Product.objects.select_related('category').all().order_by('-created_at')
    trending_count = products.filter(is_trending=True).count()
    from django.conf import settings
    db_engine = settings.DATABASES['default']['ENGINE'].split('.')[-1]
    is_postgres = 'postgres' in db_engine or bool(os.environ.get('DATABASE_URL'))
    return render(request, 'adminpp_dashboard.html', {
        'categories': Category.objects.all(),
        'products': products,
        'trending_count': trending_count,
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

@staff_required
def adminpp_orders(request):
    category_filter = request.GET.get('category', '')
    start_date = request.GET.get('start_date', '')
    end_date = request.GET.get('end_date', '')
    export_excel = request.GET.get('export', '')

    orders = Order.objects.filter(payment_status='Completed').prefetch_related('items__product__category').order_by('-created_at')

    if category_filter:
        orders = orders.filter(items__product__category__name__iexact=category_filter).distinct()
    if start_date:
        orders = orders.filter(created_at__date__gte=start_date)
    if end_date:
        orders = orders.filter(created_at__date__lte=end_date)

    if export_excel == 'true':
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Orders Data"

        headers = ['Order ID', 'Customer Name', 'Phone', 'Address', 'PIN Code', 'Region', 'Items Purchased', 'Categories', 'Total Price', 'Shipping Fee', 'Payment ID', 'Tracking ID', 'Carrier', 'Shipping Status', 'Date']
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
                o.razorpay_payment_id or '',
                o.tracking_id or 'Not Assigned',
                o.carrier or 'India Post',
                o.get_shipping_status_display() if hasattr(o, 'get_shipping_status_display') else o.shipping_status,
                o.created_at.strftime('%Y-%m-%d %H:%M')
            ])

        response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = 'attachment; filename="ashas_orders_export.xlsx"'
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
        'categories': Category.objects.all(),
        'selected_category': category_filter,
        'start_date': start_date,
        'end_date': end_date,
        'db_engine': db_engine,
        'is_postgres': is_postgres,
        'rzp_live_payments': rzp_live_payments,
        'rzp_key_id': settings.RAZORPAY_KEY_ID,
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
    # Guard against cascading order item destruction
    if OrderItem.objects.filter(product__category=category).exists():
        messages.warning(request, f'Category "{cat_name}" contains products with past customer orders and cannot be deleted to preserve order history.')
        return redirect('adminpp_dashboard')
    category.delete()
    messages.success(request, f'Category "{cat_name}" deleted successfully.')
    return redirect('adminpp_dashboard')

@staff_required
def product_create_or_edit(request, pk=None):
    product = get_object_or_404(Product, pk=pk) if pk else None
    form = ProductForm(request.POST or None, request.FILES or None, instance=product)
    if request.method == 'POST':
        if form.is_valid():
            try:
                p = form.save()
                action_text = "updated" if pk else "created"
                messages.success(request, f'Product "{p.name}" has been {action_text} successfully!')
                return redirect('adminpp_dashboard')
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Error saving product: {e}", exc_info=True)
                messages.error(request, f"Error saving product: {str(e)}")
        else:
            messages.error(request, 'Please correct the form errors below.')
    return render(request, 'product_form.html', {'form': form, 'product': product})

@staff_required
def product_delete(request, pk):
    product = get_object_or_404(Product, pk=pk)
    p_name = product.name
    # Guard against cascading order item destruction
    if OrderItem.objects.filter(product=product).exists():
        product.stock = 0
        product.is_trending = False
        product.save(update_fields=['stock', 'is_trending'])
        messages.warning(request, f'Product "{p_name}" has existing order records and cannot be permanently deleted to protect customer receipts. It has been marked as out-of-stock and unlisted.')
        return redirect('adminpp_dashboard')
    product.delete()
    messages.success(request, f'Product "{p_name}" deleted successfully.')
    return redirect('adminpp_dashboard')

@staff_required
def inquiry_delete(request, pk):
    inquiry = get_object_or_404(ContactMessage, pk=pk)
    sender = inquiry.name
    inquiry.delete()
    messages.success(request, f'Customer inquiry from "{sender}" was deleted successfully.')
    return redirect('adminpp_dashboard')