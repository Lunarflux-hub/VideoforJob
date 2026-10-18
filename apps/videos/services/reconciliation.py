"""
Сервис финансовой сверки (Reconciliation) расходов Polza AI и юнит-экономики VideoLab AI.
Решает Проблему № 1 и Проблему № 4:
- Сверяет расчётные списания по каталогу (pricing_estimate) и фактические отчёты API (provider_actual) с реальным балансом провайдера.
- Контролирует прямые технические потери (Failure COGS) от сбоев и таймаутов GPU.
- Проверяет пороги налоговых режимов (НПД 2.4 млн ₽, УСН НДС 20 млн ₽).
"""
from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from django.db.models import Sum, Count, Q
from django.utils import timezone

from apps.videos.models import VideoJob
from apps.videos.services.cost_calculator import (
    get_current_tax_regime,
    TAX_REGIME_LABELS,
    check_tax_threshold_warnings,
)

logger = logging.getLogger(__name__)


def get_financial_reconciliation_summary(
    date_from=None,
    date_to=None,
) -> dict[str, Any]:
    """
    Формирует сводный аналитический отчёт по выручке, расходам на Polza API,
    маржинальной прибыли (Contribution Profit) и потерям от сбоев за выбранный период.
    """
    qs = VideoJob.objects.all()
    if date_from:
        qs = qs.filter(created_at__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__lte=date_to)

    total_jobs = qs.count()
    done_jobs = qs.filter(status=VideoJob.Status.DONE).count()
    failed_jobs = qs.filter(status=VideoJob.Status.FAILED).count()
    other_jobs = total_jobs - (done_jobs + failed_jobs)

    aggregates = qs.aggregate(
        total_revenue=Sum("cost_rub", filter=Q(status=VideoJob.Status.DONE)),
        total_polza_cost=Sum("polza_cost_rub"),
        actual_cost_rub=Sum("polza_cost_rub", filter=Q(cost_source=VideoJob.CostSource.PROVIDER_ACTUAL)),
        estimate_cost_rub=Sum("polza_cost_rub", filter=Q(cost_source=VideoJob.CostSource.PRICING_ESTIMATE)),
        total_failure_cogs=Sum("failure_cogs_rub"),
        total_refunded_credits=Sum("refunded_credits"),
        total_infra=Sum("infra_cost_rub"),
        total_payment_fee=Sum("payment_fee_rub"),
        total_tax=Sum("tax_rub"),
        total_contribution_profit=Sum("contribution_profit_rub"),
    )

    actual_source_count = qs.filter(cost_source=VideoJob.CostSource.PROVIDER_ACTUAL).count()
    estimate_source_count = qs.filter(cost_source=VideoJob.CostSource.PRICING_ESTIMATE).count()
    verified_count = qs.filter(cost_verified=True).count()
    unverified_count = qs.filter(cost_verified=False).count()
    provider_refunded_count = qs.filter(provider_refunded=True).count()

    total_revenue = aggregates["total_revenue"] or Decimal("0.00")
    total_polza_cost = aggregates["total_polza_cost"] or Decimal("0.00")
    actual_cost_rub = aggregates["actual_cost_rub"] or Decimal("0.00")
    estimate_cost_rub = aggregates["estimate_cost_rub"] or Decimal("0.00")
    total_failure_cogs = aggregates["total_failure_cogs"] or Decimal("0.00")
    total_refunded_credits = aggregates["total_refunded_credits"] or Decimal("0.0")
    total_infra = aggregates["total_infra"] or Decimal("0.00")
    total_payment_fee = aggregates["total_payment_fee"] or Decimal("0.00")
    total_tax = aggregates["total_tax"] or Decimal("0.00")
    total_contribution_profit = aggregates["total_contribution_profit"] or Decimal("0.00")

    effective_margin_pct = (
        ((total_contribution_profit / total_revenue) * Decimal("100")).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP
        )
        if total_revenue > Decimal("0.00")
        else Decimal("0.0")
    )

    tax_regime = get_current_tax_regime()
    tax_label = TAX_REGIME_LABELS.get(tax_regime, tax_regime)
    tax_warnings = check_tax_threshold_warnings(total_revenue)

    return {
        "period": {
            "date_from": date_from,
            "date_to": date_to,
        },
        "jobs": {
            "total": total_jobs,
            "done": done_jobs,
            "failed": failed_jobs,
            "other": other_jobs,
        },
        "financials": {
            "total_revenue_rub": total_revenue,
            "total_polza_cost_rub": total_polza_cost,
            "actual_cost_rub": actual_cost_rub,
            "estimate_cost_rub": estimate_cost_rub,
            "actual_source_count": actual_source_count,
            "estimate_source_count": estimate_source_count,
            "verified_count": verified_count,
            "unverified_count": unverified_count,
            "total_infra_cost_rub": total_infra,
            "total_payment_fee_rub": total_payment_fee,
            "total_tax_rub": total_tax,
            "total_contribution_profit_rub": total_contribution_profit,
            "effective_margin_pct": effective_margin_pct,
        },
        "failures_and_refunds": {
            "total_failure_cogs_rub": total_failure_cogs,
            "total_refunded_credits": total_refunded_credits,
            "provider_refunded_count": provider_refunded_count,
        },
        "tax": {
            "regime": tax_regime,
            "label": tax_label,
            "warnings": tax_warnings,
        },
    }


def reconcile_with_provider_balance(
    actual_spent_rub: Decimal,
    date_from=None,
    date_to=None,
    auto_verify: bool = False,
) -> dict[str, Any]:
    """
    Сверяет сумму расходов в БД с фактическим расходом по балансу Polza.ai (Проблема № 1).
    При авто-верификации помечает задачи как cost_verified = True.
    """
    summary = get_financial_reconciliation_summary(date_from=date_from, date_to=date_to)
    db_polza_cost = summary["financials"]["total_polza_cost_rub"]

    actual_spent = Decimal(str(actual_spent_rub)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    discrepancy = (actual_spent - db_polza_cost).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    if actual_spent > Decimal("0.00"):
        variance_pct = ((discrepancy / actual_spent) * Decimal("100")).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP
        )
    else:
        variance_pct = Decimal("0.0")

    # Статус сверки:
    # OK: расхождение не более 5%
    # WARNING: расхождение от 5% до 15%
    # CRITICAL: расхождение свыше 15% (опасность дефицита бюджета)
    abs_variance = abs(variance_pct)
    if abs_variance <= Decimal("5.0"):
        status = "OK"
        status_message = "Расходы в приложении соответствуют балансу Polza AI (погрешность <= 5%)."
    elif abs_variance <= Decimal("15.0"):
        status = "WARNING"
        status_message = (
            f"Внимание: обнаружено расхождение {variance_pct}% ({discrepancy:,.2f} руб.). "
            "Возможно, часть вызовов тарифицировалась по другим параметрам длительности."
        )
    else:
        status = "CRITICAL"
        status_message = (
            f"КРИТИЧЕСКОЕ РАСХОЖДЕНИЕ {variance_pct}% ({discrepancy:,.2f} руб.)! "
            "Фактический баланс Polza расходуется значительно быстрее расчётных цен в приложении. "
            "Требуется корректировка тарифной матрицы каталога или проверка неучтенных запросов."
        )

    verified_updated_count = 0
    if auto_verify and status in ("OK", "WARNING"):
        qs = VideoJob.objects.filter(cost_verified=False)
        if date_from:
            qs = qs.filter(created_at__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__lte=date_to)
        verified_updated_count = qs.update(cost_verified=True)
        summary["financials"]["verified_count"] += verified_updated_count
        summary["financials"]["unverified_count"] -= verified_updated_count

    return {
        "status": status,
        "status_message": status_message,
        "actual_spent_rub": actual_spent,
        "db_polza_cost_rub": db_polza_cost,
        "discrepancy_rub": discrepancy,
        "variance_pct": variance_pct,
        "verified_updated_count": verified_updated_count,
        "summary": summary,
    }
