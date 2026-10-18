"""
Команда ежедневной финансовой сверки расходов Polza AI с базой данных (Проблема № 1 и № 4).
Использование:
    python manage.py reconcile_polza_costs
    python manage.py reconcile_polza_costs --days 7
    python manage.py reconcile_polza_costs --days 1 --actual-spent 12450.00 --auto-verify
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.videos.services.reconciliation import (
    get_financial_reconciliation_summary,
    reconcile_with_provider_balance,
)


class Command(BaseCommand):
    help = "Проводит финансовую сверку расходов Polza.ai, юнит-экономики и маржинальности."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=1,
            help="Количество дней для анализа (по умолчанию 1 — за последние 24 часа).",
        )
        parser.add_argument(
            "--actual-spent",
            type=str,
            default=None,
            help="Фактическая сумма списания по балансу Polza.ai в рублях для сверки расхождений.",
        )
        parser.add_argument(
            "--auto-verify",
            action="store_true",
            help="Автоматически подтвердить (cost_verified=True) задачи за период при успешной сверке.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        actual_spent_str = options.get("actual_spent")
        auto_verify = options.get("auto_verify", False)

        date_from = timezone.now() - timedelta(days=days)
        date_to = timezone.now()

        self.stdout.write(self.style.MIGRATE_HEADING("=" * 70))
        self.stdout.write(self.style.MIGRATE_HEADING(f"ФИНАНСОВАЯ СВЕРКА РАСХОДОВ POLZA AI (Период: {days} дн.)"))
        self.stdout.write(self.style.MIGRATE_HEADING("=" * 70))

        if actual_spent_str:
            actual_spent = Decimal(actual_spent_str)
            res = reconcile_with_provider_balance(
                actual_spent_rub=actual_spent,
                date_from=date_from,
                date_to=date_to,
                auto_verify=auto_verify,
            )
            summary = res["summary"]

            # Вывод статуса расхождения
            status = res["status"]
            if status == "OK":
                self.stdout.write(self.style.SUCCESS(f"[OK] {res['status_message']}"))
            elif status == "WARNING":
                self.stdout.write(self.style.WARNING(f"[WARNING] {res['status_message']}"))
            else:
                self.stdout.write(self.style.ERROR(f"[CRITICAL] {res['status_message']}"))

            self.stdout.write(f"Фактически списано провайдером: {res['actual_spent_rub']:,.2f} руб.")
            self.stdout.write(f"Учтено в БД приложения:         {res['db_polza_cost_rub']:,.2f} руб.")
            self.stdout.write(f"Расхождение:                     {res['discrepancy_rub']:,.2f} руб. ({res['variance_pct']}%)")
            if auto_verify:
                self.stdout.write(self.style.SUCCESS(f"Подтверждено задач: {res['verified_updated_count']}"))
        else:
            summary = get_financial_reconciliation_summary(date_from=date_from, date_to=date_to)

        jobs = summary["jobs"]
        fin = summary["financials"]
        failures = summary["failures_and_refunds"]
        tax = summary["tax"]

        self.stdout.write("\n" + self.style.MIGRATE_LABEL("--- Статистика заказов ---"))
        self.stdout.write(f"Всего задач:       {jobs['total']}")
        self.stdout.write(f"Выполнено успешно: {jobs['done']}")
        self.stdout.write(f"Сбоев / Ошибок:    {jobs['failed']}")

        self.stdout.write("\n" + self.style.MIGRATE_LABEL("--- Расходы Polza AI и источники себестоимости ---"))
        self.stdout.write(f"Общий расход Polza в БД:      {fin['total_polza_cost_rub']:,.2f} руб.")
        self.stdout.write(f"  - Фактический отчёт API:    {fin['actual_cost_rub']:,.2f} руб. ({fin['actual_source_count']} задач)")
        self.stdout.write(f"  - Оценка по каталогу:       {fin['estimate_cost_rub']:,.2f} руб. ({fin['estimate_source_count']} задач)")
        self.stdout.write(f"Подтверждено сверкой:         {fin['verified_count']} задач")
        self.stdout.write(f"Не подтверждено (ожидают):    {fin['unverified_count']} задач")

        self.stdout.write("\n" + self.style.MIGRATE_LABEL("--- Учёт технических сбоев (Проблема № 4) ---"))
        self.stdout.write(f"Прямой убыток от сбоев (Failure COGS): {failures['total_failure_cogs_rub']:,.2f} руб.")
        self.stdout.write(f"Возвращено кредитов клиентам:         {failures['total_refunded_credits']} кредитов")
        self.stdout.write(f"Компенсировано провайдером:           {failures['provider_refunded_count']} задач")

        self.stdout.write("\n" + self.style.MIGRATE_LABEL("--- Маржинальная прибыль (Contribution Profit) ---"))
        self.stdout.write(f"Выручка:                      {fin['total_revenue_rub']:,.2f} руб.")
        self.stdout.write(f"Эквайринг (3.5%):             {fin['total_payment_fee_rub']:,.2f} руб.")
        self.stdout.write(f"Налог ({tax['label']}):       {fin['total_tax_rub']:,.2f} руб.")
        self.stdout.write(f"Инфраструктура (S3/сервер):   {fin['total_infra_cost_rub']:,.2f} руб.")
        self.stdout.write(self.style.SUCCESS(f"Маржинальная прибыль заказа:  {fin['total_contribution_profit_rub']:,.2f} руб."))
        self.stdout.write(self.style.SUCCESS(f"Маржинальность:               {fin['effective_margin_pct']}%"))

        if tax["warnings"]:
            self.stdout.write("\n" + self.style.ERROR("--- НАЛОГОВЫЕ ПРЕДУПРЕЖДЕНИЯ (ФНС 2026) ---"))
            for w in tax["warnings"]:
                self.stdout.write(self.style.WARNING(f"[!] {w}"))

        self.stdout.write(self.style.MIGRATE_HEADING("=" * 70) + "\n")

