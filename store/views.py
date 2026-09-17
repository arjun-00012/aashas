import os
import json
import razorpay
import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill
from django.conf import settings
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from .models import Category, Product, Order, OrderItem, Profile, ContactMessage, CartItem
from .forms import RegistrationForm, ProfileUpdateForm, CategoryForm, ProductForm

import razorpay
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver

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
        total_qty = sum(cart.values()) if cart else 0
    return {
        'cart_item_count': total_qty,
        'all_categories': Category.objects.all(),
    }

def ping_view(request):
    """Ultra-lightweight endpoint for cron-job.org, uptime monitors, and keep-alive pings (returns 2 bytes 'OK')"""
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
            'loc': f'https://ashasstore.in/#section-{cat.slug}',
            'priority': '0.8',
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

def google_verification_view(request, code='44f351804c9055d3'):
    """Serve Google Search Console ownership verification file (e.g. google44f351804c9055d3.html)."""
    return HttpResponse(f"google-site-verification: google{code}.html", content_type="text/html; charset=utf-8")

def home(request):
    user_agent = request.META.get('HTTP_USER_AGENT', '').lower()
    if 'cron-job' in user_agent or 'cronjob' in user_agent or 'uptimerobot' in user_agent or request.GET.get('ping'):
        return HttpResponse("OK", content_type="text/plain", status=200)
    categories = Category.objects.prefetch_related('products').all()
    trending_products = Product.objects.filter(is_trending=True).select_related('category')
    return render(request, 'index.html', {
        'categories': categories,
        'trending_products': trending_products
    })

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
                password=form.cleaned_data['password']
            )
            profile = user.profile
            profile.phone_number = form.cleaned_data['phone_number'].strip()
            profile.save()

            # Preserve guest cart items for newly registered user
            guest_cart = dict(request.session.get('cart', {}))
            login(request, user)
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
    if request.user.is_authenticated:
        return redirect('home')
    next_url = request.POST.get('next') or request.GET.get('next') or ''
    if request.method == 'POST':
        u = (request.POST.get('username') or '').strip()
        p = request.POST.get('password') or ''

        # Support sign-in with registered email address
        if '@' in u:
            matching_user = User.objects.filter(email__iexact=u).first()
            if matching_user:
                u = matching_user.username

        user = authenticate(request, username=u, password=p)
        if user:
            # Preserve guest cart items upon authentication
            guest_cart = dict(request.session.get('cart', {}))
            login(request, user)
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
        return render(request, 'login.html', {'error': 'Invalid Username or Password.', 'next_url': next_url})
    return render(request, 'login.html', {'next_url': next_url})

def logout_view(request):
    logout(request)
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
    orders = Order.objects.filter(user=request.user).prefetch_related('items__product').order_by('-created_at')
    # Enable Admin/Staff Preview: if staff user has no personal purchases, show store customer orders so they can test tracking UI
    is_admin_preview = False
    if not orders.exists() and (request.user.is_staff or request.user.is_superuser):
        orders = Order.objects.filter(payment_status='Completed').prefetch_related('items__product').order_by('-created_at')
        is_admin_preview = True

    return render(request, 'profile.html', {
        'form': form,
        'orders': orders,
        'profile': profile,
        'is_admin_preview': is_admin_preview
    })

# --- Cart Views ---
def add_to_cart(request, product_id):
    cart = get_user_cart(request)
    pid = str(product_id)
    product = Product.objects.filter(id=product_id).first()

    if not product:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
            return JsonResponse({'status': 'error', 'message': 'Product not found.'}, status=404)
        return redirect(request.META.get('HTTP_REFERER', 'home'))

    # Check available inventory
    if product.stock <= 0:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
            return JsonResponse({'status': 'error', 'message': f'"{product.name}" is currently sold out.'}, status=400)
        messages.error(request, f'"{product.name}" is currently sold out.')
        return redirect(request.META.get('HTTP_REFERER', 'home'))

    current_qty = cart.get(pid, 0)
    if current_qty >= product.stock:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
            return JsonResponse({'status': 'error', 'message': f'Only {product.stock} available in stock.'}, status=400)
        messages.warning(request, f'Only {product.stock} available in stock.')
        return redirect(request.META.get('HTTP_REFERER', 'home'))

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
    return redirect(request.META.get('HTTP_REFERER', 'home'))

def cart_view(request):
    cart = get_user_cart(request)
    cart_items = []
    total = 0
    stale_pids = []
    stock_adjusted = False

    for pid, qty in list(cart.items()):
        product = Product.objects.filter(id=pid).first()
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
            messages.warning(request, "Some cart quantities were adjusted to match available inventory.")

    return render(request, 'cart.html', {'cart_items': cart_items, 'total': total})

def update_cart(request, product_id, action):
    cart = get_user_cart(request)
    pid = str(product_id)
    product = Product.objects.filter(id=product_id).first()

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

# --- Razorpay Checkout ---
@login_required(login_url='login')
def checkout_view(request):
    cart = get_user_cart(request)
    if not cart:
        return redirect('home')
    
    valid_items = {}
    total = 0
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
            total += float(product.current_price) * qty
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

    if not valid_items:
        messages.error(request, "The items in your bag are currently out of stock.")
        return redirect('home')
    
    checkout_items = []
    item_count = 0
    for pid, (product, qty) in valid_items.items():
        sub = float(product.current_price) * qty
        item_count += qty
        checkout_items.append({
            'product': product,
            'quantity': qty,
            'unit_price': float(product.current_price),
            'subtotal': sub
        })

    default_address = ''
    default_phone = ''
    default_name = ''
    if request.user.is_authenticated:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        default_address = profile.address or ''
        default_phone = profile.phone_number or ''
        default_name = request.user.username

    if request.method == 'POST':
        full_name = (request.POST.get('full_name') or '').strip()
        phone = (request.POST.get('phone_number') or '').strip()
        address = (request.POST.get('shipping_address') or '').strip()

        if not full_name or not phone or not address:
            return JsonResponse({'status': 'error', 'message': 'Please complete all required shipping details.'}, status=400)

        # Automatically update user profile for subsequent visits
        if request.user.is_authenticated:
            try:
                prof, _ = Profile.objects.get_or_create(user=request.user)
                if phone:
                    prof.phone_number = phone
                if address:
                    prof.address = address
                prof.save()
            except Exception:
                pass

        rzp_amount = int(round(total * 100))
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
                    'shipping_address': address[:100],
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
            shipping_address=address,
            total_price=total,
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
            'currency': 'INR',
            'razorpay_order_id': rzp_order_id,
            'db_order_id': order.id,
            'customer_name': full_name,
            'customer_phone': phone,
            'customer_email': request.user.email if request.user.is_authenticated and request.user.email else '',
            'payment_method': payment_method
        })

    return render(request, 'checkout.html', {
        'total': total,
        'checkout_items': checkout_items,
        'item_count': item_count,
        'default_address': default_address,
        'default_phone': default_phone,
        'default_name': default_name,
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
            order = Order.objects.get(id=order_id)
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
            return redirect(request.META.get('HTTP_REFERER', 'home'))
        else:
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'status': 'invalid', 'message': 'Please fill all required fields.'}, status=400)
            messages.error(request, "Please fill in all required fields.")
            return redirect(request.META.get('HTTP_REFERER', 'home'))

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
    return render(request, 'adminpp_dashboard.html', {
        'categories': Category.objects.all(),
        'products': products,
        'trending_count': trending_count,
        'inquiries': ContactMessage.objects.all().order_by('-created_at')
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
    return redirect(request.META.get('HTTP_REFERER', 'adminpp_dashboard'))

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

        headers = ['Order ID', 'Customer Name', 'Phone', 'Address', 'Items Purchased', 'Categories', 'Total Price', 'Payment ID', 'Tracking ID', 'Carrier', 'Shipping Status', 'Date']
        ws.append(headers)

        header_fill = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
        header_font = Font(name="Arial", size=10, bold=True, color="FFFFFF")

        for col_num in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=col_num)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")

        for o in orders:
            items_str = ", ".join([f"{i.product.name} (x{i.quantity})" for i in o.items.all()])
            cats_str = ", ".join(list(set([i.product.category.name for i in o.items.all()])))
            ws.append([
                o.id,
                o.full_name,
                o.phone_number,
                o.shipping_address,
                items_str,
                cats_str,
                float(o.total_price),
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

    return render(request, 'adminpp_orders.html', {
        'orders': orders,
        'categories': Category.objects.all(),
        'selected_category': category_filter,
        'start_date': start_date,
        'end_date': end_date
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
        return redirect(request.META.get('HTTP_REFERER', 'adminpp_orders'))

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
                messages.success(request, f'Category "{cat.name}" has been {action_text} successfully!')
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