# RK Manager Agent 🤖

Advanced Telegram AI agent — manages your **channel + group**, learns from
chats, replies from learned content, escalates to admin then owner when stuck.

## Kya karta hai

- **Seekhta hai**: Group chats + channel posts MongoDB me save. Bot ko group/channel ka content pata chal jata hai.
- **Jawab deta hai**: Group me `/ask <sawal>` ya bot ko mention karo. Grounded in learned content. File maango to direct channel se bhej dega.
- **2-level escalation**: User → ADMIN pehle → OWNER last resort. Admin "owner ko forward" button se escalate kare.
- **Owner DM panel** (`/start`):
  - ➕ Group Admin / ➕ Channel Admin
  - 🔗 Link Group ↔ Channel
  - 🤖 Choose Model (saare Gemini models inline buttons)
  - 🔑 API Keys (Gemini + DeepSeek, status, test, reset)
  - 🧠 Memory Cleanup (prune, reconcile, forget)
  - ➕ Add bot to group/channel (Rose-style deep link)
  - 📊 Stats / ❓ Help
- **Multi-AI rotation**: Gemini (7-8 keys) + DeepSeek (backup). Random provider + random key. Fail pe next key with backoff.
- **Self-cleanup**: TTL + importance score + channel reconciliation (deleted posts auto-detect).
- **Auto-link**: Group/Channel add hone par auto-set + announce.

## Default model
`gemini-2.5-flash-lite` — panel se change kar sakte ho.

## Setup (Render pe deploy)

**1. BotFather (zaroori)**:
- BotFather → bot → Bot Settings → **Group Privacy → Disable**
- Channel me bot ko **admin** banao (Post messages permission)

**2. Render env vars** (Dashboard me set karo, kabhi commit mat karo):

| Variable | Kya hai |
|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather se mila bot token |
| `GEMINI_KEYS` | 7-8 Gemini API keys, newline/comma separated |
| `DEEPSEEK_KEY` | (optional) DeepSeek API key as fallback |
| `MONGODB_URI` | MongoDB Atlas connection string |
| `OWNER_CHAT_ID` | Tera numeric Telegram chat id (@userinfobot se) |
| `ADMIN_CHAT_ID` | (optional) Admin ka chat id; default = owner |
| `WEBHOOK_URL` | (optional) Free-tier ke liye — service ka public URL |
| `WEBHOOK_SECRET` | (optional) Webhook path secret |
| `DEFAULT_MODEL` | (optional) Default: `gemini-2.5-flash-lite` |

**3. Deploy**:
- Push this repo to GitHub
- Render Dashboard → New → **Blueprint** → select repo
- `render.yaml` read karke service create ho jayega
- Env vars set karo → deploy automatically start

## Local test

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=... \
       GEMINI_KEYS="key1,key2,key3" \
       MONGODB_URI=... \
       OWNER_CHAT_ID=...
python3 bot.py
```

## Limitations (honest)

1. **Purana history nahi** — Bot API purane messages nahi deta. Add hone ke din se seekhega.
2. **Privacy Mode OFF** hona chahiye group me.
3. **Channel post deletion** — Telegram Bot API delete event nahi bhejta. Reconciliation + TTL se handle hota hai (orphan posts remove).
4. **24/7** — Free Render tier spin-down hota hai; paid plan best.

## Files

| File | Purpose |
|---|---|
| `bot.py` | Entry + dispatch (polling/webhook) |
| `config.py` | Env config |
| `tg_api.py` | Telegram Bot API client (urllib only) |
| `ai_engine.py` | Multi-provider AI router (Gemini + DeepSeek) with key rotation |
| `db.py` | MongoDB + in-memory fallback |
| `knowledge.py` | Ingest, search, importance, prune, reconcile |
| `agent.py` | Q&A + file delivery |
| `escalation.py` | 2-level user→admin→owner escalation |
| `links.py` | Group↔Channel linking |
| `owner_panel.py` | Owner DM control panel |
| `render.yaml` | Render blueprint |
| `Dockerfile` | Container build |
| `requirements.txt` | pymongo[srv] |
