#!/usr/bin/env python3
"""
Setup check for a new (or existing) machine.

Runs a series of checks against the real environment — Python version, Full
Disk Access, the Messages database schema, your contact, attachments, HEIC
conversion, optional tools, network, and an end-to-end export into a temporary
folder — and prints PASS / WARN / FAIL for each.

Nothing is written outside a temp directory; your real export folder is only read.

Run with:
    python3 check_setup.py                  # contact read from sync_messages.command
    python3 check_setup.py "+14155550100"   # or pass one explicitly
    python3 check_setup.py --offline        # skip the network check
"""
import argparse
import json
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXPORT_BASE = Path.home() / "Desktop" / "messages_export"

results: list[tuple[str, str]] = []   # (status, name)


def report(status: str, name: str, detail: str = ""):
    icon = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌", "SKIP": "➖"}[status]
    print(f"{icon} {status:<4}  {name}")
    if detail:
        for line in detail.splitlines():
            print(f"         {line}")
    results.append((status, name))


def contact_from_launcher() -> str | None:
    launcher = HERE / "sync_messages.command"
    if not launcher.exists():
        return None
    m = re.search(r'export_messages\.py\s+"([^"]+)"', launcher.read_text())
    return m.group(1) if m else None


# ── Checks ─────────────────────────────────────────────────────────────────────

def check_platform():
    if sys.platform == "darwin":
        report("PASS", "macOS", f"macOS {platform.mac_ver()[0]}")
    else:
        report("FAIL", "macOS", f"Running on {sys.platform}; this tool only works on macOS.")


def check_python():
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if v >= (3, 10):
        report("PASS", "Python 3.10+", f"Python {ver} at {sys.executable}")
    else:
        report("FAIL", "Python 3.10+",
               f"Found Python {ver}. Install a newer one: brew install python")


def check_project_files():
    required = ["export_messages.py", "analyze_messages.py", "test_export.py"]
    missing = [f for f in required if not (HERE / f).exists()]
    if missing:
        report("FAIL", "Project files", "Missing: " + ", ".join(missing))
    else:
        report("PASS", "Project files")

    # Launchers are gitignored, so they must be copied over by hand.
    for name in ["sync_messages.command", "analyze_messages.command"]:
        p = HERE / name
        if not p.exists():
            report("WARN", f"Launcher {name}",
                   "Not found. It isn't in git — copy it from the old machine.")
            continue
        problems = []
        if not (p.stat().st_mode & 0o111):
            problems.append(f"not executable — run: chmod +x {name}")
        xattr = subprocess.run(["xattr", "-p", "com.apple.quarantine", str(p)],
                               capture_output=True)
        if xattr.returncode == 0:
            problems.append(f"quarantined — run: xattr -d com.apple.quarantine {name}")
        if problems:
            report("WARN", f"Launcher {name}", "\n".join(problems))
        else:
            report("PASS", f"Launcher {name}")


def check_unit_tests():
    r = subprocess.run([sys.executable, str(HERE / "test_export.py")],
                       capture_output=True, text=True, cwd=HERE)
    summary = [l for l in r.stderr.splitlines() if l.startswith(("Ran ", "OK", "FAILED"))]
    if r.returncode == 0:
        report("PASS", "Unit tests (test_export.py)", " ".join(summary))
    else:
        report("FAIL", "Unit tests (test_export.py)", r.stderr[-1500:])


def check_database(em) -> sqlite3.Connection | None:
    if not em.DB_PATH.exists():
        report("FAIL", "Messages database",
               f"{em.DB_PATH} not found. Open Messages, sign in, and let it sync.")
        return None
    try:
        conn = sqlite3.connect(f"file:{em.DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        n = conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]
    except sqlite3.OperationalError as e:
        report("FAIL", "Full Disk Access / database readable",
               f"{e}\nGrant Full Disk Access to your terminal app:\n"
               "  System Settings → Privacy & Security → Full Disk Access\n"
               "then quit and reopen the terminal.")
        return None
    report("PASS", "Full Disk Access / database readable", f"{n:,} messages in chat.db")
    return conn


def check_schema(conn: sqlite3.Connection):
    needed = {
        "message": ["ROWID", "guid", "date", "is_from_me", "handle_id", "text",
                    "attributedBody", "associated_message_type",
                    "associated_message_guid", "subject", "service"],
        "chat": ["ROWID", "chat_identifier", "display_name"],
        "handle": ["ROWID", "id"],
        "attachment": ["ROWID", "filename", "mime_type", "transfer_name"],
        "chat_message_join": ["chat_id", "message_id"],
        "chat_handle_join": ["chat_id", "handle_id"],
        "message_attachment_join": ["message_id", "attachment_id"],
    }
    problems = []
    for table, cols in needed.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not have:
            problems.append(f"table '{table}' missing")
            continue
        have.add("ROWID")
        missing = [c for c in cols if c not in have]
        if missing:
            problems.append(f"{table}: missing {', '.join(missing)}")
    if problems:
        report("FAIL", "Database schema", "\n".join(problems))
    else:
        report("PASS", "Database schema")

    msg_cols = {r[1] for r in conn.execute("PRAGMA table_info(message)")}
    if "thread_originator_guid" in msg_cols:
        report("PASS", "Reply threading column")
    else:
        report("WARN", "Reply threading column",
               "thread_originator_guid not present; replies won't show quoted previews.")


def check_contact(em, conn, contact: str | None) -> list[dict] | None:
    if not contact:
        report("SKIP", "Contact",
               "No contact given and none found in sync_messages.command.\n"
               'Pass one: python3 check_setup.py "+14155550100"')
        return None
    ids = em.fetch_all_message_ids(conn, contact)
    if not ids:
        # The exporter finds messages through the chat tables. If the handle still
        # has messages that aren't linked to any chat, the conversation was most
        # likely deleted in Messages — say so instead of "not found".
        orphaned, last = conn.execute("""
            SELECT COUNT(*), MAX(m.date) FROM message m
            JOIN handle h ON m.handle_id = h.ROWID
            LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            WHERE h.id = ? AND cmj.chat_id IS NULL
        """, (contact,)).fetchone()
        if orphaned:
            last_dt = em.apple_ts_to_datetime(last)
            last_str = last_dt.astimezone().strftime("%Y-%m-%d %H:%M") if last_dt else "?"
            report("FAIL", f"Contact {contact}",
                   f"{orphaned:,} messages exist for this contact (newest {last_str}), but none\n"
                   "belong to a conversation, so the exporter can't see them. This usually\n"
                   "means the conversation was deleted or is missing in the Messages app.\n"
                   "Check that the conversation appears in Messages (and in Recently Deleted).")
            return None
        report("FAIL", f"Contact {contact}",
               "No messages found. Run: python3 export_messages.py --list-contacts\n"
               "The identifier must match exactly (the +1 prefix matters).\n"
               "If Messages is still syncing from iCloud, wait and re-run.")
        return None
    msgs = em.fetch_messages(conn, contact)
    texts = sum(1 for m in msgs if m["text"])
    first = msgs[0]["timestamp_local"] if msgs else "?"
    last = msgs[-1]["timestamp_local"] if msgs else "?"
    report("PASS", f"Contact {contact}",
           f"{len(msgs):,} messages ({texts:,} with text), {first} → {last}")
    return msgs


def check_attachments(em, msgs: list[dict]):
    atts = [a for m in msgs for a in m["attachments"]]
    if not atts:
        report("SKIP", "Attachments on disk", "Conversation has no attachments.")
        return
    found = sum(1 for a in atts if em.resolve_attachment_path(a["original_path"]))
    pct = found / len(atts) * 100
    detail = f"{found:,} of {len(atts):,} attachments available locally ({pct:.0f}%)"
    if pct >= 90:
        report("PASS", "Attachments on disk", detail)
    else:
        report("WARN", "Attachments on disk",
               detail + "\nThe rest are probably still in iCloud. Scroll back through the "
               "conversation in Messages\n(or wait for sync) to download them, then re-run.")


def check_heic(em, msgs: list[dict], tmp: Path):
    if not shutil.which("sips"):
        report("FAIL", "HEIC → JPEG (sips)", "sips not found (it ships with macOS).")
        return
    heic = next((p for m in msgs for a in m["attachments"]
                 if (p := em.resolve_attachment_path(a["original_path"]))
                 and p.suffix.lower() == ".heic"), None)
    if not heic:
        report("PASS", "HEIC → JPEG (sips)", "sips available (no local HEIC file to test on)")
        return
    out = tmp / "heic_test.jpg"
    r = subprocess.run(["sips", "-s", "format", "jpeg", str(heic), "--out", str(out)],
                       capture_output=True, text=True)
    if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
        report("PASS", "HEIC → JPEG (sips)", f"Converted {heic.name}")
    else:
        report("FAIL", "HEIC → JPEG (sips)", r.stderr.strip() or "conversion produced no file")


def check_tools():
    if shutil.which("yt-dlp"):
        v = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True).stdout.strip()
        report("PASS", "yt-dlp (optional, YouTube audio)", f"version {v}")
    else:
        report("WARN", "yt-dlp (optional, YouTube audio)",
               "Not installed. YouTube links won't get audio. Install: brew install yt-dlp")
    if shutil.which("pbcopy"):
        report("PASS", "pbcopy (used by analyze_messages.py)")
    else:
        report("FAIL", "pbcopy (used by analyze_messages.py)", "pbcopy not found.")


def check_network(em):
    try:
        og = em.fetch_og("https://example.com")
    except Exception as e:
        og, err = None, str(e)
    else:
        err = ""
    if og:
        report("PASS", "Network / link previews", f"Fetched example.com: {og.get('title')!r}")
    else:
        report("WARN", "Network / link previews",
               f"Could not fetch https://example.com {err}\n"
               "Link previews will fail; use --no-link-previews or check your connection.")


def check_end_to_end(em, contact: str, msgs: list[dict], tmp: Path):
    """Write JSON/HTML/TXT for the real conversation into a temp dir, and copy a
    small sample of attachments — without touching the real export folder."""
    out = tmp / "export"
    out.mkdir()
    try:
        sample = [json.loads(json.dumps(m)) for m in msgs if m["attachments"]][:5]
        if sample:
            em.copy_attachments(sample, out / "attachments")
            copied = [a for m in sample for a in m["attachments"] if a.get("exported_filename")]
            for a in copied:
                if not (out / "attachments" / a["exported_filename"]).exists():
                    raise AssertionError(f"attachment not copied: {a['exported_filename']}")

        ordered = sorted(msgs, key=lambda m: (m.get("timestamp_utc") or "", m["message_id"]))
        em.write_json(ordered, out / "messages.json")
        em.write_html(ordered, contact, out / "messages.html")
        em.write_txt(ordered, contact, out / "messages.txt")

        back = json.loads((out / "messages.json").read_text())
        if len(back) != len(msgs):
            raise AssertionError(f"messages.json has {len(back)} messages, expected {len(msgs)}")
        html = (out / "messages.html").read_text()
        for marker in ['id="search-input"', 'id="lb"', "</html>"]:
            if marker not in html:
                raise AssertionError(f"messages.html is missing {marker}")
        if (out / "messages.txt").stat().st_size == 0:
            raise AssertionError("messages.txt is empty")
    except Exception as e:
        report("FAIL", "End-to-end export (temp folder)", f"{type(e).__name__}: {e}")
        return
    report("PASS", "End-to-end export (temp folder)",
           f"JSON, HTML (with search + lightbox) and TXT written for {len(msgs):,} messages"
           + (f"; {len(sample)} sample attachment message(s) copied" if sample else ""))


def check_existing_export(conn, contact: str):
    """An export made on another Mac keys messages by that Mac's ROWIDs. Reusing it
    here would duplicate or skip messages on the next incremental run."""
    folder = EXPORT_BASE / contact.replace("+", "").replace("@", "_at_")
    json_path = folder / "messages.json"
    if not json_path.exists():
        report("PASS", "Existing export folder",
               f"None at {folder} — the first run will do a fresh export.")
        return
    try:
        existing = json.loads(json_path.read_text())
    except Exception as e:
        report("FAIL", "Existing export folder", f"{json_path} is unreadable: {e}")
        return
    sample = [m for m in existing if m.get("guid")][-200:]
    if not sample:
        report("WARN", "Existing export folder", "messages.json has no GUIDs to verify against.")
        return
    db_guid = {r[0]: r[1] for r in conn.execute(
        f"SELECT ROWID, guid FROM message WHERE ROWID IN ({','.join('?' * len(sample))})",
        [m["message_id"] for m in sample])}
    mismatched = sum(1 for m in sample if db_guid.get(m["message_id"]) != m["guid"])
    if mismatched == 0:
        report("PASS", "Existing export folder",
               f"{folder}\nmessage IDs match this machine's database — safe for incremental runs.")
    else:
        report("FAIL", "Existing export folder",
               f"{mismatched} of {len(sample)} sampled messages in {json_path.name} don't match this\n"
               "machine's database (it was probably made on another Mac). The next run would\n"
               "duplicate or skip messages. Move it aside to keep as an archive, e.g.:\n"
               f'  mv "{folder}" "{folder}_old_mac"')


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Check that this machine is set up to run the exporter.")
    ap.add_argument("contact", nargs="?", help="Phone number or email to test with")
    ap.add_argument("--offline", action="store_true", help="Skip the network check")
    args = ap.parse_args()

    print("\nApple Messages Exporter — setup check\n")

    check_platform()
    check_python()
    if sys.version_info < (3, 10):
        sys.exit("\nStopping: the remaining checks need Python 3.10+.")
    check_project_files()

    sys.path.insert(0, str(HERE))
    import export_messages as em

    check_unit_tests()
    check_tools()
    if args.offline:
        report("SKIP", "Network / link previews", "--offline")
    else:
        check_network(em)

    conn = check_database(em)
    if conn:
        check_schema(conn)
        contact = args.contact or contact_from_launcher()
        msgs = check_contact(em, conn, contact)
        if msgs:
            with tempfile.TemporaryDirectory() as t:
                tmp = Path(t)
                check_attachments(em, msgs)
                check_heic(em, msgs, tmp)
                check_end_to_end(em, contact, msgs, tmp)
        if contact:
            check_existing_export(conn, contact)

    counts = {s: sum(1 for r, _ in results if r == s) for s in ("PASS", "WARN", "FAIL", "SKIP")}
    print(f"\n{counts['PASS']} passed, {counts['WARN']} warnings, "
          f"{counts['FAIL']} failed, {counts['SKIP']} skipped")
    if counts["FAIL"]:
        print("Fix the ❌ items above and re-run.\n")
        sys.exit(1)
    print("Ready to go.\n" if not counts["WARN"] else "Usable — review the ⚠️  items above.\n")


if __name__ == "__main__":
    main()
