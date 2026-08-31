#!/usr/bin/env python3
"""
imessage-bridge: two-way bridge between macOS Messages (chat.db) and a
Google Drive desktop-synced folder.

READ path : copies ~/Library/Messages/chat.db, exports new messages to
            <drive_folder>/inbox.json
SEND path : polls <drive_folder>/outbox.json, sends each entry through
            Messages.app via AppleScript, records results in
            outbox_done.json, then clears the outbox.

Runs forever as a launchd agent. No third-party dependencies.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone

APPLE_EPOCH = 978307200  # seconds between 1970-01-01 and 2001-01-01
CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")
DEFAULT_CONFIG = os.path.expanduser("~/imessage-bridge/config.json")


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def load_config(path):
    with open(path) as f:
        cfg = json.load(f)
    cfg["drive_folder"] = os.path.expanduser(cfg["drive_folder"])
    return cfg


def apple_time_to_iso(raw):
    """chat.db stores dates relative to 2001-01-01; modern macOS uses
    nanoseconds, older rows may be plain seconds."""
    if raw is None:
        return None
    seconds = raw / 1_000_000_000 if raw > 100_000_000_000_000 else raw
    return datetime.fromtimestamp(seconds + APPLE_EPOCH, tz=timezone.utc).isoformat()


def export_new_messages(cfg, state):
    """Copy chat.db (avoids locking the live database), read every message
    newer than the last exported ROWID, append them to inbox.json."""
    inbox_path = os.path.join(cfg["drive_folder"], "inbox.json")
    with tempfile.TemporaryDirectory() as tmp:
        for suffix in ("", "-wal", "-shm"):
            src = CHAT_DB + suffix
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(tmp, "chat.db" + suffix))
        conn = sqlite3.connect(os.path.join(tmp, "chat.db"))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT m.ROWID      AS rowid,
                   m.guid       AS guid,
                   m.text       AS text,
                   m.is_from_me AS is_from_me,
                   m.date       AS date,
                   h.id         AS handle,
                   c.chat_identifier AS conversation_id,
                   c.display_name    AS conversation_name
            FROM message m
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            JOIN chat c ON c.ROWID = cmj.chat_id
            WHERE m.ROWID > ?
            ORDER BY m.ROWID ASC
            LIMIT ?
            """,
            (state["last_rowid"], cfg.get("export_batch_limit", 500)),
        ).fetchall()
        conn.close()

    if not rows:
        return

    messages = []
    for r in rows:
        state["last_rowid"] = max(state["last_rowid"], r["rowid"])
        messages.append({
            "rowid": r["rowid"],
            "guid": r["guid"],
            "conversation_id": r["conversation_id"],
            "conversation_name": r["conversation_name"] or None,
            "from": "me" if r["is_from_me"] else (r["handle"] or "unknown"),
            "is_from_me": bool(r["is_from_me"]),
            "text": r["text"] if r["text"] is not None else "[attachment, reaction, or rich content]",
            "sent_at": apple_time_to_iso(r["date"]),
        })

    inbox = {"exported_at": datetime.now(timezone.utc).isoformat(), "messages": []}
    if os.path.exists(inbox_path):
        try:
            with open(inbox_path) as f:
                inbox = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass  # corrupt/partial read while Drive syncs - start fresh
    inbox["exported_at"] = datetime.now(timezone.utc).isoformat()
    inbox["messages"].extend(messages)
    inbox["messages"] = inbox["messages"][-cfg.get("inbox_keep", 2000):]  # keep file bounded

    tmp_out = inbox_path + ".tmp"
    with open(tmp_out, "w") as f:
        json.dump(inbox, f, indent=2)
    os.replace(tmp_out, inbox_path)  # atomic, so Drive never syncs half a file
    log(f"exported {len(messages)} new message(s), last_rowid={state['last_rowid']}")


APPLESCRIPT = """
on run argv
    set theRecipient to item 1 of argv
    set theBody to item 2 of argv
    tell application "Messages"
        set svc to 1st account whose service type = iMessage
        try
            set theBuddy to participant theRecipient of svc
        on error
            set theBuddy to buddy theRecipient of svc
        end try
        send theBody to theBuddy
    end tell
end run
"""


def send_one(recipient, body):
    """Send one iMessage via Messages.app. Returns (ok, error)."""
    try:
        subprocess.run(
            ["osascript", "-e", APPLESCRIPT, recipient, body],
            check=True, capture_output=True, text=True, timeout=30,
        )
        return True, None
    except subprocess.CalledProcessError as e:
        return False, (e.stderr or str(e)).strip()
    except subprocess.TimeoutExpired:
        return False, "osascript timed out"


def process_outbox(cfg):
    outbox_path = os.path.join(cfg["drive_folder"], "outbox.json")
    done_path = os.path.join(cfg["drive_folder"], "outbox_done.json")
    if not os.path.exists(outbox_path):
        return
    try:
        with open(outbox_path) as f:
            outbox = json.load(f)
    except (json.JSONDecodeError, OSError):
        return  # Drive may still be syncing the file; try again next cycle
    pending = outbox.get("messages", [])
    if not pending:
        return

    results = []
    for item in pending:
        ok, err = send_one(item["to"], item["body"])
        results.append({
            "id": item.get("id"),
            "to": item["to"],
            "body": item["body"],
            "status": "sent" if ok else "error",
            "error": err,
            "processed_at": datetime.now(timezone.utc).isoformat(),
        })
        log(f"send -> {item['to']}: {'sent' if ok else 'ERROR ' + str(err)}")

    done = {"messages": []}
    if os.path.exists(done_path):
        try:
            with open(done_path) as f:
                done = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    done["messages"].extend(results)
    with open(done_path + ".tmp", "w") as f:
        json.dump(done, f, indent=2)
    os.replace(done_path + ".tmp", done_path)

    # clear the outbox only after results are safely recorded
    os.replace(outbox_path, outbox_path + f".processed-{int(time.time())}")


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONFIG
    cfg = load_config(config_path)
    os.makedirs(cfg["drive_folder"], exist_ok=True)

    state_path = os.path.join(cfg["drive_folder"], ".bridge_state.json")
    state = {"last_rowid": 0}
    if os.path.exists(state_path):
        with open(state_path) as f:
            state = json.load(f)
    if state["last_rowid"] == 0:
        # first run: start from the newest message, not the whole history
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy2(CHAT_DB, os.path.join(tmp, "chat.db"))
            conn = sqlite3.connect(os.path.join(tmp, "chat.db"))
            state["last_rowid"] = conn.execute("SELECT IFNULL(MAX(ROWID),0) FROM message").fetchone()[0]
            conn.close()
        log(f"first run - starting from current newest message (rowid {state['last_rowid']})")

    interval = cfg.get("poll_interval_seconds", 60)
    log(f"bridge running; drive folder: {cfg['drive_folder']}; interval {interval}s")
    while True:
        try:
            export_new_messages(cfg, state)
            process_outbox(cfg)
            with open(state_path, "w") as f:
                json.dump(state, f)
        except Exception:
            log("cycle failed:\n" + traceback.format_exc())
        time.sleep(interval)


if __name__ == "__main__":
    main()
