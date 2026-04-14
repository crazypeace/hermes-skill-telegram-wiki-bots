#!/usr/bin/env python3
"""
Wiki Admin Bot - Telegram bot that ingests content into the 233boy LLM wiki.
When @mentioned in a group, the bot passes the full message to the llm_wiki skill
via the API server, which processes and catalogs the content automatically.
"""

import requests
import re
import time
import json
import logging
import sys
import signal
from pathlib import Path

# ─── Config ───────────────────────────────────────────────────────────
BOT_TOKEN="1234567890:AAHkMpXv2nQrWsYd8bJtLfCeUo9GiN1KmZw"
API_URL = "http://127.0.0.1:8642/v1/chat/completions"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

BOT_USERNAME = ""
BOT_ID = 0

POLL_TIMEOUT = 30
REQUEST_TIMEOUT = 300  # 5 min for ingestion (may be slow)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("wiki_admin_bot")

# ─── Telegram helpers ─────────────────────────────────────────────────

def tg_api(method: str, data: dict = None, timeout: int = POLL_TIMEOUT) -> dict:
    """Call Telegram Bot API."""
    url = f"{TELEGRAM_API}/{method}"
    try:
        r = requests.post(url, json=data or {}, timeout=timeout)
        result = r.json()
        if not result.get("ok"):
            log.warning("Telegram API error [%s]: %s", method, result.get("description"))
        return result
    except requests.exceptions.Timeout:
        return {"ok": False, "description": "timeout"}
    except Exception as e:
        log.error("Telegram API exception [%s]: %s", method, e)
        return {"ok": False, "description": str(e)}


def get_me() -> dict:
    """Get bot info."""
    return tg_api("getMe")


def send_message(chat_id: int, text: str, reply_to: int = None,
                 thread_id: int = None) -> dict:
    """Send a plain text message (no parse_mode)."""
    data = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_to:
        data["reply_to_message_id"] = reply_to
    if thread_id:
        data["message_thread_id"] = thread_id
    return tg_api("sendMessage", data)


def send_chat_action(chat_id: int, action: str = "typing", thread_id: int = None):
    """Show typing indicator."""
    data = {"chat_id": chat_id, "action": action}
    if thread_id:
        data["message_thread_id"] = thread_id
    tg_api("sendChatAction", data, timeout=5)


def react_to_message(chat_id: int, message_id: int, emoji: str = "👀"):
    """React to a message with an emoji."""
    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "reaction": [{"type": "emoji", "emoji": emoji}],
    }
    tg_api("setMessageReaction", data, timeout=5)


def get_updates(offset: int = None) -> list:
    """Long-poll for updates."""
    data = {"timeout": POLL_TIMEOUT}
    if offset:
        data["offset"] = offset
    result = tg_api("getUpdates", data, timeout=POLL_TIMEOUT + 5)
    if result.get("ok"):
        return result.get("result", [])
    return []


# ─── Message processing ───────────────────────────────────────────────

def should_respond(msg: dict) -> bool:
    """Respond when @mentioned in a group, or when /ingest command is used."""
    text = msg.get("text", "")
    entities = msg.get("entities", [])

    # Check for /ingest command (with or without @bot)
    if text.strip().startswith("/ingest"):
        return True

    # In groups: only respond when bot is @mentioned
    for ent in entities:
        if ent["type"] == "mention":
            mention = text[ent["offset"]:ent["offset"] + ent["length"]]
            if mention.lower() == f"@{BOT_USERNAME}".lower():
                return True
        if ent["type"] == "text_mention":
            user = ent.get("user", {})
            if user.get("id") == BOT_ID:
                return True

    return False


def extract_content(msg: dict) -> str:
    """Extract full message content, stripping @mention or /ingest command prefix."""
    text = msg.get("text", "").strip()
    entities = msg.get("entities", [])

    # Strip /ingest command (with optional @bot suffix like /ingest@botname)
    if text.startswith("/ingest"):
        # Remove /ingest or /ingest@botname
        text = re.sub(r"^/ingest(@\S+)?\s*", "", text)
        return text.strip()

    # Remove the @bot_username mention entity
    for ent in sorted(entities, key=lambda e: e["offset"], reverse=True):
        if ent["type"] == "mention":
            mention = text[ent["offset"]:ent["offset"] + ent["length"]]
            if mention.lower() == f"@{BOT_USERNAME}".lower():
                text = text[:ent["offset"]] + text[ent["offset"] + ent["length"]:]
        if ent["type"] == "text_mention":
            user = ent.get("user", {})
            if user.get("id") == BOT_ID:
                text = text[:ent["offset"]] + text[ent["offset"] + ent["length"]:]

    # Clean up leftover whitespace
    return text.strip()


def ingest_content(content: str) -> bool:
    """Send content to llm_wiki via API server. Returns True on success."""
    # Wrap content in Ingest command. Use raw string to avoid escape issues.
    command = f'/llm-wiki Ingest """{content}"""'

    payload = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是一个 wiki 内容摄入助手。使用 llm-wiki skill 把用户提供的内容摄入 wiki 知识库。"
                    "完成后只需回复「处理完成」，不要输出其他内容。"
                ),
            },
            {"role": "user", "content": command},
        ]
    }
    try:
        log.info("Sending Ingest command (%d chars content)", len(content))
        r = requests.post(API_URL, json=payload, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        log.info("API response: %s", str(data.get("choices", [{}])[0].get("message", {}).get("content", ""))[:200])
        return True
    except requests.exceptions.Timeout:
        log.error("API timeout for ingestion")
        return False
    except Exception as e:
        log.error("API ingestion error: %s", e)
        return False


# ─── Main loop ────────────────────────────────────────────────────────

def main():
    global BOT_USERNAME, BOT_ID

    # Get bot info
    me = get_me()
    if not me.get("ok"):
        log.error("Failed to get bot info. Check token.")
        sys.exit(1)

    BOT_USERNAME = me["result"]["username"]
    BOT_ID = me["result"]["id"]
    log.info("Bot started: @%s (ID: %s)", BOT_USERNAME, BOT_ID)

    # Clear any pending updates from previous sessions to avoid conflicts
    try:
        clear_result = tg_api("deleteWebhook", {"drop_pending_updates": True}, timeout=10)
        if clear_result.get("ok"):
            log.info("Cleared pending updates")
    except Exception:
        pass

    # Register slash commands
    set_commands_result = tg_api("setMyCommands", {
        "commands": [
            {"command": "ingest", "description": "摄入内容到 wiki 知识库"}
        ]
    })
    if set_commands_result.get("ok"):
        log.info("Registered /ingest command")
    else:
        log.warning("Failed to register commands: %s", set_commands_result.get("description"))

    # Graceful shutdown
    running = True
    def shutdown(sig, frame):
        nonlocal running
        log.info("Shutting down...")
        running = False
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    offset = None
    log.info("Listening for messages...")

    while running:
        try:
            updates = get_updates(offset)
            for update in updates:
                offset = update["update_id"] + 1

                msg = update.get("message") or update.get("channel_post")
                if not msg:
                    continue

                chat_id = msg["chat"]["id"]
                msg_id = msg["message_id"]
                thread_id = msg.get("message_thread_id")

                if not should_respond(msg):
                    continue

                content = extract_content(msg)
                if not content:
                    send_message(chat_id, "请在 @bot 后面附上要摄入的内容", reply_to=msg_id, thread_id=thread_id)
                    continue

                log.info("Ingesting content from chat %s (%d chars)", chat_id, len(content))

                # React to show we received the message
                react_to_message(chat_id, msg_id)

                # Show typing while processing
                send_chat_action(chat_id, thread_id=thread_id)

                # Ingest content into wiki
                success = ingest_content(content)

                if success:
                    send_message(chat_id, "处理完成", reply_to=msg_id, thread_id=thread_id)
                else:
                    send_message(chat_id, "处理失败，请检查 API server 日志", reply_to=msg_id, thread_id=thread_id)

                log.info("Ingestion %s for chat %s", "completed" if success else "failed", chat_id)

        except requests.exceptions.ConnectionError:
            log.warning("Connection lost, retrying in 5s...")
            time.sleep(5)
        except Exception as e:
            log.error("Unexpected error: %s", e)
            time.sleep(2)


if __name__ == "__main__":
    main()
