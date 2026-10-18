from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from django.views.generic import TemplateView

from apps.billing.views import BotPayRedirectView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/billing/", include("apps.billing.urls")),
    path("billing/pay/<int:payment_id>/", BotPayRedirectView.as_view(), name="billing_bot_pay"),
    path("billing/success/", TemplateView.as_view(template_name="billing_success.html"), name="billing_success"),
    path("billing/fail/", TemplateView.as_view(template_name="billing_fail.html"), name="billing_fail"),
    path("policy/", TemplateView.as_view(template_name="policy.html"), name="policy"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)