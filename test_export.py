#!/usr/bin/env python3
"""
Tests for export_messages.py

Run with:  python3 test_export.py
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import export_messages as em


# ── attributedBody decoding ────────────────────────────────────────────────────

def make_attributed_body(text: str, marker_offset: int = 70) -> bytes:
    """Build a minimal NSArchiver streamtyped blob containing the given text."""
    encoded = text.encode("utf-8")
    length = len(encoded)

    if length < 0x80:
        length_bytes = bytes([length])
    elif length <= 0xFFFF:
        length_bytes = bytes([0x81]) + length.to_bytes(2, "little")
    else:
        length_bytes = bytes([0x82]) + length.to_bytes(4, "little")

    payload = b"\x84\x01+" + length_bytes + encoded
    # Pad with zeros before marker so it lands at marker_offset
    padding = bytes(marker_offset)
    return padding + payload


class TestExtractAttributedBody(unittest.TestCase):
    def test_short_text(self):
        blob = make_attributed_body("Hello, world!")
        self.assertEqual(em.extract_attributed_body(blob), "Hello, world!")

    def test_two_byte_length(self):
        # 200-char string requires 0x81 prefix
        text = "A" * 200
        blob = make_attributed_body(text)
        self.assertEqual(em.extract_attributed_body(blob), text)

    def test_unicode(self):
        text = "Yea\u2026 whoever this is"
        blob = make_attributed_body(text)
        self.assertEqual(em.extract_attributed_body(blob), text)

    def test_empty_blob(self):
        self.assertIsNone(em.extract_attributed_body(b""))
        self.assertIsNone(em.extract_attributed_body(None))

    def test_no_marker(self):
        self.assertIsNone(em.extract_attributed_body(b"\x00" * 50))

    def test_whitespace_only_returns_none(self):
        blob = make_attributed_body("   ")
        self.assertIsNone(em.extract_attributed_body(blob))

    def test_marker_at_different_offsets(self):
        for offset in (0, 70, 118):
            blob = make_attributed_body("Test text", marker_offset=offset)
            self.assertEqual(em.extract_attributed_body(blob), "Test text",
                             f"Failed at offset {offset}")


# ── Timestamp conversion ───────────────────────────────────────────────────────

class TestTimestamp(unittest.TestCase):
    def test_known_date(self):
        # Apple ts 0 → 2001-01-01 00:00:00 UTC
        dt = em.apple_ts_to_datetime(0)
        self.assertIsNone(dt)  # 0 is treated as missing

    def test_nonzero(self):
        # 1_000_000_000 ns = 1 second after 2001-01-01 = 2001-01-01T00:00:01
        dt = em.apple_ts_to_datetime(1_000_000_000)
        self.assertEqual(dt.year, 2001)
        self.assertEqual(dt.second, 1)

    def test_none_input(self):
        self.assertIsNone(em.apple_ts_to_datetime(None))


# ── patch_missing_text ─────────────────────────────────────────────────────────

class TestPatchMissingText(unittest.TestCase):
    def _make_db(self, rows):
        """Create an in-memory SQLite DB with a message table populated by rows."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, attributedBody BLOB)"
        )
        conn.executemany(
            "INSERT INTO message (ROWID, text, attributedBody) VALUES (?, ?, ?)",
            rows,
        )
        conn.commit()
        return conn

    def test_patches_empty_text(self):
        text = "They keep getting better."
        blob = make_attributed_body(text)
        conn = self._make_db([(1, None, blob)])
        messages = [{"message_id": 1, "text": "", "is_reaction": False}]
        count = em.patch_missing_text(conn, messages)
        self.assertEqual(count, 1)
        self.assertEqual(messages[0]["text"], text)

    def test_does_not_overwrite_existing_text(self):
        conn = self._make_db([(1, "DB text", None)])
        messages = [{"message_id": 1, "text": "Already has text", "is_reaction": False}]
        count = em.patch_missing_text(conn, messages)
        self.assertEqual(count, 0)
        self.assertEqual(messages[0]["text"], "Already has text")

    def test_skips_reactions(self):
        blob = make_attributed_body("reaction text")
        conn = self._make_db([(1, None, blob)])
        messages = [{"message_id": 1, "text": "", "is_reaction": True}]
        count = em.patch_missing_text(conn, messages)
        self.assertEqual(count, 0)
        self.assertEqual(messages[0]["text"], "")

    def test_no_empty_messages(self):
        conn = self._make_db([])
        messages = [{"message_id": 1, "text": "Fine", "is_reaction": False}]
        count = em.patch_missing_text(conn, messages)
        self.assertEqual(count, 0)

    def test_uses_db_text_when_no_attributed_body(self):
        conn = self._make_db([(1, "Plain text from DB", None)])
        messages = [{"message_id": 1, "text": "", "is_reaction": False}]
        count = em.patch_missing_text(conn, messages)
        self.assertEqual(count, 1)
        self.assertEqual(messages[0]["text"], "Plain text from DB")


# ── fetch_messages incremental state ──────────────────────────────────────────

class TestFetchMessagesIncremental(unittest.TestCase):
    def _make_full_db(self):
        """Create a minimal chat.db schema with a few messages."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT, display_name TEXT);
            CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
            CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                guid TEXT, date INTEGER, is_from_me INTEGER,
                handle_id INTEGER, text TEXT, attributedBody BLOB,
                associated_message_type INTEGER DEFAULT 0,
                associated_message_guid TEXT, subject TEXT, service TEXT
            );
            CREATE TABLE attachment (
                ROWID INTEGER PRIMARY KEY, filename TEXT, mime_type TEXT, transfer_name TEXT
            );
            CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);

            INSERT INTO handle VALUES (1, '+15550001111');
            INSERT INTO chat VALUES (1, '+15550001111', NULL);
            INSERT INTO chat_handle_join VALUES (1, 1);

            INSERT INTO message (ROWID, guid, date, is_from_me, handle_id, text)
                VALUES (10, 'guid-10', 1000000000, 1, NULL, 'Hello');
            INSERT INTO message (ROWID, guid, date, is_from_me, handle_id, text)
                VALUES (20, 'guid-20', 2000000000, 0, 1, 'Hi there');
            INSERT INTO message (ROWID, guid, date, is_from_me, handle_id, text)
                VALUES (30, 'guid-30', 3000000000, 1, NULL, 'How are you?');
            INSERT INTO chat_message_join VALUES (1, 10);
            INSERT INTO chat_message_join VALUES (1, 20);
            INSERT INTO chat_message_join VALUES (1, 30);
        """)
        return conn

    def test_full_fetch(self):
        conn = self._make_full_db()
        msgs = em.fetch_messages(conn, "+15550001111")
        self.assertEqual(len(msgs), 3)

    def test_incremental_fetch(self):
        conn = self._make_full_db()
        msgs = em.fetch_messages(conn, "+15550001111", since_id=20)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["message_id"], 30)

    def test_incremental_fetch_no_new(self):
        conn = self._make_full_db()
        msgs = em.fetch_messages(conn, "+15550001111", since_id=30)
        self.assertEqual(len(msgs), 0)

    def test_sender_assignment(self):
        conn = self._make_full_db()
        msgs = em.fetch_messages(conn, "+15550001111")
        by_id = {m["message_id"]: m for m in msgs}
        self.assertEqual(by_id[10]["sender"], "me")
        self.assertEqual(by_id[20]["sender"], "+15550001111")

    def test_fetch_all_message_ids(self):
        conn = self._make_full_db()
        ids = em.fetch_all_message_ids(conn, "+15550001111")
        self.assertEqual(ids, {10, 20, 30})

    def test_fetch_messages_by_ids(self):
        conn = self._make_full_db()
        msgs = em.fetch_messages_by_ids(conn, "+15550001111", {10, 30})
        self.assertEqual({m["message_id"] for m in msgs}, {10, 30})

    def test_fetch_messages_by_ids_empty(self):
        conn = self._make_full_db()
        msgs = em.fetch_messages_by_ids(conn, "+15550001111", set())
        self.assertEqual(msgs, [])

    def test_gap_fill_detects_missing(self):
        """gap_ids = db_ids - json_ids should catch messages missed in prior runs."""
        conn = self._make_full_db()
        db_ids  = em.fetch_all_message_ids(conn, "+15550001111")
        # Simulate JSON that is missing message 20 (a historical gap)
        json_ids = {10, 30}
        gap_ids  = db_ids - json_ids
        self.assertEqual(gap_ids, {20})
        gap_msgs = em.fetch_messages_by_ids(conn, "+15550001111", gap_ids)
        self.assertEqual(len(gap_msgs), 1)
        self.assertEqual(gap_msgs[0]["message_id"], 20)


# ── JSON always written (regression test for incremental state bug) ────────────

class TestJsonAlwaysWritten(unittest.TestCase):
    def test_messages_json_written_for_html_format(self):
        """messages.json must be written even when --format html is requested,
        so last_message_id and the cached message list stay in sync."""
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            messages = [
                {"message_id": 1, "timestamp_utc": "2024-01-01T00:00:00+00:00",
                 "timestamp_local": "2024-01-01 00:00:00", "is_from_me": True,
                 "sender": "me", "text": "hi", "subject": "", "service": "iMessage",
                 "guid": "g1", "is_reaction": False, "reaction_type": 0,
                 "reaction_target": "", "attachments": [], "link_previews": None}
            ]
            em.write_json(messages, out / "messages.json")
            self.assertTrue((out / "messages.json").exists())
            loaded = json.loads((out / "messages.json").read_text())
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["text"], "hi")


# ── Suno lyrics extraction ─────────────────────────────────────────────────────

class TestSunoLyrics(unittest.TestCase):
    def _make_html(self, idx: str, lyrics: str, split: bool = False) -> str:
        """Build a minimal Suno page HTML fragment.

        The T-chunk hex length is the byte length of the *escaped* content as
        it appears inside the JS string literal (matching real Suno behavior).
        """
        escaped = lyrics.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
        hex_len = format(len(escaped.encode()), "x")
        ref = f'\\"prompt\\":\\"${idx}\\"'
        if split:
            # First push has only the chunk header; second has the full content.
            chunk = (
                f'self.__next_f.push([1,"{idx}:T{hex_len},"])</script>'
                f'<script>self.__next_f.push([1,"{escaped}"])'
            )
        else:
            chunk = f'self.__next_f.push([1,"{idx}:T{hex_len},{escaped}"])'
        return f'<html><head></head><body>{ref} {chunk}</body></html>'

    def _extract_lyrics(self, html: str):
        """Mirror the lyrics extraction logic from fetch_og."""
        import re
        ref = re.search(r'(?:\\"|")prompt(?:\\"|")\s*:\s*(?:\\"|")\$(\d+)(?:\\"|")', html)
        if not ref:
            return None
        idx = ref.group(1)
        chunk_m = re.search(rf'{re.escape(idx)}:T([0-9a-f]+),', html)
        if not chunk_m:
            return None
        byte_len = int(chunk_m.group(1), 16)
        bridge = '"])</script><script>self.__next_f.push([1,"'
        parts: list[str] = []
        remaining = byte_len
        pos = chunk_m.end()
        while remaining > 0 and pos < len(html):
            next_b = html.find(bridge, pos)
            segment = html[pos: next_b if next_b != -1 else pos + remaining]
            take = min(len(segment), remaining)
            parts.append(segment[:take])
            remaining -= take
            if next_b == -1 or take < len(segment):
                break
            pos = next_b + len(bridge)
        raw = "".join(parts)
        raw = raw.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\').strip()
        return raw or None

    def test_lyrics_same_push(self):
        lyrics = "Verse 1\nSmoke curls slow\nTruth ain't loud"
        html = self._make_html("42", lyrics, split=False)
        result = self._extract_lyrics(html)
        self.assertEqual(result, lyrics)

    def test_lyrics_split_push(self):
        lyrics = "Verse 1\nLine one here\nLine two here\nLine three here"
        html = self._make_html("42", lyrics, split=True)
        result = self._extract_lyrics(html)
        self.assertEqual(result, lyrics)

    def test_no_prompt_ref(self):
        html = '<html><body>no prompt here</body></html>'
        result = self._extract_lyrics(html)
        self.assertIsNone(result)

    def test_empty_prompt(self):
        # Upsample remasters have empty prompt — should return None
        html = r'<html><body>\"prompt\":\"\" other stuff</body></html>'
        result = self._extract_lyrics(html)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
