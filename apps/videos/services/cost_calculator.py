"""
Модуль расчёта финансовой себестоимости и юнит-экономики для VideoLab AI.
Учитывает:
1. Фактическое списание Polza AI (из ответа API или из настраиваемой матрицы каталога).
2. Инфраструктурные затраты (S3, сервер, Celery, трафик).
3. Комиссии платёжного шлюза (PayAnyWay: ~3.5%).
4. Налоги (УСН Доходы 6% / самозанятость).
5. Итоговую валовую прибыль (Gross Profit) и маржинальность (Gross Margin %).
"""
from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from django.conf import settings

logger = logging.getLogger(__name__)

# Базовая условная выручка за 1 кредит для расчёта маржи (219 ₽ из тарифа Starter 2 190 ₽ за 10 кредитов)
DEFAULT_CREDIT_BASE_REVENUE_RUB = Decimal("219.00")

# Тарифы каталога Polza AI (Gemini Omni 1.1 Flash) для видео:
# Разрешение -> Длительность (сек) -> Стоимость в рублях
DEFAULT_POLZA_PRICING_MATRIX: dict[str, dict[int, Decimal]] = {
    "draft": {
        4: Decimal("48.00"),
        6: Decimal("55.00"),
        8: Decimal("65.00"),
        10: Decimal("75.00"),
    },
    "4k": {
        4: Decimal("111.00"),
        6: Decimal("126.00"),
        8: Decimal("142.00"),
        10: Decimal("158.00"),
    },
    "1080p": {
        4: Decimal("85.00"),
        6: Decimal("98.00"),
        8: Decimal("112.00"),
        10: Decimal("125.00"),
    },
    "720p": {
        4: Decimal("58.00"),
        6: Decimal("72.00"),
        8: Decimal("85.00"),
        10: Decimal("98.00"),
    },
}

# Инфраструктурная себестоимость по умолчанию (S3, сервер, GPU очереди)
DEFAULT_INFRASTRUCTURE_COST_RUB = Decimal("10.00")

# Ставка эквайринга (PayAnyWay): 3.5%
DEFAULT_PAYMENT_FEE_RATE = Decimal("0.035")

# Налоговые режимы РФ (Проблема № 2: УСН vs НПД vs НДС)
class TaxRegime:
    USN_INCOME = "usn_income"              # УСН «Доходы» 6%
    NPD_INDIVIDUAL = "npd_individual"      # НПД / Самозанятость: физлица 4% (лимит 2.4 млн ₽/год)
    NPD_LEGAL = "npd_legal"                # НПД / Самозанятость: юрлица и ИП 6% (лимит 2.4 млн ₽/год)
    USN_VAT_5 = "usn_vat_5"                # УСН + НДС 5% (176-ФЗ / 2026 при доходах 20–250 млн ₽)
    USN_VAT_7 = "usn_vat_7"                # УСН + НДС 7% (176-ФЗ / 2026 при доходах 250–450 млн ₽)
    OSNO_VAT_20 = "osno_vat_20"            # ОСНО с НДС 20%
    CUSTOM = "custom"                      # Индивидуальная ставка

TAX_REGIME_RATES: dict[str, Decimal] = {
    TaxRegime.USN_INCOME: Decimal("0.060"),
    TaxRegime.NPD_INDIVIDUAL: Decimal("0.040"),
    TaxRegime.NPD_LEGAL: Decimal("0.060"),
    TaxRegime.USN_VAT_5: Decimal("0.110"),   # 6% УСН + 5% НДС
    TaxRegime.USN_VAT_7: Decimal("0.130"),   # 6% УСН + 7% НДС
    TaxRegime.OSNO_VAT_20: Decimal("0.200"),
}

TAX_REGIME_LABELS: dict[str, str] = {
    TaxRegime.USN_INCOME: "УСН «Доходы» (6.0%)",
    TaxRegime.NPD_INDIVIDUAL: "НПД / Самозанятость: физлица (4.0%, лимит 2.4 млн ₽/год)",
    TaxRegime.NPD_LEGAL: "НПД / Самозанятость: юрлица и ИП (6.0%, лимит 2.4 млн ₽/год)",
    TaxRegime.USN_VAT_5: "УСН + НДС 5% (176-ФЗ / 2026 при доходах > 20 млн ₽)",
    TaxRegime.USN_VAT_7: "УСН + НДС 7% (176-ФЗ / 2026 при доходах > 250 млн ₽)",
    TaxRegime.OSNO_VAT_20: "ОСНО: НДС 20% + налог на прибыль",
    TaxRegime.CUSTOM: "Индивидуальная ставка",
}

# Пороги доходов согласно налоговому законодательству РФ (ФНС 2026)
NPD_ANNUAL_LIMIT_RUB = Decimal("2400000.00")         # 2.4 млн ₽ порог самозанятости
USN_VAT_THRESHOLD_2026_RUB = Decimal("20000000.00")  # 20 млн ₽ порог освобождения от НДС по УСН на 2026 г. (ФНС)

# Версия тарифной матрицы каталога Polza по умолчанию
DEFAULT_PRICING_VERSION = "v1.1-2026-10"

# Базовая налоговая ставка по умолчанию (УСН Доходы): 6.0%
DEFAULT_TAX_RATE = Decimal("0.060")


def get_current_tax_regime() -> str:
    """Возвращает текущий налоговый режим из settings.TAX_REGIME (по умолчанию usn_income)."""
    return getattr(settings, "TAX_REGIME", TaxRegime.USN_INCOME)


def get_tax_rate(regime: str | None = None) -> Decimal:
    """Определяет эффективную ставку налога с учётом выбранного режима или прямого оверрайда."""
    override = getattr(settings, "TAX_RATE", None)
    if override is not None:
        return Decimal(str(override))
    current_regime = regime or get_current_tax_regime()
    return TAX_REGIME_RATES.get(current_regime, DEFAULT_TAX_RATE)


def check_tax_threshold_warnings(annual_revenue_rub: Decimal) -> list[str]:
    """Проверяет достижение налоговых порогов законодательства РФ (ФНС 2026)."""
    regime = get_current_tax_regime()
    warnings = []
    if regime in (TaxRegime.NPD_INDIVIDUAL, TaxRegime.NPD_LEGAL):
        if annual_revenue_rub >= NPD_ANNUAL_LIMIT_RUB:
            warnings.append(
                f"КРИТИЧЕСКИЙ ЛИМИТ: Годовой доход ({annual_revenue_rub:,.2f} руб.) превысил лимит НПД 2.4 млн руб. "
                "Необходим немедленный переход на УСН (ФЗ № 422-ФЗ)."
            )
        elif annual_revenue_rub >= NPD_ANNUAL_LIMIT_RUB * Decimal("0.85"):
            warnings.append(
                f"ПРЕДУПРЕЖДЕНИЕ: Доход ({annual_revenue_rub:,.2f} руб.) приближается к лимиту НПД (2.4 млн руб., 85%+)."
            )
    elif regime == TaxRegime.USN_INCOME:
        if annual_revenue_rub >= USN_VAT_THRESHOLD_2026_RUB:
            warnings.append(
                f"ЛИМИТ НДС 2026: Доход ({annual_revenue_rub:,.2f} руб.) превысил порог освобождения от НДС (20 млн руб.). "
                "Возникает обязанность уплаты НДС 5% (176-ФЗ)."
            )
    return warnings


def get_polza_pricing_matrix() -> dict[str, dict[int, Decimal]]:
    """Возвращает матрицу цен с учётом возможных переопределений из settings."""
    overrides = getattr(settings, "POLZA_PRICING_OVERRIDES", None)
    if not overrides:
        return DEFAULT_POLZA_PRICING_MATRIX

    matrix = {res: dict(dur_map) for res, dur_map in DEFAULT_POLZA_PRICING_MATRIX.items()}
    for res, dur_map in overrides.items():
        res_key = str(res).lower()
        if res_key not in matrix:
            matrix[res_key] = {}
        for dur, price in dur_map.items():
            matrix[res_key][int(dur)] = Decimal(str(price))
    return matrix


def resolve_nearest_duration(duration_sec: float | int | None) -> int:
    """Округляет фактическую длительность ролика до ближайшей сетки тарификации Polza (4, 6, 8, 10)."""
    if duration_sec is None or duration_sec <= 0:
        return 6  # базовый ориентир

    dur = round(duration_sec)
    if dur <= 4:
        return 4
    if dur <= 6:
        return 6
    if dur <= 8:
        return 8
    return 10


def extract_polza_cost_with_source(
    raw_payload: dict | None,
    resolution: str = "720p",
    duration_sec: float | int | None = None,
) -> tuple[Decimal, str]:
    """
    Определяет точный расход в Polza AI и возвращает кортеж (cost, cost_source):
    - cost_source == 'provider_actual', если сумма найдена в ответе Polza (cost, cost_rub, charged и т.д.)
    - cost_source == 'pricing_estimate', если сумма рассчитана по матрице каталога
    """
    if raw_payload and isinstance(raw_payload, dict):
        # 1. Поиск в корневом ответе
        for key in ("cost_rub", "cost", "charged", "charged_rub", "price", "amount"):
            val = raw_payload.get(key)
            if val is not None:
                try:
                    return Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "provider_actual"
                except Exception:
                    pass

        # 2. Поиск во вложенных секциях
        for section_key in ("billing", "usage", "meta", "data"):
            section = raw_payload.get(section_key)
            if isinstance(section, dict):
                for key in ("cost_rub", "cost", "charged", "charged_rub", "price", "amount"):
                    val = section.get(key)
                    if val is not None:
                        try:
                            return Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "provider_actual"
                        except Exception:
                            pass

    # 3. Резервная матрица каталога Polza
    matrix = get_polza_pricing_matrix()
    res_key = (resolution or "720p").lower()
    res_table = matrix.get(res_key, matrix.get("720p", {}))

    nearest_dur = resolve_nearest_duration(duration_sec)
    cost = res_table.get(nearest_dur, Decimal("85.00"))
    return cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "pricing_estimate"


def extract_polza_cost(
    raw_payload: dict | None,
    resolution: str = "720p",
    duration_sec: float | int | None = None,
) -> Decimal:
    """Алиас для обратной совместимости, возвращающий только сумму Decimal."""
    cost, _ = extract_polza_cost_with_source(raw_payload, resolution=resolution, duration_sec=duration_sec)
    return cost


def calculate_tiered_credits(
    resolution: str = "720p",
    duration_sec: float | int | None = 6.0,
    is_draft: bool = False,
) -> tuple[Decimal, Decimal]:
    """
    Рассчитывает количество кредитов к списанию согласно обновленной Матрице Tiered Billing:
    - До 65 ₽          -> 0.5 кредита  (Draft, 4–6 секунд)
    - 66–85 ₽          -> 0.75 кредита (Draft, 8–10 секунд; 720p, 4–6 секунд)
    - 86–115 ₽         -> 1.0 кредит   (720p, 8–10 секунд; 1080p, 4–6 секунд)
    - 116–160 ₽        -> 1.5 кредита  (1080p, 8–10 секунд; 4K, 4–8 секунд)
    - 161–205 ₽        -> 2.0 кредита  (4K, 10 секунд)
    - Свыше 205 ₽      -> Безопасный пересчет (2.0 + 0.5 кр. за каждые 50 ₽)
    """
    res_key = "draft" if is_draft else (resolution or "720p").lower()
    polza_cost = extract_polza_cost(None, resolution=res_key, duration_sec=duration_sec)
    infra_cost = getattr(settings, "INFRASTRUCTURE_COST_RUB", DEFAULT_INFRASTRUCTURE_COST_RUB)
    total_cogs = polza_cost + infra_cost

    if total_cogs <= Decimal("65.00"):
        credits = Decimal("0.50")
    elif total_cogs <= Decimal("85.00"):
        credits = Decimal("0.75")
    elif total_cogs <= Decimal("115.00"):
        credits = Decimal("1.00")
    elif total_cogs <= Decimal("160.00"):
        credits = Decimal("1.50")
    elif total_cogs <= Decimal("205.00"):
        credits = Decimal("2.00")
    else:
        extra = ((total_cogs - Decimal("205.00")) / Decimal("50.00")).quantize(Decimal("0.50"), rounding=ROUND_HALF_UP)
        credits = Decimal("2.00") + max(Decimal("0.50"), extra)

    return credits, total_cogs


def calculate_job_financials(
    job,
    raw_payload: dict | None = None,
    duration_sec: float | int | None = None,
) -> dict[str, Any]:
    """
    Рассчитывает полную юнит-экономику для выполненной задачи VideoJob:
    - revenue_rub: признанная выручка от списанных кредитов (credits_cost * 219 ₽).
    - polza_cost_rub: расход на вызов Polza API.
    - cost_source: 'provider_actual' (из ответа API) или 'pricing_estimate' (из матрицы каталога).
    - pricing_version: версия каталога (DEFAULT_PRICING_VERSION).
    - infra_cost_rub: фиксированная инфраструктура (10 ₽).
    - payment_fee_rub: эквайринг (3.5%).
    - tax_rub: налог согласно выбранному режиму (УСН 6%, НПД 4%/6%, НДС).
    - tax_regime: строковый код режима.
    - contribution_profit_rub: маржинальная прибыль заказа = Выручка - Эквайринг - Налог - Polza - Инфра.
    - contribution_margin_percent: маржинальность после переменных расходов (%).
    - gross_profit_rub: алиас для совместимости.
    - gross_margin_percent: алиас для совместимости.
    """
    revenue = getattr(job, "cost_rub", None)
    if revenue is None:
        credits_cost = getattr(job, "credits_cost", None)
        if credits_cost is not None:
            credit_base_rate = getattr(settings, "CREDIT_BASE_REVENUE_RUB", DEFAULT_CREDIT_BASE_REVENUE_RUB)
            revenue = (Decimal(str(credits_cost)) * credit_base_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            revenue = getattr(settings, "VIDEO_PRICE_SINGLE", Decimal("299.00"))
    revenue = Decimal(str(revenue)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    resolution = getattr(job, "resolution", "720p")
    eff_duration = duration_sec if duration_sec is not None else getattr(job, "duration_sec", 6.0)

    polza_cost, cost_source = extract_polza_cost_with_source(raw_payload, resolution=resolution, duration_sec=eff_duration)
    pricing_version = getattr(settings, "POLZA_PRICING_VERSION", DEFAULT_PRICING_VERSION)
    infra_cost = getattr(settings, "INFRASTRUCTURE_COST_RUB", DEFAULT_INFRASTRUCTURE_COST_RUB)
    payment_fee_rate = getattr(settings, "PAYMENT_FEE_RATE", DEFAULT_PAYMENT_FEE_RATE)

    tax_regime = get_current_tax_regime()
    tax_rate = get_tax_rate(tax_regime)

    payment_fee = (revenue * payment_fee_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    tax = (revenue * tax_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    total_cogs = polza_cost + infra_cost

    contribution_profit = revenue - payment_fee - tax - total_cogs
    contribution_margin_pct = (
        ((contribution_profit / revenue) * Decimal("100")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        if revenue > 0
        else Decimal("0.0")
    )

    return {
        "revenue_rub": revenue,
        "polza_cost_rub": polza_cost,
        "cost_source": cost_source,
        "pricing_version": pricing_version,
        "infra_cost_rub": infra_cost,
        "payment_fee_rub": payment_fee,
        "tax_rub": tax,
        "tax_regime": tax_regime,
        "tax_rate": tax_rate,
        "total_cogs_rub": total_cogs,
        "contribution_profit_rub": contribution_profit,
        "contribution_margin_percent": contribution_margin_pct,
        "gross_profit_rub": contribution_profit,
        "gross_margin_percent": contribution_margin_pct,
    }
