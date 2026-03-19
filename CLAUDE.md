# Apple Messages Exporter

## Project Overview
A Python script that exports iMessage conversations from macOS's local SQLite database (`~/Library/Messages/chat.db`) into HTML, JSON, and plain-text formats, with attachment copying and rich link previews (including inline Suno and YouTube audio).

## Key Files
- `export_messages.py` — the main (and only) script
- `test_export.py` — unit tests (stdlib only, no external deps)

## Running Tests
```bash
python3 test_export.py
```
Run after any non-trivial change. Tests cover: `attributedBody` decoding, timestamp conversion, `patch_missing_text`, incremental fetch, `messages.json` always-written invariant, and Suno lyrics extraction (same-push and split-push RSC cases).

## How to Run
```bash
# List all contacts in the database
python3 export_messages.py --list-contacts

# Export a contact by phone number or email
python3 export_messages.py "+14155550100"
python3 export_messages.py "someone@icloud.com"

# Custom output directory
python3 export_messages.py "+14155550100" --output ~/Desktop/export

# Only generate one format
python3 export_messages.py "+14155550100" --format html

# Skip tapback reactions
python3 export_messages.py "+14155550100" --no-reactions

# Skip link preview fetching (faster, offline)
python3 export_messages.py "+14155550100" --no-link-previews
```

## Requirements
- macOS only
- Python 3.10+ (uses `list[dict]` and `Path | None` type hints)
- No third-party Python dependencies — only stdlib (`sqlite3`, `shutil`, `json`, `pathlib`, `argparse`, `re`, `urllib`, `subprocess`)
- `yt-dlp` CLI for YouTube support (`brew install yt-dlp`) — optional
- Terminal must have **Full Disk Access** (System Settings → Privacy & Security → Full Disk Access)

## Database Schema Notes
The script reads from `~/Library/Messages/chat.db`. Key tables:
- `message` — every message; `is_from_me` flag; `date` is nanoseconds since 2001-01-01 UTC
- `chat` — a conversation thread; identified by `chat_identifier` (phone number or email)
- `handle` — a contact's address (phone/email)
- `attachment` — metadata for media/files; `filename` is a `~/Library/Messages/Attachments/...` path
- Join tables: `chat_message_join`, `chat_handle_join`, `message_attachment_join`

Timestamp conversion:
```python
unix_ts = apple_nanoseconds / 1_000_000_000 + 978307200  # 978307200 = 2001-01-01 in Unix epoch
```

## Output Structure
```
<output_dir>/<contact>/
├── messages.html          # iMessage-style visual transcript
├── messages.json          # full structured data
├── messages.txt           # plain-text transcript (reactions excluded)
├── attachments/           # copied media and files from the conversation
│   ├── image.jpg
│   ├── video.mov
│   └── ...
└── previews/              # downloaded link preview assets
    ├── img_<hash>.jpeg    # OG cover images (Suno, etc.)
    ├── audio_<hash>.mp3   # Suno audio files
    └── yt_<id>.mp3        # YouTube audio files
```

## Reaction / Tapback Types
Stored as `associated_message_type` on the `message` row:

| Value | Meaning           |
|-------|-------------------|
| 0     | Normal message    |
| 2000  | ❤️ Loved          |
| 2001  | 👍 Liked          |
| 2002  | 👎 Disliked       |
| 2003  | 😂 Laughed at     |
| 2004  | ‼️ Emphasized     |
| 2005  | ❓ Questioned     |
| 3000–3005 | Removed versions of the above |

## Common Issues
- **Permission denied on chat.db** → grant Full Disk Access to Terminal / iTerm
- **No messages found for contact** → run `--list-contacts`; the identifier must match exactly (e.g. `+1` prefix matters)
- **Attachments missing** → messages synced via iCloud won't have local files until you scroll to them in the Messages app
- **Group chats** → `chat_identifier` for group chats is a UUID string, not a phone number; visible in `--list-contacts` output

## Link Preview System
- `fetch_og(url)` — fetches Open Graph metadata via urllib; extracts Suno audio CDN URL from page HTML
- `fetch_youtube(url, preview_dir)` — uses `yt-dlp --dump-json` for metadata and `-x --audio-format mp3` for audio
- `fetch_link_previews(messages, out_dir)` — orchestrates fetching for all unique URLs; caches assets to `previews/`; annotates each message with a `link_previews` list
- Preview assets are keyed by `abs(hash(url)) & 0xFFFFFF` for non-YouTube, or `yt_{video_id}` for YouTube
- YouTube playlists/channels get a preview card but no audio download

## Potential Enhancements
- Group chat support (query by `chat.ROWID` instead of `handle.id`)
- Date range filtering (`--after`, `--before` flags)
- iCloud attachment auto-download via `brctl` CLI
- CSV export format
- Search / keyword filtering across all conversations
- Parallel link preview fetching (currently sequential)
