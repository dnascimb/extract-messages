# Apple Messages Exporter

Export iMessage conversations from macOS into HTML, JSON, and plain-text formats, with attachments and rich link previews.

## Requirements

- macOS only
- Python 3.10+
- No third-party Python dependencies (stdlib only)
- [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) for YouTube audio download (`brew install yt-dlp`) — optional
- Terminal app must have **Full Disk Access** (System Settings → Privacy & Security → Full Disk Access)

## Usage

```bash
# List all contacts in the database
python3 export_messages.py --list-contacts

# Export by phone number or email (all formats + attachments + link previews)
python3 export_messages.py "+14155550100"
python3 export_messages.py "someone@icloud.com"

# Custom output directory
python3 export_messages.py "+14155550100" --output ~/Desktop/export

# Only generate one format
python3 export_messages.py "+14155550100" --format html
python3 export_messages.py "+14155550100" --format json
python3 export_messages.py "+14155550100" --format txt

# Skip tapback reactions
python3 export_messages.py "+14155550100" --no-reactions

# Skip link preview fetching (faster, offline-friendly)
python3 export_messages.py "+14155550100" --no-link-previews
```

## Output

Each export is written to `~/Desktop/messages_export/<contact>/` by default:

```
<output_dir>/<contact>/
├── messages.html          # iMessage-style visual transcript
├── messages.json          # full structured data
├── messages.txt           # plain-text transcript (reactions excluded)
├── attachments/           # copied media and files from the conversation
└── previews/              # downloaded link preview assets (images, audio)
```

Open `messages.html` in a browser for a visual, bubble-style transcript.

## HTML Features

- **iMessage-style bubbles** — blue for iMessage, green for SMS, gray for received; dark mode supported
- **Inline media** — images, video, and audio attachments render directly in the conversation
- **Image lightbox** — click any image to view it full-screen
- **Link preview cards** — URLs are replaced with rich preview cards (title, thumbnail, description)
- **Suno** — cover art, audio player, and lyrics embedded directly; songs play inline with a collapsible lyrics view; lyrics saved locally to `previews/lyrics_*.txt`
- **YouTube** — thumbnail and audio extracted via `yt-dlp` and embedded as an inline player
- **Responsive** — adapts to desktop and mobile screen sizes

## Development

Run the test suite (no dependencies beyond stdlib):

```bash
python3 test_export.py
```

Tests cover: `attributedBody` decoding, timestamp conversion, incremental fetch logic, the empty-text backfill patch, the always-written `messages.json` invariant, and Suno lyrics extraction (same-push and split-push RSC cases).

## Notes

- **Phone number format matters** — use full E.164 format (e.g. `+14155550100`). Run `--list-contacts` to see exactly how identifiers are stored.
- **Missing attachments** — media synced via iCloud won't be available locally until you scroll to it in the Messages app. The script will warn you about any it couldn't find.
- **Link previews are cached** — preview assets are saved to `previews/` on first run. Re-running won't re-download existing files.
- **Group chats** — group chat identifiers are UUIDs, visible via `--list-contacts`.
- **Permission denied** — if the script can't open the database, grant Full Disk Access to your terminal in System Settings.
