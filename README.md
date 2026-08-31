# imessage-bridge

A two-way bridge between macOS Messages and a Google Drive folder, so an assistant (or any tool) can read and send your iMessages without any Apple API access. Runs entirely on an always-on Mac that is signed into your Apple ID in Messages.

## How it works

macOS Messages stores every iMessage/SMS in a local SQLite database at `~/Library/Messages/chat.db`. This bridge runs two loops on that Mac:

```
READ  chat.db --(copy, query new rows)--> inbox.json in Google Drive
SEND  outbox.json in Google Drive --(AppleScript)--> Messages.app --> iMessage
```

- **READ**: `bridge.py` copies `chat.db` (plus its WAL files) to a temp dir - never touching the live database - reads every message newer than the last one it saw, and appends them to `inbox.json` inside a folder synced by Google Drive for desktop. Anything that can read that Drive folder sees your texts within a sync cycle.
- **SEND**: drop JSON into `outbox.json` in the same folder. The bridge picks it up, sends each message through Messages.app via AppleScript (from your number, syncing to all your devices like a normal text), appends the result to `outbox_done.json`, and archives the outbox.

One launchd agent keeps `bridge.py` running (KeepAlive + RunAtLoad). The Mac must stay awake and signed in; an always-on Mac Mini is ideal.

## Files

| File | Purpose |
|---|---|
| `bridge.py` | The whole bridge: read loop + send loop. Python 3 stdlib only. |
| `config.example.json` | Config template; copy to `config.json`. |
| `com.imessagebridge.plist` | launchd agent template (`__HOME__` is filled in by the installer). |
| `install.sh` | Copies files, writes config, installs and loads the launchd agent. |

## Setup

Run on the Mac that hosts Messages (your Mac Mini):

```bash
git clone <this repo>
cd imessage-bridge
./install.sh
```

Then the two manual steps the installer prints:

1. **Full Disk Access** (mandatory - without it, reading `chat.db` fails with an sqlite authorization error): `System Settings > Privacy & Security > Full Disk Access > +`, press `Cmd+Shift+G`, enter `/usr/bin/python3`, add and enable it. Add Terminal/iTerm too if you want to test by hand.
2. **Point the config at Drive**: edit `~/imessage-bridge/config.json` and set `drive_folder`. With Google Drive for desktop it looks like: `~/Library/CloudStorage/GoogleDrive-you@gmail.com/My Drive/imessage-bridge`. Then restart: `launchctl kickstart -k gui/$(id -u)/com.imessagebridge`

Test before trusting launchd:

```bash
/usr/bin/python3 ~/imessage-bridge/bridge.py ~/imessage-bridge/config.json
```

Logs: `~/Library/Logs/imessage-bridge.log`

## The contract (how a consumer talks to the bridge)

**Reading** - parse `inbox.json`:

```json
{
  "exported_at": "2026-08-30T23:40:00+00:00",
  "messages": [
    {
      "rowid": 10234,
      "guid": "...",
      "conversation_id": "+15551234567",
      "conversation_name": null,
      "from": "+15551234567",
      "is_from_me": false,
      "text": "see you at 7",
      "sent_at": "2026-08-30T23:39:41+00:00"
    }
  ]
}
```

`conversation_id` groups a thread; `from` is `me` for your own messages. Tapbacks/reactions and attachments have no plain text and appear as `[attachment, reaction, or rich content]`. The file holds the most recent `inbox_keep` messages; on first run the bridge starts from the newest message and does not backfill history.

**Sending** - write `outbox.json`:

```json
{
  "messages": [
    {"id": "any-unique-string", "to": "+15551234567", "body": "running 10 min late"}
  ]
}
```

`to` accepts a phone number or an Apple-ID email. Results land in `outbox_done.json` with `status: sent` or `error` per message, and the outbox file is renamed to `outbox.json.processed-<timestamp>`.

## Security notes - read these

- `chat.db` contains every message you have ever sent or received. The exported `inbox.json` is a slice of that, in plaintext, synced to Google Drive. Keep the Drive folder private, do not share it, and remember it exists before sharing your Drive or screen.
- The Google account on that Drive folder now gates access to your texts. 2FA on that account is not optional.
- Anyone who can write to the Drive folder can send iMessages as you via `outbox.json`. Treat write access as the ability to impersonate you.
- The bridge only ever reads `chat.db` (it works on a copy); the only writes it makes are inside the bridge folder.
- Uninstall: `launchctl bootout gui/$(id -u)/com.imessagebridge`, delete `~/imessage-bridge`, `~/Library/LaunchAgents/com.imessagebridge.plist`, and the Drive folder.

## Limitations

- Group chats: sending targets one recipient at a time. Messages AppleScripted to a phone number land in your existing 1:1 thread with that person.
- Rich content (photos, reactions, voice notes) is exported as a placeholder, not the content itself.
- `participant`/`buddy` AppleScript terminology varies by macOS version; the script tries both, but verify sending once after install.
