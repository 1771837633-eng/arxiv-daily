import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fetch_arxiv as fetch


def paper(paper_id="2610.00001v1"):
    return fetch.Paper(
        arxiv_id=paper_id, title="Measured spin correlations", authors=["A. Author"],
        published="2026-10-07T00:00:00Z", updated="2026-10-07T00:00:00Z",
        primary_category="cond-mat.str-el", categories=["cond-mat.str-el"],
        abstract="We measured spin correlations with neutron scattering.",
    )


def atom_feed(ids):
    root = ET.Element(fetch.ATOM_NS + "feed")
    for value in ids:
        entry = ET.SubElement(root, fetch.ATOM_NS + "entry")
        ET.SubElement(entry, fetch.ATOM_NS + "id").text = "https://arxiv.org/abs/" + value
        ET.SubElement(entry, fetch.ATOM_NS + "title").text = "Spin correlations"
        ET.SubElement(entry, fetch.ATOM_NS + "summary").text = "We measured spin correlations."
    return ET.tostring(root)


class FetchSafetyTests(unittest.TestCase):
    def structured_response(self):
        return {
            "study_type_zh": "实验；中子散射", "research_object_zh": "二维磁性体系",
            "core_finding_zh": "观测到随温度改变的自旋关联。",
            "research_question_zh": "关联长度是否受热涨落限制？",
            "evidence_zh": "比较不同温度下的散射谱与控制样品。",
            "method_zh": "中子散射", "novelty_zh": "摘要未明确说明。",
            "limitations_zh": "仅根据摘要，未确认全部拟合假设。",
        }

    def mock_llm_response(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(self.structured_response())}}],
        }).encode()
        return response

    def test_summary_fields_have_separate_roles(self):
        with patch.dict(fetch.os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True):
            with patch.object(fetch, "urlopen", return_value=self.mock_llm_response()):
                result = fetch.summarize_with_llm("Spin study", "We measured neutron spectra.", {})
        self.assertNotIn(result["abstract_summary_zh"], result["main_content_zh"])
        self.assertIn("证据与比较", result["main_content_zh"])
        self.assertEqual(result["summary_schema_version"], 2)

    def test_duplicate_summary_fields_are_rejected(self):
        response = self.structured_response()
        response["evidence_zh"] = response["core_finding_zh"]
        with self.assertRaisesRegex(ValueError, "repeated"):
            fetch._parse_llm_response(json.dumps(response))

    def test_incomplete_summary_is_not_counted_as_ai(self):
        with self.assertRaises(ValueError):
            fetch._parse_llm_response('{"study_type_zh":"理论"}')

    def test_rules_do_not_repeat_conclusion_in_background(self):
        result, _ = fetch.fallback_chinese_summary("Spin correlations", "The origin of the magnetic order remains unclear. We find that cooling increases correlations.")
        self.assertIn("cooling increases", result["abstract_summary_zh"])
        self.assertNotIn("cooling increases", result["main_content_zh"])

    def test_llm_auth_failure_opens_circuit(self):
        config = {"use_openai_summary": True}
        error = HTTPError("https://example.invalid", 401, "unauthorized", {}, None)
        with patch.dict(fetch.os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True):
            with patch.object(fetch, "urlopen", side_effect=error) as request:
                first, _ = fetch.make_chinese_summary("Title", "Abstract", config)
                second, _ = fetch.make_chinese_summary("Another title", "Abstract", config)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(first["summary_mode"], "rule-based")
        self.assertEqual(second["summary_mode"], "rule-based")
        self.assertNotIn("test-key", json.dumps(first))

    def test_llm_transient_error_retries(self):
        error = HTTPError("https://example.invalid", 503, "unavailable", {}, None)
        with patch.dict(fetch.os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True):
            with patch.object(fetch.time, "sleep"):
                with patch.object(fetch, "urlopen", side_effect=[error, self.mock_llm_response()]) as request:
                    result = fetch.summarize_with_llm("Title", "Abstract", {})
        self.assertEqual(request.call_count, 2)
        self.assertEqual(result["summary_mode"], "llm-openai")

    def test_llm_request_budget_is_bounded(self):
        runtime = fetch.SummaryRuntime({"llm": {"max_requests_per_run": 1}})
        runtime.reserve()
        with self.assertRaises(RuntimeError):
            runtime.reserve()

    def test_cache_reuses_only_matching_schema_and_model(self):
        cache = fetch.PaperCache(Path(self.directory.name) / "summary-cache.json")
        config = {"use_openai_summary": False, "llm": {"model": "first-model"}}
        summary, keywords = fetch.fallback_chinese_summary("Title", "Abstract")
        summary["summary_mode"] = "llm-test"
        cache.set("id", summary, keywords, fetch.summary_fingerprint("Title", "Abstract", config))
        with patch.object(fetch, "make_chinese_summary", return_value=(summary, keywords)) as model:
            fetch.summarize_cached("id", "Title", "Abstract", config, cache)
            model.assert_not_called()
            config["llm"]["model"] = "second-model"
            fetch.summarize_cached("id", "Title", "Abstract", config, cache)
        self.assertEqual(model.call_count, 1)

    def test_old_summary_cache_is_invalidated(self):
        cache = fetch.PaperCache(Path(self.directory.name) / "summary-cache.json")
        cache.set("id", {"summary_mode": "llm-test", "abstract_summary_zh": "Old repeated finding"}, [])
        fallback = fetch.fallback_chinese_summary("Title", "Abstract")
        with patch.object(fetch, "make_chinese_summary", return_value=fallback) as model:
            fetch.summarize_cached("id", "Title", "Abstract", {}, cache)
        model.assert_called_once()

    def test_structurally_broken_cache_is_rebuilt(self):
        path = Path(self.directory.name) / "summary-cache.json"
        fingerprint = fetch.summary_fingerprint("Title", "Abstract", {})
        for contents in ([], {"id": None}, {"id": {"summary": None, "fingerprint": fingerprint}}):
            with self.subTest(contents=contents):
                path.write_text(json.dumps(contents), encoding="utf-8")
                cache = fetch.PaperCache(path)
                summary, _ = fetch.summarize_cached("id", "Title", "Abstract", {"use_openai_summary": False}, cache)
                self.assertEqual(summary["summary_mode"], "rule-based")
                cache.save()

    def test_empty_cached_summary_is_not_reused(self):
        cache = fetch.PaperCache(Path(self.directory.name) / "summary-cache.json")
        summary, keywords = fetch.fallback_chinese_summary("Title", "Abstract")
        summary.update(summary_mode="llm-test", main_content_zh="")
        config = {"use_openai_summary": False}
        cache.set("id", summary, keywords, fetch.summary_fingerprint("Title", "Abstract", config))
        result, _ = fetch.summarize_cached("id", "Title", "Abstract", config, cache)
        self.assertTrue(result["main_content_zh"])
        self.assertEqual(result["summary_mode"], "rule-based")

    def test_retry_after_http_date_and_long_delay_are_bounded(self):
        self.assertEqual(fetch.retry_delay({"Retry-After": "86400"}, 0), 60)
        self.assertGreaterEqual(fetch.retry_delay({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}, 0), 3.1)

    def test_llm_empty_response_is_bounded_and_falls_back(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"choices":[{"message":{"content":""}}]}'
        config = {"use_openai_summary": True}
        with patch.dict(fetch.os.environ, {"OPENAI_API_KEY": "test"}, clear=True):
            with patch.object(fetch.time, "sleep"):
                with patch.object(fetch, "urlopen", return_value=response) as request:
                    result, _ = fetch.make_chinese_summary("Title", "Abstract", config)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(result["summary_mode"], "rule-based")

    def test_metadata_links_are_https(self):
        entry = ET.fromstring(atom_feed(["2610.00001v1"])).find(fetch.ATOM_NS + "entry")
        ET.SubElement(entry, fetch.ATOM_NS + "link", {"rel": "alternate", "href": "http://arxiv.org/abs/2610.00001v1"})
        result = fetch.parse_entry(entry, self.config)
        self.assertTrue(result.abs_url.startswith("https://"))
        self.assertTrue(result.pdf_url.startswith("https://"))

    def test_openai_key_is_not_sent_to_deepseek(self):
        config = {
            "use_openai_summary": True,
            "llm": {"provider": "deepseek", "api_key_env": "DEEPSEEK_API_KEY"},
        }
        with patch.dict(fetch.os.environ, {"OPENAI_API_KEY": "test-openai-key"}, clear=True):
            with patch.object(fetch, "urlopen") as request:
                with self.assertRaisesRegex(RuntimeError, "DEEPSEEK_API_KEY"):
                    fetch.summarize_with_llm("Spin correlations", "Measured neutron spectra.", config)
                summary, _ = fetch.make_chinese_summary("Spin correlations", "Measured neutron spectra.", config)
        self.assertTrue(summary["summary_mode"].startswith("rule"))
        request.assert_not_called()

    def test_prb_title_preserves_inline_formula_and_tail(self):
        item = ET.fromstring(
            '<item xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:m="http://www.w3.org/1998/Math/MathML">'
            '<dc:title>Magnetism in <m:math><m:msub><m:mi>Fe</m:mi>'
            '<m:mn>4.5</m:mn></m:msub><m:mi>GeTe</m:mi>'
            '<m:mn>2</m:mn></m:math> under pressure</dc:title></item>'
        )
        self.assertEqual(
            fetch.prb_text_of(item, fetch.DC_NS + "title"),
            "Magnetism in Fe4.5GeTe2 under pressure",
        )

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / "latest.json"
        self.config = {
            "use_openai_summary": False, "prb": {"enabled": True},
            "cache_path": str(Path(self.directory.name) / ".paper_cache.json"),
        }

    def test_empty_listing_is_an_error(self):
        with patch.object(fetch, "http_get", return_value=b"<html>Unavailable</html>"):
            with self.assertRaisesRegex(RuntimeError, "no paper IDs"):
                fetch.fetch_recent_listing_ids({})

    def test_missing_api_entries_are_retried_then_rejected(self):
        with patch.object(fetch, "http_get", return_value=atom_feed(["2610.00001v1"])) as request:
            with patch.object(fetch.time, "sleep"):
                with self.assertRaisesRegex(RuntimeError, "omitted 1"):
                    fetch.fetch_feed_by_ids(["2610.00001", "2610.00002"])
        self.assertEqual(request.call_count, 3)

    def test_malformed_feed_can_recover(self):
        with patch.object(fetch, "http_get", side_effect=[b"<html>", atom_feed(["2610.00001v1"])]) as request:
            with patch.object(fetch.time, "sleep"):
                result = fetch.fetch_feed_by_ids(["2610.00001"])
        self.assertEqual(len(result.findall(fetch.ATOM_NS + "entry")), 1)
        self.assertEqual(request.call_count, 2)

    def test_network_503_is_retried(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"ok"
        error = HTTPError("https://example.org/feed", 503, "unavailable", {}, None)
        with patch.object(fetch, "urlopen", side_effect=[error, response]) as request:
            with patch.object(fetch.time, "sleep"):
                self.assertEqual(fetch.http_get("https://example.org/feed"), b"ok")
        self.assertEqual(request.call_count, 2)

    def test_arxiv_requests_are_spaced(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"ok"
        with patch.object(fetch, "_LAST_ARXIV_REQUEST", 100.0):
            with patch.object(fetch.time, "monotonic", return_value=101.0):
                with patch.object(fetch.time, "sleep") as sleep:
                    with patch.object(fetch, "urlopen", return_value=response):
                        fetch.http_get("https://export.arxiv.org/api/query")
        self.assertAlmostEqual(sleep.call_args.args[0], 2.1)

    def test_failed_batch_is_not_silently_skipped(self):
        with patch.object(fetch, "fetch_recent_listing_ids", return_value=(["2610.00001"], [])):
            with patch.object(fetch, "fetch_feed_by_ids", side_effect=RuntimeError("network failure")):
                with self.assertRaisesRegex(RuntimeError, "previous data will be kept"):
                    fetch.fetch_papers_by_listing(self.config)

    def test_failed_arxiv_run_keeps_previous_file_unchanged(self):
        previous_bytes = b'{"papers": [{"source": "arxiv"}], "generated_at": "previous"}'
        self.output.write_bytes(previous_bytes)
        config_path = Path(self.directory.name) / "config.json"
        config_path.write_text(json.dumps(self.config), encoding="utf-8")
        with patch.object(sys, "argv", ["fetch_arxiv.py", "--config", str(config_path), "--output", str(self.output)]):
            with patch.object(fetch, "fetch_papers_by_listing", side_effect=RuntimeError("arXiv unavailable")):
                self.assertEqual(fetch.main(), 1)
        self.assertEqual(self.output.read_bytes(), previous_bytes)

    def test_prb_failure_does_not_block_arxiv_and_preserves_old_prb_date(self):
        self.output.write_text(json.dumps({
            "generated_at": "2026-10-06T00:00:00Z",
            "source_status": {"prb": {"updated_at": "2026-10-05T00:00:00Z"}},
            "papers": [{"arxiv_id": "PRB:10.1103/old", "source": "prb"}],
            "listing_sections": [{"title": "PRB yesterday", "ids": ["PRB:10.1103/old"]}],
        }), encoding="utf-8")
        with patch.object(fetch, "fetch_papers_by_listing", return_value=([paper()], [])):
            with patch.object(fetch, "fetch_prb_papers", side_effect=RuntimeError("PRB unavailable")):
                payload = fetch.build_payload(self.config, self.output)
        self.assertEqual(payload["source_status"]["arxiv"]["status"], "ok")
        self.assertEqual(payload["source_status"]["prb"]["status"], "stale")
        self.assertEqual(payload["source_status"]["prb"]["updated_at"], "2026-10-05T00:00:00Z")
        fetch.write_payload(self.output, payload)
        self.assertEqual(json.loads(self.output.read_text(encoding="utf-8"))["count"], 2)

    def test_prb_failure_on_first_run_still_publishes_arxiv(self):
        with patch.object(fetch, "fetch_papers_by_listing", return_value=([paper()], [])):
            with patch.object(fetch, "fetch_prb_papers", side_effect=RuntimeError("PRB unavailable")):
                payload = fetch.build_payload(self.config, self.output)
        self.assertEqual(payload["source_status"]["prb"]["status"], "unavailable")
        self.assertEqual(payload["count"], 1)

    def test_arxiv_only_never_calls_prb(self):
        self.config["prb"]["enabled"] = False
        with patch.object(fetch, "fetch_papers_by_listing", return_value=([paper()], [])):
            with patch.object(fetch, "fetch_prb_papers") as prb:
                payload = fetch.build_payload(self.config, self.output)
        prb.assert_not_called()
        self.assertEqual(payload["sources"], ["arxiv"])

    def test_prb_excerpt_strips_authors_for_any_sentence_start(self):
        item = ET.Element(fetch.RSS_NS + "item")
        ET.SubElement(item, fetch.DC_NS + "creator").text = "A. Author and B. Author"
        ET.SubElement(item, fetch.RSS_NS + "description").text = (
            "Author(s): A. Author and B. Author<br/><p>Lithium phases show electride behavior...</p>"
            "<br/>[Phys. Rev. B 113, 123456] Published Wed Oct 7, 2026"
        )
        self.assertEqual(fetch.prb_abstract_from_item(item), "Lithium phases show electride behavior...")

    def test_prb_missing_excerpt_is_not_summarized(self):
        item = ET.Element(fetch.RSS_NS + "item")
        ET.SubElement(item, fetch.DC_NS + "title").text = "Erratum"
        ET.SubElement(item, fetch.PRISM_NS + "doi").text = "10.1103/erratum"
        with patch.object(fetch, "make_chinese_summary") as summary:
            with self.assertRaises(ValueError):
                fetch.parse_prb_item(item, self.config)
        summary.assert_not_called()

    def test_atomic_writer_refuses_empty_arxiv_data(self):
        self.output.write_bytes(b"previous good data")
        with self.assertRaises(RuntimeError):
            fetch.write_payload(self.output, {"papers": [{"source": "prb"}]})
        self.assertEqual(self.output.read_bytes(), b"previous good data")


if __name__ == "__main__":
    unittest.main()
