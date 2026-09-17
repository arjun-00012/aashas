import json
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from store.models import Category, Product, Order, OrderItem, Profile
from store.forms import ProductForm, RegistrationForm
from store.views import safe_referer

class CategorySlugTests(TestCase):
    def test_category_slug_collision_handling(self):
        # Distinct names that slugify to the same string 'rings'
        c1 = Category.objects.create(name="Rings", description="First")
        c2 = Category.objects.create(name="Rings!", description="Second")
        c3 = Category.objects.create(name="Rings?", description="Third")

        self.assertEqual(c1.slug, "rings")
        self.assertEqual(c2.slug, "rings-1")
        self.assertEqual(c3.slug, "rings-2")

class WhatsAppUrlTests(TestCase):
    def test_whatsapp_notification_phone_normalization(self):
        # 10 digits without country code
        o1 = Order.objects.create(
            full_name="Customer 1",
            phone_number="9876543210",
            shipping_address="Calicut, Kerala",
            total_price=999.00
        )
        self.assertIn("wa.me/919876543210", o1.whatsapp_notification_url)

        # 11 digits starting with 0
        o2 = Order.objects.create(
            full_name="Customer 2",
            phone_number="09876543210",
            shipping_address="Calicut, Kerala",
            total_price=999.00
        )
        self.assertIn("wa.me/919876543210", o2.whatsapp_notification_url)

class ProductFormValidationTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Accessories")

    def test_price_must_be_positive(self):
        form = ProductForm(data={
            'category': self.category.id,
            'name': 'Invalid Price Item',
            'price': 0,
            'stock': 5
        })
        self.assertFalse(form.is_valid())
        self.assertIn('price', form.errors)

    def test_discount_price_must_be_less_than_price(self):
        form = ProductForm(data={
            'category': self.category.id,
            'name': 'Invalid Discount Item',
            'price': 500,
            'discount_price': 600,
            'stock': 5
        })
        self.assertFalse(form.is_valid())
        self.assertIn('discount_price', form.errors)

    def test_valid_product_form(self):
        form = ProductForm(data={
            'category': self.category.id,
            'name': 'Valid Ring',
            'price': 1000,
            'discount_price': 799,
            'stock': 10
        })
        self.assertTrue(form.is_valid())

class PaymentVerifyIdempotencyTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.category = Category.objects.create(name="Chains")
        self.product = Product.objects.create(
            category=self.category,
            name="Cuban Chain",
            price=1200.00,
            stock=10
        )
        self.order = Order.objects.create(
            full_name="Arjun",
            phone_number="9876543210",
            shipping_address="Kochi, Kerala",
            total_price=1200.00,
            payment_status='Pending'
        )
        OrderItem.objects.create(
            order=self.order,
            product=self.product,
            price=1200.00,
            quantity=2
        )

    def test_payment_verify_idempotency_prevents_double_stock_deduction(self):
        payload = {
            'db_order_id': self.order.id,
            'razorpay_order_id': 'demo_order_123',
            'razorpay_payment_id': 'pay_test_456',
            'razorpay_signature': 'dummy_sig'
        }

        # First verification
        res1 = self.client.post(
            reverse('payment_verify'),
            data=json.dumps(payload),
            content_type='application/json'
        )
        self.assertEqual(res1.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 8)  # 10 - 2 = 8

        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, 'Completed')

        # Second verification (simulating browser duplicate POST or network retry)
        res2 = self.client.post(
            reverse('payment_verify'),
            data=json.dumps(payload),
            content_type='application/json'
        )
        self.assertEqual(res2.status_code, 200)
        self.product.refresh_from_db()
        # Stock MUST remain 8, NOT deducted a second time to 6!
        self.assertEqual(self.product.stock, 8)

class OrderHistoryProtectionTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('admin_tester', 'admin@test.com', 'password123')
        self.client = Client()
        self.client.login(username='admin_tester', password='password123')

        self.category = Category.objects.create(name="Watches")
        self.product = Product.objects.create(
            category=self.category,
            name="Classic Watch",
            price=2500.00,
            stock=5,
            is_trending=True
        )
        self.order = Order.objects.create(
            full_name="Buyer",
            phone_number="9876543210",
            shipping_address="Trivandrum",
            total_price=2500.00,
            payment_status='Completed'
        )
        self.order_item = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            price=2500.00,
            quantity=1
        )

    def test_product_with_orders_cannot_be_deleted(self):
        # Attempt to delete product
        res = self.client.get(reverse('product_delete', kwargs={'pk': self.product.pk}), follow=True)
        self.assertEqual(res.status_code, 200)
        
        # Product record must still exist
        self.assertTrue(Product.objects.filter(pk=self.product.pk).exists())
        
        # Product stock should be 0 and unlisted
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 0)
        self.assertFalse(self.product.is_trending)

        # Order item receipt must still be intact
        self.assertTrue(OrderItem.objects.filter(pk=self.order_item.pk).exists())

    def test_category_with_ordered_products_cannot_be_deleted(self):
        res = self.client.get(reverse('category_delete', kwargs={'pk': self.category.pk}), follow=True)
        self.assertEqual(res.status_code, 200)

        # Category must still exist
        self.assertTrue(Category.objects.filter(pk=self.category.pk).exists())

class OpenRedirectProtectionTests(TestCase):
    def setUp(self):
        from django.test import RequestFactory
        self.factory = RequestFactory()

    def test_safe_referer_blocks_external_domains(self):
        req = self.factory.get('/', HTTP_HOST='ashasstore.in', HTTP_REFERER='https://evil-attacker.com/steal')
        safe_url = safe_referer(req, fallback='home')
        self.assertEqual(safe_url, 'home')

    def test_safe_referer_allows_internal_domain(self):
        req = self.factory.get('/', HTTP_HOST='ashasstore.in', HTTP_REFERER='https://ashasstore.in/cart/')
        safe_url = safe_referer(req, fallback='home')
        self.assertEqual(safe_url, 'https://ashasstore.in/cart/')

class CartViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.category = Category.objects.create(name="Shades")
        self.product = Product.objects.create(
            category=self.category,
            name="Matrix Shades",
            price=899.00,
            stock=15
        )

    def test_cart_renders_successfully_when_empty(self):
        res = self.client.get(reverse('cart'))
        self.assertEqual(res.status_code, 200)
        self.assertIn("Your shopping bag is empty", res.content.decode('utf-8'))

    def test_cart_renders_successfully_with_items_desktop_and_mobile(self):
        # Add item to cart
        self.client.get(reverse('add_to_cart', kwargs={'product_id': self.product.id}))
        
        # Test desktop rendering
        res_desktop = self.client.get(reverse('cart'))
        self.assertEqual(res_desktop.status_code, 200)
        self.assertIn("Matrix Shades", res_desktop.content.decode('utf-8'))

        # Test mobile rendering (Mobile Safari User-Agent)
        res_mobile = self.client.get(
            reverse('cart'),
            HTTP_USER_AGENT='Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1'
        )
        self.assertEqual(res_mobile.status_code, 200)
        self.assertIn("Matrix Shades", res_mobile.content.decode('utf-8'))


class ReturnHomeButtonTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin_user = User.objects.create_superuser('admin_tester', 'admin@example.com', 'AdminPass123!')
        self.regular_user = User.objects.create_user('john_doe', 'john@example.com', 'UserPass123!')
        self.category = Category.objects.create(name="Rings")
        self.product = Product.objects.create(category=self.category, name="Silver Band", price=500, stock=5)

    def test_return_home_buttons_present_on_storefront_subpages(self):
        home_url = reverse('home')
        pages_to_test = [
            reverse('cart'),
            reverse('login'),
            reverse('register'),
        ]
        for url in pages_to_test:
            res = self.client.get(url)
            self.assertEqual(res.status_code, 200, f"Page {url} failed to load")
            content = res.content.decode('utf-8')
            # Check presence of link to home
            self.assertIn(f'href="{home_url}"', content, f"Home link missing in {url}")

        # Test checkout page when logged in and cart has item
        self.client.login(username='john_doe', password='UserPass123!')
        self.client.get(reverse('add_to_cart', kwargs={'product_id': self.product.id}))
        res_checkout = self.client.get(reverse('checkout'))
        self.assertEqual(res_checkout.status_code, 200)
        self.assertIn(f'href="{home_url}"', res_checkout.content.decode('utf-8'))

    def test_return_home_button_present_on_profile_page(self):
        self.client.login(username='john_doe', password='UserPass123!')
        res = self.client.get(reverse('profile'))
        self.assertEqual(res.status_code, 200)
        content = res.content.decode('utf-8')
        self.assertIn(f'href="{reverse("home")}"', content)

    def test_return_home_button_present_on_admin_pages(self):
        self.client.login(username='admin_tester', password='AdminPass123!')
        admin_pages = [
            reverse('adminpp_dashboard'),
            reverse('adminpp_orders'),
            reverse('category_add'),
            reverse('product_add'),
        ]
        for url in admin_pages:
            res = self.client.get(url)
            self.assertEqual(res.status_code, 200, f"Admin page {url} failed to load")
            content = res.content.decode('utf-8')
            self.assertIn(f'href="{reverse("home")}"', content, f"Home link missing in {url}")

