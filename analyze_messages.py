#!/usr/bin/env python3
"""
analyze_messages.py
-------------------
Formats exported messages into a Claude-ready transcript, copies it to
the clipboard, and opens Claude.ai so you can paste and go.

Usage:
    python3 analyze_messages.py "+14155550100"
    python3 analyze_messages.py "+14155550100" --task summarize
    python3 analyze_messages.py "+14155550100" --task translate
    python3 analyze_messages.py "+14155550100" --last 7
    python3 analyze_messages.py "+14155550100" --since 2026-03-01
    python3 analyze_messages.py "+14155550100" --since 2026-03-01 --until 2026-03-15
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

EXPORT_BASE = Path.home() / "Desktop" / "messages_export"
CLAUDE_URL  = "https://claude.ai/new"

# Rough guidance: Claude.ai handles ~200k tokens. 1 token ≈ 4 chars.
WARN_CHARS = 700_000   # ~175k tokens — warn if above this

TASK_PROMPTS = {
    "summarize": (
        "Please provide a thorough summary of the conversation below. "
        "Cover the main topics discussed, key decisions or plans made, "
        "notable emotional moments, recurring subjects, and the overall arc "
        "of the relationship over the time period shown."
    ),
    "topics": (
        "Please identify and list every distinct topic or theme discussed in "
        "the conversation below. For each topic include: a short title, a 1–2 "
        "sentence description, and the approximate date range when it came up."
    ),
    "key_moments": (
        "Please identify the most significant moments in the conversation below — "
        "breakthroughs, conflicts, milestones, funny exchanges, surprises, or "
        "turning points. For each, note the date and explain why it stands out."
    ),
    "sentiment": (
        "Please analyze the emotional tone and sentiment of the conversation below "
        "over time. Describe shifts in mood, recurring emotional patterns, how each "
        "person tends to communicate, and the overall relational dynamic."
    ),
    "translate": (
        "Please identify every message in the conversation below that is not in "
        "English and translate it. Present each as:\n"
        "  [timestamp] Speaker (LANGUAGE): original text\n"
        "  → Translation: english text\n"
        "Skip messages already in English. Note mixed-language messages."
    ),
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_messages(contact: str, since: datetime | None, until: datetime | None) -> list[dict]:
    safe      = contact.lstrip("+").replace("@", "_")
    json_path = EXPORT_BASE / safe / "messages.json"
    if not json_path.exists():
        sys.exit(f"[error] No messages.json found at {json_path}\n"
                 "Run export_messages.py first.")
    msgs = json.loads(json_path.read_text())

    if since or until:
        filtered = []
        for m in msgs:
            ts = m.get("timestamp_utc") or ""
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if since and dt < since:
                continue
            if until and dt > until:
                continue
            filtered.append(m)
        msgs = filtered

    return msgs


def format_transcript(messages: list[dict], contact: str) -> str:
    lines    = [f"Conversation between Me and {contact}", "=" * 60]
    last_day = None

    for msg in messages:
        if msg.get("is_reaction"):
            continue

        ts  = msg.get("timestamp_local") or ""
        day = ts[:10] if ts else ""

        if day and day != last_day:
            try:
                day_fmt = datetime.strptime(day, "%Y-%m-%d").strftime("%B %-d, %Y")
            except ValueError:
                day_fmt = day
            lines.append(f"\n── {day_fmt} ──")
            last_day = day

        try:
            time_str = datetime.strptime(ts[11:19], "%H:%M:%S").strftime("%-I:%M %p")
        except (ValueError, IndexError):
            time_str = ts[11:16] if len(ts) >= 16 else ""

        sender = "Me" if msg["is_from_me"] else contact

        parts = []
        text  = (msg.get("text") or "").replace("\ufffc", "").strip()
        if text:
            parts.append(text)

        for att in msg.get("attachments", []):
            name = att.get("name") or att.get("exported_filename") or ""
            if name.endswith(".pluginPayloadAttachment"):
                continue
            mime = att.get("mime_type") or ""
            if mime.startswith("image/"):
                parts.append("[Image]")
            elif mime.startswith("video/"):
                parts.append("[Video]")
            elif mime.startswith("audio/"):
                parts.append("[Audio clip]")
            elif name:
                parts.append(f"[File: {name}]")

        for lp in msg.get("link_previews") or []:
            title = lp.get("title") or ""
            url   = lp.get("url") or ""
            if "suno.com" in url:
                parts.append(f"[Suno song: {title}]" if title else "[Suno song]")
            elif title:
                parts.append(f"[Link: {title}]")
            else:
                parts.append(f"[Link: {url}]")

        if not parts:
            continue

        lines.append(f"[{time_str}] {sender}: {' '.join(parts)}")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Format messages for Claude.ai."
    )
    parser.add_argument("contact", help="Contact identifier, e.g. +14155550100")
    parser.add_argument(
        "--task", default="summarize",
        choices=list(TASK_PROMPTS.keys()),
        help="Analysis task (default: summarize)",
    )
    parser.add_argument("--last", type=int, metavar="DAYS",
                        help="Only include messages from the last N days")
    parser.add_argument("--since", metavar="YYYY-MM-DD",
                        help="Include messages on or after this date")
    parser.add_argument("--until", metavar="YYYY-MM-DD",
                        help="Include messages on or before this date")
    parser.add_argument("--output", metavar="DIR",
                        help="Output directory (default: export dir for the contact)")
    args = parser.parse_args()

    contact = args.contact
    safe    = contact.lstrip("+").replace("@", "_")
    out_dir = Path(args.output) if args.output else EXPORT_BASE / safe

    # ── Date range ──
    since = until = None
    now = datetime.now(tz=timezone.utc)
    if args.last:
        since = now - timedelta(days=args.last)
    if args.since:
        since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    if args.until:
        until = (datetime.fromisoformat(args.until)
                 .replace(tzinfo=timezone.utc)
                 .replace(hour=23, minute=59, second=59))

    # ── Load & format ──
    print(f"Loading messages for {contact}…")
    messages = load_messages(contact, since, until)
    non_rx   = [m for m in messages if not m.get("is_reaction")]
    if not non_rx:
        sys.exit("[error] No messages found for the specified date range.")

    range_parts = []
    if since:
        range_parts.append(f"from {since.strftime('%Y-%m-%d')}")
    if until:
        range_parts.append(f"until {until.strftime('%Y-%m-%d')}")
    range_str = " ".join(range_parts) or "all time"
    print(f"  {len(non_rx):,} messages  ({range_str})")

    transcript  = format_transcript(messages, contact)
    task_prompt = TASK_PROMPTS[args.task]
    full_text   = f"{task_prompt}\n\n{'='*60}\n\n{transcript}"

    # ── Size check ──
    char_count    = len(full_text)
    approx_tokens = char_count // 4
    print(f"  ~{approx_tokens:,} tokens")
    if char_count > WARN_CHARS:
        print(f"  [warn] This is large. Consider --last or --since to narrow the range.")

    # ── Save to file ──
    out_path = out_dir / f"claude_{args.task}.txt"
    out_path.write_text(full_text, encoding="utf-8")
    print(f"  Saved → {out_path}")

    # ── Copy to clipboard ──
    subprocess.run(["pbcopy"], input=full_text.encode("utf-8"), check=True)
    print("  Copied to clipboard ✓")

    # ── Open Claude.ai ──
    subprocess.run(["open", CLAUDE_URL])
    print(f"  Opened {CLAUDE_URL}")
    print()
    print("  Paste (⌘V) into the Claude message box and send.")
    print()


if __name__ == "__main__":
    main()
