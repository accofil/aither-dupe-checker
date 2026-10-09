from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from report import ATTENTION, format_table, report_rows, write_tsv

STATE = {
    "releases": {
        "a": {
            "name": "Moana.2026.1080p",
            "status": "DUPLICATE",
            "reason": "MATCHED_TECHNICAL_SIGNATURE",
            "tmdb_id": 1,
            "matched_evidence": {
                "attributes": {
                    "name": "Moana 2026 1080p REMUX-GRP",
                    "type": "Remux",
                    "size": 2 * 1024**3,
                    "details_link": "https://aither.cc/torrents/1?x=y",
                }
            },
        },
        "b": {"name": "El.Circulo.S01E01", "status": "UNRESOLVED", "reason": "NO_CATALOG_IDENTITY"},
        "c": {"name": "Other", "status": "NOT_FOUND", "reason": "COMPLETE_QUERY_NO_MATCH"},
    }
}


class ReportTests(unittest.TestCase):
    def test_rows_are_sorted_by_attention_and_filterable(self):
        rows = report_rows(STATE)
        self.assertEqual([r["status"] for r in rows], ["NOT_FOUND", "UNRESOLVED", "DUPLICATE"])
        self.assertEqual(len(report_rows(STATE, needle="moana")), 1)
        self.assertEqual(len(report_rows(STATE, only_keys=["b"])), 1)
        self.assertIn("UNRESOLVED", ATTENTION)

    def test_verbose_table_shows_match_without_query_string(self):
        text = format_table(report_rows(STATE, needle="Moana"), verbose=True)
        self.assertIn("Moana 2026 1080p REMUX-GRP [Remux 2.00 GiB]", text)
        self.assertIn("https://aither.cc/torrents/1", text)
        self.assertNotIn("x=y", text)

    def test_tsv_is_written_atomically_with_private_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.tsv"
            write_tsv(report_rows(STATE), path)
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 4)
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")


if __name__ == "__main__":
    unittest.main()
