from django.contrib import admin
from django.utils.html import format_html
from .models import Category, Product, ProductSize, Order, OrderItem, Profile, ContactMessage

@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'has_sizes', 'slug', 'image_preview', 'products_count')
    list_editable = ('has_sizes',)
    list_filter = ('has_sizes',)
    search_fields = ('name', 'description')
    prepopulated_fields = {'slug': ('name',)}

    def image_preview(self, obj):
        img_url = obj.display_image
        if img_url:
            return format_html('<img src="{}" style="width: 40px; height: 40px; object-fit: cover; border-radius: 4px;" />', img_url)
        return '-'
    image_preview.short_description = 'Preview'

    def products_count(self, obj):
        return obj.products.count()
    products_count.short_description = 'Products'


class ProductSizeInline(admin.TabularInline):
    model = ProductSize
    extra = 0
    fields = ('size', 'stock')
    verbose_name = 'Size Variant'
    verbose_name_plural = 'Size Inventory (XS, S, M, L, XL, XXL)'


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('admin_code', 'name', 'category', 'has_size_chart', 'image_preview', 'photo_count_display', 'price', 'discount_price', 'stock', 'sizes_display', 'is_trending', 'created_at')
    list_filter = ('has_size_chart', 'category', 'category__has_sizes', 'is_trending', 'created_at')
    search_fields = ('admin_code', 'name', 'description')
    list_editable = ('has_size_chart', 'price', 'discount_price', 'stock')
    inlines = [ProductSizeInline]

    fieldsets = (
        ('Basic Information', {
            'fields': ('admin_code', 'category', 'name', 'description', 'is_trending')
        }),
        ('Size Chart & Sizing', {
            'fields': ('has_size_chart', 'size_chart_image', 'size_chart_url'),
            'description': 'Enable size selection (XS to XXL) and interactive size chart modal for this specific product.'
        }),
        ('Photo 1 (Primary / Cover Photo)', {
            'fields': ('image', 'image_url'),
            'description': 'Main product image displayed in catalogs, trending lists, cart, and preview cards.'
        }),
        ('Photo 2 (Side / Angle View)', {
            'fields': ('image_2', 'image_2_url'),
            'description': 'Photo 2 for interactive product carousel and card hover view.'
        }),
        ('Photo 3 (On-Model / Wearing View)', {
            'fields': ('image_3', 'image_3_url'),
            'description': 'Photo 3 for interactive product carousel.'
        }),
        ('Photo 4 (Detail / Packaging View)', {
            'fields': ('image_4', 'image_4_url'),
            'description': 'Photo 4 for interactive product carousel.'
        }),
        ('Pricing & Inventory', {
            'fields': ('price', 'discount_price', 'stock'),
            'description': 'Note: If sizes are added below, overall stock automatically equals the total of all sizes.'
        }),
        ('Timestamps', {
            'fields': ('created_at',),
            'classes': ('collapse',)
        }),
    )
    readonly_fields = ('created_at',)

    def image_preview(self, obj):
        img_url = obj.display_image
        if img_url:
            return format_html('<img src="{}" style="width: 40px; height: 40px; object-fit: cover; border-radius: 4px;" />', img_url)
        return '-'
    image_preview.short_description = 'Cover'

    def photo_count_display(self, obj):
        count = obj.photo_count
        color = '#16a34a' if count >= 4 else ('#ca8a04' if count > 1 else '#64748b')
        return format_html('<span style="background: {}; color: #fff; padding: 2px 8px; border-radius: 999px; font-size: 0.75rem; font-weight: 600;">{} / 4</span>', color, count)
    photo_count_display.short_description = 'Photos'

    def sizes_display(self, obj):
        if not obj.has_sizes:
            return format_html('<span style="color: #94a3b8; font-size: 0.75rem;">Standard</span>')
        sizes = obj.available_sizes
        if not sizes:
            return format_html('<span style="color: #ca8a04; font-size: 0.75rem; font-weight: 600;">Sizes enabled (none set)</span>')
        parts = []
        for s in sizes:
            bg = '#16a34a' if s.stock > 0 else '#94a3b8'
            parts.append(f'<span style="background: {bg}; color: #fff; padding: 2px 6px; border-radius: 4px; font-size: 0.7rem; font-weight: 700; margin-right: 3px; display: inline-block; margin-bottom: 2px;">{s.size}: {s.stock}</span>')
        return format_html(''.join(parts))
    sizes_display.short_description = 'Sizes & Stock'

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        if form.instance.sizes.exists():
            form.instance.sync_stock_from_sizes()


@admin.register(ProductSize)
class ProductSizeAdmin(admin.ModelAdmin):
    list_display = ('product', 'size', 'stock')
    list_filter = ('size', 'product__category')
    search_fields = ('product__name', 'size')
    list_editable = ('stock',)


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    fields = ('product', 'size', 'price', 'quantity')
    readonly_fields = ('product', 'size', 'price', 'quantity')


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'full_name', 'phone_number', 'phone_number_2', 'display_district', 'pincode', 'total_price', 'payment_status', 'shipping_status', 'tracking_id', 'created_at')
    list_filter = ('payment_status', 'shipping_status', 'carrier', 'delivery_region', 'created_at')
    search_fields = ('id', 'full_name', 'phone_number', 'phone_number_2', 'shipping_address', 'landmark', 'district', 'pincode', 'tracking_id', 'razorpay_order_id', 'razorpay_payment_id')
    readonly_fields = ('created_at', 'razorpay_order_id', 'razorpay_payment_id')
    fieldsets = (
        ('Customer & Delivery Information', {
            'fields': ('user', 'full_name', 'phone_number', 'phone_number_2', 'shipping_address', 'landmark', 'district', 'city', 'state', 'pincode', 'delivery_region')
        }),
        ('Payment Details', {
            'fields': ('total_price', 'shipping_fee', 'payment_status', 'razorpay_order_id', 'razorpay_payment_id')
        }),
        ('Fulfillment & Consignment Tracking', {
            'fields': ('shipping_status', 'carrier', 'tracking_id', 'tracking_notes', 'tracking_updated_at')
        }),
        ('System Timestamps', {
            'fields': ('created_at',),
            'classes': ('collapse',)
        }),
    )
    inlines = [OrderItemInline]


@admin.register(ContactMessage)
class ContactMessageAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'subject', 'created_at')
    search_fields = ('name', 'email', 'subject', 'message')
    readonly_fields = ('name', 'email', 'subject', 'message', 'created_at')


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'phone_number', 'phone_number_2', 'district', 'state', 'pincode')
    search_fields = ('user__username', 'phone_number', 'phone_number_2', 'district', 'pincode')
