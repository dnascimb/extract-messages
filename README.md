# Apple Messages Exporter

Export iMessage conversations from macOS into HTML, JSON, and plain-text formats, with attachments copied locally.

## Requirements

- macOS only
- Python 3.10+
- No third-party dependencies
- Terminal app must have **Full Disk Access** (System Settings → Privacy & Security → Full Disk Access)

## Usage

```bash
# List all contacts in the database
python3 export_messages.py --list-contacts

# Export by phone number or email (all formats + attachments)
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
```

## Output

Each export is written to `~/Desktop/messages_export/<contact>/` by default:

```
<output_dir>/<contact>/
├── messages.html        # iMessage-style visual transcript
├── messages.json        # full structured data
├── messages.txt         # plain-text transcript (reactions excluded)
└── attachments/         # copied media and files
```

Open `messages.html` in a browser for a visual, bubble-style transcript.

## Notes

- **Phone number format matters** — use the full E.164 format (e.g. `+14155550100`). Run `--list-contacts` to see exactly how identifiers are stored.
- **Missing attachments** — media synced via iCloud won't be available locally until you scroll to it in the Messages app. The script will warn you about any it couldn't find.
- **Group chats** — group chat identifiers are UUIDs, visible via `--list-contacts`.
- **Permission denied** — if the script can't open the database, grant Full Disk Access to your terminal in System Settings.
