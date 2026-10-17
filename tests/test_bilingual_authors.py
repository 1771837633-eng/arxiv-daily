import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fetch_arxiv as fetch
from author_metadata import parse_author_html, enrich_authors
from english_guide import FIELDS, supplement_english, english_fingerprint, parse_english
from daily_brief import supplement_brief_english


HTML = '''<div class="ltx_authors">
<span class="ltx_role_author"><span class="ltx_personname">First Author</span>
<span class="ltx_role_affiliation">Affiliation: Institute One</span>
<span class="ltx_author_notes"><a href="mailto:first@example.org">Email</a></span></span>
<span class="ltx_role_author"><span class="ltx_personname">Last Author</span>
<span class="ltx_author_notes">Corresponding author</span>
<span class="ltx_role_affiliation">Institute Two</span></span></div>'''


class BilingualAuthorTests(unittest.TestCase):
    def setUp(self):
        self.english = {key: "Test " + key for key in FIELDS}
        self.summary, _ = fetch.fallback_chinese_summary("Title", "Abstract")
        self.summary["summary_mode"] = "llm-test"

    def test_supplement_only_once_and_keep_chinese(self):
        request = Mock(return_value=self.english)
        cfg = {"english_guide": {"enabled": True}}
        first = supplement_english(self.summary, "Title", "Abstract", cfg, "fp", request)
        second = supplement_english(first, "Title", "Abstract", cfg, "fp", request)
        self.assertEqual(first, second)
        self.assertEqual(first["abstract_summary_zh"], self.summary["abstract_summary_zh"])
        self.assertEqual(request.call_count, 1)

    def test_failed_supplement_does_not_erase_chinese(self):
        result = supplement_english(self.summary, "T", "A", {"english_guide": {"enabled": True}},
                                    "fp", Mock(side_effect=RuntimeError()))
        self.assertEqual(result, self.summary)

    def test_english_rejects_incomplete_response(self):
        with self.assertRaises(ValueError):
            parse_english({"finding": "Only finding"})

    def test_new_paper_generates_both_languages_in_one_request(self):
        response = {"study_type_zh": "Experimental", "research_object_zh": "Spin system",
                    "core_finding_zh": "Cooling increases correlations",
                    "research_question_zh": "What limits the correlation length?",
                    "evidence_zh": "Scattering spectra compared across temperatures",
                    "method_zh": "Neutron scattering", "novelty_zh": "Not specified",
                    "limitations_zh": "Abstract only", "english_guide": self.english}
        def request(prompt, config, parser, **kwargs):
            return parser(json.dumps(response))
        with patch.object(fetch, "request_llm_json", side_effect=request) as call:
            value = fetch.summarize_with_llm("T", "A", {"english_guide": {"enabled": True}})
        call.assert_called_once()
        self.assertEqual(value["english_guide"], self.english)
        self.assertEqual(value["abstract_summary_zh"], response["core_finding_zh"])

    def test_source_fingerprint_change_regenerates_english(self):
        request = Mock(return_value=self.english)
        old = {**self.summary, "english_guide": self.english, "english_fingerprint": english_fingerprint("old")}
        supplement_english(old, "T", "A", {"english_guide": {"enabled": True}}, "new", request)
        request.assert_called_once()

    def test_cached_chinese_adds_english_without_chinese_call(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = {"english_guide": {"enabled": True}}
            cache = fetch.PaperCache(Path(folder) / "cache.json")
            cache.set("id", self.summary, [], fetch.summary_fingerprint("T", "A", cfg))
            with patch.object(fetch, "make_chinese_summary") as chinese, patch.object(fetch, "request_llm_json", return_value=self.english) as request:
                first, _ = fetch.summarize_cached("id", "T", "A", cfg, cache)
                fetch.summarize_cached("id", "T", "A", cfg, cache)
            chinese.assert_not_called()
            request.assert_called_once()
            self.assertEqual(first["english_guide"], self.english)

    def test_email_alone_does_not_make_corresponding_author(self):
        value = parse_author_html(HTML, ["First Author", "Last Author"], "https://arxiv.org/html/test")
        self.assertEqual(value["corresponding_authors"], ["Last Author"])
        self.assertEqual(value["contact_authors"], ["First Author"])
        self.assertEqual(value["first_affiliation"], "Institute One")

    def test_missing_author_mapping_does_not_guess(self):
        value = parse_author_html(HTML, ["Different Author"], "https://arxiv.org/html/test")
        self.assertEqual(value["first_affiliation"], "")
        self.assertEqual(value["corresponding_authors"], [])

    def test_highwire_institution_follows_first_author(self):
        html = '<meta name="citation_author" content="First Author"><meta name="citation_author_institution" content="One"><meta name="citation_author" content="Last Author"><meta name="citation_author_institution" content="Two">'
        self.assertEqual(parse_author_html(html, ["First Author", "Last Author"], "url")["first_affiliation"], "One")

    def test_metadata_cache_budget_and_no_model_needed(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = {"author_metadata": {"enabled": True, "cache_path": str(Path(folder)/"cache.json"), "max_requests_per_run": 1}}
            papers = [{"arxiv_id": "2610.00001v1", "authors": ["First Author", "Last Author"]},
                      {"arxiv_id": "2610.00002v1", "authors": ["First Author"]}]
            request = Mock(return_value=HTML.encode())
            stats = enrich_authors(papers, {}, cfg, request)
            self.assertEqual(stats["requests"], 1)
            self.assertEqual(stats["deferred"], 1)
            self.assertEqual(papers[0]["corresponding_author"], "Last Author")
            stats = enrich_authors(papers[:1], {}, cfg, request)
            self.assertEqual(stats["cache_hits"], 1)
            self.assertEqual(request.call_count, 1)

    def test_metadata_failure_retains_atom_affiliation(self):
        with tempfile.TemporaryDirectory() as folder:
            papers = [{"arxiv_id": "2610.00001v1", "authors": [], "first_affiliation": "Atom Institute"}]
            cfg = {"author_metadata": {"enabled": True, "cache_path": str(Path(folder)/"cache.json")}}
            enrich_authors(papers, {}, cfg, Mock(side_effect=OSError()))
            self.assertEqual(papers[0]["first_affiliation"], "Atom Institute")
            self.assertEqual(papers[0]["author_metadata"]["status"], "unavailable")

    def test_daily_english_cache_keeps_chinese(self):
        brief = {"fingerprint": "fp", "headline": "Title", "overview": "Overview", "themes": [
            {"title": "Theme", "summary": "Evidence", "paper_ids": ["id"]}], "caveat": "Abstract only"}
        request = Mock(return_value={k:v for k,v in brief.items() if k != "fingerprint"})
        cfg = {"english_guide": {"enabled": True}}
        result = supplement_brief_english(brief, [{"id":"id", "title":"T"}], cfg, request)
        supplement_brief_english(result, [{"id":"id", "title":"T"}], cfg, request)
        request.assert_called_once()
        self.assertEqual(result["overview"], brief["overview"])


if __name__ == "__main__":
    unittest.main()
