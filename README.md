# RK Manager Agent 🤖 v3

Advanced Telegram AI agent — manages your **channel + group**, learns from
chats, replies from learned content, and acts like a real group manager.

## v3.0 SPEED kit (reply latency fix)

Replies were taking 15–35s. Root causes (live-measured on the free tiers)
and fixes:

| Cause | Measured | Fix |
|---|---|---|
| knowledge.search = 5 queries × up to 6 tokens (30 Atlas roundtrips), ran TWICE per answer | ~4.3s ×2 | ONE combined `$or` pass per collection (~0.43s, 10×) |
| 6+ state reads per message (filters/terms/teach/rules/links) | ~816ms | in-process write-through state cache (~0ms) |
| fresh TCP+TLS per Telegram API call | 576–834ms/call | persistent keep-alive connection (192ms/call, 3×) |
| gate LLM + answer LLM sequential, search stacked after gate | seconds | answer-context prefetch runs DURING the gate call |
| answers generated up to 1024 tokens | seconds | `ANSWER_MAX_TOKENS` (default 350) |
| qa/ingest Mongo writes sat between question and answer | ~300ms | moved after delivery |
| Gemini hidden "thinking" latency | varies | `thinkingConfig.thinkingBudget=0` with self-healing fallback |

New env knobs: `ANSWER_MAX_TOKENS` (350), `GATE_TIMEOUT_SECONDS` (10).

## Kya karta hai

- **Seekhta hai**: Group chats + channel posts MongoDB me save. MongoDB unavailable ho to RAM fallback use hota hai; restart par us fallback ka data lost hota hai.
- **Jawab deta hai**: Group me natural sawal/request, `/ask <sawal>`, ya bot mention ka jawab deta hai. Plain greetings/chatter par interrupt nahi karta; reply kisi aur member ko ho to ignore karta hai. File maango to direct channel se bhej dega. **Har group reply user ke message ka REPLY hota hai** — thread me dikhta hai kis baat ka jawab hai.
- **Read-only channel**: Bot channel posts read/index karta hai, **khud se KABHI channel par post nahi karta**. Sirf owner ke DM command `/post <message>` par channel me post hota hai.
- **Group scan**: “group status”, “permissions”, “member count” jaisi request par Telegram se live bot membership/permissions check karta hai; result fail ho to claim nahi karta.
- **Per-user memory**: Group message history sirf usi Telegram user ke answers me search hoti hai; inactive user ka stored history 180 din ke baad clear hota hai.
- **Moderation (smart catch)**: Gaaliyon ki evasive spellings bhi pakdi jati hain — `madharchood`, `m@darch0d`, `b.h.o.s.d.a` sab fuzzy match se delete. Warn first, repeat par 10-min mute, ban kabhi nahi (sirf owner `/ban`).
- **2-level escalation**: User → ADMIN pehle → OWNER last resort. Admin "owner ko forward" button se escalate kare. 48 ghante pending escalation auto-expire.
- **Owner DM panel** (`/start`): Group/Channel admin add, link, model choose, API keys, memory cleanup, deep-link add buttons, stats aur help.
- **Multi-AI rotation**: Gemini (7-8 keys) + DeepSeek (backup). Random provider + random key. Fail pe next key with backoff.
- **Self-cleanup**: TTL + importance score + channel reconciliation (deleted posts auto-detect).
- **Auto-link**: Group/Channel add hone par auto-set + announce.
- **Telegram command menu**: Startup par `setMyCommands` — '/' button me sab commands dikhte hain (members + admins ke liye alag scope).

## Default model

`gemini-3.5-flash-lite` — panel se change kar sakte ho.

## Commands

**Sab members (group):**

| Command | Kaam |
|---|---|
| `/ask <sawal>` | Bot se kuch bhi pucho |
| `/help` | Poora command menu |
| `/files [query]` | Channel files/media list |
| `/id` | Chat/user ID info |
| `/stats` | Group stats (members, top talkers) |
| `/report` | Replied message admin ko report |
| `/rules` | Standing rules |
| `/admin <note>` | Admin se baat |

**Owner/admin (group):**

| Command | Kaam |
|---|---|
| `/mute /unmute` | Reply, ID, @username YA **naam** se (`unmute rich ko`) |
| `/tmute 30m\|2h\|1d` | Temporary mute |
| `/warn /warnings /resetwarns` | Warning system — 3 warn par 1hr auto-mute |
| `/delete` | Replied message delete |
| `/purge [n]` | Reply se lekar command tak bulk delete (max 100) |
| `/pin /unpin [all]` | Message pin/unpin |
| `/filter <word>` | **Rose-style content filter** — kisi bhi message pe REPLY karke `/filter <word>`: file/video/photo/text **as-is** save hota hai; jab bhi koi `<word>` bolega, bot usi content ko REPLY me bhejta hai (delete kabhi nahi). Ya `/filter <word> <text>` — text reply |
| `/filters / /stop / /stopall` | Filter list / remove / clear (`!filter`, `!stop` bhi chalta hai) |
| `/modlog` | Recent mod actions |
| `/kick` | Member bahar (owner only; link se wapas aa sakta) |
| `/ban / /unban` | Ban/unban (owner only) |

**Owner teaching system (group):**

| Command | Kaam |
|---|---|
| `/yaad <baat>` | Hamesha follow karne wali instruction |
| `/sikhao <trigger> :: <reply>` | Jab koi `<trigger>` bole to bot `<reply>` bolega |
| `/sikhao <trigger> :: file <naam>` | Jab koi `<trigger>` bole to channel se file milegi |
| `/sikhai` | Sikhaaye hue tasks ki list |
| `/bhulo <trigger>` | Sikhaaya hua task bhul jao |

**Owner DM:** `/start` (panel), `/post <message>` (channel post — **sirf owner ke kehne par**), `/mod_add|/mod_remove|/mod_list`, `/mod_on|/mod_off`, `/mute <id> [min]`, `/status`, `/yaad`, `/rules`, `/bhul`.

## Action honesty (important design)

Bot kabhi bhi `"karta hu"` / `"kar diya"` jaisa **fake action claim nahi karta**.
Agar owner bolke action bole to system use REAL command me convert karta hai
aur ASLI result (success/fail) report hota hai. Target na mile to bot target
puuchta hai — LLM ko action claim karne ka mauka hi nahi milta.

## Setup (Render pe deploy)

**1. BotFather (zaroori)**:
- BotFather → bot → Bot Settings → **Group Privacy → Disable**
- Channel me bot ko **admin** banao; `Post Messages` permission OFF rakh sakte ho
- Group me moderation chahiye to bot ko `Delete Messages` aur `Restrict Members` permissions do

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
| `WEBHOOK_SECRET` | (optional) Telegram webhook token; random token is generated if omitted |
| `DEFAULT_MODEL` | (optional) Default: `gemini-3.5-flash-lite` |
| `USER_MEMORY_IDLE_DAYS` | (optional) Inactive user-memory retention; default 180 days |

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
5. **Channel read-only** — Bot intentionally channel me messages nahi bhejta; only ingests channel posts.

## Owner-only DM commands

- `/mod_add <word or phrase>` / `/mod_remove <word or phrase>` / `/mod_list`
- `/mod_on` / `/mod_off`
- `/mute <telegram_user_id> [minutes]` — 1–1440 minutes, managed group only
- `/status` — reports managed chats, moderation, and whether storage is MongoDB or volatile RAM

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
| `triggers.py` | Low-noise natural question/request detection |
| `commands.py` | Public commands: /help /id /stats /files /report + bot menu |
| `teach.py` | Owner teaching system (/sikhao /sikhai /bhulo) |
| `moderation.py` | Owner-controlled word moderation; no-ban |
| `escalation.py` | 2-level user→admin→owner escalation |
| `links.py` | Group↔Channel linking |
| `owner_panel.py` | Owner DM control panel |
| `render.yaml` | Render blueprint |
| `Dockerfile` | Container build |
| `requirements.txt` | pymongo[srv] |