import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from daily_brief import build_daily_briefs, parse_brief, edition_date


class BriefTests(unittest.TestCase):
    def setUp(self):
        self.papers = [{"arxiv_id": "1v1", "published": "2026-10-09T00:00:00Z",
                        "title": "Spin study", "primary_category": "a", "summary_mode": "llm-deepseek",
                        "abstract_summary_zh": "Measured correlations"}]
        self.sections = [{"title": "Mon, 12 Oct 2026", "ids": ["1"]}]
        self.config = {"llm": {"model": "deepseek-flash"}}
        self.brief = {"headline": "Spin research", "overview": "A measured correlation.",
                      "themes": [{"title": "Correlations", "summary": "Measured evidence.", "paper_ids": ["1v1"]}],
                      "caveat": "Abstract only."}
        self.request = Mock(return_value=self.brief)

    def build(self, previous=None):
        return build_daily_briefs(self.papers, self.sections, previous or {}, self.config, self.request)

    def test_listing_date_and_valid_references(self):
        result = self.build()["2026-10-12"]
        self.assertEqual(result["covered"], 1)
        self.assertEqual(result["status"], "ready")
        parser = self.request.call_args.args[2]
        self.assertEqual(parser(json.dumps(self.brief)), self.brief)
        self.assertEqual(edition_date("PRB 2026-10-09"), "2026-10-09")

    def test_unknown_reference_and_empty_fields_rejected(self):
        for invalid in ({**self.brief, "overview": ""}, {**self.brief, "themes": []},
                        {**self.brief, "themes": [{"title": "a", "summary": "b", "paper_ids": ["invented"]}]}):
            with self.assertRaises(ValueError):
                parse_brief(json.dumps(invalid), {"1v1"})

    def test_cache_reuse_and_content_invalidation(self):
        first = self.build()
        self.request.reset_mock()
        self.assertEqual(self.build(first), first)
        self.request.assert_not_called()
        self.papers[0]["abstract_summary_zh"] = "Changed finding"
        self.build(first)
        self.request.assert_called_once()

    def test_model_change_invalidates_cache(self):
        first = self.build()
        self.request.reset_mock()
        self.config["llm"]["model"] = "different-model"
        self.build(first)
        self.request.assert_called_once()

    def test_operational_settings_do_not_invalidate_cached_brief(self):
        first = self.build()
        self.request.reset_mock()
        self.config["llm"].update(timeout=90, max_concurrent=2, max_requests_per_run=10)
        self.assertEqual(self.build(first), first)
        self.request.assert_not_called()

    def test_legacy_fingerprint_is_migrated_without_request(self):
        import hashlib
        first = self.build()
        prompt = self.request.call_args.args[0]
        records = json.loads(prompt.split('\n', 1)[1])["papers"]
        first["2026-10-12"]["fingerprint"] = hashlib.sha256(json.dumps(
            [1, "2026-10-12", records, 1, self.config["llm"]], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.request.reset_mock()
        self.assertEqual(self.build(first)["2026-10-12"]["status"], "ready")
        self.request.assert_not_called()

    def test_rules_not_used_as_evidence(self):
        self.papers[0]["summary_mode"] = "rule-based"
        self.assertEqual(self.build()["2026-10-12"]["covered"], 0)
        self.request.assert_not_called()

    def test_failure_does_not_reuse_stale_brief(self):
        first = self.build()
        self.papers[0]["title"] = "Changed input"
        self.request.side_effect = RuntimeError("provider unavailable")
        result = self.build(first)["2026-10-12"]
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("overview", result)

    def test_disabled_does_not_call_provider(self):
        self.config["daily_brief"] = {"enabled": False}
        self.assertEqual(self.build()["2026-10-12"]["status"], "disabled")
        self.request.assert_not_called()

    def test_input_cap_balances_categories(self):
        self.papers = [{**self.papers[0], "arxiv_id": str(i), "primary_category": "a" if i < 130 else "b"}
                       for i in range(140)]
        self.sections = []
        result = self.build()["2026-10-09"]
        self.assertEqual((result["covered"], result["total"]), (120, 140))
        prompt = self.request.call_args.args[0]
        self.assertIn('"id": "139"', prompt)


if __name__ == "__main__":
    unittest.main()
