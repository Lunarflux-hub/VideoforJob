import datetime

from django.contrib import admin
from django.template.response import TemplateResponse
from django.urls import path
from django.utils import timezone

from .models import GenerationPackage, Payment, GenerationLedgerEntry, PromoCode
from .reports import sales_report


@admin.register(GenerationPackage)
class GenerationPackageAdmin(admin.ModelAdmin):
    list_display = ("title", "slug", "price", "generations", "visibility", "is_featured", "is_active", "sort_order")
    list_editable = ("price", "generations", "is_active", "sort_order")
    list_filter = ("is_active", "visibility")
    search_fields = ("title", "slug")


@admin.register(PromoCode)
class PromoCodeAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "discount_type",
        "discount_value",
        "packages_display",
        "valid_from",
        "valid_until",
        "status_display",
        "used_count",
        "max_uses",
    )
    list_filter = ("is_active", "discount_type", "valid_until")
    search_fields = ("code",)
    filter_horizontal = ("packages",)
    list_editable = ("discount_value",)
    fields = (
        "code",
        "discount_type",
        "discount_value",
        "packages",
        "valid_from",
        "valid_until",
        "max_uses",
        "max_uses_per_user",
        "is_active",
    )

    def save_model(self, request, obj, form, change):
        obj.code = obj.code.strip().upper()
        obj.full_clean()
        super().save_model(request, obj, form, change)

    @admin.display(description="Тарифы")
    def packages_display(self, obj):
        packages = list(obj.packages.all())
        if not packages:
            return "Все тарифы"
        return ", ".join(p.title for p in packages)

    @admin.display(description="Использовано")
    def used_count(self, obj):
        return obj.payments.filter(status=Payment.Status.PAID).count()

    @admin.display(description="Статус", boolean=False)
    def status_display(self, obj):
        if not obj.is_active:
            return "Отключён"
        now = timezone.now()
        if now < obj.valid_from:
            return "Ещё не начался"
        if now > obj.valid_until:
            return "Истёк"
        return "Активен"


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "id", "user", "package", "amount", "discount_amount", "promo_code",
        "generations_granted", "status", "is_test", "utm_source", "created_at", "paid_at",
    )
    list_filter = ("status", "is_test", "created_at", "promo_code", "utm_source")
    search_fields = ("user__username", "user__email", "id", "yclid", "utm_campaign")
    readonly_fields = ("raw_result_payload", "purchase_reported_at")
    # Кнопка «Отчёт по продажам» над списком платежей
    change_list_template = "admin/billing/payment/change_list.html"

    def get_urls(self):
        return [
            path("report/", self.admin_site.admin_view(self.sales_report_view), name="billing_sales_report"),
        ] + super().get_urls()

    def sales_report_view(self, request):
        """Отчёт по дням без тестовых платежей (apps/billing/reports.py)."""
        today = timezone.localdate()
        try:
            date_to = datetime.date.fromisoformat(request.GET.get("to", "")) if request.GET.get("to") else today
            date_from = (
                datetime.date.fromisoformat(request.GET["from"])
                if request.GET.get("from")
                else date_to - datetime.timedelta(days=29)
            )
        except ValueError:
            date_from, date_to = today - datetime.timedelta(days=29), today
        if date_from > date_to:
            date_from, date_to = date_to, date_from
        date_from = max(date_from, date_to - datetime.timedelta(days=366))  # не больше года за раз

        context = {
            **self.admin_site.each_context(request),
            "title": "Отчёт по продажам",
            "opts": self.model._meta,
            "date_from": date_from,
            "date_to": date_to,
            "report": sales_report(date_from, date_to),
        }
        return TemplateResponse(request, "admin/billing/sales_report.html", context)


@admin.register(GenerationLedgerEntry)
class GenerationLedgerEntryAdmin(admin.ModelAdmin):
    list_display = ("user", "kind", "amount", "balance_after", "created_at")
    list_filter = ("kind", "created_at")
    search_fields = ("user__username", "user__email")