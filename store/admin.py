from django.contrib import admin
from django.utils.html import format_html
from .models import Category, Product, Order, OrderItem, Profile, ContactMessage

@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'image_preview', 'products_count')
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


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'category', 'image_preview', 'photo_count_display', 'price', 'discount_price', 'stock', 'is_trending', 'created_at')
    list_filter = ('category', 'is_trending', 'created_at')
    search_fields = ('name', 'description')
    list_editable = ('price', 'discount_price', 'stock')

    fieldsets = (
        ('Basic Information', {
            'fields': ('category', 'name', 'description', 'is_trending')
        }),
        ('Photo 1 (Primary / Cover Photo)', {
            'fields': ('image', 'image_url'),
            'description': 'Main product image displayed in catalogs, trending lists, cart, and preview cards.'
        }),
        ('Photo 2 (Side / Angle View)', {
            'fields': ('image_2', 'image_2_url'),
            'classes': ('collapse',)
        }),
        ('Photo 3 (On-Model / Wearing View)', {
            'fields': ('image_3', 'image_3_url'),
            'classes': ('collapse',)
        }),
        ('Photo 4 (Detail / Packaging View)', {
            'fields': ('image_4', 'image_4_url'),
            'classes': ('collapse',)
        }),
        ('Pricing & Inventory', {
            'fields': ('price', 'discount_price', 'stock')
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


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = ('product', 'price', 'quantity')


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'full_name', 'phone_number', 'total_price', 'payment_status', 'shipping_status', 'tracking_id', 'created_at')
    list_filter = ('payment_status', 'shipping_status', 'carrier', 'created_at')
    search_fields = ('id', 'full_name', 'phone_number', 'tracking_id', 'razorpay_order_id', 'razorpay_payment_id')
    readonly_fields = ('created_at', 'razorpay_order_id', 'razorpay_payment_id')
    fieldsets = (
        ('Customer & Delivery Information', {
            'fields': ('user', 'full_name', 'phone_number', 'shipping_address')
        }),
        ('Payment Details', {
            'fields': ('total_price', 'payment_status', 'razorpay_order_id', 'razorpay_payment_id')
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
    list_display = ('user', 'phone_number')
    search_fields = ('user__username', 'phone_number')
