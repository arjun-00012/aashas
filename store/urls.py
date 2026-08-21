from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('register/', views.register_view, name='register'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('profile/', views.profile_view, name='profile'),
    path('cart/', views.cart_view, name='cart'),
    path('cart/add/<int:product_id>/', views.add_to_cart, name='add_to_cart'),
    path('cart/update/<int:product_id>/<str:action>/', views.update_cart, name='update_cart'),
    path('checkout/', views.checkout_view, name='checkout'),
    path('verify-payment/', views.payment_verify, name='payment_verify'),
    path('contact-submit/', views.contact_submit, name='contact_submit'),

    # Admin portal endpoints
    path('adminpp/', views.adminpp_dashboard, name='adminpp_dashboard'),
    path('adminpp/orders/', views.adminpp_orders, name='adminpp_orders'),
    path('adminpp/category/add/', views.category_create_or_edit, name='category_add'),
    path('adminpp/category/edit/<int:pk>/', views.category_create_or_edit, name='category_edit'),
    path('adminpp/category/delete/<int:pk>/', views.category_delete, name='category_delete'),
    path('adminpp/product/add/', views.product_create_or_edit, name='product_add'),
    path('adminpp/product/edit/<int:pk>/', views.product_create_or_edit, name='product_edit'),
    path('adminpp/product/delete/<int:pk>/', views.product_delete, name='product_delete'),
]