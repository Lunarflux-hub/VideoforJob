"""
Синхронные функции с бизнес-логикой бота. Вызываются из async-хендлеров
через asgiref.sync.sync_to_async — сами по себе остаются обычным
синхронным Django-кодом, чтобы 1:1 переиспользовать существующие
apps.billing.services / apps.photos.tasks, как это делает apps.photos.views.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.validators import validate_email
from django.db import transaction

from apps.billing.catalog import can_buy
from apps.billing.models import GenerationPackage
from apps.billing.services import (
    InsufficientBalanceError,
    base_amount_for,
    create_topup_payment,
    get_balance,
    get_payment_status,
    make_pay_link,
    spend_generation,
)
from apps.support.serializers import SupportTicketCreateSerializer
from apps.support.tasks import send_support_ticket_email
from apps.videos.models import VideoJob

from ..models import TelegramProfile

User = get_user_model()


def get_or_create_profile(telegram_id: int, telegram_username: str) -> TelegramProfile:
    profile = TelegramProfile.objects.select_related("user").filter(telegram_id=telegram_id).first()
    if profile:
        return profile

    with transaction.atomic():
        user = User.objects.create(username=f"tg_{telegram_id}")
        user.set_unusable_password()
        user.save(update_fields=["password"])
        profile = TelegramProfile.objects.create(
            user=user,
            telegram_id=telegram_id,
            telegram_username=telegram_username or "",
        )
    return profile


def get_user_balance(user) -> int:
    return get_balance(user)


@dataclass
class SetEmailResult:
    ok: bool
    error: str | None = None  # "invalid" | "taken"


def set_user_email(user, raw_email: str) -> SetEmailResult:
    email = (raw_email or "").strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        return SetEmailResult(ok=False, error="invalid")

    if User.objects.exclude(pk=user.pk).filter(email__iexact=email).exists():
        return SetEmailResult(ok=False, error="taken")

    user.email = email
    user.save(update_fields=["email"])
    return SetEmailResult(ok=True)


@dataclass
class ProfileStats:
    email: str
    balance: int
    orders_total: int
    orders_done: int
    member_since: datetime.datetime


def get_profile_stats(user, profile: TelegramProfile) -> ProfileStats:
    jobs = VideoJob.objects.filter(user=user)
    return ProfileStats(
        email=user.email or "",
        balance=get_balance(user),
        orders_total=jobs.count(),
        orders_done=jobs.filter(status=VideoJob.Status.DONE).count(),
        member_since=profile.created_at,
    )


@dataclass
class SupportTicketResult:
    ok: bool
    errors: dict | None = None


@dataclass
class TariffOption:
    id: int
    title: str
    price: Decimal
    generations: int
    is_promo: bool  # спеццена первой покупки


def list_tariffs(user) -> list[TariffOption]:
    options = []
    for package in GenerationPackage.objects.filter(is_active=True).order_by("sort_order", "id"):
        if not can_buy(package, user):
            continue  # допродажа — только тем, кто уже платил
        amount = base_amount_for(package, user)
        options.append(
            TariffOption(
                id=package.id,
                title=package.title,
                price=amount,
                generations=package.generations,
                is_promo=amount != package.price,
            )
        )
    return options


@dataclass
class CreatePaymentResult:
    payment_id: int | None
    pay_url: str | None
    amount: Decimal | None = None
    generations: int | None = None
    error: str | None = None  # "invalid_package"


def create_bot_payment(user, package_id: int) -> CreatePaymentResult:
    package = GenerationPackage.objects.filter(id=package_id).first()
    if package is None or not can_buy(package, user):
        return CreatePaymentResult(payment_id=None, pay_url=None, error="invalid_package")

    payment = create_topup_payment(user, package)
    return CreatePaymentResult(
        payment_id=payment.id,
        pay_url=make_pay_link(payment),
        amount=payment.amount,
        generations=payment.generations_granted,
    )


def get_bot_payment_status(user, payment_id: int) -> str | None:
    return get_payment_status(user, payment_id)


def create_support_ticket(email: str, message: str) -> SupportTicketResult:
    """Переиспользует ту же валидацию, что и веб-форма /support (см.
    apps.support.views.SupportTicketCreateView)."""
    serializer = SupportTicketCreateSerializer(data={"email": email, "message": message})
    if not serializer.is_valid():
        return SupportTicketResult(ok=False, errors=serializer.errors)

    ticket = serializer.save(ip_address=None)
    send_support_ticket_email.delay(str(ticket.id))
    return SupportTicketResult(ok=True)
