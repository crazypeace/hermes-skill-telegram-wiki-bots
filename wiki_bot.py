#!/usr/bin/env python3
"""
Wiki Bot - Telegram bot that queries the 233boy LLM wiki via the Hermes API server.
"""

import requests
import time
import json
import logging
import html
import re
import sys
import signal
import os
import fcntl
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# ─── Config ───────────────────────────────────────────────────────────
BOT_TOKEN="8752294555:AAHbNSEABE3ji_Cmc58D1LLGLc4cWp6aBYg"
API_URL = "http://127.0.0.1:8642/v1/chat/completions"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Bot username (will be fetched on startup)
BOT_USERNAME = ""
BOT_ID = 0

# Output directory for .md files
OUTPUT_DIR = Path("/root/wiki_bot_output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

POLL_TIMEOUT = 30  # seconds

# 线程池和全局锁（用于串行化 wiki 操作）
MAX_WORKERS = 1
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
is_processing = False  # True 表示正在处理一个 query
REQUEST_TIMEOUT = 300  # seconds for API calls

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("wiki_bot")

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
                 parse_mode: str = "HTML", thread_id: int = None) -> dict:
    """Send a message, with optional reply."""
    data = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        data["parse_mode"] = parse_mode
    if reply_to:
        data["reply_to_message_id"] = reply_to
    if thread_id:
        data["message_thread_id"] = thread_id
    return tg_api("sendMessage", data)


def send_document(chat_id: int, file_path: str, caption: str = None,
                  reply_to: int = None, thread_id: int = None) -> dict:
    """Send a .md file as document attachment."""
    url = f"{TELEGRAM_API}/sendDocument"
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    if reply_to:
        data["reply_to_message_id"] = reply_to
    if thread_id:
        data["message_thread_id"] = thread_id
    try:
        with open(file_path, "rb") as f:
            files = {"document": (os.path.basename(file_path), f, "text/markdown")}
            r = requests.post(url, data=data, files=files, timeout=30)
            result = r.json()
            if not result.get("ok"):
                log.warning("sendDocument error: %s", result.get("description"))
            return result
    except Exception as e:
        log.error("sendDocument exception: %s", e)
        return {"ok": False, "description": str(e)}


def save_markdown(query: str, answer: str) -> str:
    """Save query answer as a .md file, return the file path."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Sanitize query for filename
    safe_query = re.sub(r'[^\w\u4e00-\u9fff-]', '_', query)[:40].strip('_')
    filename = f"{timestamp}_{safe_query}.md"
    filepath = OUTPUT_DIR / filename
    content = f"""---
query: "{query}"
time: {datetime.now().isoformat()}
---

# {query}

{answer}
"""
    filepath.write_text(content, encoding="utf-8")
    return str(filepath)


def send_chat_action(chat_id: int, action: str = "typing", thread_id: int = None):
    """Show typing indicator."""
    data = {"chat_id": chat_id, "action": action}
    if thread_id:
        data["message_thread_id"] = thread_id
    tg_api("sendChatAction", data, timeout=5)


def react_to_message(chat_id: int, message_id: int, emoji: str = "👀"):
    """React to a message with an emoji.
    
    Only certain emojis are valid Telegram reactions.
    Common valid ones: 👍 👎 ❤️ 🔥 🎉 😱 😢 🤔 😍 🤡 💩 🥳 🤷 👀 
    """
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
    """Decide if the bot should respond to this message."""
    text = msg.get("text", "")
    entities = msg.get("entities", [])

    # Always respond to private chats
    if msg["chat"].get("type") == "private":
        return bool(text.strip())

    # In groups: only respond when bot is mentioned or message is a reply to the bot
    # Check for @bot_username mention or text_mention (from popup menu selection)
    for ent in entities:
        if ent["type"] == "mention":
            mention = text[ent["offset"]:ent["offset"] + ent["length"]]
            if mention.lower() == f"@{BOT_USERNAME}".lower():
                return True
        if ent["type"] == "text_mention":
            user = ent.get("user", {})
            if user.get("id") == BOT_ID:
                return True
        if ent["type"] == "bot_command":
            cmd = text[ent["offset"]:ent["offset"] + ent["length"]]
            if cmd.lower().startswith("/query"):
                return True

    return False


def extract_query(msg: dict) -> str:
    """Extract the actual query text from a message."""
    text = msg.get("text", "").strip()

    # Remove bot command prefix (/query)
    for cmd in ["/query", f"/query@{BOT_USERNAME}"]:
        if text.lower().startswith(cmd.lower()):
            text = text[len(cmd):].strip()
            break

    # Remove @mention
    if BOT_USERNAME:
        text = re.sub(rf"@{re.escape(BOT_USERNAME)}\s*", "", text, flags=re.IGNORECASE).strip()

    return text


def query_wiki(query: str) -> str:
    """Send query to the LLM wiki via API server."""

    payload = {
        "messages": [
            {"role": "system", "content": "你是一个 wiki 查询助手。只回答用户的问题，不要建议创建 wiki 页面、更新 index 或修改任何文件。使用 Wiki 内容完整回答用户问题，不要出现'请参考...'或'基于...页面'这类引导式语句。直接给出自包含的完整答案，必要时将引用内容自然地融入答案中。回答结束后停止。"},
            {"role": "user", "content": f"/llm-wiki query {query}"}
        ]
    }
    try:
        r = requests.post(API_URL, json=payload, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        return content
    except requests.exceptions.Timeout:
        return "⏳ 查询超时，请稍后重试。"
    except Exception as e:
        log.error("API query error: %s", e)
        return f"❌ 查询出错: {e}"


def format_for_telegram(text: str) -> str:
    """Convert markdown-ish wiki response to Telegram HTML."""

    # First, protect code blocks from HTML escaping
    code_blocks = []
    def save_code_block(m):
        code_blocks.append(m.group(0))
        return f"\x00CODEBLOCK{len(code_blocks)-1}\x00"

    # Save triple-backtick code blocks
    text = re.sub(r"```\w*\n?.*?```", save_code_block, text, flags=re.DOTALL)

    # Save inline code
    inline_codes = []
    def save_inline(m):
        inline_codes.append(m.group(1))
        return f"\x00INLINE{len(inline_codes)-1}\x00"
    text = re.sub(r"`([^`]+)`", save_inline, text)

    # HTML-escape the remaining text (prevents <wget from being parsed as tag)
    text = html.escape(text, quote=False)

    # Restore inline code (now escaped inside <code> tags)
    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00INLINE{i}\x00", f"<code>{code}</code>")

    # Restore code blocks (content NOT escaped — preserves bash syntax)
    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00CODEBLOCK{i}\x00", block)

    # Convert ```code blocks``` to <pre>
    text = re.sub(r"```(\w*)\n?(.*?)```", r"<pre>\2</pre>", text, flags=re.DOTALL)

    # Convert markdown tables to <pre> blocks
    lines = text.split('\n')
    result_lines = []
    table_lines = []
    in_table = False
    for line in lines:
        stripped = line.strip()
        if '|' in stripped and stripped.startswith('|') and stripped.endswith('|'):
            table_lines.append(line)
            in_table = True
        else:
            if in_table:
                # Flush table — skip separator rows, format as text
                table_text = []
                for tl in table_lines:
                    tl_stripped = tl.strip()
                    # Skip separator rows like |---|---|
                    if re.match(r'^\|[\s\-:|]+\|$', tl_stripped):
                        continue
                    # Clean up pipe separators
                    cells = [c.strip() for c in tl_stripped.split('|')]
                    # Remove empty first/last from leading/trailing pipes
                    if cells and cells[0] == '':
                        cells = cells[1:]
                    if cells and cells[-1] == '':
                        cells = cells[:-1]
                    if cells:
                        table_text.append(' | '.join(cells))
                if table_text:
                    result_lines.append(f"<pre>{chr(10).join(table_text)}</pre>")
                table_lines = []
                in_table = False
            result_lines.append(line)
    if in_table:
        table_text = []
        for tl in table_lines:
            tl_stripped = tl.strip()
            if re.match(r'^\|[\s\-:|]+\|$', tl_stripped):
                continue
            cells = [c.strip() for c in tl_stripped.split('|')]
            if cells and cells[0] == '':
                cells = cells[1:]
            if cells and cells[-1] == '':
                cells = cells[:-1]
            if cells:
                table_text.append(' | '.join(cells))
        if table_text:
            result_lines.append(f"<pre>{chr(10).join(table_text)}</pre>")
    text = '\n'.join(result_lines)

    # Clean up wikilinks [[xxx]] → xxx
    text = re.sub(r'\[\[([^\]]+)\]\]', r'\1', text)

    # Convert markdown headers to bold
    text = re.sub(r"^#{1,3}\s*(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # Convert **bold** to <b>
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)

    # Convert remaining `code` to <code> (already handled above — this line kept for edge cases)
    # text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)

    # Convert [text](url) to "text: url"
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'\1: \2', text)

    # Convert bullet points
    text = re.sub(r"^[-•]\s*", "• ", text, flags=re.MULTILINE)

    # Truncate if too long (Telegram limit is 4096 chars)
    if len(text) > 4000:
        text = text[:3900] + "\n\n...(回复过长，已截断)"

    return text


# ─── Main loop ────────────────────────────────────────────────────────

def main():
    # 用于跟踪待处理的异步任务
    pending_futures = {}  # future -> (chat_id, msg_id, query)

    global BOT_USERNAME, BOT_ID

    # Get bot info
    me = get_me()
    if not me.get("ok"):
        log.error("Failed to get bot info. Check token.")
        sys.exit(1)

    BOT_USERNAME = me["result"]["username"]
    BOT_ID = me["result"]["id"]
    log.info("Bot started: @%s (ID: %s)", BOT_USERNAME, BOT_ID)

    # Register slash command
    cmds = [{"command": "query", "description": "查询 wiki 知识库"}]
    r = tg_api("setMyCommands", {"commands": cmds})
    if r.get("ok"):
        log.info("Registered /query command")
    else:
        log.warning("Failed to register commands: %s", r)

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
        global is_processing

        try:
            # 1. 长轮询获取 updates（同步阻塞 0-30s）
            updates = get_updates(offset)

            # 2. 先检查并处理已完成的 futures（解锁以便接受新消息）
            done_futures = [f for f in pending_futures if f.done()]
            for f in done_futures:
                chat_id, msg_id, query, thread_id = pending_futures.pop(f)
                try:
                    answer = f.result()
                    md_path = save_markdown(query, answer)
                    formatted = format_for_telegram(answer)
                    result = send_message(chat_id, formatted, reply_to=msg_id, thread_id=thread_id)
                    if not result.get("ok"):
                        log.warning("HTML send failed, retrying as plain text")
                        plain = answer[:4000] + ("\n...(回复过长，已截断)" if len(answer) > 4000 else "")
                        send_message(chat_id, plain, reply_to=msg_id, thread_id=thread_id, parse_mode=None)
                    send_document(chat_id, md_path, reply_to=msg_id, thread_id=thread_id)
                    log.info("Replied to chat %s (%d chars)", chat_id, len(formatted))
                except Exception as e:
                    log.error("Query failed for chat %s: %s", chat_id, e, exc_info=True)
                    send_message(chat_id, f"❌ 查询处理失败: {e}", reply_to=msg_id, thread_id=thread_id)
                finally:
                    is_processing = False

            # 3. 遍历所有收到的 updates
            for update in updates:
                # 关键：始终推进 offset，无论 reject 还是 accept
                update_id = update.get("update_id")
                if update_id is not None:
                    offset = update_id + 1

                msg = update.get("message") or update.get("channel_post")
                if not msg:
                    continue

                chat_id = msg["chat"]["id"]
                msg_id = msg["message_id"]
                thread_id = msg.get("message_thread_id")

                if not should_respond(msg):
                    continue

                query = extract_query(msg)
                if not query:
                    send_message(chat_id, "请发送你的问题，例如:\\n/query 什么是 VLESS-REALITY",
                                 reply_to=msg_id, thread_id=thread_id)
                    continue

                # 忙状态检查：react 后直接跳过（offset 已推进，不会重复）
                if is_processing:
                    react_to_message(chat_id, msg_id, emoji="🙈")
                    log.info("Rejected query from chat %s (busy): %s", chat_id, query[:50])
                    continue

                # 空闲：加锁，提交异步任务
                is_processing = True
                log.info("Accepted query from chat %s: %s", chat_id, query[:50])

                # 提交任务到线程池（非阻塞）
                future = executor.submit(query_wiki, query)
                pending_futures[future] = (chat_id, msg_id, query, thread_id)

                # React 👀 表示已接受
                react_to_message(chat_id, msg_id, emoji="👀")
                send_chat_action(chat_id, thread_id=thread_id)


        except requests.exceptions.ConnectionError:
            log.warning("Connection lost, retrying in 5s...")
            time.sleep(5)
        except Exception as e:
            log.error("Unexpected error in main loop: %s", e, exc_info=True)
            time.sleep(2)

if __name__ == "__main__":
    main()
