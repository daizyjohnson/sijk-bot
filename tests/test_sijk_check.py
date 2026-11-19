"""Offline smoke tests: no Discord token or Google access required."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sijk_check import CheckStore, SijkCheck, normalize_riot_id, parse_evidence, public_report_embed
from sijk_check_sheets import CheckSheet, CheckSheetError


class TestCheck(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_riot_id(" Subset#Aura ")[1], "subset#aura")
        for bad in ["missingtag", "a#b#c", "a#b\nInjected", "#tag"]:
            with self.assertRaises(ValueError):
                normalize_riot_id(bad)

    def test_evidence(self):
        self.assertEqual(parse_evidence("https://example.com/a\nhttps://example.com/a"),
                         ["https://example.com/a"])
        for bad in ["http://example.com", "https://u:p@example.com", "not a link"]:
            with self.assertRaises(ValueError):
                parse_evidence(bad)

    def test_cases_and_history(self):
        with tempfile.TemporaryDirectory() as folder:
            store = CheckStore(Path(folder) / "test.sqlite3")
            store.configure(1, 100, 101, 102)
            self.assertIsNone(store.settings(1)["feed_channel_id"])
            store.set_feed_channel(1, 103)
            self.assertEqual(store.settings(1)["feed_channel_id"], 103)
            case, result = store.submit(1, 55, "Subset#Aura", "reason", ["https://example.com/1"])
            self.assertEqual(result, "new")
            self.assertEqual(store.submit(1, 55, "subset#aura", "repeat", ["https://example.com/1"])[1], "unchanged")
            self.assertEqual(store.submit(1, 55, "Subset#Aura", "more", ["https://example.com/2"])[1], "supplement")
            self.assertEqual(store.submit(1, 56, "Subset#Aura", "another", ["https://example.com/3"])[1], "new")
            self.assertEqual(store.case(case)[1], 2)
            with store.connect() as db:
                db.execute("UPDATE check_cases SET feed_message_id=321 WHERE id=?", (case,))
                db.commit()
            with store.connect() as db:
                db.execute("UPDATE check_settings SET feed_panel_message_id=456 WHERE guild_id=1")
                db.commit()
            store.set_feed_channel(1, 104)
            self.assertIsNone(store.case(case)[0]["feed_message_id"])
            self.assertIsNone(store.settings(1)["feed_panel_message_id"])
            self.assertEqual(store.settings(1)["review_channel_id"], 101)
            with store.connect() as db:
                db.execute("UPDATE check_cases SET state='approved' WHERE id=?", (case,))
                db.commit()
            store.submit(1, 55, "Subset#Aura", "again", ["https://example.com/4"])
            self.assertEqual(store.case(case)[0]["state"], "approved")

    def test_closed_case_reopens_and_approved_case_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            store = CheckStore(Path(folder) / "test.sqlite3")
            case, _ = store.submit(1, 10, "Player#Test", "first", ["https://example.com/a"])
            with store.connect() as db:
                db.execute(
                    "UPDATE check_cases SET state='rejected', reviewer_comment='Old decision' WHERE id=?",
                    (case,),
                )
                db.execute(
                    "INSERT INTO check_events(case_id,user_id,event,detail,created_at) VALUES(?,?,?,?,?)",
                    (case, 1, "decision:rejected", "Old decision", "2026-11-01T00:00:00"),
                )
                db.commit()
            self.assertEqual(
                store.submit(1, 11, "player#test", "second", ["https://example.com/b"]),
                (case, "reopened"),
            )
            row, count = store.case(case)
            self.assertEqual((row["state"], count), ("pending", 2))
            self.assertIsNone(row["reviewer_comment"])
            with store.connect() as db:
                events = db.execute(
                    "SELECT event FROM check_events WHERE case_id=? ORDER BY id", (case,)
                ).fetchall()
            self.assertIn("auto_reopened", [event["event"] for event in events])
            self.assertIn("decision:rejected", [event["event"] for event in events])
            self.assertEqual(
                store.submit(1, 11, "player#test", "duplicate", ["https://example.com/b"])[1],
                "unchanged",
            )
            with store.connect() as db:
                db.execute(
                    "UPDATE check_cases SET state='approved' WHERE id=?", (case,)
                )
                db.commit()
            self.assertEqual(
                store.submit(1, 12, "player#test", "new", ["https://example.com/c"])[1],
                "approved_update",
            )
            self.assertEqual(store.case(case)[0]["state"], "approved")
            with store.connect() as db:
                event = db.execute(
                    "SELECT event FROM check_events WHERE case_id=? ORDER BY id DESC LIMIT 1", (case,)
                ).fetchone()
            self.assertEqual(event["event"], "approved_update")

    def test_public_report_layout_and_privacy(self):
        case = {"id": 9, "riot_id": "hekilopik#2624", "state": "rejected"}
        embed = public_report_embed(case, 2)
        self.assertEqual(embed.title, "hekilopik#2624")
        self.assertTrue(embed.description.startswith("## ⛔ ЗАЯВКА ОТКЛОНЕНА"))
        self.assertIn("Обращение №9", embed.description)
        self.assertIn("Заявителей: 2", embed.description)
        self.assertNotIn("Заявитель:", embed.description)
        self.assertEqual(embed.footer.text, "SIJK CHECK • Публичный реестр")

    def test_unconfigured_bridge(self):
        with patch.dict("os.environ", {}, clear=True):
            sheet = CheckSheet()
            self.assertFalse(sheet.configured)
            with self.assertRaises(CheckSheetError):
                asyncio.run(sheet.find("subset#aura"))


if __name__ == "__main__":
    unittest.main()
