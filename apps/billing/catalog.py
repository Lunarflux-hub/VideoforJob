"""
Витрина тарифов: какие пакеты кому показывать и что написать на карточке
(цена за фото, экономия). Используется и серверным рендером карточек
(главная, /payment/), и API (/api/billing/config/), и Telegram-ботом —
чтобы цифры везде совпадали и не хардкодились.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .models import GenerationPackage, Payment


def _whole(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_price(value: Decimal) -> str:
    """199.00 → «199», 49.50 → «49.50» — копейки только если они есть."""
    return f"{value:.0f}" if value == value.to_integral_value() else f"{value:.2f}"


@dataclass
class Tariff:
    package: GenerationPackage
    price: Decimal
    per_photo: int  # округлённая цена за фото, ₽
    savings_percent: int  # экономия относительно самого дорогого за фото пакета; 0 — нет

    @property
    def id(self):
        return self.package.id

    @property
    def slug(self):
        return self.package.slug

    @property
    def price_display(self) -> str:
        return format_price(self.price)

    @property
    def features_list(self) -> list[tuple[str, str]]:
        ru = [line.strip() for line in self.package.features.splitlines() if line.strip()]
        en = [line.strip() for line in self.package.features_en.splitlines() if line.strip()]
        return [(text, en[i] if i < len(en) else text) for i, text in enumerate(ru)]


def has_paid(user) -> bool:
    return bool(
        user
        and user.is_authenticated
        and Payment.objects.filter(user=user, status=Payment.Status.PAID).exists()
    )


def can_buy(package: GenerationPackage, user) -> bool:
    """Пакет активен и виден этому пользователю (допродажа — только платившим)."""
    if not package.is_active:
        return False
    if package.visibility == GenerationPackage.Visibility.PAID_ONLY:
        return has_paid(user)
    return True


def _public_packages() -> list[GenerationPackage]:
    return list(
        GenerationPackage.objects.filter(
            is_active=True, visibility=GenerationPackage.Visibility.PUBLIC
        ).order_by("sort_order", "id")
    )


def _build(packages: list[GenerationPackage], base: Decimal | None = None) -> list[Tariff]:
    if not packages:
        return []
    per_photo = {p.id: p.price / p.generations for p in packages if p.generations}
    # База для «экономии» — самый дорогой за фото пакет основной линейки (сейчас «Старт»)
    if base is None:
        base = max(per_photo.values()) if per_photo else None
    tariffs = []
    for p in packages:
        unit = per_photo.get(p.id, p.price)
        savings = _whole((1 - unit / base) * 100) if base else 0
        tariffs.append(Tariff(package=p, price=p.price, per_photo=_whole(unit), savings_percent=max(savings, 0)))
    return tariffs


def public_tariffs() -> list[Tariff]:
    """Основная линейка — то, что видят все (главная, /payment/)."""
    return _build(_public_packages())


def upsell_tariffs() -> list[Tariff]:
    """Допродажа (paid_only) — экономия считается от основной линейки,
    чтобы «−39%» на карточке было честным сравнением со «Стартом».
    Кому её показывать, решает вызывающий код через has_paid/can_buy."""
    public = [p for p in _public_packages() if p.generations]
    base = max((p.price / p.generations for p in public), default=None)
    upsell = GenerationPackage.objects.filter(
        is_active=True, visibility=GenerationPackage.Visibility.PAID_ONLY
    ).order_by("sort_order", "id")
    return _build(list(upsell), base=base)


def min_price_per_photo() -> int | None:
    """«от 30 ₽ за фото» на карточках стилей."""
    tariffs = public_tariffs()
    return min(t.per_photo for t in tariffs) if tariffs else None


def min_package_price() -> str | None:
    """Самый дешёвый вход — для кнопки «Попробовать за 49 ₽»."""
    tariffs = public_tariffs()
    return format_price(min(t.price for t in tariffs)) if tariffs else None


def unlock_offers() -> tuple[GenerationPackage | None, GenerationPackage | None]:
    return None, None
