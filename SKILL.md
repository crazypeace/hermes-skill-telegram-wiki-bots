---
name: telegram-wiki-bots
description: "Deploy Telegram bots as a frontend to LLM Wiki — query bot for knowledge retrieval, admin bot for content ingestion. Long-polling, systemd-managed, no webhook needed."
version: 1.0.0
author: 3号传声筒
metadata:
  hermes:
    tags: [telegram, wiki, bot, knowledge-base, long-polling]
    category: telegram-bots
    related_skills: [llm-wiki]
---

# Telegram Wiki Bots

Two Telegram bots serving as a user-facing frontend to the [LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) via the Hermes API server.

## Architecture

```
┌─────────────────┐     ┌──────────────────────┐     ┌─────────────────┐
│  Telegram User  │────▶│  Wiki Bot (query)     │────▶│  Hermes API     │
│                 │     │  @bot /query          │     │  127.0.0.1:8642 │
│                 │────▶│  Admin Bot (ingest)   │───▶│  /llm_wiki      │
│                 │     │  @bot /ingest          │     │                 │
└─────────────────┘     └──────────────────────┘     └─────────────────┘
```

- **Wiki Bot** (`wiki_bot.py`) — query side: users ask questions, get answers + .md file
- **Wiki Admin Bot** (`wiki_admin_bot.py`) — ingestion side: users send content, bot catalogs it into the wiki
- Both communicate with Hermes API server at `127.0.0.1:8642/v1/chat/completions`
- Both use **long-polling** (no webhook setup needed)
- Both managed by **systemd** for auto-restart

## Prerequisites

- Hermes API server running on `127.0.0.1:8642`
- LLM Wiki skill installed and configured
- Python 3.8+ with `requests` library
- Two Telegram bot tokens (obtain from @BotFather)

## Wiki Bot (Query Bot)

### Trigger Rules
- **Private chat**: always responds to any text
- **Group chat**: only responds when `@mentioned`, replied to, or `/query` command used
- `/query <question>` slash command

### Flow
1. React with 👀 to acknowledge the message
2. Show typing indicator
3. Send query to API: `/llm_wiki query <question>`
4. System prompt constrains: "你是一个 wiki 查询助手。只回答用户的问题，不要建议创建 wiki 页面、更新 index 或修改任何文件。使用 Wiki 内容完整回答用户问题，不要出现'请参考...'或'基于...页面'这类引导式语句。直接给出自包含的完整答案，必要时将引用内容自然地融入答案中。回答结束后停止。"
5. Save answer as timestamped `.md` file in `OUTPUT_DIR`
6. Format answer as Telegram HTML and send
7. Send `.md` file as document attachment
8. Fallback to plain text if HTML send fails

### Key Functions

```python
# Trigger detection
def should_respond(msg: dict) -> bool:
    """Private: always. Group: @mention, /query, or reply to bot."""

# Query extraction — strips @mention and /query prefix
def extract_query(msg: str) -> str

# API call to Hermes
def query_wiki(query: str) -> str
    # POST /v1/chat/completions with:
    # system: "你是一个 wiki 查询助手。只回答用户的问题..."
    # user: "/llm_wiki query <question>"

# Markdown → Telegram HTML
def format_for_telegram(text: str) -> str
    # Converts: headers → <b>, **bold** → <b>, ```code``` → <pre>,
    # `inline` → <code>, tables → <pre>, [[wikilinks]] → plain text,
    # [text](url) → "text: url", bullets → •
    # Truncates at 4000 chars

# Save as .md file
def save_markdown(query: str, answer: str) -> str
    # Format: YYYYMMDD_HHMMSS_<sanitized_query>.md
    # With YAML frontmatter (query, time)
```

### Config Constants

```python
BOT_TOKEN = "your-query-bot-token"
API_URL = "http://127.0.0.1:8642/v1/chat/completions"
OUTPUT_DIR = Path("/root/wiki_bot_output")  # .md files saved here
POLL_TIMEOUT = 30
REQUEST_TIMEOUT = 120  # seconds
```

### Registered Commands
- `/query` — "查询 wiki 知识库"

---

## Wiki Admin Bot (Ingestion Bot)

### Trigger Rules
- `@mentioned` in group (passes full message as-is to ingest)
- `/ingest <content>` slash command

### Flow
1. React with 👀 to acknowledge
2. Show typing indicator
3. Send to API: `/llm_wiki Ingest """<full_content>"""`
4. System prompt: "你是一个 wiki 内容摄入助手...完成后只需回复「处理完成」"
5. Reply "处理完成" on success, error message on failure

### Key Functions

```python
# Trigger detection
def should_respond(msg: dict) -> bool
    # Group: @mention or /ingest command

# Content extraction — strips @mention, keeps everything else as-is
def extract_content(msg: dict) -> str

# API call to Hermes
def ingest_content(content: str) -> bool
    # POST /v1/chat/completions with:
    # system: "你是一个 wiki 内容摄入助手..."
    # user: '/llm_wiki Ingest """<content>"""'
    # Uses triple-quoted string to preserve content verbatim
```

### Config Constants

```python
BOT_TOKEN = "your-admin-bot-token"
API_URL = "http://127.0.0.1:8642/v1/chat/completions"
POLL_TIMEOUT = 30
REQUEST_TIMEOUT = 300  # 5 min (ingestion may be slow)
```

### Registered Commands
- `/ingest` — "摄入内容到 wiki 知识库"

---

## Deployment

### 1. Create Bot Files

Place `wiki_bot.py` and `wiki_admin_bot.py` in `/root/` (or any directory).

### 2. Systemd Services

```ini
# /etc/systemd/system/wiki-bot.service
[Unit]
Description=Wiki Bot (@<username>) - Telegram wiki query bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/root
ExecStart=/usr/bin/python3 /root/wiki_bot.py
Restart=always
RestartSec=5
StandardOutput=append:/root/wiki_bot.log
StandardError=append:/root/wiki_bot.log

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/wiki-admin-bot.service
[Unit]
Description=Wiki Admin Bot (@<username>) - Telegram wiki ingestion bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/root
ExecStart=/usr/bin/python3 /root/wiki_admin_bot.py
Restart=always
RestartSec=5
StandardOutput=append:/root/wiki_admin_bot.log
StandardError=append:/root/wiki_admin_bot.log

[Install]
WantedBy=multi-user.target
```

### 3. Enable & Start

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now wiki-bot wiki-admin-bot
sudo systemctl status wiki-bot wiki-admin-bot
```

### 4. Monitor

```bash
# Check logs
journalctl -u wiki-bot -f
journalctl -u wiki-admin-bot -f

# Or directly
tail -f /root/wiki_bot.log
tail -f /root/wiki_admin_bot.log

# Check running processes
ps -ef | grep wiki
```

## Common Pitfalls

- **Duplicate processes**: hermes terminal wrappers can leave zombie processes. Check `ps -ef | grep wiki` and kill any with a hermes bash wrapper parent PID. Clean instances should have PPID=1.
- **No webhook**: these bots use long-polling. Don't set a webhook with `setWebhook` — it will conflict. The admin bot explicitly calls `deleteWebhook` on startup to clear pending updates.
- **Telegram HTML parsing**: the `format_for_telegram` function is critical. Without it, characters like `<`, `>` in code blocks break the HTML parse. Always protect code blocks before `html.escape()`.
- **API timeout**: ingestion can take up to 5 minutes for large content. The admin bot sets `REQUEST_TIMEOUT=300`. The query bot uses 120s.
- **Token conflict**: two bots cannot share the same token. Use separate bot tokens for query and admin.
- **System prompt constraints**: the query bot's system prompt explicitly says "不要建议创建 wiki 页面、更新 index 或修改任何文件" — this prevents the bot from trying to modify wiki files when it should only be reading.

## Extending

### Adding New Actions (e.g., lint, create)

To add a new slash command like `/lint`:

1. Add the command to the bot's `setMyCommands` list
2. Add trigger detection in `should_respond()` (for `/lint` prefix)
3. Add content extraction in `extract_query()` or `extract_content()`
4. Add the API call pattern:
   ```python
   def lint_wiki() -> str:
       payload = {
           "messages": [
               {"role": "system", "content": "你是一个 wiki 审计助手..."},
               {"role": "user", "content": "/llm_wiki lint"}
           ]
       }
       # ... POST to API
   ```
5. Add dispatch logic in the main loop's `for update in updates` block

### Multi-bot Pattern

This two-bot architecture (read + write) is a clean pattern for any skill-based bot:

- **Read bot**: queries knowledge, returns formatted answers + .md files
- **Write bot**: ingests/modifies content, returns confirmation
- Separate tokens, separate triggers, same API backend

### HTML Formatting Module

The `format_for_telegram()` function is reusable. Extract it into a shared module if building multiple Telegram bots. Key steps:

1. Extract and protect code blocks (triple-backtick and inline)
2. `html.escape()` the remaining text
3. Restore code blocks
4. Convert markdown → HTML tags
5. Truncate at 4000 chars

---

## File Checklist

```
/root/
├── wiki_bot.py              # Query bot
├── wiki_admin_bot.py        # Ingestion bot
├── wiki_bot.log             # Query bot log
├── wiki_admin_bot.log       # Admin bot log
└── wiki_bot_output/         # .md files from queries

/etc/systemd/system/
├── wiki-bot.service
└── wiki-admin-bot.service
```
