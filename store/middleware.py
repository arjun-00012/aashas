from django.conf import settings
from django.contrib.auth import logout as auth_logout
from django.shortcuts import redirect
from django.urls import reverse
from django.http import JsonResponse
from django.utils import timezone
from urllib.parse import quote

EXCLUDED_PATHS = (
    '/static/',
    '/media/',
    '/ping/',
    '/health/',
    '/cron/',
    '/favicon.ico',
    '/robots.txt',
    '/sitemap.xml',
)

class SessionSecurityMiddleware:
    """
    Enforces idle inactivity timeouts and strict session hygiene:
    1. Tracks `_last_activity` on every authenticated request.
    2. Automatically logs out and flushes sessions that exceed idle timeout:
       - Staff/Superuser: 30 minutes idle timeout (SESSION_IDLE_TIMEOUT_STAFF).
       - Regular members: 2 hours idle timeout (SESSION_IDLE_TIMEOUT_CUSTOMER).
    3. Seamlessly redirects expired sessions to `/login/?expired=1&next=...` (or returns 401 for AJAX).
    4. Guarantees staff/admin sessions are bound to browser closure.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path

        # Bypass static and health endpoints
        if any(path.startswith(prefix) for prefix in EXCLUDED_PATHS):
            return self.get_response(request)

        if request.user.is_authenticated:
            now = timezone.now().timestamp()
            last_activity = request.session.get('_last_activity')

            # Determine idle timeout based on user role
            if request.user.is_staff or request.user.is_superuser:
                idle_timeout = getattr(settings, 'SESSION_IDLE_TIMEOUT_STAFF', 1800)
                # Ensure staff session is always set to expire when browser closes
                if not request.session.get_expire_at_browser_close():
                    request.session.set_expiry(0)
            else:
                idle_timeout = getattr(settings, 'SESSION_IDLE_TIMEOUT_CUSTOMER', 7200)

            # Check if idle threshold is exceeded
            if last_activity is not None and (now - last_activity) > idle_timeout:
                auth_logout(request)
                request.session.flush()

                # Handle AJAX / JSON fetch requests
                is_ajax = (
                    request.headers.get('x-requested-with') == 'XMLHttpRequest'
                    or request.GET.get('format') == 'json'
                    or 'application/json' in request.headers.get('accept', '')
                )
                if is_ajax:
                    return JsonResponse({
                        'status': 'session_expired',
                        'message': 'Your session has expired due to inactivity. Please sign in again.'
                    }, status=401)

                # Standard browser navigation
                login_url = reverse('login')
                redirect_url = f"{login_url}?expired=1"
                full_path = request.get_full_path()
                if full_path and full_path not in ('/', '/logout/', '/login/'):
                    redirect_url += f"&next={quote(full_path)}"
                return redirect(redirect_url)

            # Update last activity for authenticated user
            request.session['_last_activity'] = now

        response = self.get_response(request)
        return response
