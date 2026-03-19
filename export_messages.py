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
import re
import subprocess
import urllib.request
import urllib.error
from collections import Counter
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

# ── attributedBody decoder ──────────────────────────────────────────────────────
def extract_attributed_body(blob: bytes) -> str | None:
    """Extract plain text from an NSArchiver streamtyped NSAttributedString blob."""
    if not blob:
        return None
    try:
        # Text follows the marker \x84\x01+ with a variable-length byte count prefix
        marker = blob.find(b'\x84\x01+')
        if marker == -1:
            return None
        pos = marker + 3
        b0 = blob[pos]
        if b0 < 0x80:
            length, pos = b0, pos + 1
        elif b0 == 0x81:
            length = int.from_bytes(blob[pos + 1:pos + 3], 'little')
            pos += 3
        elif b0 == 0x82:
            length = int.from_bytes(blob[pos + 1:pos + 5], 'little')
            pos += 5
        else:
            return None
        text = blob[pos:pos + length].decode('utf-8', errors='replace')
        return text.strip() or None
    except Exception:
        return None


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

def fetch_messages(conn: sqlite3.Connection, contact: str, since_id: int = 0) -> list[dict]:
    """Return all messages (including attachments) for the given contact.
    If since_id > 0, only returns messages with ROWID > since_id."""
    rows = conn.execute(f"""
        SELECT
            m.ROWID                         AS message_id,
            m.guid                          AS guid,
            m.date                          AS apple_ts,
            m.is_from_me                    AS is_from_me,
            h.id                            AS sender,
            m.text                          AS text,
            m.attributedBody                AS attributed_body,
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
        {"AND m.ROWID > " + str(since_id) if since_id else ""}
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
                "text":          r["text"] or extract_attributed_body(r["attributed_body"]) or "",
                "subject":       r["subject"] or "",
                "service":       r["service"] or "",
                "guid":          r["guid"] or "",
                "is_reaction":   r["reaction_type"] not in (0, None),
                "reaction_type": r["reaction_type"],
                "reaction_target": (r["reaction_target"] or "").split("/")[-1],
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
                if dst.exists():
                    att["exported_filename"] = dst.name  # already copied
                else:
                    # Avoid name collisions with other new files
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

# ── Link previews ───────────────────────────────────────────────────────────────
URL_RE = re.compile(r'https?://[^\s]+')
YOUTUBE_RE = re.compile(r'https?://(www\.)?(youtube\.com|youtu\.be|music\.youtube\.com)')

def fetch_og(url: str) -> dict | None:
    """Fetch Open Graph metadata (and Suno audio URL) from a URL."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="ignore")
    except Exception:
        return None

    def og(prop):
        m = re.search(rf'<meta[^>]+property=["\']og:{prop}["\'][^>]+content=["\']([^"\']+)["\']', html)
        if not m:
            m = re.search(rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:{prop}["\']', html)
        return m.group(1) if m else None

    result = {
        "url":         url,
        "title":       og("title"),
        "description": og("description"),
        "image_url":   og("image"),
        "audio_url":   None,
    }

    # Suno: extract audio URL from embedded MP3 reference
    if "suno.com" in url:
        mp3 = re.search(r'https://cdn\d*\.suno\.ai/([a-f0-9\-]{36})\.mp3', html)
        if mp3:
            result["audio_url"] = f"https://cdn1.suno.ai/{mp3.group(1)}.mp3"

    return result if (result["title"] or result["image_url"]) else None


def fetch_youtube(url: str, preview_dir: Path) -> dict | None:
    """Use yt-dlp to fetch metadata and download audio for a YouTube URL."""
    try:
        meta_raw = subprocess.run(
            ["yt-dlp", "--dump-json", "--no-playlist", url],
            capture_output=True, text=True, timeout=30
        )
        if meta_raw.returncode != 0:
            return None
        meta = json.loads(meta_raw.stdout)
    except Exception:
        return None

    title     = meta.get("title")
    desc      = meta.get("description", "")[:120] or None
    thumb_url = meta.get("thumbnail")
    video_id  = meta.get("id", re.sub(r'[^a-zA-Z0-9_-]', '_', url)[-24:])
    is_single = meta.get("_type", "video") != "playlist"

    result = {
        "url":         url,
        "title":       title,
        "description": desc,
        "image_url":   thumb_url,
        "audio_url":   None,
        "local_image": None,
        "local_audio": None,
    }

    preview_dir.mkdir(parents=True, exist_ok=True)

    # Download thumbnail
    if thumb_url:
        img_dest = preview_dir / f"yt_{video_id}.jpg"
        if not img_dest.exists():
            try:
                urllib.request.urlretrieve(thumb_url, img_dest)
            except Exception:
                img_dest = None
        if img_dest and img_dest.exists():
            result["local_image"] = f"previews/{img_dest.name}"

    # Download audio for individual videos/songs (skip playlists/channels)
    if is_single:
        audio_dest = preview_dir / f"yt_{video_id}.mp3"
        if not audio_dest.exists():
            try:
                subprocess.run(
                    ["yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "5",
                     "--no-playlist", "-o", str(audio_dest.with_suffix("")),
                     url],
                    capture_output=True, timeout=120
                )
            except Exception:
                pass
        if audio_dest.exists():
            result["local_audio"] = f"previews/{audio_dest.name}"

    return result if result["title"] else None


def fetch_link_previews(messages: list[dict], out_dir: Path,
                        preview_cache: dict | None = None) -> tuple[list[dict], dict]:
    """Fetch OG previews for URLs in messages and download assets locally.
    preview_cache maps url -> og dict for already-processed URLs (no re-fetch).
    Returns (annotated_messages, updated_cache)."""
    preview_cache = dict(preview_cache or {})

    # Collect unique URLs across all messages
    unique_urls: dict[str, None] = {}
    for msg in messages:
        for url in URL_RE.findall(msg.get("text") or ""):
            unique_urls[url] = None

    if not unique_urls:
        return messages, preview_cache

    preview_dir = out_dir / "previews"
    new_urls = [u for u in unique_urls if u not in preview_cache]

    if new_urls:
        print(f"  Fetching link previews for {len(new_urls)} new URL(s)…")
        for i, url in enumerate(new_urls, 1):
            print(f"    [{i}/{len(new_urls)}] {url[:80]}", end="\r", flush=True)
            if YOUTUBE_RE.match(url):
                og = fetch_youtube(url, preview_dir)
            else:
                og = fetch_og(url)
            if og:
                preview_cache[url] = og
        print()

        # Download assets for non-YouTube URLs
        preview_dir.mkdir(parents=True, exist_ok=True)

        def download(src_url: str, dest: Path) -> bool:
            if dest.exists():
                return True
            try:
                urllib.request.urlretrieve(src_url, dest)
                return True
            except Exception:
                return False

        for url in new_urls:
            og = preview_cache.get(url)
            if not og or YOUTUBE_RE.match(url):
                continue
            key = abs(hash(url)) & 0xFFFFFF
            if og.get("image_url"):
                ext = og["image_url"].rsplit(".", 1)[-1].split("?")[0][:4] or "jpg"
                dest = preview_dir / f"img_{key}.{ext}"
                if download(og["image_url"], dest):
                    og["local_image"] = f"previews/{dest.name}"
            if og.get("audio_url"):
                dest = preview_dir / f"audio_{key}.mp3"
                if download(og["audio_url"], dest):
                    og["local_audio"] = f"previews/{dest.name}"

        new_dl = sum(1 for u in new_urls if preview_cache.get(u, {}).get("local_audio"))
        print(f"  ✔ {len(new_urls)} new URL(s) processed"
              + (f", {new_dl} audio file(s) downloaded." if new_dl else "."))
    else:
        print(f"  ✔ Link previews up to date ({len(preview_cache)} cached).")

    # Annotate messages
    for msg in messages:
        urls_in_msg = URL_RE.findall(msg.get("text") or "")
        msg["link_previews"] = [preview_cache[u] for u in urls_in_msg if u in preview_cache]

    return messages, preview_cache


# ── Export formats ──────────────────────────────────────────────────────────────
def write_json(messages: list[dict], path: Path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(messages, f, indent=2, ensure_ascii=False, default=str)

def write_html(messages: list[dict], contact: str, path: Path):
    """Write a self-contained, iMessage-style HTML transcript."""

    avatar_letter = contact[1] if contact and len(contact) > 1 else contact[0] if contact else "?"
    total = len(messages)

    header = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Messages with {contact}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  :root {{
    --bg: #f5f5f7;
    --text: #1c1c1e;
    --subtext: #8e8e93;
    --them-bubble: #e5e5ea;
    --them-text: #1c1c1e;
    --me-blue: #007aff;
    --me-green: #34c759;
    --divider: #c6c6c8;
    --card-bg: rgba(0,0,0,0.06);
    --card-hover: rgba(0,0,0,0.1);
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #000;
      --text: #fff;
      --subtext: #8e8e93;
      --them-bubble: #2c2c2e;
      --them-text: #fff;
      --me-blue: #0a84ff;
      --me-green: #30d158;
      --divider: #3a3a3c;
      --card-bg: rgba(255,255,255,0.1);
      --card-hover: rgba(255,255,255,0.15);
    }}
  }}

  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 21px;
    background: var(--bg);
    color: var(--text);
    padding: 0 0 48px;
  }}


  /* ── Header ── */
  .msg-header {{
    position: sticky;
    top: 0;
    z-index: 10;
    background: var(--bg);
    border-bottom: 1px solid var(--divider);
    text-align: center;
    padding: 14px 16px 12px;
  }}
  .avatar {{
    width: 52px; height: 52px;
    border-radius: 50%;
    background: var(--me-blue);
    color: #fff;
    font-size: 1.3rem;
    font-weight: 600;
    display: flex; align-items: center; justify-content: center;
    margin: 0 auto 6px;
  }}
  .msg-header h1 {{ font-size: 1rem; font-weight: 600; }}
  .msg-header .sub {{ font-size: 0.75rem; color: var(--subtext); margin-top: 2px; }}

  /* ── Day label ── */
  .day-label {{
    text-align: center;
    font-size: 0.68rem;
    color: var(--subtext);
    font-weight: 500;
    margin: 18px 0 6px;
    letter-spacing: 0.04em;
  }}

  /* ── Bubble rows ── */
  .bubble-row {{
    display: flex;
    padding: 2px 16px;
    align-items: flex-end;
    gap: 6px;
  }}
  .bubble-row.me   {{ justify-content: flex-end; }}
  .bubble-row.them {{ justify-content: flex-start; }}

  .bubble-wrap {{
    max-width: 55%;
    display: flex;
    flex-direction: column;
  }}
  .me   .bubble-wrap {{ align-items: flex-end; }}
  .them .bubble-wrap {{ align-items: flex-start; }}

  .bubble {{
    padding: 10px 15px;
    border-radius: 18px;
    font-size: 1rem;
    line-height: 1.5;
    word-break: break-word;
    white-space: pre-wrap;
  }}
  .me            .bubble {{ background: var(--me-blue);  color: #fff;            border-bottom-right-radius: 4px; }}
  .me.sms        .bubble {{ background: var(--me-green); color: #fff;            border-bottom-right-radius: 4px; }}
  .them          .bubble {{ background: var(--them-bubble); color: var(--them-text); border-bottom-left-radius: 4px; }}
  .reaction      .bubble {{ font-size: 0.75rem; opacity: 0.6; padding: 3px 10px; border-radius: 12px; }}

  .time {{
    font-size: 0.62rem;
    color: var(--subtext);
    margin-top: 3px;
    padding: 0 4px;
  }}

  /* ── Attachments ── */
  .attachment {{ margin-top: 6px; line-height: 0; }}
  .attachment:first-child {{ margin-top: 0; }}

  img.inline {{
    max-width: 100%;
    width: auto;
    max-height: 400px;
    height: auto;
    border-radius: 14px;
    display: block;
    cursor: zoom-in;
    object-fit: cover;
  }}
  video.inline {{
    max-width: 100%;
    width: auto;
    max-height: 400px;
    border-radius: 14px;
    display: block;
  }}
  audio.inline {{
    max-width: 100%;
    width: 280px;
    display: block;
    margin: 2px 0;
  }}

  .file-card {{
    display: inline-flex;
    align-items: center;
    gap: 10px;
    background: var(--card-bg);
    border-radius: 12px;
    padding: 9px 12px;
    text-decoration: none;
    color: inherit;
    max-width: 240px;
    transition: background 0.15s;
  }}
  .file-card:hover {{ background: var(--card-hover); }}
  .file-card .icon {{ font-size: 1.5rem; flex-shrink: 0; line-height: 1; }}
  .file-card .info {{ min-width: 0; }}
  .file-card .fname {{
    font-size: 0.83rem;
    font-weight: 500;
    line-height: 1.3;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 170px;
  }}
  .file-card .ftype {{ font-size: 0.7rem; opacity: 0.55; margin-top: 1px; line-height: 1; }}

  .missing {{ font-size: 0.78rem; opacity: 0.45; font-style: italic; line-height: 1.4; }}

  /* ── Tapback reactions ── */
  .bubble-outer {{
    position: relative;
    display: inline-block;
  }}
  .me   .bubble-outer {{ align-self: flex-end; }}
  .them .bubble-outer {{ align-self: flex-start; }}
  .has-reactions {{ margin-bottom: 14px; }}
  .reaction-badges {{
    position: absolute;
    bottom: -13px;
    display: flex;
    gap: 3px;
    flex-wrap: wrap;
  }}
  .me   .reaction-badges {{ left: 6px; }}
  .them .reaction-badges {{ right: 6px; }}
  .reaction-badge {{
    background: var(--bg);
    border: 1.5px solid var(--divider);
    border-radius: 999px;
    padding: 1px 6px;
    font-size: 0.8rem;
    white-space: nowrap;
    box-shadow: 0 1px 3px rgba(0,0,0,0.12);
    line-height: 1.4;
  }}

  /* ── Link preview cards ── */
  .link-preview {{
    display: block;
    border-radius: 14px;
    overflow: hidden;
    background: var(--card-bg);
    text-decoration: none;
    color: inherit;
    margin-top: 6px;
    max-width: 300px;
    transition: opacity 0.15s;
  }}
  .link-preview:hover {{ opacity: 0.85; }}
  .link-preview .preview-img {{
    width: 100%;
    height: 160px;
    object-fit: cover;
    display: block;
  }}
  .link-preview .preview-body {{
    padding: 9px 12px 11px;
  }}
  .link-preview .preview-domain {{
    font-size: 0.68rem;
    opacity: 0.5;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 3px;
  }}
  .link-preview .preview-title {{
    font-size: 0.9rem;
    font-weight: 600;
    line-height: 1.3;
  }}
  .link-preview .preview-desc {{
    font-size: 0.78rem;
    opacity: 0.65;
    margin-top: 3px;
    line-height: 1.35;
  }}
  .link-preview audio {{
    width: 100%;
    margin-top: 8px;
    display: block;
  }}

  /* ── Lightbox ── */
  #lb {{
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.9);
    z-index: 100;
    align-items: center;
    justify-content: center;
    cursor: zoom-out;
  }}
  #lb.open {{ display: flex; }}
  #lb img {{ max-width: 95vw; max-height: 95vh; border-radius: 6px; object-fit: contain; }}

  /* ── Responsive ── */
  @media (max-width: 600px) {{
    .bubble-wrap {{ max-width: 82%; }}
    .file-card   {{ max-width: 200px; }}
    .file-card .fname {{ max-width: 130px; }}
  }}
</style>
</head>
<body>

<div class="msg-header">
  <div class="avatar">{avatar_letter}</div>
  <h1>{contact}</h1>
  <div class="sub">{total:,} messages</div>
</div>

<div id="lb" onclick="this.classList.remove('open')">
  <img id="lb-img" src="" alt="">
</div>
<script>
function lb(src){{document.getElementById('lb-img').src=src;document.getElementById('lb').classList.add('open');}}
document.addEventListener('keydown',function(e){{if(e.key==='Escape')document.getElementById('lb').classList.remove('open');}});
</script>
<div class="conversation">
"""

    REACTION_EMOJI = {
        2000: "❤️", 2001: "👍", 2002: "👎",
        2003: "😂", 2004: "‼️", 2005: "❓",
    }

    # Build reactions map: message guid → list of emoji (deduped by sender+type)
    reactions_map: dict[str, list[str]] = {}
    seen = set()
    for msg in messages:
        rtype = msg.get("reaction_type")
        if rtype in REACTION_EMOJI:
            target = msg.get("reaction_target") or ""
            key = (target, rtype, msg["sender"])
            if target and key not in seen:
                seen.add(key)
                reactions_map.setdefault(target, []).append(REACTION_EMOJI[rtype])
        elif rtype and 3000 <= rtype <= 3005:
            # Removed reaction — cancel it
            target = msg.get("reaction_target") or ""
            original = rtype - 1000
            key = (target, original, msg["sender"])
            seen.add(key)  # prevent the original from being re-added

    lines = [header]
    last_day = None

    for msg in messages:
        if msg["is_reaction"]:
            continue  # rendered as badges on the target bubble, not as rows
        ts  = msg.get("timestamp_local") or ""
        day = ts[:10] if ts else "Unknown date"

        if day != last_day:
            try:
                day_fmt = datetime.strptime(day, "%Y-%m-%d").strftime("%B %-d, %Y")
            except Exception:
                day_fmt = day
            lines.append(f'<div class="day-label">{day_fmt}</div>\n')
            last_day = day

        side    = "me" if msg["is_from_me"] else "them"
        svc_cls = " sms" if (msg.get("service") or "").upper() == "SMS" else ""
        time_str = ts[11:16] if len(ts) >= 16 else ""

        raw_text = msg["text"] or ""
        # If the entire message is just a URL that has a preview, suppress the raw URL
        previewed_urls = {og["url"] for og in msg.get("link_previews", [])}
        stripped = raw_text.strip()
        if stripped in previewed_urls:
            content = ""
        else:
            content = raw_text

        content = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        att_html = ""
        for att in msg["attachments"]:
            fname = att.get("exported_filename") or att.get("name") or "attachment"
            mime  = att.get("mime_type") or ""
            ext   = fname.rsplit(".", 1)[-1].upper() if "." in fname else "FILE"

            is_plugin = fname.endswith(".pluginPayloadAttachment")
            if is_plugin:
                att_html += f'<div class="attachment"><span class="missing">⚙️ iMessage app content (not previewable)</span></div>'
            elif att.get("exported_filename"):
                rel = f"attachments/{fname}"
                if mime.startswith("image/"):
                    att_html += f'<div class="attachment"><img class="inline" src="{rel}" alt="{fname}" onclick="lb(this.src)"></div>'
                elif mime.startswith("video/"):
                    att_html += f'<div class="attachment"><video class="inline" controls src="{rel}" preload="metadata"></video></div>'
                elif mime.startswith("audio/"):
                    att_html += f'<div class="attachment"><audio class="inline" controls src="{rel}" preload="metadata"></audio></div>'
                else:
                    icon = ("📄" if "pdf" in mime
                            else "📝" if mime.startswith("text/")
                            else "🗜" if "zip" in mime or "compressed" in mime
                            else "📦")
                    att_html += (f'<div class="attachment">'
                                 f'<a class="file-card" href="{rel}" download>'
                                 f'<span class="icon">{icon}</span>'
                                 f'<span class="info">'
                                 f'<span class="fname">{fname}</span>'
                                 f'<span class="ftype">{ext}</span>'
                                 f'</span></a></div>')
            else:
                att_html += f'<div class="attachment"><span class="missing">📎 {fname} — not available locally</span></div>'

        # Build link preview cards
        preview_html = ""
        for og in msg.get("link_previews", []):
            domain = re.sub(r'^https?://(www\.)?', '', og["url"]).split("/")[0]
            img_tag = ""
            if og.get("local_image"):
                img_tag = f'<img class="preview-img" src="{og["local_image"]}" alt="">'
            audio_tag = ""
            if og.get("local_audio"):
                audio_tag = f'<audio controls src="{og["local_audio"]}" preload="metadata"></audio>'
            title = (og.get("title") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            desc  = (og.get("description") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            preview_html += (
                f'<a class="link-preview" href="{og["url"]}" target="_blank">'
                f'{img_tag}'
                f'<div class="preview-body">'
                f'<div class="preview-domain">{domain}</div>'
                f'<div class="preview-title">{title}</div>'
                + (f'<div class="preview-desc">{desc}</div>' if desc else '')
                + audio_tag
                + '</div></a>'
            )

        msg_reactions = reactions_map.get(msg.get("guid", ""), [])
        badges_html = ""
        if msg_reactions:
            counts = Counter(msg_reactions)
            badges = "".join(
                f'<span class="reaction-badge">{emoji}{" " + str(n) if n > 1 else ""}</span>'
                for emoji, n in counts.items()
            )
            badges_html = f'<div class="reaction-badges">{badges}</div>'

        has_reactions_cls = " has-reactions" if msg_reactions else ""

        lines.append(
            f'<div class="bubble-row {side}{svc_cls}">\n'
            f'  <div class="bubble-wrap">\n'
            f'    <div class="bubble-outer{has_reactions_cls}">\n'
            f'      <div class="bubble">{content}{att_html}{preview_html}</div>\n'
            f'      {badges_html}\n'
            f'    </div>\n'
            f'    <div class="time">{time_str}</div>\n'
            f'  </div>\n'
            f'</div>\n'
        )

    lines.append("</div></body></html>")
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
    parser.add_argument("--no-link-previews", action="store_true",
                        help="Skip fetching link previews and downloading audio")
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

    # ── Load state (for incremental runs) ──
    state_path = out_dir / "export_state.json"
    state: dict = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except Exception:
            pass

    last_id       = state.get("last_message_id", 0)
    preview_cache = state.get("preview_cache", {})
    existing_json = out_dir / "messages.json"
    is_incremental = last_id > 0 and existing_json.exists()

    # ── Fetch messages ──
    if is_incremental:
        existing = json.loads(existing_json.read_text())
        new_msgs = fetch_messages(conn, contact, since_id=last_id)
        if new_msgs:
            print(f"  Found {len(new_msgs)} new message(s) "
                  f"({len(existing) + len(new_msgs):,} total).")
            if args.no_reactions:
                new_msgs = [m for m in new_msgs if not m["is_reaction"]]
            new_atts = sum(len(m["attachments"]) for m in new_msgs)
            if new_atts:
                print("  Copying new attachments…")
                new_msgs = copy_attachments(new_msgs, out_dir / "attachments")
            messages = existing + new_msgs
        else:
            print(f"  No new messages — regenerating outputs ({len(existing):,} total).")
            messages = existing
    else:
        messages = fetch_messages(conn, contact)
        if not messages:
            sys.exit(f"[error] No messages found for '{contact}'.\n"
                     "Run with --list-contacts to see available identifiers.")
        if args.no_reactions:
            messages = [m for m in messages if not m["is_reaction"]]
        total = len(messages)
        atts  = sum(len(m["attachments"]) for m in messages)
        print(f"  Found {total:,} message(s), {atts} attachment(s).")
        if atts:
            print("  Copying attachments…")
            messages = copy_attachments(messages, out_dir / "attachments")

    # ── Link previews — always run on full set so cache gaps get filled ──
    if not args.no_link_previews and args.format in ("all", "html"):
        messages, preview_cache = fetch_link_previews(messages, out_dir, preview_cache)

    # ── Save state ──
    state["last_message_id"] = max(m["message_id"] for m in messages)
    state["preview_cache"]   = preview_cache
    state_path.write_text(json.dumps(state, indent=2, default=str))

    # ── Write exports ──
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
