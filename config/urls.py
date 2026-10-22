
"""
URL configuration for config project.
"""
from functools import wraps

from django.contrib import admin
from django.urls import path, include
from django.views.generic import TemplateView
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import redirect

from apps.accounts.models import User


def role_required(*allowed_roles):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if request.user.role not in allowed_roles:
                return HttpResponseForbidden(
                    "You do not have permission to access this page."
                )

            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator


@login_required
@role_required(User.Role.ADMIN)
def admin_dashboard(request):
    return TemplateView.as_view(
        template_name='dashboards/admin.html'
    )(request)


@login_required
@role_required(User.Role.ADMIN, User.Role.HR)
def hr_dashboard(request):
    return TemplateView.as_view(
        template_name='dashboards/hr.html'
    )(request)


@login_required
def my_dashboard(request):
    return TemplateView.as_view(
        template_name='dashboards/employee.html'
    )(request)


urlpatterns = [
    path('admin/', admin.site.urls),

    # Auth URLs
    path('accounts/', include('apps.accounts.urls', namespace='accounts')),

    # Dashboard URLs
    path('admin-dashboard/', admin_dashboard, name='admin-dashboard'),
    path('hr-dashboard/', hr_dashboard, name='hr-dashboard'),
    path('my-dashboard/', my_dashboard, name='my-dashboard'),

    # Root redirect to login
    path('', lambda request: redirect('accounts:login')),
]
