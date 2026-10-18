from decimal import Decimal

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

promo_code_validator = RegexValidator(
    regex=r"^[A-Za-z0-9_-]+$",
    message="Промокод может содержать только латинские буквы, цифры, «-» и «_».",
)


class GenerationPackage(models.Model):
    """Пакет генераций, доступный для покупки (кнопки на фронте).

    Карточки тарифов рендерятся на сервере (см. apps/billing/catalog.py и
    templates/partials/tariff_card.html) — всё, что на них написано, берётся
    из этих полей, а не хардкодится в шаблонах/JS."""

    class Visibility(models.TextChoices):
        PUBLIC = "public", "Всем"
        # Допродажа: не показывается в общем списке тарифов и продаётся только
        # тем, у кого уже есть хотя бы одна оплата (см. catalog.can_buy)
        PAID_ONLY = "paid_only", "Только уже платившим"

    title = models.CharField(max_length=100)
    title_en = models.CharField(max_length=100, blank=True)
    # Стабильный идентификатор для ссылок (/payment/?package=optimal) — не
    # зависит от id в БД и переименований
    slug = models.SlugField(max_length=32, blank=True, db_index=True)
    visibility = models.CharField(max_length=10, choices=Visibility.choices, default=Visibility.PUBLIC)
    is_featured = models.BooleanField(
        default=False, help_text="Выделен визуально и выбран по умолчанию на странице оплаты"
    )
    badge = models.CharField(max_length=40, blank=True, help_text="Бейдж на карточке, например «Популярный»")
    badge_en = models.CharField(max_length=40, blank=True)
    features = models.TextField(blank=True, help_text="Пункты на карточке — по одному в строке")
    features_en = models.TextField(blank=True, help_text="То же на английском, в том же порядке")
    price = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="RUB")
    generations = models.PositiveIntegerField(help_text="Сколько генераций даёт этот пакет")
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)
    first_purchase_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Спеццена для первой покупки пользователя (акция). Пусто — без акции.",
    )

    class Meta:
        ordering = ["sort_order"]

    def __str__(self):
        return f"{self.title} – {self.generations} генераций"


class PromoCode(models.Model):
    """Промокод со скидкой на один или несколько пакетов, действующий ограниченный срок."""

    class DiscountType(models.TextChoices):
        PERCENT = "percent", "Процент от цены"
        FIXED = "fixed", "Фиксированная сумма"

    code = models.CharField(
        max_length=32,
        unique=True,
        validators=[promo_code_validator],
        help_text="Промокод, который вводит пользователь (регистр не важен). Разрешены латиница, цифры, «-», «_».",
    )
    discount_type = models.CharField(max_length=10, choices=DiscountType.choices, default=DiscountType.PERCENT)
    discount_value = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text="Для типа «Процент» — число от 0 до 100, для «Фиксированная сумма» — сумма в рублях",
    )
    packages = models.ManyToManyField(
        GenerationPackage,
        blank=True,
        related_name="promo_codes",
        help_text="К каким тарифам применяется. Пусто — применяется ко всем тарифам.",
    )
    valid_from = models.DateTimeField(default=timezone.now, help_text="С какого момента промокод действует")
    valid_until = models.DateTimeField(help_text="До какого момента промокод действует (обязательно)")
    max_uses = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Максимум успешных оплат по этому промокоду всего. Пусто — без ограничения.",
    )
    max_uses_per_user = models.PositiveIntegerField(
        default=1,
        help_text="Сколько раз один пользователь может использовать этот промокод.",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        if self.discount_type == self.DiscountType.PERCENT:
            return f"{self.code} (-{self.discount_value}%)"
        return f"{self.code} (-{self.discount_value} ₽)"

    def clean(self):
        from django.core.exceptions import ValidationError

        errors = {}
        if self.discount_type == self.DiscountType.PERCENT:
            if self.discount_value is not None and not (0 < self.discount_value <= 100):
                errors["discount_value"] = "Процент скидки должен быть больше 0 и не больше 100."
        elif self.discount_value is not None and self.discount_value <= 0:
            errors["discount_value"] = "Сумма скидки должна быть больше 0."
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            errors["valid_until"] = "Дата окончания должна быть позже даты начала."
        if errors:
            raise ValidationError(errors)

    def is_valid_now(self) -> bool:
        now = timezone.now()
        return self.is_active and self.valid_from <= now <= self.valid_until

    def applies_to(self, package: "GenerationPackage") -> bool:
        return not self.packages.exists() or self.packages.filter(pk=package.pk).exists()

    def compute_discount(self, base_amount):
        """Возвращает (итоговая_сумма, размер_скидки), не уходя ниже нуля."""
        if self.discount_type == self.DiscountType.PERCENT:
            discount = (base_amount * self.discount_value / Decimal("100")).quantize(Decimal("0.01"))
        else:
            discount = self.discount_value
        discount = min(discount, base_amount)
        return base_amount - discount, discount


class Payment(models.Model):
    """Один счёт = один MNT_TRANSACTION_ID в PayAnyWay (Moneta.ru)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает оплаты"
        PAID = "paid", "Оплачен"
        FAILED = "failed", "Не оплачен"
        EXPIRED = "expired", "Истёк"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="payments"
    )
    package = models.ForeignKey(
        GenerationPackage,
        null=True,
        blank=True,
        on_delete=models.SET_NULL
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)   # OutSum
    currency = models.CharField(max_length=3, default="RUB")
    generations_granted = models.DecimalField(max_digits=10, decimal_places=1, default=Decimal("0.0"))
    promo_code = models.ForeignKey(
        PromoCode,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="payments",
    )
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING
    )
    # Тестовый платёж — не попадает в отчёт по продажам и цель purchase.
    # Ставится автоматически (services.is_test_payment): тестовый режим
    # PayAnyWay, staff-пользователь или сумма < 10 ₽.
    is_test = models.BooleanField(default=False)

    # Источник регистрации покупателя (копия accounts.UserAcquisition на
    # момент платежа) — для выручки по источникам и оплат с Директа
    utm_source = models.CharField(max_length=100, blank=True)
    utm_medium = models.CharField(max_length=100, blank=True)
    utm_campaign = models.CharField(max_length=200, blank=True)
    utm_content = models.CharField(max_length=200, blank=True)
    utm_term = models.CharField(max_length=200, blank=True)
    yclid = models.CharField(max_length=100, blank=True)
    # Когда страница чека отправила цель purchase в Метрику — повторно (при
    # перезагрузке) она не отправится (views.PurchaseGoalClaimView)
    purchase_reported_at = models.DateTimeField(null=True, blank=True)

    payanyway_operation_id = models.CharField(max_length=64, blank=True)  # MNT_OPERATION_ID
    raw_result_payload = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self):
        return f"Payment #{self.id} – {self.user} – {self.amount}"


class UserBalance(models.Model):
    """
    Кэш текущего баланса кредитов/генераций пользователя. Источник правды для быстрого
    чтения; GenerationLedgerEntry остаётся неизменяемым журналом-аудитом.
    Обновляется только внутри services.py под select_for_update(), чтобы
    конкурентные списания/начисления не гонялись друг с другом.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="balance",
        primary_key=True,
    )
    generations = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def credits(self) -> Decimal:
        return self.generations

    def __str__(self):
        val = f"{self.generations:.2f}".rstrip("0").rstrip(".")
        return f"{self.user} — {val} кр."


class GenerationLedgerEntry(models.Model):
    """Неизменяемый журнал начислений/списаний генераций/кредитов – источник правды для баланса."""

    class Kind(models.TextChoices):
        TOPUP = "topup", "Пополнение"
        SPEND = "spend", "Списание"
        REFUND = "refund", "Возврат"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="generation_entries"
    )
    kind = models.CharField(max_length=10, choices=Kind.choices)
    amount = models.DecimalField(max_digits=10, decimal_places=2)          # положительный для пополнения, отрицательный для списания
    balance_after = models.DecimalField(max_digits=10, decimal_places=2)
    payment = models.ForeignKey(
        Payment,
        null=True,
        blank=True,
        on_delete=models.SET_NULL
    )
    video_job = models.ForeignKey(
        "videos.VideoJob",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ledger_entries",
        help_text="Заказ на видео, на который списана генерация или оформлен возврат",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user} – {self.kind} – {self.amount} (balance {self.balance_after})"