"""Tests for the alert contract: optional alerts, blocks, budgets, advisories.

Run:  <hermes venv>/bin/python tests/test_alert.py -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_SANDBOX = tempfile.mkdtemp(prefix="bark-alert-test-")
os.environ["HOME"] = _SANDBOX
os.environ["HERMES_HOME"] = str(Path(_SANDBOX) / "hermes")
for _name in [key for key in os.environ if key.startswith("BARK_")]:
    os.environ.pop(_name, None)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import alert as A  # noqa: E402


class WeightTests(unittest.TestCase):
    def test_full_width_counts_double(self):
        self.assertEqual(A.display_weight("中文abc"), 7)   # full-width = 2, latin = 1
        self.assertEqual(A.display_weight(""), 0)

    def test_collapsed_budget_is_four_lines(self):
        self.assertEqual(A.COLLAPSED_WEIGHT, 140)
        self.assertEqual(A.display_weight("一" * 70), 140)


class ClipTests(unittest.TestCase):
    def test_short_text_untouched(self):
        self.assertEqual(A.clip("Short.", 40), ("Short.", False))

    def test_prefers_sentence_boundary(self):
        text, clipped = A.clip("First one. The second sentence is much longer.", 20)
        self.assertTrue(clipped)
        self.assertEqual(text, "First one.")

    def test_hard_cut_is_marked(self):
        text, clipped = A.clip("a long run with no punctuation " * 10, 30)
        self.assertTrue(clipped)
        self.assertLessEqual(A.display_weight(text), 30)
        self.assertTrue(text.endswith("…"))


class BlockTests(unittest.TestCase):
    def test_chinese_block(self):
        got = A.parse_block("标题：Nightly job\n摘要：All checks passed.\n\n## Report\nbody...")
        self.assertIsNotNone(got)
        self.assertEqual(got.title, "Nightly job")
        self.assertEqual(got.body, "All checks passed.")
        self.assertEqual(got.source, "block")

    def test_english_block_and_subtitle(self):
        self.assertIsNone(A.parse_block("title: DDNS\ntext: IPv6 unchanged."))
        got = A.parse_block("title: DDNS\nbody: IPv6 unchanged.\nsubtitle: 06:00")
        self.assertEqual((got.title, got.body, got.extras["subtitle"]),
                         ("DDNS", "IPv6 unchanged.", "06:00"))

    def test_multi_line_body_until_blank(self):
        got = A.parse_block("标题：Cleanup\n摘要：Cleared 3 orphaned processes.\n290 GB free.\n\nDetails...")
        self.assertEqual(got.body, "Cleared 3 orphaned processes.\n290 GB free.")

    def test_label_deep_inside_text_is_not_a_block(self):
        text = "\n".join(["line"] * 20 + ["标题：not a block", "摘要：not a block"])
        self.assertIsNone(A.parse_block(text))

    def test_missing_body_label_is_not_a_block(self):
        self.assertIsNone(A.parse_block("标题：title only"))


class ValidateTests(unittest.TestCase):
    def test_empty_body_rejected_unless_allowed(self):
        with self.assertRaises(A.AlertError):
            A.validate(A.Alert(title="t", body=""))
        A.validate(A.Alert(title="t", body=""), allow_empty=True)

    def test_long_body_is_allowed(self):
        body = "第一句是结论。" + "后面还有很长的说明，" * 40
        got = A.validate(A.Alert(title="t", body=body))
        self.assertEqual(got.body, body.strip())
        self.assertFalse(got.clipped)

    def test_payload_guard(self):
        with self.assertRaises(A.AlertError) as ctx:
            A.validate(A.Alert(title="t", body="a" * (A.HARD_BODY_BYTES + 1)))
        self.assertIn("payload guard", str(ctx.exception))

    def test_title_clipped_not_rejected(self):
        got = A.validate(A.Alert(title="标" * 40, body="ok"))
        self.assertTrue(got.clipped)
        self.assertLessEqual(A.display_weight(got.title), A.DEFAULT_MAX_TITLE_WEIGHT)


class AdvisoryTests(unittest.TestCase):
    def test_long_first_sentence_is_flagged(self):
        body = "this sentence is far too long " * 8 + "."
        got = A.validate(A.Alert(title="t", body=body))
        self.assertTrue(any("first sentence" in note for note in got.warnings))

    def test_markdown_markers_are_flagged(self):
        got = A.validate(A.Alert(title="t", body="**Verdict**: normal."))
        self.assertTrue(any("markdown" in note for note in got.warnings))

    def test_punctuated_title_is_flagged(self):
        got = A.validate(A.Alert(title="Nightly done.", body="Normal."))
        self.assertTrue(any("punctuation" in note for note in got.warnings))

    def test_clean_alert_has_no_warnings(self):
        got = A.validate(A.Alert(title="Nightly job · 1 item to review",
                                 body="All checks passed; one item waits for your decision."))
        self.assertEqual(got.warnings, ())


class EnvSettingsTests(unittest.TestCase):
    """An env-only install must not be pushed to the public server by a schema default."""

    def test_env_server_beats_the_schema_default(self):
        import settings as S

        os.environ["BARK_SERVER"] = "http://127.0.0.1:18080/prefix"
        os.environ["BARK_TOKEN"] = "k" * 20
        try:
            got = S.load_settings({})
            self.assertEqual(got.server, "http://127.0.0.1:18080/prefix")
            self.assertIn("default", got.devices)
            self.assertEqual(got.values["missing_alert"], "passthrough")
        finally:
            os.environ.pop("BARK_SERVER", None)
            os.environ.pop("BARK_TOKEN", None)


class MappingTests(unittest.TestCase):
    def test_stale_alert_file_refused(self):
        path = Path(_SANDBOX) / "stale.json"
        path.write_text(json.dumps({"title": "t", "body": "old conclusion.", "created_at": 1}), encoding="utf-8")
        with self.assertRaises(A.AlertError) as ctx:
            A.load_file(path)
        self.assertIn("old", str(ctx.exception))
        self.assertEqual(A.load_file(path, max_age_s=0).body, "old conclusion.")

    def test_run_id_dedupe_key(self):
        one = A.from_mapping({"title": "t", "body": "ok", "run_id": "run-2026-11-26"}, source="cli")
        two = A.from_mapping({"title": "t", "body": "ok", "run_id": "run-2026-11-26"}, source="cli")
        other = A.from_mapping({"title": "t", "body": "ok", "run_id": "run-2026-11-27"}, source="cli")
        self.assertEqual(A.dedupe_key(one), A.dedupe_key(two))
        self.assertNotEqual(A.dedupe_key(one), A.dedupe_key(other))
        self.assertEqual(A.dedupe_key(A.from_mapping({"title": "t", "body": "ok"})), "")

    def test_from_mapping_requires_body(self):
        with self.assertRaises(A.AlertError):
            A.from_mapping({"title": "t"}, source="tool")

    def test_from_mapping_carries_extras(self):
        got = A.from_mapping({"title": "t", "body": "Nominal.", "level": "passive", "url": "https://x"},
                             source="tool")
        self.assertEqual(got.extras["level"], "passive")
        self.assertEqual(got.extras["url"], "https://x")

    def test_alert_file_roundtrip(self):
        path = Path(_SANDBOX) / "alert.json"
        path.write_text(json.dumps({"title": "Daily sync", "body": "No changes.", "level": "passive"}),
                        encoding="utf-8")
        got = A.load_file(path)
        self.assertEqual((got.title, got.body), ("Daily sync", "No changes."))
        self.assertEqual(got.source, "cli")

    def test_alert_file_rejects_non_json(self):
        path = Path(_SANDBOX) / "bad.json"
        path.write_text("not json", encoding="utf-8")
        with self.assertRaises(A.AlertError):
            A.load_file(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
