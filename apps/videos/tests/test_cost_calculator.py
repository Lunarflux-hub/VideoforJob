from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.videos.models import VideoJob
from apps.videos.services.cost_calculator import (
    calculate_job_financials,
    calculate_tiered_credits,
    extract_polza_cost,
    extract_polza_cost_with_source,
    get_polza_pricing_matrix,
    resolve_nearest_duration,
)

User = get_user_model()


class CostCalculatorTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="test_econ_user")
        self.job = VideoJob.objects.create(
            user=self.user,
            user_prompt="Make it high contrast",
            final_prompt="Make it high contrast cinematic",
            cost_rub=Decimal("349.00"),
            resolution="720p",
            duration_sec=6.0,
        )

    def test_resolve_nearest_duration(self):
        self.assertEqual(resolve_nearest_duration(3.5), 4)
        self.assertEqual(resolve_nearest_duration(5.2), 6)
        self.assertEqual(resolve_nearest_duration(7.8), 8)
        self.assertEqual(resolve_nearest_duration(9.5), 10)
        self.assertEqual(resolve_nearest_duration(None), 6)

    def test_extract_polza_cost_from_api_payload_cost_field(self):
        payload = {"id": "123", "cost": "126.00"}
        cost = extract_polza_cost(payload, resolution="4k", duration_sec=6)
        self.assertEqual(cost, Decimal("126.00"))

    def test_extract_polza_cost_from_nested_billing(self):
        payload = {"id": "123", "billing": {"cost": 142.5}}
        cost = extract_polza_cost(payload, resolution="4k", duration_sec=8)
        self.assertEqual(cost, Decimal("142.50"))

    def test_extract_polza_cost_fallback_to_matrix(self):
        # Если API не прислал поле стоимости — берем из матрицы
        cost_720_6s = extract_polza_cost({}, resolution="720p", duration_sec=6)
        self.assertEqual(cost_720_6s, Decimal("72.00"))

        cost_4k_8s = extract_polza_cost(None, resolution="4k", duration_sec=8)
        self.assertEqual(cost_4k_8s, Decimal("142.00"))

        cost_1080_10s = extract_polza_cost(None, resolution="1080p", duration_sec=10)
        self.assertEqual(cost_1080_10s, Decimal("125.00"))

    @override_settings(
        POLZA_PRICING_OVERRIDES={
            "4k": {8: "160.00"},
        }
    )
    def test_pricing_matrix_override(self):
        matrix = get_polza_pricing_matrix()
        self.assertEqual(matrix["4k"][8], Decimal("160.00"))

    def test_calculate_job_financials_standard_single_job(self):
        # 349 ₽ выручка, 720p, 6с (Polza 72 ₽, Infra 10 ₽, Fee 3.5% = 12.22 ₽, Tax 6% = 20.94 ₽)
        fin = calculate_job_financials(self.job, raw_payload=None, duration_sec=6.0)

        self.assertEqual(fin["revenue_rub"], Decimal("349.00"))
        self.assertEqual(fin["polza_cost_rub"], Decimal("72.00"))
        self.assertEqual(fin["infra_cost_rub"], Decimal("10.00"))
        self.assertEqual(fin["total_cogs_rub"], Decimal("82.00"))
        self.assertEqual(fin["payment_fee_rub"], Decimal("12.22"))
        self.assertEqual(fin["tax_rub"], Decimal("20.94"))

        # Gross profit = 349 - 12.22 - 20.94 - 82 = 233.84 ₽
        self.assertEqual(fin["gross_profit_rub"], Decimal("233.84"))
        self.assertEqual(fin["gross_margin_percent"], Decimal("67.0"))

    def test_calculate_job_financials_4k_8s_scenario(self):
        # 349 ₽ выручка, 4K, 8с (Polza 142 ₽, Infra 10 ₽, Fee 12.22 ₽, Tax 20.94 ₽)
        self.job.resolution = "4k"
        self.job.duration_sec = 8.0
        fin = calculate_job_financials(self.job)

        self.assertEqual(fin["polza_cost_rub"], Decimal("142.00"))
        self.assertEqual(fin["total_cogs_rub"], Decimal("152.00"))
        # Gross profit = 349 - 12.22 - 20.94 - 152 = 163.84 ₽
        self.assertEqual(fin["gross_profit_rub"], Decimal("163.84"))
        self.assertEqual(fin["gross_margin_percent"], Decimal("46.9"))

    def test_calculate_tiered_credits_matrix(self):
        # 1. Draft (до 65 ₽ себестоимость) -> 0.5 кредита
        credits_draft, cogs_draft = calculate_tiered_credits("720p", 4.0, is_draft=True)
        self.assertEqual(credits_draft, Decimal("0.50"))
        self.assertEqual(cogs_draft, Decimal("58.00"))

        # 2. Легкая обработка 720p 6s (66–85 ₽ себестоимость) -> 0.75 кредита
        credits_light, cogs_light = calculate_tiered_credits("720p", 6.0)
        self.assertEqual(credits_light, Decimal("0.75"))
        self.assertEqual(cogs_light, Decimal("82.00"))

        # 3. Стандарт 720p 8s (86–115 ₽ себестоимость) -> 1.0 кредит
        credits_std, cogs_std = calculate_tiered_credits("720p", 8.0)
        self.assertEqual(credits_std, Decimal("1.00"))
        self.assertEqual(cogs_std, Decimal("95.00"))

        # 4. Высокое качество 1080p 8s (116–160 ₽ себестоимость) -> 1.5 кредита
        credits_high, cogs_high = calculate_tiered_credits("1080p", 8.0)
        self.assertEqual(credits_high, Decimal("1.50"))
        self.assertEqual(cogs_high, Decimal("122.00"))

        # 5. Максимум 4K 10s (161–205 ₽ себестоимость) -> 2.0 кредита
        credits_ultra, cogs_ultra = calculate_tiered_credits("4k", 10.0)
        self.assertEqual(credits_ultra, Decimal("2.00"))
        self.assertEqual(cogs_ultra, Decimal("168.00"))

    def test_job_financials_with_tiered_credits_cost(self):
        # Задача с 1.5 кредитами: выручка по тарифу Starter: 1.5 * 219 ₽ = 328.50 ₽
        self.job.cost_rub = None
        self.job.credits_cost = Decimal("1.5")
        self.job.resolution = "1080p"
        self.job.duration_sec = 8.0
        fin = calculate_job_financials(self.job)

        self.assertEqual(fin["revenue_rub"], Decimal("328.50"))
        self.assertEqual(fin["polza_cost_rub"], Decimal("112.00"))
        self.assertEqual(fin["infra_cost_rub"], Decimal("10.00"))
        self.assertEqual(fin["total_cogs_rub"], Decimal("122.00"))
        # Gross profit = 328.50 - 11.50 (3.5% fee) - 19.71 (6% tax) - 122.00 = 175.29 ₽
        self.assertEqual(fin["gross_profit_rub"], Decimal("175.29"))
        # Маржа: 175.29 / 328.50 = 53.4%
        self.assertEqual(fin["gross_margin_percent"], Decimal("53.4"))
        # Маржинальная прибыль заказа (Contribution Profit)
        self.assertEqual(fin["contribution_profit_rub"], Decimal("175.29"))
        self.assertEqual(fin["contribution_margin_percent"], Decimal("53.4"))

    def test_cost_source_detection_provider_actual_vs_estimate(self):
        # 1. API возвращает документированное поле cost_rub
        cost1, src1 = extract_polza_cost_with_source({"cost_rub": "118.50"})
        self.assertEqual(cost1, Decimal("118.50"))
        self.assertEqual(src1, "provider_actual")

        # 2. API возвращает charged во вложенной секции billing
        cost2, src2 = extract_polza_cost_with_source({"billing": {"charged": 95.0}})
        self.assertEqual(cost2, Decimal("95.00"))
        self.assertEqual(src2, "provider_actual")

        # 3. API не вернул фактическую стоимость -> резервная матрица каталога (pricing_estimate)
        cost3, src3 = extract_polza_cost_with_source({}, resolution="720p", duration_sec=6)
        self.assertEqual(cost3, Decimal("72.00"))
        self.assertEqual(src3, "pricing_estimate")

    @override_settings(TAX_REGIME="npd_individual")
    def test_tax_regime_npd_individual_4_percent(self):
        # При НПД физлица ставка 4% (349 * 4% = 13.96 ₽ вместо 20.94 ₽ при УСН)
        fin = calculate_job_financials(self.job, duration_sec=6.0)
        self.assertEqual(fin["tax_regime"], "npd_individual")
        self.assertEqual(fin["tax_rub"], Decimal("13.96"))
        # Маржинальная прибыль выше за счёт меньшего налога (240.82 ₽ vs 233.84 ₽)
        self.assertEqual(fin["contribution_profit_rub"], Decimal("240.82"))

    @override_settings(TAX_REGIME="usn_vat_5")
    def test_tax_regime_usn_vat_5_percent(self):
        # При УСН + НДС 5% ставка 11% (349 * 11% = 38.39 ₽)
        fin = calculate_job_financials(self.job, duration_sec=6.0)
        self.assertEqual(fin["tax_regime"], "usn_vat_5")
        self.assertEqual(fin["tax_rub"], Decimal("38.39"))

    @override_settings(TAX_REGIME="npd_individual")
    def test_npd_annual_limit_warning_starter_scale(self):
        from apps.videos.services.cost_calculator import check_tax_threshold_warnings

        # 100 клиентов на тарифе Starter (2 490 ₽/мес) = 2 988 000 ₽ в год
        annual_revenue = Decimal("100") * Decimal("2490.00") * Decimal("12")
        self.assertEqual(annual_revenue, Decimal("2988000.00"))

        warnings = check_tax_threshold_warnings(annual_revenue)
        self.assertEqual(len(warnings), 1)
        self.assertIn("превысил лимит НПД 2.4 млн", warnings[0])

    @override_settings(TAX_REGIME="usn_income")
    def test_usn_vat_threshold_warning_2026(self):
        from apps.videos.services.cost_calculator import check_tax_threshold_warnings

        # Выручка превышает 20 млн ₽ (176-ФЗ 2026 г.)
        annual_revenue = Decimal("21500000.00")
        warnings = check_tax_threshold_warnings(annual_revenue)
        self.assertEqual(len(warnings), 1)
        self.assertIn("превысил порог освобождения от НДС (20 млн", warnings[0])
