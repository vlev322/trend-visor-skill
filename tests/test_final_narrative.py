import unittest

from tests.final_answer_helpers import final_operations
from tools.model_eval.final_narrative import interpretations, render_narrative


class FinalNarrativeTests(unittest.TestCase):
    def test_direction_is_derived_from_periods_not_the_opposite_model_slope(self):
        operations = final_operations()
        self.assertEqual(interpretations(operations), [{
            "language": "uk", "period_change_direction": "increase", "trend_interval_available": False,
        }])
        text = render_narrative(operations, ["validate_interest_with_users"])
        self.assertIn("зросли з 10,00 до 20,00 на день", text["summary_uk"])
        self.assertNotIn("за тиждень", text["summary_uk"])
        self.assertIn("+100,00%", text["summary_uk"])
        self.assertTrue(any("день тижня" in line and "місяць року" in line for line in text["limitations_uk"]))
        self.assertTrue(any("від’ємні" in line and "історич" in line for line in text["limitations_uk"]))
        self.assertTrue(any("попит" in line for line in text["next_steps_uk"]))

    def test_decrease_uses_daily_units_and_never_labels_it_persistent(self):
        operations = final_operations()
        analysis = operations["study_fresh"]["results"][0]["analysis"]
        analysis["current"].update(sum_observed_views=8, mean_daily_views_observed=4.0)
        analysis["comparison"]["change_percent"] = -60.0
        text = render_narrative(operations, ["review_page_history"])
        self.assertEqual(interpretations(operations)[0]["period_change_direction"], "decrease")
        self.assertIn("знизилися з 10,00 до 4,00 на день", text["summary_uk"])
        self.assertIn("−60,00%", text["summary_uk"])
        for misleading in ("зросли", "стійкий", "стабільний", "за тиждень"):
            self.assertNotIn(misleading, text["summary_uk"])

    def test_incomplete_coverage_does_not_generate_a_full_period_change(self):
        operations = final_operations()
        analysis = operations["study_fresh"]["results"][0]["analysis"]
        analysis["current"]["coverage"].update(observed_days=1, missing_days=1)
        analysis["current"]["status"] = "partial"
        analysis["comparison"].update(status="not_computed", change_percent=None, reason="incomplete_coverage")
        text = render_narrative(operations, ["check_measurement_coverage"])
        self.assertEqual(interpretations(operations)[0]["period_change_direction"], "not_computed")
        self.assertIn("не обчислено", text["summary_uk"])
        self.assertIn("1/2", text["summary_uk"])
        self.assertNotIn("100,00%", text["summary_uk"])

    def test_unresolved_language_and_unchanged_periods_remain_explicit(self):
        operations = final_operations()
        analysis = operations["study_fresh"]["results"][0]["analysis"]
        analysis["current"].update(sum_observed_views=20, mean_daily_views_observed=10.0)
        analysis["comparison"]["change_percent"] = 0.0
        operations["study_fresh"]["results"].append({
            "language": "pl", "status": "not_collected", "reason": "no_sitelink",
        })
        rows = interpretations(operations)
        self.assertEqual([row["period_change_direction"] for row in rows], ["unchanged", "not_computed"])
        text = render_narrative(operations, ["review_page_history"])
        self.assertIn("не змінилися", text["summary_uk"])
        self.assertIn("pl", text["summary_uk"])
        self.assertIn("no_sitelink", text["summary_uk"])

    def test_unavailable_trim_scenario_is_not_rendered_as_a_result(self):
        operations = final_operations()
        operations["analyze_uk"]["diagnostics"] = {
            "window_edges": {"status": "not_computed", "reason": "period_too_short", "scenario": None},
        }
        text = render_narrative(operations, ["review_page_history"])
        self.assertIn("+100,00%", text["summary_uk"])
        self.assertNotIn("після обрізання", text["summary_uk"])

    def test_no_observations_stay_unknown_not_zero(self):
        operations = final_operations()
        analysis = operations["study_fresh"]["results"][0]["analysis"]
        analysis["current"].update(status="no_observations", sum_observed_views=None, mean_daily_views_observed=None)
        analysis["current"]["coverage"].update(observed_days=0, missing_days=2)
        analysis["comparison"].update(status="not_computed", change_percent=None, reason="incomplete_coverage")
        text = render_narrative(operations, ["check_measurement_coverage"])
        self.assertIn("10,00 та невідомо на день", text["summary_uk"])
        self.assertIn("0/2", text["summary_uk"])
        self.assertIn("не обчислено", text["summary_uk"])
        self.assertEqual(interpretations(operations)[0]["period_change_direction"], "not_computed")