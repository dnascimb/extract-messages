#!/usr/bin/env python3
"""
Apple Messages Exporter
-----------------------
Exports all messages and attachments for a specific contact from chat.db.

Usage:
    python3 export_messages.py "+14155550100"
    python3 export_messages.py "example@icloud.com"
    python3 export_messages.py "+14155550100" --output ~/Desktop/my_export
    python3 export_messages.py --list-contacts   # show all available contacts
"""

import sqlite3
import os
import sys
import json
import shutil
import argparse
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
DB_PATH        = Path.home() / "Library" / "Messages" / "chat.db"
ATTACHMENTS_DIR = Path.home() / "Library" / "Messages" / "Attachments"

# Apple's reference date: Jan 1, 2001 00:00:00 UTC  (in Unix epoch seconds)
APPLE_EPOCH_OFFSET = 978307200

def apple_ts_to_datetime(ts: int) -> datetime:
    """Convert Apple nanosecond timestamp → UTC datetime."""
    if not ts:
        return None
    unix_ts = ts / 1_000_000_000 + APPLE_EPOCH_OFFSET
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc)

# ── Database helpers ────────────────────────────────────────────────────────────
def open_db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        sys.exit(f"[error] Database not found at {DB_PATH}")
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.OperationalError as e:
        sys.exit(
            f"[error] Cannot open database: {e}\n\n"
            "Make sure your terminal has Full Disk Access:\n"
            "  System Settings → Privacy & Security → Full Disk Access"
        )

def list_contacts(conn: sqlite3.Connection):
    """Print all unique contacts/conversations found in the database."""
    rows = conn.execute("""
        SELECT
            h.id            AS contact,
            c.display_name  AS display_name,
            c.chat_identifier,
            COUNT(m.ROWID)  AS message_count
        FROM chat c
        JOIN chat_handle_join chj ON c.ROWID = chj.chat_id
        JOIN handle h             ON chj.handle_id = h.ROWID
        LEFT JOIN chat_message_join cmj ON c.ROWID = cmj.chat_id
        LEFT JOIN message m         ON cmj.message_id = m.ROWID
        GROUP BY c.ROWID, h.id
        ORDER BY message_count DESC
    """).fetchall()

    print(f"\n{'Contact / Identifier':<35} {'Display Name':<30} {'Messages':>8}")
    print("─" * 75)
    for r in rows:
        name = r["display_name"] or ""
        print(f"{r['contact']:<35} {name:<30} {r['message_count']:>8}")
    print(f"\n{len(rows)} conversation(s) found.\n")

def fetch_messages(conn: sqlite3.Connection, contact: str) -> list[dict]:
    """Return all messages (including attachments) for the given contact."""
    rows = conn.execute("""
        SELECT
            m.ROWID                         AS message_id,
            m.date                          AS apple_ts,
            m.is_from_me                    AS is_from_me,
            h.id                            AS sender,
            m.text                          AS text,
            m.associated_message_type       AS reaction_type,
            m.associated_message_guid       AS reaction_target,
            m.subject                       AS subject,
            m.service                       AS service,
            a.filename                      AS attachment_filename,
            a.mime_type                     AS attachment_mime,
            a.transfer_name                 AS attachment_name
        FROM message m
        JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        JOIN chat c                ON cmj.chat_id = c.ROWID
        JOIN chat_handle_join chj  ON c.ROWID = chj.chat_id
        JOIN handle h2             ON chj.handle_id = h2.ROWID
        LEFT JOIN handle h         ON m.handle_id = h.ROWID
        LEFT JOIN message_attachment_join maj ON m.ROWID = maj.message_id
        LEFT JOIN attachment a     ON maj.attachment_id = a.ROWID
        WHERE h2.id = ?
        ORDER BY m.date ASC
    """, (contact,)).fetchall()

    # A single message can have multiple attachments → group by message_id
    messages: dict[int, dict] = {}
    for r in rows:
        mid = r["message_id"]
        if mid not in messages:
            dt = apple_ts_to_datetime(r["apple_ts"])
            messages[mid] = {
                "message_id":    mid,
                "timestamp_utc": dt.isoformat() if dt else None,
                "timestamp_local": dt.astimezone().strftime("%Y-%m-%d %H:%M:%S") if dt else None,
                "is_from_me":    bool(r["is_from_me"]),
                "sender":        "me" if r["is_from_me"] else (r["sender"] or contact),
                "text":          r["text"] or "",
                "subject":       r["subject"] or "",
                "service":       r["service"] or "",
                "is_reaction":   r["reaction_type"] not in (0, None),
                "reaction_type": r["reaction_type"],
                "attachments":   [],
            }
        # Attach file info if present
        if r["attachment_filename"]:
            messages[mid]["attachments"].append({
                "original_path": r["attachment_filename"],
                "name":          r["attachment_name"] or "",
                "mime_type":     r["attachment_mime"] or "",
            })

    return list(messages.values())

# ── Attachment handling ─────────────────────────────────────────────────────────
def resolve_attachment_path(raw: str) -> Path | None:
    """Expand ~/Library/... style paths returned by the DB."""
    if not raw:
        return None
    p = Path(raw.replace("~", str(Path.home())))
    return p if p.exists() else None

def copy_attachments(messages: list[dict], dest_dir: Path) -> list[dict]:
    """Copy every attachment into dest_dir and update paths in-place."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    missing = 0

    for msg in messages:
        for att in msg["attachments"]:
            src = resolve_attachment_path(att["original_path"])
            if src:
                dst = dest_dir / src.name
                # Avoid name collisions
                counter = 1
                stem, suffix = dst.stem, dst.suffix
                while dst.exists():
                    dst = dest_dir / f"{stem}_{counter}{suffix}"
                    counter += 1
                shutil.copy2(src, dst)
                att["exported_filename"] = dst.name
            else:
                att["exported_filename"] = None
                missing += 1

    if missing:
        print(f"  [warn] {missing} attachment(s) not found locally "
              "(may be iCloud-only — scroll to them in Messages to download).")
    return messages

# ── Export formats ──────────────────────────────────────────────────────────────
def write_json(messages: list[dict], path: Path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(messages, f, indent=2, ensure_ascii=False, default=str)

def write_html(messages: list[dict], contact: str, path: Path):
    """Write a self-contained, iMessage-style HTML transcript."""
    lines = [f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Messages with {contact}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #f5f5f7;
    color: #1c1c1e;
    padding: 24px;
  }}
  h1 {{ font-size: 1.1rem; color: #6e6e73; margin-bottom: 20px; text-align: center; }}
  .day-label {{
    text-align: center;
    font-size: 0.72rem;
    color: #8e8e93;
    margin: 18px 0 8px;
    letter-spacing: 0.03em;
    text-transform: uppercase;
  }}
  .bubble-row {{
    display: flex;
    margin: 3px 0;
  }}
  .bubble-row.me    {{ justify-content: flex-end; }}
  .bubble-row.them  {{ justify-content: flex-start; }}
  .bubble {{
    max-width: 68%;
    padding: 8px 13px;
    border-radius: 18px;
    font-size: 0.93rem;
    line-height: 1.45;
    word-break: break-word;
    white-space: pre-wrap;
  }}
  .me   .bubble {{ background: #0b93f6; color: #fff; border-bottom-right-radius: 4px; }}
  .them .bubble {{ background: #e5e5ea; color: #1c1c1e; border-bottom-left-radius: 4px; }}
  .reaction .bubble {{
    font-size: 0.78rem;
    opacity: 0.65;
    padding: 4px 10px;
    border-radius: 12px;
  }}
  .time {{
    font-size: 0.65rem;
    color: #8e8e93;
    margin-top: 2px;
    text-align: right;
    padding: 0 4px;
  }}
  .them .time {{ text-align: left; }}
  .attachment {{ margin-top: 6px; }}
  .attachment a {{ color: inherit; text-decoration: underline; opacity: 0.85; }}
  img.inline {{ max-width: 260px; border-radius: 12px; margin-top: 4px; display: block; }}
  video.inline {{ max-width: 280px; border-radius: 12px; margin-top: 4px; }}
</style>
</head>
<body>
<h1>Messages with {contact}</h1>
"""]

    REACTION_LABELS = {
        2000: "❤️ Loved", 2001: "👍 Liked", 2002: "👎 Disliked",
        2003: "😂 Laughed", 2004: "‼️ Emphasized", 2005: "❓ Questioned",
        3000: "♥ Loved (removed)", 3001: "👍 Like (removed)",
    }

    last_day = None
    for msg in messages:
        ts = msg.get("timestamp_local") or ""
        day = ts[:10] if ts else "Unknown date"

        if day != last_day:
            try:
                day_fmt = datetime.strptime(day, "%Y-%m-%d").strftime("%B %d, %Y")
            except Exception:
                day_fmt = day
            lines.append(f'<div class="day-label">{day_fmt}</div>\n')
            last_day = day

        side  = "me" if msg["is_from_me"] else "them"
        extra = " reaction" if msg["is_reaction"] else ""
        time_str = ts[11:16] if len(ts) >= 16 else ""

        # Build content
        if msg["is_reaction"]:
            label = REACTION_LABELS.get(msg["reaction_type"], f"Reaction {msg['reaction_type']}")
            content = label
        else:
            content = msg["text"] or ""

        # Escape HTML
        content = (content
                   .replace("&", "&amp;")
                   .replace("<", "&lt;")
                   .replace(">", "&gt;"))

        att_html = ""
        for att in msg["attachments"]:
            fname = att.get("exported_filename") or att.get("name") or "attachment"
            mime  = att.get("mime_type", "")
            if att.get("exported_filename"):
                rel = f"attachments/{fname}"
                if mime.startswith("image/"):
                    att_html += f'<div class="attachment"><img class="inline" src="{rel}" alt="{fname}"></div>'
                elif mime.startswith("video/"):
                    att_html += f'<div class="attachment"><video class="inline" controls src="{rel}"></video></div>'
                else:
                    att_html += f'<div class="attachment">📎 <a href="{rel}">{fname}</a></div>'
            else:
                att_html += f'<div class="attachment">📎 {fname} <em>(not available locally)</em></div>'

        lines.append(
            f'<div class="bubble-row {side}{extra}">\n'
            f'  <div>\n'
            f'    <div class="bubble">{content}{att_html}</div>\n'
            f'    <div class="time">{time_str}</div>\n'
            f'  </div>\n'
            f'</div>\n'
        )

    lines.append("</body></html>")
    path.write_text("\n".join(lines), encoding="utf-8")

def write_txt(messages: list[dict], contact: str, path: Path):
    """Write a plain-text transcript."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"Messages with {contact}\n")
        f.write("=" * 60 + "\n\n")
        for msg in messages:
            if msg["is_reaction"]:
                continue  # skip tapbacks in plain text
            sender = "Me" if msg["is_from_me"] else contact
            ts = msg.get("timestamp_local", "")
            f.write(f"[{ts}] {sender}:\n")
            if msg["text"]:
                f.write(f"  {msg['text']}\n")
            for att in msg["attachments"]:
                name = att.get("exported_filename") or att.get("name") or "unknown"
                f.write(f"  [Attachment: {name}]\n")
            f.write("\n")

# ── Main ────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Export Apple Messages for a contact.")
    parser.add_argument("contact", nargs="?", help="Phone number or email to export (e.g. +14155550100)")
    parser.add_argument("--output", "-o", default="~/Desktop/messages_export",
                        help="Output directory (default: ~/Desktop/messages_export)")
    parser.add_argument("--list-contacts", action="store_true",
                        help="List all contacts in the database and exit")
    parser.add_argument("--no-reactions", action="store_true",
                        help="Exclude tapback/reaction messages")
    parser.add_argument("--format", choices=["all", "json", "html", "txt"], default="all",
                        help="Export format (default: all)")
    args = parser.parse_args()

    conn = open_db()

    if args.list_contacts:
        list_contacts(conn)
        return

    if not args.contact:
        parser.error("Provide a contact identifier, or use --list-contacts to see options.")

    contact = args.contact
    out_dir = Path(args.output.replace("~", str(Path.home()))) / contact.replace("+", "").replace("@", "_at_")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nExporting messages with: {contact}")
    print(f"Output directory: {out_dir}\n")

    messages = fetch_messages(conn, contact)
    if not messages:
        sys.exit(f"[error] No messages found for '{contact}'.\n"
                 "Run with --list-contacts to see available identifiers.")

    if args.no_reactions:
        messages = [m for m in messages if not m["is_reaction"]]

    total = len(messages)
    attachments_total = sum(len(m["attachments"]) for m in messages)
    print(f"  Found {total} message(s), {attachments_total} attachment(s).")

    # Copy attachments
    if attachments_total > 0:
        print("  Copying attachments…")
        messages = copy_attachments(messages, out_dir / "attachments")

    # Write exports
    fmt = args.format
    if fmt in ("all", "json"):
        p = out_dir / "messages.json"
        write_json(messages, p)
        print(f"  ✔ JSON   → {p}")

    if fmt in ("all", "html"):
        p = out_dir / "messages.html"
        write_html(messages, contact, p)
        print(f"  ✔ HTML   → {p}")

    if fmt in ("all", "txt"):
        p = out_dir / "messages.txt"
        write_txt(messages, contact, p)
        print(f"  ✔ TXT    → {p}")

    print(f"\nDone! Open {out_dir / 'messages.html'} in a browser for a visual transcript.\n")

if __name__ == "__main__":
    main()
