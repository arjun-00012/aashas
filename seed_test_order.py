import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'aashas_store.settings')
django.setup()

from django.contrib.auth.models import User
from store.models import Profile, Product, Order, OrderItem
from django.utils import timezone
from decimal import Decimal

# 1. Create or update user: username='user', password='user@123'
user, created = User.objects.get_or_create(username='user', defaults={'email': 'user@ashasstore.in', 'first_name': 'User'})
user.set_password('user@123')
user.save()

profile, _ = Profile.objects.get_or_create(user=user)
profile.phone_number = '8281451481'
profile.address = 'Krishnamanam Building, Karuvissery, Kozhikode, Kerala - 673010'
profile.save()

print(f"User 'user' configured. Created: {created}, Password set to: user@123")

# 2. Get sample products
products = list(Product.objects.all()[:4])
print("Available products:", [p.name for p in products])

if products:
    # Remove existing orders for 'user' to ensure fresh reference state
    Order.objects.filter(user=user).delete()

    # Order 1: Dispatched with tracking ID
    p1 = products[0]
    p2 = products[1] if len(products) > 1 else products[0]
    total1 = p1.current_price + p2.current_price

    order1 = Order.objects.create(
        user=user,
        full_name='User Customer',
        phone_number='8281451481',
        shipping_address='Krishnamanam Building, Karuvissery, Kozhikode, Kerala – 673010',
        total_price=total1,
        razorpay_order_id='demo_order_ref001',
        razorpay_payment_id='pay_ref_ashas_001',
        payment_status='Completed',
        tracking_id='ET828145148IN',
        carrier='India Post (Speed Post)',
        shipping_status='Dispatched',
        tracking_notes='Booked at Karuvissery Post Office, Kozhikode',
        tracking_updated_at=timezone.now()
    )
    OrderItem.objects.create(order=order1, product=p1, price=p1.current_price, quantity=1)
    OrderItem.objects.create(order=order1, product=p2, price=p2.current_price, quantity=1)
    print(f"Order #{order1.id} created: Dispatched with Tracking ID {order1.tracking_id}")

    # Order 2: Processing (no tracking ID assigned yet - perfect for admin manual test)
    if len(products) > 2:
        p3 = products[2]
        order2 = Order.objects.create(
            user=user,
            full_name='User Customer',
            phone_number='8281451481',
            shipping_address='Krishnamanam Building, Karuvissery, Kozhikode, Kerala – 673010',
            total_price=p3.current_price,
            razorpay_order_id='demo_order_ref002',
            razorpay_payment_id='pay_ref_ashas_002',
            payment_status='Completed',
            tracking_id='',
            carrier='India Post',
            shipping_status='Processing',
            tracking_notes='',
            tracking_updated_at=None
        )
        OrderItem.objects.create(order=order2, product=p3, price=p3.current_price, quantity=1)
        print(f"Order #{order2.id} created: Processing (no tracking ID yet, ready for admin manual update)")

print("Successfully seeded reference purchases for user 'user'!")
