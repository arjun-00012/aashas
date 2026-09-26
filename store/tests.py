import json
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from store.models import Category, Product, Order, OrderItem, Profile, CartItem
from unittest.mock import patch, MagicMock
from store.forms import ProductForm, RegistrationForm, ProfileUpdateForm
from store.views import safe_referer, check_cart_has_shades, check_cart_has_test_bracelet, calculate_shipping_fee, is_kerala_pincode, determine_delivery_region

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


class RazorpayWebhookTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.category = Category.objects.create(name="Chains")
        self.product = Product.objects.create(category=self.category, name="Cuban Chain", price=1000, stock=10)
        self.order = Order.objects.create(
            full_name="Webhook Tester",
            phone_number="9876543210",
            shipping_address="Calicut, Kerala",
            total_price=1000,
            razorpay_order_id="order_webhook_test_123",
            payment_status="Pending"
        )
        OrderItem.objects.create(
            order=self.order,
            product=self.product,
            price=1000,
            quantity=2
        )

    def test_webhook_marks_order_completed_and_deducts_stock(self):
        payload = {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_webhook_999",
                        "order_id": "order_webhook_test_123",
                        "status": "captured",
                        "amount": 100000
                    }
                }
            }
        }
        res = self.client.post(
            reverse('razorpay_webhook'),
            data=json.dumps(payload),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, 200)

        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, 'Completed')
        self.assertEqual(self.order.razorpay_payment_id, 'pay_webhook_999')

        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 8)  # 10 - 2

    def test_webhook_get_method_rejected(self):
        res = self.client.get(reverse('razorpay_webhook'))
        self.assertEqual(res.status_code, 405)


class ShippingCalculationTests(TestCase):
    def setUp(self):
        self.cat_shades = Category.objects.create(name="Shades", slug="shades")
        self.cat_rings = Category.objects.create(name="Rings", slug="rings")
        self.cat_bracelets = Category.objects.create(name="Bracelets", slug="bracelets")
        self.shade_product = Product.objects.create(
            category=self.cat_shades,
            name="Y2K Retro Shades",
            price=999.00,
            stock=10
        )
        self.ring_product = Product.objects.create(
            category=self.cat_rings,
            name="Obsidian Ring",
            price=499.00,
            stock=10
        )
        self.bracelet_product = Product.objects.create(
            category=self.cat_bracelets,
            name="Matte Obsidian Stone Bracelet",
            price=899.00,
            discount_price=699.00,
            stock=10
        )

    def test_check_cart_has_shades(self):
        self.assertTrue(check_cart_has_shades([self.shade_product]))
        self.assertFalse(check_cart_has_shades([self.ring_product]))
        self.assertTrue(check_cart_has_shades([self.ring_product, self.shade_product]))

    def test_calculate_shipping_outside_kerala(self):
        # Outside Kerala is flat 95 regardless of products
        fee_ring = calculate_shipping_fee([self.ring_product], delivery_region="outside_kerala")
        fee_shade = calculate_shipping_fee([self.shade_product], delivery_region="outside_kerala")
        self.assertEqual(fee_ring, 95.0)
        self.assertEqual(fee_shade, 95.0)

    def test_calculate_shipping_inside_kerala_shades(self):
        # Inside Kerala with shades is 65
        fee = calculate_shipping_fee([self.shade_product], delivery_region="kerala")
        self.assertEqual(fee, 65.0)
        # Inside Kerala with both shade and ring is 65
        fee_mixed = calculate_shipping_fee([self.ring_product, self.shade_product], delivery_region="kerala")
        self.assertEqual(fee_mixed, 65.0)

    def test_calculate_shipping_inside_kerala_other_products(self):
        # Inside Kerala for other products (no shades) is 55
        fee = calculate_shipping_fee([self.ring_product], delivery_region="kerala")
        self.assertEqual(fee, 55.0)

    def test_calculate_shipping_featured_bracelet_standard_rate(self):
        # Featured bracelet now uses standard shipping rates: ₹55 inside Kerala, ₹95 outside Kerala
        fee_kerala = calculate_shipping_fee([self.bracelet_product], delivery_region="kerala")
        fee_outside = calculate_shipping_fee([self.bracelet_product], delivery_region="outside_kerala")
        self.assertEqual(fee_kerala, 55.0)
        self.assertEqual(fee_outside, 95.0)

    def test_is_kerala_pincode(self):
        # Kerala postal circle PIN codes (67xxxx, 68xxxx, 69xxxx)
        self.assertTrue(is_kerala_pincode("673001"))  # Kozhikode
        self.assertTrue(is_kerala_pincode("682001"))  # Kochi
        self.assertTrue(is_kerala_pincode("695001"))  # Thiruvananthapuram
        self.assertTrue(is_kerala_pincode("670001"))  # Kannur
        self.assertTrue(is_kerala_pincode("673 001")) # Spaces handled
        self.assertTrue(is_kerala_pincode(682001))    # Integer handled

        # Outside Kerala
        self.assertFalse(is_kerala_pincode("560001")) # Bengaluru, Karnataka
        self.assertFalse(is_kerala_pincode("110001")) # New Delhi
        self.assertFalse(is_kerala_pincode("400001")) # Mumbai, Maharashtra
        self.assertFalse(is_kerala_pincode("600001")) # Chennai, Tamil Nadu
        self.assertFalse(is_kerala_pincode("700001")) # Kolkata, West Bengal
        self.assertFalse(is_kerala_pincode("123"))    # Too short
        self.assertFalse(is_kerala_pincode(""))       # Empty

    def test_calculate_shipping_with_pincode(self):
        # Kerala PIN (673001) -> ₹55 for rings, ₹65 for shades
        self.assertEqual(calculate_shipping_fee([self.ring_product], pincode="673001"), 55.0)
        self.assertEqual(calculate_shipping_fee([self.shade_product], pincode="682001"), 65.0)
        self.assertEqual(calculate_shipping_fee([self.ring_product, self.shade_product], pincode="695001"), 65.0)

        # Outside Kerala PIN (560001, 110001) -> ₹95 flat
        self.assertEqual(calculate_shipping_fee([self.ring_product], pincode="560001"), 95.0)
        self.assertEqual(calculate_shipping_fee([self.shade_product], pincode="110001"), 95.0)

        # Featured bracelet uses standard shipping rates (55 Kerala, 95 Outside)
        self.assertEqual(calculate_shipping_fee([self.bracelet_product], pincode="673001"), 55.0)
        self.assertEqual(calculate_shipping_fee([self.bracelet_product], pincode="560001"), 95.0)


class CheckoutShippingIntegrationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username="testbuyer", password="testpassword123")
        self.client.login(username="testbuyer", password="testpassword123")
        
        self.cat_shades = Category.objects.create(name="Shades", slug="shades")
        self.cat_rings = Category.objects.create(name="Rings", slug="rings")
        
        self.shade_product = Product.objects.create(
            category=self.cat_shades,
            name="McStan Shades",
            price=800.00,
            stock=5
        )
        self.ring_product = Product.objects.create(
            category=self.cat_rings,
            name="Dragon Ring",
            price=300.00,
            stock=5
        )

    @patch('store.views.get_razorpay_client')
    def test_checkout_post_inside_kerala_other_products(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.order.create.return_value = {'id': 'order_rzp_mock_55', 'amount': 35500}
        mock_get_client.return_value = mock_client

        # Put ring in cart (subtotal = 300)
        CartItem.objects.create(user=self.user, product=self.ring_product, quantity=1)

        res = self.client.post(reverse('checkout'), {
            'full_name': 'Buyer One',
            'phone_number': '9876543210',
            'shipping_address': 'Kozhikode, Kerala',
            'delivery_region': 'kerala',
            'payment_method': 'upi'
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['subtotal'], 300.0)
        self.assertEqual(data['shipping_fee'], 55.0)
        self.assertEqual(data['total'], 355.0)
        self.assertEqual(data['amount'], 35500)

        order = Order.objects.get(id=data['db_order_id'])
        self.assertEqual(float(order.shipping_fee), 55.0)
        self.assertEqual(float(order.total_price), 355.0)

    @patch('store.views.get_razorpay_client')
    def test_checkout_post_inside_kerala_with_shades(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.order.create.return_value = {'id': 'order_rzp_mock_65', 'amount': 86500}
        mock_get_client.return_value = mock_client

        # Put shade in cart (subtotal = 800)
        CartItem.objects.create(user=self.user, product=self.shade_product, quantity=1)

        res = self.client.post(reverse('checkout'), {
            'full_name': 'Buyer Two',
            'phone_number': '9876543210',
            'shipping_address': 'Ernakulam, Kerala',
            'delivery_region': 'kerala',
            'payment_method': 'upi'
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['shipping_fee'], 65.0)
        self.assertEqual(data['total'], 865.0)
        self.assertEqual(data['amount'], 86500)

        order = Order.objects.get(id=data['db_order_id'])
        self.assertEqual(float(order.shipping_fee), 65.0)
        self.assertEqual(float(order.total_price), 865.0)

    @patch('store.views.get_razorpay_client')
    def test_checkout_post_outside_kerala(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.order.create.return_value = {'id': 'order_rzp_mock_95', 'amount': 39500}
        mock_get_client.return_value = mock_client

        # Put ring in cart (subtotal = 300)
        CartItem.objects.create(user=self.user, product=self.ring_product, quantity=1)

        res = self.client.post(reverse('checkout'), {
            'full_name': 'Buyer Three',
            'phone_number': '9876543210',
            'shipping_address': 'Bangalore, Karnataka',
            'delivery_region': 'outside_kerala',
            'payment_method': 'upi'
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['shipping_fee'], 95.0)
        self.assertEqual(data['total'], 395.0)
        self.assertEqual(data['amount'], 39500)

        order = Order.objects.get(id=data['db_order_id'])
        self.assertEqual(float(order.shipping_fee), 95.0)
        self.assertEqual(float(order.total_price), 395.0)

    @patch('store.views.get_razorpay_client')
    def test_checkout_post_featured_bracelet_standard_shipping(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.order.create.return_value = {'id': 'order_rzp_mock_1', 'amount': 75400}
        mock_get_client.return_value = mock_client

        cat_bracelets = Category.objects.create(name="Bracelets", slug="bracelets")
        bracelet = Product.objects.create(
            category=cat_bracelets,
            name="Matte Obsidian Stone Bracelet",
            price=899.00,
            discount_price=699.00,
            stock=5
        )

        CartItem.objects.create(user=self.user, product=bracelet, quantity=1)

        res = self.client.post(reverse('checkout'), {
            'full_name': 'Tester',
            'phone_number': '9876543210',
            'shipping_address': 'Calicut, Kerala',
            'delivery_region': 'kerala',
            'payment_method': 'upi'
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['subtotal'], 699.0)
        self.assertEqual(data['shipping_fee'], 55.0)
        self.assertEqual(data['total'], 754.0)
        self.assertEqual(data['amount'], 75400)

        order = Order.objects.get(id=data['db_order_id'])
        self.assertEqual(float(order.shipping_fee), 55.0)
        self.assertEqual(float(order.total_price), 754.0)

    @patch('store.views.get_razorpay_client')
    def test_checkout_post_with_kerala_pincode_auto_calculates_kerala_shipping(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.order.create.return_value = {'id': 'order_rzp_mock_kerala_pin', 'amount': 35500}
        mock_get_client.return_value = mock_client

        CartItem.objects.create(user=self.user, product=self.ring_product, quantity=1)

        res = self.client.post(reverse('checkout'), {
            'full_name': 'Kerala Buyer',
            'phone_number': '9876543210',
            'pincode': '673001',
            'city': 'Kozhikode',
            'shipping_address': 'Flat 4A, Marine Heights',
            'payment_method': 'upi'
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['shipping_fee'], 55.0)
        self.assertEqual(data['delivery_region'], 'kerala')
        self.assertEqual(data['pincode'], '673001')
        self.assertEqual(data['total'], 355.0)

        order = Order.objects.get(id=data['db_order_id'])
        self.assertEqual(order.pincode, '673001')
        self.assertEqual(order.city, 'Kozhikode')
        self.assertEqual(order.delivery_region, 'kerala')
        self.assertEqual(float(order.shipping_fee), 55.0)

        # Profile is updated automatically
        profile = Profile.objects.get(user=self.user)
        self.assertEqual(profile.pincode, '673001')
        self.assertEqual(profile.city, 'Kozhikode')

    @patch('store.views.get_razorpay_client')
    def test_checkout_post_with_outside_kerala_pincode_auto_calculates_outside_shipping(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.order.create.return_value = {'id': 'order_rzp_mock_outside_pin', 'amount': 39500}
        mock_get_client.return_value = mock_client

        CartItem.objects.create(user=self.user, product=self.ring_product, quantity=1)

        res = self.client.post(reverse('checkout'), {
            'full_name': 'Bangalore Buyer',
            'phone_number': '9876543210',
            'pincode': '560001',
            'city': 'Bengaluru',
            'shipping_address': 'MG Road, Residency Building',
            'payment_method': 'upi'
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['shipping_fee'], 95.0)
        self.assertEqual(data['delivery_region'], 'outside_kerala')
        self.assertEqual(data['pincode'], '560001')
        self.assertEqual(data['total'], 395.0)

        order = Order.objects.get(id=data['db_order_id'])
        self.assertEqual(order.pincode, '560001')
        self.assertEqual(order.city, 'Bengaluru')
        self.assertEqual(order.delivery_region, 'outside_kerala')
        self.assertEqual(float(order.shipping_fee), 95.0)

    @patch('store.views.get_razorpay_client')
    def test_outside_kerala_pincode_forces_mandatory_95_even_if_user_requests_kerala_rate(self, mock_get_client):
        """User outside Kerala has no option to select Kerala rate; backend strictly enforces mandatory ₹95 fee."""
        mock_client = MagicMock()
        mock_client.order.create.return_value = {'id': 'order_rzp_mock_tamper_attempt', 'amount': 39500}
        mock_get_client.return_value = mock_client

        CartItem.objects.create(user=self.user, product=self.ring_product, quantity=1)

        # Attempt to submit an outside-Kerala PIN with 'delivery_region': 'kerala'
        res = self.client.post(reverse('checkout'), {
            'full_name': 'Sneaky Buyer',
            'phone_number': '9876543210',
            'pincode': '560001', # Bengaluru PIN
            'city': 'Bengaluru',
            'shipping_address': 'Brigade Road, Bengaluru',
            'delivery_region': 'kerala', # Trying to cheat inside Kerala rate
            'payment_method': 'upi'
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['status'], 'success')
        # Mandatory ₹95 applied regardless of requested 'kerala' region!
        self.assertEqual(data['shipping_fee'], 95.0)
        self.assertEqual(data['delivery_region'], 'outside_kerala')
        self.assertEqual(data['total'], 395.0)

        order = Order.objects.get(id=data['db_order_id'])
        self.assertEqual(order.delivery_region, 'outside_kerala')
        self.assertEqual(float(order.shipping_fee), 95.0)

    def test_profile_update_form_pincode_validation(self):
        profile = Profile.objects.get(user=self.user)
        
        # Valid 6-digit Kerala PIN
        form_valid = ProfileUpdateForm({
            'phone_number': '9876543210',
            'pincode': '673001',
            'city': 'Kozhikode',
            'address': 'Beach Road'
        }, instance=profile)
        self.assertTrue(form_valid.is_valid())
        saved = form_valid.save()
        self.assertEqual(saved.pincode, '673001')

        # Invalid PIN (not 6 digits)
        form_invalid = ProfileUpdateForm({
            'phone_number': '9876543210',
            'pincode': '123',
            'city': 'Kozhikode',
            'address': 'Beach Road'
        }, instance=profile)
        self.assertFalse(form_invalid.is_valid())
        self.assertIn('pincode', form_invalid.errors)


class SessionSecurityTests(TestCase):
    def setUp(self):
        self.password = "SecTestPass123!"
        self.customer_user = User.objects.create_user(
            username="customer_user",
            email="customer@example.com",
            password=self.password
        )
        self.staff_user = User.objects.create_user(
            username="staff_user",
            email="staff@example.com",
            password=self.password,
            is_staff=True
        )

    def test_settings_session_expire_at_browser_close(self):
        """Settings must enforce session termination upon browser closure."""
        from django.conf import settings
        self.assertTrue(settings.SESSION_EXPIRE_AT_BROWSER_CLOSE)
        self.assertEqual(settings.SESSION_IDLE_TIMEOUT_STAFF, 1800)
        self.assertEqual(settings.SESSION_IDLE_TIMEOUT_CUSTOMER, 7200)

    def test_login_page_renders_clean_platform(self):
        """Login page must render the unified luxury platform for both members and staff."""
        response = self.client.get(reverse('login'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SIGN IN")
        self.assertContains(response, "Protected by Session Security")

    def test_login_page_renders_expired_notice(self):
        """Visiting login with ?expired=1 must render the session expired alert."""
        response = self.client.get(f"{reverse('login')}?expired=1")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Session Expired")
        self.assertContains(response, "Your previous session was closed due to inactivity or closing your browser.")

    def test_login_page_renders_admin_target_notice(self):
        """Visiting login with next targeting admin portal must render administrative verification notice."""
        response = self.client.get(f"{reverse('login')}?next=/adminpp/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Administrative Verification")
        self.assertContains(response, "Staff credentials with administrative privileges are required")

    def test_staff_login_enforces_browser_close_expiry(self):
        """When staff logs in, session must strictly expire on browser close."""
        response = self.client.post(reverse('login'), {
            'username': 'staff_user',
            'password': self.password,
        })
        self.assertEqual(response.status_code, 302)
        session = self.client.session
        self.assertTrue(session.get_expire_at_browser_close())
        self.assertIn('_last_activity', session)

    def test_customer_login_remember_me_options(self):
        """Customer without remember_me expires on browser close; with remember_me lasts longer."""
        # Without remember_me
        self.client.post(reverse('login'), {
            'username': 'customer_user',
            'password': self.password,
        })
        self.assertTrue(self.client.session.get_expire_at_browser_close())
        self.client.logout()

        # With remember_me
        self.client.post(reverse('login'), {
            'username': 'customer_user',
            'password': self.password,
            'remember_me': 'on',
        })
        self.assertFalse(self.client.session.get_expire_at_browser_close())

    def test_middleware_idle_timeout_auto_logouts_staff(self):
        """Staff inactive for more than 30 minutes (1800s) must be logged out and redirected."""
        self.client.post(reverse('login'), {
            'username': 'staff_user',
            'password': self.password,
        })
        # Fast-forward last_activity by 1900 seconds in the past
        s = self.client.session
        s['_last_activity'] = timezone.now().timestamp() - 1900
        s.save()

        # Next request must detect timeout, log user out, and redirect to login?expired=1
        response = self.client.get(reverse('profile'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/?expired=1', response.url)

        # Confirm session is flushed and user is no longer logged in
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_middleware_idle_timeout_ajax_response(self):
        """AJAX request on expired session must return 401 with JSON session_expired."""
        self.client.post(reverse('login'), {
            'username': 'staff_user',
            'password': self.password,
        })
        s = self.client.session
        s['_last_activity'] = timezone.now().timestamp() - 1900
        s.save()

        response = self.client.get(reverse('profile'), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 401)
        data = response.json()
        self.assertEqual(data.get('status'), 'session_expired')

    def test_logout_flushes_session(self):
        """Logging out must completely flush session and redirect safely."""
        self.client.post(reverse('login'), {
            'username': 'customer_user',
            'password': self.password,
        })
        self.assertIn('_auth_user_id', self.client.session)

        response = self.client.get(reverse('logout'))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_non_staff_logging_into_admin_target_redirects_safely(self):
        """Non-staff member logging in targeting admin portal must be safely redirected with notice."""
        response = self.client.post(f"{reverse('login')}?next=/adminpp/", {
            'username': 'customer_user',
            'password': self.password,
            'next': '/adminpp/',
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Staff privileges are required to access the Admin Portal")


class CategoryPagesAndCardDesignTests(TestCase):
    def setUp(self):
        self.cat_rings = Category.objects.create(name="Rings", slug="rings")
        self.cat_shades = Category.objects.create(name="Shades", slug="shades")
        self.ring_prod = Product.objects.create(
            category=self.cat_rings,
            name="Vampire Bat Ring",
            price=399.00,
            discount_price=299.00,
            stock=5,
            is_trending=True
        )
        self.shade_prod = Product.objects.create(
            category=self.cat_shades,
            name="Futuristic Y2K Shades",
            price=499.00,
            stock=3,
            is_trending=False
        )

    def test_category_detail_view_renders_products(self):
        """Dedicated category page renders products with 5-col minimalist cards and RS. pricing without description."""
        response = self.client.get(reverse('category_detail', kwargs={'slug': 'rings'}))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'category_detail.html')
        self.assertContains(response, "RINGS")
        self.assertContains(response, "VAMPIRE BAT RING")
        self.assertContains(response, "RS. 299.00")
        self.assertContains(response, "luxury-product-grid")
        self.assertNotContains(response, "Curated statement rings designed with")

    def test_category_detail_404_for_invalid_slug(self):
        """Invalid category slug must return 404."""
        response = self.client.get(reverse('category_detail', kwargs={'slug': 'unknown-cat'}))
        self.assertEqual(response.status_code, 404)

    def test_home_view_shows_trending_and_routes_categories_to_pages(self):
        """Homepage must show New Drop items and link collections to dedicated pages without rendering full stacked category lists."""
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        # New Drop section must be present
        self.assertContains(response, "NEW DROP")
        self.assertNotContains(response, "TRENDING NOW")
        self.assertContains(response, "VAMPIRE BAT RING")
        self.assertContains(response, "RS. 299.00")
        # Category links must route to dedicated pages
        self.assertContains(response, reverse('category_detail', kwargs={'slug': 'rings'}))
        self.assertContains(response, reverse('category_detail', kwargs={'slug': 'shades'}))
        # Stacked category product sections must NOT exist on home
        self.assertNotContains(response, 'id="section-shades"')
        self.assertNotContains(response, 'id="section-rings"')

    def test_product_detail_view_renders_large_image_and_share_button(self):
        """Product page renders large image without INR badge or flag, uppercase title, RS. price, share button, Add to Cart, Buy It Now, and WhatsApp enquiry."""
        response = self.client.get(reverse('product_detail', kwargs={'pk': self.ring_prod.id}))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'product_detail.html')
        self.assertContains(response, "VAMPIRE BAT RING")
        self.assertContains(response, "RS. 299.00")
        self.assertNotContains(response, "🇮🇳")
        self.assertNotContains(response, "pdp-currency-badge")
        self.assertContains(response, "pdp-share-icon-btn")
        self.assertContains(response, "ADD TO CART")
        self.assertContains(response, "BUY IT NOW")
        self.assertContains(response, "ENQUIRE ON WHATSAPP")
        self.assertNotContains(response, "collapseDescription")
        self.assertContains(response, "SHIPPING &amp; DELIVERY")

    def test_product_detail_slug_routing(self):
        """Product page is accessible via slug or numeric id slug."""
        response = self.client.get(reverse('product_detail_slug', kwargs={'slug': 'vampire-bat-ring'}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "VAMPIRE BAT RING")

    def test_buy_now_adds_item_and_redirects_to_checkout(self):
        """Buy It Now view adds product to user's cart and immediately redirects to checkout."""
        response = self.client.get(reverse('buy_now', kwargs={'product_id': self.ring_prod.id}))
        self.assertRedirects(response, reverse('checkout'), fetch_redirect_response=False)
        session = self.client.session
        cart = session.get('cart', {})
        self.assertEqual(cart.get(str(self.ring_prod.id)), 1)

    def test_sitemap_xml_includes_product_urls(self):
        """Dynamic sitemap must include product URLs for Google indexing."""
        response = self.client.get(reverse('sitemap_xml'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"/product/{self.ring_prod.id}/")
        self.assertContains(response, f"/product/{self.shade_prod.id}/")

    def test_ajax_add_to_cart_returns_json_and_updates_count(self):
        """AJAX request to add_to_cart must return 200 JSON with cart_item_count."""
        response = self.client.get(
            reverse('add_to_cart', kwargs={'product_id': self.ring_prod.id}),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['cart_item_count'], 1)
        self.assertEqual(data['product_id'], self.ring_prod.id)

    def test_product_detail_open_graph_tags(self):
        """Product detail page must render product-specific Open Graph and Twitter Card tags."""
        response = self.client.get(reverse('product_detail', kwargs={'pk': self.ring_prod.id}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'property="og:title" content="{self.ring_prod.name} | RS. 299 | ASHAS STORE Kozhikode"')
        self.assertContains(response, f'name="twitter:title" content="{self.ring_prod.name} | RS. 299 | ASHAS STORE"')

    def test_cart_page_links_items_to_product_detail(self):
        """Cart page must link product thumbnail and name to product detail page."""
        # Add product to cart
        self.client.get(reverse('add_to_cart', kwargs={'product_id': self.ring_prod.id}))
        response = self.client.get(reverse('cart'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('product_detail', kwargs={'pk': self.ring_prod.id}))

    def test_dynamic_category_page_generation_when_category_added_manually(self):
        """When a category is created manually, its dedicated page, shortcuts (.html, direct slug), and navigation work automatically."""
        new_cat = Category.objects.create(name="Luxury Pendants")
        self.assertEqual(new_cat.slug, "luxury-pendants")

        # 1. /category/<slug>/
        resp = self.client.get(f'/category/{new_cat.slug}/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "LUXURY PENDANTS")
        self.assertContains(resp, "NEW ARRIVALS DROPPING SOON")

        # 2. /<slug>/
        resp = self.client.get(f'/{new_cat.slug}/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "LUXURY PENDANTS")

        # 3. /<slug>.html
        resp = self.client.get(f'/{new_cat.slug}.html')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "LUXURY PENDANTS")

        # 4. Appears automatically on home collections
        home_resp = self.client.get(reverse('home'))
        self.assertContains(home_resp, "Luxury Pendants")
        self.assertContains(home_resp, f"/category/{new_cat.slug}/")


class ProductFourPhotoAndLookbookTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.category, _ = Category.objects.get_or_create(name="Rings", defaults={'slug': 'rings'})
        self.product_multi = Product.objects.create(
            category=self.category,
            name="Four Photo Signet Ring",
            price=1299.00,
            image_url="https://example.com/photo1.webp",
            image_2_url="https://example.com/photo2.webp",
            image_3_url="https://example.com/photo3.webp",
            image_4_url="https://example.com/photo4.webp",
            stock=5
        )
        self.product_single = Product.objects.create(
            category=self.category,
            name="Single Photo Ring",
            price=899.00,
            image_url="https://example.com/single.webp",
            stock=3
        )

    def test_product_display_image_is_always_first_photo(self):
        """Primary display_image must always return the first photo (image / image_url)."""
        self.assertEqual(self.product_multi.display_image, "https://example.com/photo1.webp")
        self.assertEqual(self.product_single.display_image, "https://example.com/single.webp")

    def test_gallery_images_property_ordering_and_count(self):
        """gallery_images must return all valid photos with Photo 1 guaranteed at index 0."""
        self.assertEqual(self.product_multi.photo_count, 4)
        gallery = self.product_multi.gallery_images
        self.assertEqual(len(gallery), 4)
        self.assertEqual(gallery[0], "https://example.com/photo1.webp")
        self.assertEqual(gallery[1], "https://example.com/photo2.webp")
        self.assertEqual(gallery[2], "https://example.com/photo3.webp")
        self.assertEqual(gallery[3], "https://example.com/photo4.webp")

        self.assertEqual(self.product_single.photo_count, 1)
        self.assertEqual(self.product_single.gallery_images, ["https://example.com/single.webp"])

    def test_product_form_supports_four_photos(self):
        """ProductForm must accept all 4 photo URL and file fields."""
        form = ProductForm(data={
            'category': self.category.id,
            'name': 'Form Tested Ring',
            'price': 999,
            'image_url': 'https://example.com/f1.webp',
            'image_2_url': 'https://example.com/f2.webp',
            'image_3_url': 'https://example.com/f3.webp',
            'image_4_url': 'https://example.com/f4.webp',
            'stock': 10
        })
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.photo_count, 4)
        self.assertEqual(saved.display_image, 'https://example.com/f1.webp')

    def test_product_detail_page_renders_four_photo_carousel(self):
        """PDP must render the interactive thumbnail carousel with all 4 photos below main image and no INR badge."""
        resp = self.client.get(reverse('product_detail', kwargs={'pk': self.product_multi.id}))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="pdpCarouselWrapper"')
        self.assertContains(resp, 'class="pdp-thumbnails-strip"')
        self.assertContains(resp, 'https://example.com/photo1.webp')
        self.assertContains(resp, 'https://example.com/photo2.webp')
        self.assertContains(resp, 'https://example.com/photo3.webp')
        self.assertContains(resp, 'https://example.com/photo4.webp')
        self.assertNotContains(resp, 'pdp-currency-badge')
        self.assertNotContains(resp, '>INR<')

    def test_index_page_renders_oldtheory_style_spotlights_and_collections(self):
        """Index page must render Old Theory editorial sections below trending section with 7 unique photos."""
        resp = self.client.get(reverse('home'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="section-lookbook"')
        self.assertContains(resp, 'Beyond the basics.')
        self.assertContains(resp, 'Timeless classics')
        self.assertContains(resp, 'Latest addition')
        self.assertContains(resp, 'Shop Favorite Collections')
        self.assertContains(resp, 'The New Arrivals')
        self.assertContains(resp, 'The Signature Line')
        self.assertContains(resp, 'The Limited Edition')
        self.assertContains(resp, 'The Collectives')
        self.assertNotContains(resp, 'Frequently Asked')
        self.assertNotContains(resp, "I've tried several alternatives")
        self.assertContains(resp, 'look_1.jpg')
        self.assertContains(resp, 'look_2.jpg')
        self.assertContains(resp, 'look_3.jpg')
        self.assertContains(resp, 'look_4.jpg')
        self.assertContains(resp, 'look_5.jpg')
        self.assertContains(resp, 'look_6.jpg')
        self.assertContains(resp, 'look_7.jpg')
        self.assertContains(resp, 'EXPRESS PAN-INDIA SPEED POST DELIVERY')
        self.assertNotContains(resp, 'FREE SHIPPING')
        self.assertContains(resp, 'data-cat="rings"')
        self.assertContains(resp, 'data-cat="chains"')
        self.assertContains(resp, 'data-cat="shades"')









