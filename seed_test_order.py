import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'aashas_store.settings')
django.setup()

from django.contrib.auth.models import User
from store.models import Profile, Product, Order, OrderItem
from django.utils import timezone

# 1. Configure user 'user' as a regular customer (NOT admin, NOT staff)
user, created = User.objects.get_or_create(username='user', defaults={'email': 'user@ashasstore.in', 'first_name': 'User', 'is_staff': False, 'is_superuser': False})
user.set_password('user@123')
user.is_staff = False
user.is_superuser = False
user.save()

profile, _ = Profile.objects.get_or_create(user=user)
profile.phone_number = '9074277754'
profile.address = 'Krishnamanam Building, Karuvissery, Kozhikode, Kerala – 673010'
profile.save()

print(f"User 'user' updated: Phone = {profile.phone_number}, Staff Access = {user.is_staff}, Superuser = {user.is_superuser}, Password = user@123")

# 2. Only seed sample orders if NO orders exist at all in database
if Order.objects.exists():
    print(f"Orders already exist in database ({Order.objects.count()} orders). Skipping sample order generation.")
    exit(0)

# 3. Create fresh purchases with new phone number 9074277754 ("and then number chnaged sfter that puraches some products")
products = list(Product.objects.all())
print(f"Catalog has {len(products)} products.")

if products:
    p1 = Product.objects.filter(name__icontains='Watch').first() or products[0]
    p2 = Product.objects.filter(name__icontains='Belt').first() or (products[1] if len(products) > 1 else products[0])
    total1 = p1.current_price + p2.current_price

    # Order 1: Dispatched with India Post Tracking ID
    order1 = Order.objects.create(
        user=user,
        full_name='User Customer',
        phone_number='9074277754',
        shipping_address='Krishnamanam Building, Karuvissery, Kozhikode, Kerala – 673010',
        total_price=total1,
        razorpay_order_id='demo_order_9074277754_01',
        razorpay_payment_id='pay_demo_9074277754_01',
        payment_status='Completed',
        tracking_id='ET907427775IN',
        carrier='India Post (Speed Post)',
        shipping_status='Dispatched',
        tracking_notes='Booked at Karuvissery Post Office, Kozhikode',
        tracking_updated_at=timezone.now()
    )
    OrderItem.objects.create(order=order1, product=p1, price=p1.current_price, quantity=1)
    OrderItem.objects.create(order=order1, product=p2, price=p2.current_price, quantity=1)
    print(f"Created Order #{order1.id}: Phone={order1.phone_number}, Total=INR {order1.total_price}, Tracking ID={order1.tracking_id} ({order1.shipping_status})")

    # Order 2: Processing (no tracking ID yet - ready for manual admin update)
    p3 = Product.objects.filter(name__icontains='Ring').first() or products[0]
    p4 = Product.objects.filter(name__icontains='Sunglasses').first() or (products[1] if len(products) > 1 else products[0])
    total2 = p3.current_price + (p4.current_price * 2)

    order2 = Order.objects.create(
        user=user,
        full_name='User Customer',
        phone_number='9074277754',
        shipping_address='Krishnamanam Building, Karuvissery, Kozhikode, Kerala – 673010',
        total_price=total2,
        razorpay_order_id='demo_order_9074277754_02',
        razorpay_payment_id='pay_demo_9074277754_02',
        payment_status='Completed',
        tracking_id='',
        carrier='India Post',
        shipping_status='Processing',
        tracking_notes='Order verified • Packing at Kozhikode Boutique',
        tracking_updated_at=None
    )
    OrderItem.objects.create(order=order2, product=p3, price=p3.current_price, quantity=1)
    OrderItem.objects.create(order=order2, product=p4, price=p4.current_price, quantity=2)
    print(f"Created Order #{order2.id}: Phone={order2.phone_number}, Total=INR {order2.total_price}, Tracking ID=Pending ({order2.shipping_status})")

print("All tasks completed successfully!")
