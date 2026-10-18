import re
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView
from django.views.decorators.csrf import csrf_exempt
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle
from rest_framework.views import APIView

from apps.accounts import attribution

from .models import GenerationPackage, Payment, GenerationLedgerEntry, PromoCode
from .payanyway import build_payment_request, verify_pay_url_signature
from .serializers import PaymentSerializer
from .tasks import send_payment_receipt_email, notify_payment_telegram
from . import catalog, services

_base_amount = services.base_amount_for  # общая с bot-топапом логика акционной цены (services.py)


# Промокоды создаются только в админке под тем же ограничением (см. promo_code_validator
# в models.py) — любой ввод вне этого набора символов заведомо не может совпасть ни с одним
# промокодом, поэтому отсекаем его до похода в БД (белый список — дополнительный рубеж
# защиты поверх параметризованных запросов ORM, которые и так исключают SQL-инъекции).
PROMO_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _resolve_promo_code(raw_code: str, package: GenerationPackage, user):
    """Возвращает (PromoCode, None) или (None, error_code). Всегда проверяется заново на сервере."""
    code = (raw_code or "").strip()
    if not code:
        return None, "promo_required"
    if not PROMO_CODE_RE.match(code):
        return None, "promo_not_found"
    try:
        promo = PromoCode.objects.get(code__iexact=code)
    except PromoCode.DoesNotExist:
        return None, "promo_not_found"

    if not promo.is_active:
        return None, "promo_inactive"
    now = timezone.now()
    if now < promo.valid_from:
        return None, "promo_not_started"
    if now > promo.valid_until:
        return None, "promo_expired"
    if not promo.applies_to(package):
        return None, "promo_not_applicable"
    if promo.max_uses is not None:
        total_used = Payment.objects.filter(promo_code=promo, status=Payment.Status.PAID).count()
        if total_used >= promo.max_uses:
            return None, "promo_limit_reached"
    if promo.max_uses_per_user is not None:
        user_used = Payment.objects.filter(
            promo_code=promo, status=Payment.Status.PAID, user=user
        ).count()
        if user_used >= promo.max_uses_per_user:
            return None, "promo_already_used"
    return promo, None


PROMO_ERROR_MESSAGES = {
    "promo_required": "Введите промокод.",
    "invalid_package": "Пакет не найден или отключён.",
    "promo_not_found": "Промокод не найден.",
    "promo_inactive": "Промокод отключён.",
    "promo_not_started": "Этот промокод пока не активен.",
    "promo_expired": "Срок действия промокода истёк.",
    "promo_not_applicable": "Промокод не подходит для выбранного тарифа.",
    "promo_limit_reached": "Промокод больше недоступен — лимит использований исчерпан.",
    "promo_already_used": "Вы уже использовали этот промокод.",
}


class PollingAnonRateThrottle(AnonRateThrottle):
    scope = "polling"


class PollingUserRateThrottle(UserRateThrottle):
    scope = "polling"


class BillingConfigView(APIView):
    """GET /api/billing/config/ — пакеты, доступные этому пользователю
    (допродажа — только тем, кто уже платил; см. catalog.can_buy)."""

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        user = request.user if request.user.is_authenticated else None
        has_paid_before = catalog.has_paid(user)

        tariffs = catalog.public_tariffs()
        if has_paid_before:
            tariffs += catalog.upsell_tariffs()

        packages = []
        for t in tariffs:
            p = t.package
            promo_active = p.first_purchase_price is not None and not has_paid_before
            packages.append(
                {
                    "id": p.id,
                    "slug": p.slug,
                    "title": p.title,
                    "title_en": p.title_en,
                    "price": str(p.price),
                    "generations": p.generations,
                    "per_photo": t.per_photo,
                    "savings_percent": t.savings_percent,
                    "is_featured": p.is_featured,
                    "visibility": p.visibility,
                    "promo_price": str(p.first_purchase_price) if promo_active else None,
                }
            )
        return Response({"packages": packages})


class PromoCodeCheckView(APIView):
    """POST /api/billing/promo/check/ — проверяет промокод и считает цену для выбранного пакета."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        package_id = request.data.get("package_id")
        if not package_id:
            return Response({"error": "package_id_required"}, status=status.HTTP_400_BAD_REQUEST)
        package = GenerationPackage.objects.filter(id=package_id).first()
        if package is None or not catalog.can_buy(package, request.user):
            return Response({"error": "invalid_package"}, status=status.HTTP_400_BAD_REQUEST)

        promo, error = _resolve_promo_code(request.data.get("code"), package, request.user)
        if error:
            return Response(
                {"error": error, "message": PROMO_ERROR_MESSAGES.get(error, "Промокод недействителен.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        base_amount = _base_amount(package, request.user)
        final_amount, discount = promo.compute_discount(base_amount)
        return Response(
            {
                "valid": True,
                "code": promo.code,
                "original_price": str(base_amount),
                "discount_amount": str(discount),
                "final_price": str(final_amount),
            }
        )


class CreateTopupView(APIView):
    """
    POST /api/billing/topup/ — создаёт платёж за выбранный пакет генераций и
    возвращает данные для отправки формы на PayAnyWay (MONETA.Assistant).
    Купить можно только готовый пакет — свободная сумма не поддерживается.
    Опционально принимает promo_code — скидка всегда пересчитывается на сервере.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        package_id = request.data.get("package_id")
        if not package_id:
            return Response({"error": "package_id_required"}, status=status.HTTP_400_BAD_REQUEST)

        package = GenerationPackage.objects.filter(id=package_id).first()
        if package is None or not catalog.can_buy(package, request.user):
            return Response({"error": "invalid_package"}, status=status.HTTP_400_BAD_REQUEST)

        amount = _base_amount(package, request.user)
        is_promo = amount != package.price

        promo_code = None
        discount_amount = Decimal("0")
        raw_promo = request.data.get("promo_code")
        if raw_promo:
            promo_code, error = _resolve_promo_code(raw_promo, package, request.user)
            if error:
                return Response(
                    {"error": error, "message": PROMO_ERROR_MESSAGES.get(error, "Промокод недействителен.")},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            amount, discount_amount = promo_code.compute_discount(amount)

        payment = Payment.objects.create(
            user=request.user,
            package=package,
            amount=amount,
            generations_granted=package.generations,
            is_test=services.is_test_payment(request.user, amount),
            promo_code=promo_code,
            discount_amount=discount_amount,
            **attribution.fields_for_payment(request.user),
        )

        description_bits = [f"Пакет «{package.title}»"]
        if is_promo:
            description_bits.append("(акция: первая генерация)")
        if promo_code:
            description_bits.append(f"(промокод {promo_code.code})")
        description_bits.append(f"— {package.generations} генераций")
        description = " ".join(description_bits)
        req = build_payment_request(payment, description, email=request.user.email)

        return Response(
            {
                "payment_id": payment.id,
                "action_url": req["action_url"],
                "method": req["method"],
                "fields": req["fields"],
            }
        )


class PaymentDetailView(APIView):
    """GET /api/billing/payments/<id>/ — статус одного платежа (для страницы чека)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        try:
            payment = Payment.objects.get(id=pk, user=request.user)
        except Payment.DoesNotExist:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(PaymentSerializer(payment).data)


class PurchaseGoalClaimView(APIView):
    """
    POST /api/billing/payments/<id>/purchase-goal/ — страница чека спрашивает,
    отправлять ли цель purchase в Метрику. Ответ report=true приходит ровно
    один раз на платёж (атомарный UPDATE по purchase_reported_at IS NULL) и
    только для реального (не тестового) платежа, подтверждённого вебхуком —
    перезагрузка страницы цель не задублирует.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        claimed = Payment.objects.filter(
            id=pk,
            user=request.user,
            status=Payment.Status.PAID,
            is_test=False,
            purchase_reported_at__isnull=True,
        ).update(purchase_reported_at=timezone.now())
        if not claimed:
            return Response({"report": False})
        payment = Payment.objects.select_related("package").get(id=pk)
        return Response(
            {
                "report": True,
                "order_price": str(payment.amount),
                "currency": payment.currency,
                "package": payment.package.slug if payment.package else "",
            }
        )


class PaymentListView(generics.ListAPIView):
    """GET /api/billing/payments/ — история всех платежей текущего пользователя."""

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = PaymentSerializer

    def get_queryset(self):
        return Payment.objects.filter(user=self.request.user).order_by("-created_at")


class BalanceView(APIView):
    """GET /api/billing/balance/ — текущий баланс генераций пользователя."""

    permission_classes = [permissions.IsAuthenticated]
    # Виджет баланса дергается и в шапке, и в форме, на каждой странице —
    # не должен делить общий "user"-лимит с остальным API.
    throttle_classes = [PollingAnonRateThrottle, PollingUserRateThrottle]

    def get(self, request):
        return Response({"balance": services.get_balance(request.user)})


@csrf_exempt
def payanyway_result(request):
    """Server-to-server "Pay URL" callback от системы Moneta.ru (PayAnyWay).
    Единственное место начисления генераций. Параметры могут прийти методом
    GET или POST в зависимости от настроек расширенного счёта — принимаем оба.
    Отвечаем текстом SUCCESS/FAIL с заголовком 200 OK, как требует документация
    PayAnyWay (в т.ч. на пустой проверочный запрос при сохранении URL в ЛК)."""
    if request.method not in ("GET", "POST"):
        return HttpResponse("FAIL", status=200)

    data = request.GET if request.method == "GET" else request.POST

    # Пустой проверочный запрос при сохранении Pay URL в личном кабинете Moneta.ru —
    # параметров нет, но ответ 200 OK всё равно обязателен.
    if not data:
        return HttpResponse("SUCCESS", status=200)

    mnt_transaction_id = data.get("MNT_TRANSACTION_ID", "")
    mnt_operation_id = data.get("MNT_OPERATION_ID", "")
    mnt_amount = data.get("MNT_AMOUNT", "")
    mnt_currency_code = data.get("MNT_CURRENCY_CODE", "")
    mnt_subscriber_id = data.get("MNT_SUBSCRIBER_ID", "")
    mnt_test_mode = data.get("MNT_TEST_MODE", "")
    signature = data.get("MNT_SIGNATURE", "")

    if not verify_pay_url_signature(
        mnt_transaction_id, mnt_operation_id, mnt_amount,
        mnt_currency_code, mnt_subscriber_id, mnt_test_mode, signature,
    ):
        return HttpResponse("FAIL", status=200)

    try:
        payment_id = int(mnt_transaction_id)
    except ValueError:
        return HttpResponse("FAIL", status=200)

    with transaction.atomic():
        try:
            payment = Payment.objects.select_for_update().get(id=payment_id)
        except Payment.DoesNotExist:
            return HttpResponse("FAIL", status=200)

        # Идемпотентность — если уже оплачен, просто возвращаем SUCCESS
        if payment.status == Payment.Status.PAID:
            return HttpResponse("SUCCESS", status=200)

        # Сверяем сумму
        if Decimal(mnt_amount) != payment.amount:
            return HttpResponse("FAIL", status=200)

        payment.status = Payment.Status.PAID
        payment.paid_at = timezone.now()
        payment.payanyway_operation_id = mnt_operation_id
        payment.raw_result_payload = dict(data)
        payment.save(update_fields=["status", "paid_at", "payanyway_operation_id", "raw_result_payload"])

        services.credit_generations(
            payment.user,
            payment.generations_granted,
            kind=GenerationLedgerEntry.Kind.TOPUP,
            payment=payment,
        )


        # on_commit — задачи ставятся в очередь, только если транзакция
        # успешно зафиксирована (иначе Celery-воркер может прочитать ещё
        # не сохранённый платёж).
        transaction.on_commit(lambda: send_payment_receipt_email.delay(payment.id))
        transaction.on_commit(lambda: notify_payment_telegram.delay(payment.id))

    return HttpResponse("SUCCESS", status=200)


def payanyway_success(request):
    """Редирект пользователя на фронт (Success URL). НЕ начисляет генерации."""
    data = request.GET if request.method == "GET" else request.POST
    inv_id = data.get("MNT_TRANSACTION_ID", "")
    return redirect(f"{settings.FRONTEND_URL}/billing/success?invoice={inv_id}")


def payanyway_fail(request):
    """Fail URL — пользователь отменил оплату или она не прошла."""
    data = request.GET if request.method == "GET" else request.POST
    inv_id = data.get("MNT_TRANSACTION_ID", "")
    return redirect(f"{settings.FRONTEND_URL}/billing/fail?invoice={inv_id}")


class BotPayRedirectView(View):
    """
    GET-страница для кнопки «Оплатить» из Telegram-бота (см.
    services.make_pay_link): открывает автоотправляемую форму на PayAnyWay —
    аналог того, что делает static/js/payment.js на сайте, но без сайтовой
    авторизации, которой у бот-пользователей нет. Адресация — по id платежа +
    подписанный токен вместо сессии/логина.
    """

    def get(self, request, payment_id):
        if not services.verify_pay_link_token(payment_id, request.GET.get("t", "")):
            return render(
                request, "billing_bot_pay_error.html",
                {"message": "Ссылка недействительна или устарела. Начните оплату заново в боте."},
                status=400,
            )

        try:
            payment = Payment.objects.select_related("package", "user").get(id=payment_id)
        except Payment.DoesNotExist:
            return render(
                request, "billing_bot_pay_error.html", {"message": "Платёж не найден."}, status=404
            )

        if payment.status == Payment.Status.PAID:
            return render(
                request, "billing_bot_pay_error.html",
                {"message": "Оплата уже получена — вернитесь в Telegram, генерации уже зачислены."},
            )
        if payment.status in (Payment.Status.FAILED, Payment.Status.EXPIRED):
            return render(
                request, "billing_bot_pay_error.html",
                {"message": "Срок действия ссылки истёк. Начните оплату заново в боте."},
            )

        description = (
            f"Пакет «{payment.package.title}»" if payment.package else "Пополнение баланса"
        ) + f" — {payment.generations_granted} генераций"
        req = build_payment_request(payment, description, email=payment.user.email or None)

        return render(
            request, "billing_bot_pay.html",
            {"action_url": req["action_url"], "fields": req["fields"]},
        )


