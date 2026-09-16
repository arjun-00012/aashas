import os
from pathlib import Path
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

# Security Settings
SECRET_KEY = os.environ.get(
    'SECRET_KEY', 
    'django-insecure-aashas-store-secret-key-change-in-prod'
)

# Detect if running in production (Render, Railway, or DATABASE_URL provided)
IS_RENDER = 'RENDER' in os.environ or 'RENDER_EXTERNAL_HOSTNAME' in os.environ
IS_PRODUCTION = IS_RENDER or os.environ.get('ENV') == 'production' or bool(os.environ.get('DATABASE_URL'))

# Default DEBUG to False in production
if IS_PRODUCTION:
    DEBUG = os.environ.get('DEBUG', 'False').lower() in ('true', '1', 't')
else:
    DEBUG = os.environ.get('DEBUG', 'True').lower() in ('true', '1', 't')

ALLOWED_HOSTS = ['*']

# Reverse proxy & SSL configuration (Crucial for Render & custom domains)
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True

# Production SSL Enforcement, Secure Cookies & HSTS Protection
if not DEBUG or IS_PRODUCTION:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000  # 1 year HSTS policy
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True

# CSRF Trusted Origins for Render, custom domains, and Hostinger domain
CSRF_TRUSTED_ORIGINS = [
    'https://*.onrender.com',
    'https://*.railway.app',
    'https://ashasstore.in',
    'https://www.ashasstore.in',
]
CSRF_EXTRA = os.environ.get('CSRF_TRUSTED_ORIGINS', '')
if CSRF_EXTRA:
    CSRF_TRUSTED_ORIGINS.extend([origin.strip() for origin in CSRF_EXTRA.split(',') if origin.strip()])


# Application Definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'whitenoise.runserver_nostatic',  # Helps runserver serve static with WhiteNoise
    'django.contrib.staticfiles',
    'store',
]

# Cloudinary Storage Configuration
CLOUDINARY_CLOUD_NAME = os.environ.get('CLOUDINARY_CLOUD_NAME', 'dwk9pw2ol')
CLOUDINARY_API_KEY = os.environ.get('CLOUDINARY_API_KEY', '934762869273294')
CLOUDINARY_API_SECRET = os.environ.get('CLOUDINARY_API_SECRET', 'bu1cNNgmT6U29O9uPTIM2yyAqFs')

if CLOUDINARY_CLOUD_NAME:
    if 'cloudinary_storage' not in INSTALLED_APPS:
        INSTALLED_APPS += [
            'cloudinary_storage',
            'cloudinary',
        ]
    CLOUDINARY_STORAGE = {
        'CLOUD_NAME': CLOUDINARY_CLOUD_NAME,
        'API_KEY': CLOUDINARY_API_KEY,
        'API_SECRET': CLOUDINARY_API_SECRET,
    }

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',  # WhiteNoise for static files
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'aashas_store.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(BASE_DIR, 'templates')],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'store.views.cart_context_processor',
            ],
        },
    },
]

WSGI_APPLICATION = 'aashas_store.wsgi.application'

# Persistent Storage directory (Render Persistent Disk or local fallback)
PERSISTENT_DATA_DIR = os.environ.get('RENDER_DISK_PATH') or os.environ.get('DATA_DIR')
if PERSISTENT_DATA_DIR and os.path.exists(PERSISTENT_DATA_DIR):
    db_file = Path(PERSISTENT_DATA_DIR) / 'db.sqlite3'
    if not db_file.exists() and (BASE_DIR / 'db.sqlite3').exists():
        try:
            import shutil
            shutil.copy2(BASE_DIR / 'db.sqlite3', db_file)
        except Exception:
            pass
    media_dir = os.path.join(PERSISTENT_DATA_DIR, 'media')
else:
    db_file = BASE_DIR / 'db.sqlite3'
    media_dir = os.path.join(BASE_DIR, 'media')

# Database: PostgreSQL via DATABASE_URL (Render Postgres, Neon, Supabase) with SQLite fallback
DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL:
    DATABASES = {
        'default': dj_database_url.config(
            default=DATABASE_URL,
            conn_max_age=600,
            conn_health_checks=True,
        )
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': db_file,
            'OPTIONS': {
                'timeout': 30,
            },
        }
    }

# Password Validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

# Static Files (CSS, JavaScript, Images)
STATIC_URL = '/static/'
STATICFILES_DIRS = [os.path.join(BASE_DIR, 'store', 'static')]
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
STORAGES = {
    "default": {
        "BACKEND": "cloudinary_storage.storage.MediaCloudinaryStorage" if CLOUDINARY_CLOUD_NAME else "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# Media Files (Uploaded images)
MEDIA_URL = '/media/'
MEDIA_ROOT = media_dir

# Session Settings - Long-lived session persistence
SESSION_COOKIE_AGE = 60 * 60 * 24 * 30  # 30 days
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Authentication Redirections
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'

# Razorpay Credentials (reads from Render Environment or falls back to local strings)
RAZORPAY_KEY_ID = os.environ.get('RAZORPAY_KEY_ID', 'rzp_test_YourTestKeyIdHere')
RAZORPAY_KEY_SECRET = os.environ.get('RAZORPAY_KEY_SECRET', 'YourTestKeySecretHere')