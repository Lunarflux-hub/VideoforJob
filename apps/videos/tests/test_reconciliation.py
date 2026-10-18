from decimal import Decimal
from io import StringIO
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from apps.videos.models import VideoJob
from apps.videos.services.reconciliation import (
    get_financial_reconciliation_summary,
    reconcile_with_provider_balance,
)

User = get_user_model()


class ReconciliationServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="recon_user")

        # 1. Успешный заказ с фактической ценой API
        self.job1 = VideoJob.objects.create(
            user=self.user,
            status=VideoJob.Status.DONE,
            cost_rub=Decimal("349.00"),
            polza_cost_rub=Decimal("72.00"),
            cost_source=VideoJob.CostSource.PROVIDER_ACTUAL,
            cost_verified=False,
            polza_task_id="polza_1",
            infra_cost_rub=Decimal("10.00"),
            payment_fee_rub=Decimal("12.22"),
            tax_rub=Decimal("20.94"),
            contribution_profit_rub=Decimal("233.84"),
            contribution_margin_percent=Decimal("67.0"),
        )

        # 2. Успешный заказ с расчетной ценой каталога
        self.job2 = VideoJob.objects.create(
            user=self.user,
            status=VideoJob.Status.DONE,
            cost_rub=Decimal("349.00"),
            polza_cost_rub=Decimal("98.00"),
            cost_source=VideoJob.CostSource.PRICING_ESTIMATE,
            cost_verified=False,
            polza_task_id="polza_2",
            infra_cost_rub=Decimal("10.00"),
            payment_fee_rub=Decimal("12.22"),
            tax_rub=Decimal("20.94"),
            contribution_profit_rub=Decimal("207.84"),
            contribution_margin_percent=Decimal("59.5"),
        )

        # 3. Сбойный заказ (GPU timeout): возврат кредита, убыток по COGS (Проблема № 4)
        self.job_failed = VideoJob.objects.create(
            user=self.user,
            status=VideoJob.Status.FAILED,
            cost_rub=Decimal("0.00"),
            polza_cost_rub=Decimal("72.00"),
            cost_source=VideoJob.CostSource.PRICING_ESTIMATE,
            cost_verified=False,
            polza_task_id="polza_failed",
            infra_cost_rub=Decimal("10.00"),
            failure_cogs_rub=Decimal("82.00"),
            refunded_credits=Decimal("1.0"),
            provider_refunded=False,
            failure_reason_category=VideoJob.FailureCategory.TIMEOUT,
            contribution_profit_rub=Decimal("-82.00"),
            contribution_margin_percent=Decimal("-100.0"),
        )

    def test_reconciliation_summary_aggregation(self):
        summary = get_financial_reconciliation_summary()

        self.assertEqual(summary["jobs"]["total"], 3)
        self.assertEqual(summary["jobs"]["done"], 2)
        self.assertEqual(summary["jobs"]["failed"], 1)

        fin = summary["financials"]
        self.assertEqual(fin["total_revenue_rub"], Decimal("698.00"))
        # Общий расход Polza: 72 + 98 + 72 = 242.00 ₽
        self.assertEqual(fin["total_polza_cost_rub"], Decimal("242.00"))
        self.assertEqual(fin["actual_cost_rub"], Decimal("72.00"))
        self.assertEqual(fin["estimate_cost_rub"], Decimal("170.00"))
        self.assertEqual(fin["actual_source_count"], 1)
        self.assertEqual(fin["estimate_source_count"], 2)
        self.assertEqual(fin["unverified_count"], 3)

        # Убыток от сбоев
        self.assertEqual(summary["failures_and_refunds"]["total_failure_cogs_rub"], Decimal("82.00"))
        self.assertEqual(summary["failures_and_refunds"]["total_refunded_credits"], Decimal("1.0"))

        # Итоговая маржинальная прибыль: 233.84 + 207.84 - 82.00 = 359.68 ₽
        self.assertEqual(fin["total_contribution_profit_rub"], Decimal("359.68"))

    def test_reconcile_with_provider_balance_ok_and_auto_verify(self):
        # Баланс Polza изменился на 245.00 ₽ (разница всего 3 ₽, < 5%)
        res = reconcile_with_provider_balance(
            actual_spent_rub=Decimal("245.00"),
            auto_verify=True,
        )

        self.assertEqual(res["status"], "OK")
        self.assertLessEqual(abs(res["variance_pct"]), Decimal("5.0"))
        self.assertEqual(res["verified_updated_count"], 3)

        # Проверяем, что в БД задачи стали cost_verified = True
        self.job1.refresh_from_db()
        self.assertTrue(self.job1.cost_verified)
        self.job2.refresh_from_db()
        self.assertTrue(self.job2.cost_verified)

    def test_reconcile_with_provider_balance_critical_discrepancy(self):
        # Фактическое списание у провайдера 500 ₽, а в БД учтено только 242 ₽ (расхождение > 15%)
        res = reconcile_with_provider_balance(
            actual_spent_rub=Decimal("500.00"),
            auto_verify=True,
        )

        self.assertEqual(res["status"], "CRITICAL")
        self.assertIn("КРИТИЧЕСКОЕ РАСХОЖДЕНИЕ", res["status_message"])
        # При критическом расхождении авто-верификация не подтверждает задачи!
        self.assertEqual(res["verified_updated_count"], 0)

    def test_reconcile_command_output(self):
        out = StringIO()
        call_command("reconcile_polza_costs", "--days", "7", stdout=out)
        output = out.getvalue()

        self.assertIn("ФИНАНСОВАЯ СВЕРКА РАСХОДОВ POLZA AI", output)
        self.assertIn("Маржинальная прибыль заказа", output)
        self.assertIn("Прямой убыток от сбоев (Failure COGS)", output)

    def test_reconcile_command_with_actual_spent(self):
        out = StringIO()
        call_command(
            "reconcile_polza_costs",
            "--days",
            "7",
            "--actual-spent",
            "245.00",
            "--auto-verify",
            stdout=out,
        )
        output = out.getvalue()

        self.assertIn("[OK]", output)
        self.assertIn("Подтверждено задач", output)
