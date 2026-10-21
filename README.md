# RK Manager Agent 🤖 v4.2 REALTIME AGENT (MTProto)

Advanced Telegram AI agent — manages your **channel + group**, learns from
chats, replies from learned content, and acts like a real group manager.
**v4.2 adds a REALTIME MTProto power layer** (owner ke API ID/Hash se,
same bot token) — live delete/edit events, channel+group message fetch
by ID, history backfill aur privacy-proof group context.

## v4.2 REALTIME KIT (owner request: API ID/Hash se full realtime access)

Owner ka brief: *"API ID/Hash use kro taki agent ki power badh jaaye aur
sabhi channel group ke message khud realtime me dekh paaye — old, new,
available sb kuch — real agent ki tarah."*

| Capability (live-verified) | Kaise |
|---|---|
| **Realtime DELETE events** — jo channel/group me udaya gaya wo memory se **<3s** me gayab | MTProto `UpdateDeleteChannelMessages` (Bot API ye event KABHI nahi deta). `mtproto_layer.py` thread 24x7 sunta hai; `bot._apply_mtproto_event` turant purge karta hai (channel posts + stored group messages + verify-cache dead-mark) |
| **Realtime EDIT sync** | `UpdateEditChannelMessage` → managed channel ke post ka naya text turant DB me |
| **Channel history — bot add hone se pehle ke posts + bot offline gap** | GetHistory bots ke liye blocked hai (live-tested `BotMethodInvalidError`), isliye **ID-range scan**: `[max_stored+1 .. +SCAN_WINDOW]` tak `channels.getMessages` (100 IDs/request) — jo mile wo ingest, deleted IDs khud-filter. Har `MTPROTO_BACKFILL_MINUTES` (15) cycle |
| **Live truth-check (reconcile)** | Stored posts batch-verify — deleted => purge, unknown => untouched (fail-safe). Private channels bhi (purana HTTP t.me probe sirf public ke liye tha) |
| **Group answers ka LIVE context ("dumb answers" fix)** | `fetch_group_context`: current message se pehle ke N messages MTProto se — **privacy mode ON ho tab bhi** (ID-fetch ko privacy touch nahi karta). Prompt me `LIVE GROUP CHAT` block — model asli conversation dekh kar jawab deta hai |
| **verify.py 2-layer** | MTProto authoritative verdict pehle (~0.1-0.3s, private bhi); layer down => purana HTTP probe fallback |
| **Graceful degradation** | Bina API ID/telethon ke bhi bot 100% chalta hai (layer disabled, sab fetch empty) — tests is contract ko lock karte hain |

Live-proof (is session me, real bot + real chats): delete-event group
message #2407 Bot API se delete hote hi MTProto pipeline me aaya;
`verify_posts` ne deleted #349/#350 → `False`, live #354 → `True` diya;
group context me real conversation fetch hui. **394 tests pass.**

Env knobs (naye): `MTPROTO_API_ID`, `MTPROTO_API_HASH`,
`MTPROTO_ENABLED` (1), `MTPROTO_SCAN_WINDOW` (200),
`MTPROTO_GROUP_CONTEXT` (15), `MTPROTO_BACKFILL_MINUTES` (15),
`MTPROTO_SESSION` (optional override; warna session Mongo `state` me
persist hoti hai — Render restarts survive).

> **Ek zaroori BotFather step (one-time, owner kare):** group me har
> message dikhaane ke liye `@BotFather` → `/setprivacy` →
> @rkgamermanagerbot → **Disable**. (MTProto ID-fetch privacy-proof hai,
> par live push-stream ke liye privacy off chahiye.) Channel posts me
> privacy ka issue nahi hai — wahan sab posts members ko dikhte hain.

## v4.1 MULTI-PROVIDER (Mercury 3) — previous

Owner ke live issues — root cause + fix + tests (362 total pass):

| Issue (live) | Root cause | Fix |
|---|---|---|
| **"Bot utna powerful nahi — Gemini old/stale message deta hai, dumb answering karta hai"** | Ek hi provider (Gemini flash-lite) sab kuch answer karta tha; owner ke paas koi control nahi tha kaunsa model jawab de | **Mercury 3 (Inception Labs) provider added** — OpenAI-compatible `api.inceptionlabs.ai`, model `mercury-3` (1M context, 1000+ tok/s diffusion LLM, reasoning_effort support). Multi-key pool + backoff sab waise hi. **Owner panel me ab `🤖 Text AI` + `👁 Vision AI` buttons** — provider choose karo (Mercury/Gemini/DeepSeek), phir model choose karo (live model list). Selection DB me save hoti hai, turant apply |
| **"Mercury 3 text-only hai — user photo/audio bheje to usko bhi response mile"** | — | **Auto vision routing (built-in)**: photo/voice/video messages HAMESHA vision-capable provider se answer hote hain (`generate_mm`) — text-only Mercury select hone par bhi media ka jawab Vision AI (Gemini) se aata hai. Vision provider+model alag se selectable (default `gemini-3.5-flash-lite`) |
| **"Selected provider down ho gaya to?"** | — | **Failover**: owner-selected provider pehle try hota hai; fail hone par baaki providers khud try karte hain — bot kabhi down nahi hota |
| **"Group ke owner/admins ke commands follow hi nahi karta — sirf given owner id ko follow karta hai"** | Permission checks hardcoded `OWNER_CHAT_ID` pe the — Telegram group ke asli creator/administrators ko privileged nahi mana jaata tha (AI prompts bhi 'sirf owner' bolte the) | **v4.1 MULTI-ADMIN**: live Telegram role (`getChatMember` → creator/administrator) ab sab jagah privileged — RULE: lines, `/yaad` `/bhul` `/sikhao` `/users`, mod tools, AI instruction authority. Prompts ab group ke owner/ADMINS ke commands binding maante hain; members ka refusal fact intact |
| **"Old deleted channel data bhi users ko de deta hai"** | v4.0 truth layer already verifies links | v4.0 truth layer intact (answer-gate + 30-min sweep + delivery-probe purge) + Mercury 3 ke 1M context ke saath grounding prompts zinda |

Env knobs (naye): `MERCURY_KEYS` (multi-line), `MERCURY_MODEL`
(`mercury-3`), `MERCURY_REASONING` (`medium`), `DEFAULT_VISION_MODEL`
(`gemini-3.5-flash-lite`).

Owner panel flow:
1. Bot ko DM me `/start` bhejo → Control Panel
2. `🤖 Text AI` → provider (✅ Mercury 3) → model (✅ mercury-3)
3. `👁 Vision AI` → provider (Gemini) → model (✅ gemini-3.5-flash-lite)
4. Panel pe neeche current selection hamesha dikhti hai; `🔑 API Keys` me
   Mercury keys bhi test hote hain

## v4.0 TRUTH AGENT (previous)

Owner ke live issues — root cause + fix + tests (335 total pass):

| Issue (live) | Root cause | Fix |
|---|---|---|
| **"Channel me koi post krta hu to channel post ko bhi ye group se delete kr deta hai (owner ka promotion bhi)"** | Channel posts jo linked group me **auto-forward** hote hain unka `from` ek FAKE pseudo-user hota hai (channel ki taraf se) — owner-exemption (`user_id == OWNER`) kabhi match nahi hoti; promo-regex owner ke promotion ke t.me links par fire karti thi; `getChatMember(fake_id)` galat status deta tha → **DELETE** | `moderation.handle_group_message` me **channel-backed messages (`is_automatic_forward` / `sender_chat` / GroupAnonymousBot id) KABHI moderate nahi hote** — owner ka channel content group me immortal hai. Plus **`getChatAdministrators` whitelist** (5-min cache, ek hi API call) — naye admins/pseudo senders sab cover |
| **"Jo channel se delete ho chuka hai vo link bhi bhej deta hai — ZYROX 7 dead links spam. Latest data se response nahi, OLD data se deta hai"** | Telegram Bot API deleted-post events push NAHI karta; DB me purane posts zinda rehte the; LLM unhi dead links ko context se quote karta tha | **Naya `verify.py` TRUTH LAYER (3 levels)**: (1) **answer-outbound gate** — answer ke har managed-channel post link ko LIVE verify karo (public channel: t.me HTTP probe, 10-min cache) — dead link = DB purge + honest rewrite ("ye post delete ho chuka hai"); (2) **background sweep** — daemon thread har 30 min recent 60 posts verify karta hai, dead purge — dead links LLM context me kabhi nahi aate; (3) delivery-time `not found` probe (v3.4) — private channels ka authority. Private channel (t.me/c/) par HTTP probe impossible — wahan delivery-pro + answer-path delivery hi authoritative hai |
| **"Mene aisa to bola hi nhi ki mujhe BAT ka key chahiye — bina maange key de diya"** | LLM context me dekha BAT key wala post aur 'helpful' banne ke liye volunteer kar diya | **NO UNREQUESTED CONTENT prompt rule** (hard ban: topic X pucha hai to topic Y ka key/code/link dena FORBIDDEN) + **deterministic `sanitize_unrequested_keys` guard** — answer me 'KEY: xxx' value line ho + question me key-intent na ho + topic bhi match na kare → line strip. Legit 'BAT ka key batao' jawab kabhi affect nahi hota |
| **"User key maang raha tha — koi response hi nahi diya" (spoo.me/Ox5jh1)** | v3.4/v3.5 exchange-guard: A<->B baat me **ASKER KHUD** genuine help-sawal puche to bhi silence; + gate-LLM error (429/timeout) par members ke liye fail-closed | **v4.0: exchange-guard ab asker-aware** — genuine help-sawal/request (`key kha milega`, `app do`) exchange me bhi ANSWER hota hai (budget-capped); sirf chatter silent. **Gate-error fail-open for help-questions** — infra failure kabhi genuine madaad nigal nahi sakta. Reply-to-OTHER-MEMBER wale requests ab bhi silent (member-to-member respect intact) |
| **"2 users ki baat-cheet me spam karta hai"** | Budget 4/300s loose tha, min-gap nahi tha | **Budget 3/600s + MIN-GAP 45s** — machine-gun undirected replies ab structurally impossible. Directed (mention//ask/reply-to-bot) + owner exempt. Channel-post copies exchange-detection me ab participant nahi (owner ka post beech me aa jaye to A-B 'exchange' nahi tootti) |
| **"Koi bhi file bhej deta hai"** (v3.4 residual) | — | v3.3/v3.4 precision gates intact (file_name 3x match, specific-token required, file-topic precondition) + naya unrequested-content guard |
| **"Gemini ke normal model ko ADVANCE agent banao — skills, tools, kaise baat kare, manage kare"** | — | **Agentic architecture already wired + v4.0 truth layer**: (1) tool-use loop — `LOOKUP:` self-directed deep-search (multi-step, instant), deterministic REAL forward/copy delivery engine (LLM kabhi 'bhej di' claim nahi kar sakta — system notice hi announce karta hai); (2) 4-tier smart routing (directed → explicit request → help-question → balanced LLM gate with full conversation context); (3) moderation tools (delete/mute/warn/ban + Rose-style /filter content-delivery filters); (4) standing rules + `/sikhao` teach system (owner ke self-learning skills); (5) **memory compaction** (industry pattern — Claude/OpenAI 'compaction' jaisa): per-chat hard cap 2000 → old messages compact LLM digest ban jaate hain, Q&A cap 400, bounded search (data jitna bada ho, latency same); (6) **real-time verification** (naya) — 3-level truth layer upar |
| **"Chats ka data bahot bada ho jayega — agent slow/stuck"** | — | v3.5 memory management intact (hard caps + LLM digests + TTL prune + bounded search) — internet-researched pattern, live-proven |

Env knobs (naye): `VERIFY_ENABLED` (default 1), `VERIFY_SWEEP_MINUTES` (30),
`VERIFY_SWEEP_BATCH` (60), `REPLY_BUDGET` (3), `REPLY_BUDGET_SECONDS` (600),
`REPLY_MIN_GAP_SECONDS` (45).

**Research notes (user ne online research karne ko bola tha):**
- Telegram Bot API deleted-message updates nahi deta — open feature request
  (Bot API issue #286). Isliye deletion-detection delivery-probe + t.me
  HTTP probe + periodic sweep se hota hai (verify.py).
- Memory growth: industry "compaction" pattern (Claude Platform / OpenAI
  docs) — old turns ko summary se replace karo, state preserve karo. v3.5
  me already implemented (LLM digests + hard caps); v4.0 recency-first
  search fetch bhi (newest posts hamesha candidate set me).

## v3.5 SMART LISTENER kit (owner request: silent bot + memory growth)

Owner ke live issues — root cause + fix + tests (304 total pass):

| Issue (live) | Root cause | Fix |
|---|---|---|
| **"User kisi se baat nahi kar raha, usko key chahiye — agent ko help karna chahiye... response hi nahi diya"** (screenshot: `key kha milega` 2 baar pucha, bot dono par silent) | v3.4 discipline itna strict ho gaya tha: `kha milega`/`koi btayega` jaise help-shapes regex me the hi nahi, aur gate prompt "When in doubt, answer NO" + gate-LLM error par member ko silence | (1) **Help-seeking question tier** (`triggers.is_help_seeking_question`): `key kha milega`, `kaha se milega`, `crown loader kaise milega`, `koi btayega`, `help chahiye` — ye ab GATE ke bina seedha answer route hote hain (gate failure kabhi genuine help ko swallow nahi kar sakta); (2) gate prompt ab **balanced** — member-to-member interrupt abhi bhi strictly banned, PAR "general help-question nobody is answering = YES, helping is your JOB" |
| **"Aisa to nahi h na ki only /ask pe hi response de / kisi ka msg hi nahi dekh rha"** | Bot hamesha sab padhta hai (ingest+context) — silent hone ka EHSAS gate-conservatism se aa raha tha | Monitoring intact proof-test: har message (chup rahe tab bhi) `_GROUP_CONTEXT` + `messages` me yaad rehta hai; **exchange guard participant-aware** hai — A<->B ki baat me bot chup, par TEESRA member `key kha milega` puche to turant help (anti-spam budget 4/5min abhi bhi lagta hai) |
| **"Channel ke hisab se usse baat karna chahiye"** | Search sirf asker ki APNI history me hota tha — dusre members ne jo process/rules group me samjhaye the, wo grounding me kabhi nahi aate the | **Group-scoped search**: group questions `chat_id` scope se search hote hain (Telegram group waise bhi sab members ko dikhta hai) — `key kha milega` ka jawab group ke ACTUAL data (pinned process message, channel posts) se aata hai. DM/private me per-user scope pehle jaisa |
| **"Chats ka data store hoga to ek din bahot bada ho jayega — agent ko response dene me bahot time ya stuck ho sakta hai"** | Messages + Q&A unbounded grow karte the (M0 512MB + regex scans slow ho jate) | **Memory management** (internet-researched best practices: hard cap + summary compaction + bounded search): (1) `MEMORY_MAX_MESSAGES_PER_CHAT=2000` hard cap — purane messages ek chhota **LLM DIGEST** (short summary, community facts, no PII) ban jaate hain aur raw delete ho jaate hain (`digests` collection, search me `kind=digest` milta hai — info zinda, size bounded); importance>=0.5 messages KABHI delete nahi hote; LLM fail = delete skip (silent loss kabhi nahi); (2) `MEMORY_MAX_QA_PER_USER=400` Q&A cap; (3) search fetch cap 80 + limit 10 waise hi bounded; (4) naye Mongo indexes (qa, digests, chat+date compound). 12h prune cycle + har ~100 messages par cap check — growth HAMESHA O(cap) |
| **"Agent ko jaha lage uski requirement hai to response de"** | Purana binary: ya strict silence ya spam | 4-tier smart routing: directed (mention/`/ask`/reply) → always; explicit request (`bhejo/dedo`) + **help-question (naya)** → direct grounded answer; sab kuch → balanced LLM gate (context dekh kar khud decide karta hai) |

Bonus: `stats()` me `digests` count; `DIGEST_ENABLED=0` se digest-generation
band (sirf TTL/importance prune chalega).

## v3.4 DISCIPLINE AGENT kit (owner request: spam + random files + deleted files)

Owner ke 5 live issues — root cause + fix + tests (281 total pass):

| Issue (live) | Root cause | Fix |
|---|---|---|
| **"2 users ke baat-cheet me bahot message krta hai"** | `?`/question-words (`kya/kab/kyu`) seedha cheap-path se answer de dete the — LLM gate bypass; gate prompt khud "topic relevant hai to YES" bolta tha | (1) **Member-exchange guard**: last 2 messages alag members ke + bot last-4 me nahi = bot chup (na gate, na answer, na forward); (2) `?`-questions ab hamesha **conservative gate** se — "Answer YES ONLY when CLEARLY meant for you... staying silent is always safe"; (3) sirf **explicit requests** (`app do`, `bhejo`, `help`) direct route; (4) **reply budget** — undirected replies max 4/5min per chat (mention/`/ask`/reply-to-bot/owner exempt) |
| **"Bina user ke message samjhe kuch bhi bhej deta hai"** | `channel_forward` handler directedness-check se PEHLE chalta tha — undirected chatter pe bhi auto-forward; `_FILE_REQUEST_RE` me `photo/video/send/mila` bare nouns trigger the; `_INTENT_RE` me `mujhe` intent tha | Natural-language forward ab **directed/gate-approved ke BAAD** chalta hai; file-request me ab **ask-verb ya key/apk ya noun+do** chahiye (`twitter pe video dekho` = chatter, `video dedo` = request); `mujhe/mail` intent list se hataye; `_best_file_offer` me **file-topic precondition** (request/problem/key) |
| **"Jo channel se delete ho chuka hai wo file bhi bhej deta hai"** | `file_id` resend fallback deleted posts ko 'zinda' kar deta tha (Telegram file_id delete ke baad bhi kaam karta hai) | **file_id resurrection REMOVED** — delivery ab sirf LIVE channel se (forward → copy); `not found` = post deleted → **DB se purge** + honest "delete ho chuka hai" message. Post phir kabhi search/offer/context me nahi aata |
| **"Har baar galat response — channel ke data ke base pe dena chahiye"** | SYSTEM_PROMPT khud kehta tha: "context me answer nahi to **general knowledge use karo**" | Prompts me **GROUNDING rule**: channel/group data = SINGLE SOURCE OF TRUTH; community content (files/keys/links/posts/members/past events) par general knowledge substitute **banned**; honest "abhi mere paas ye record nahi hai" |
| **"Galat post link / random number"** | (v3.3 se residual) post number LLM guess | `_ground_post_links` + `post_exists` checks unchanged; purged (deleted) posts ab context me hi nahi aate |

Bonus: owner ab kisi message pe REPLY karke bol sakta hai "isko delete karo"
(pehle reply-to-member wale messages silently drop ho jate the).

## v3.3 POWER AGENT kit (owner request: tools + formatting + intelligence)

Owner ke 5 live issues — root-cause + fix + tests (255 total pass):

| Issue (live) | Root cause | Fix |
|---|---|---|
| **"apne sabhi message delete kro" → sirf bola, kiya kuch nahi** | koi self-delete tool hi nahi tha + "delete kro" spelling (`kro`) mod-request regex me missing + "koshish kar raha hu" sanitize se bach gaya | `_track_own_message()` har bheje message ka id yaad rakhta hai; `apne message delete karo` / `/delmy [N]` REAL `deleteMessage` chalata hai (owner/admin only) + honest count report. `_FAKE_PROGRESS_RE` me `koshish kar raha/bata deta` patterns |
| **Key manga → text + KEY cut** ("cut kr diya text and key") | `file_offer` caption `[:200]` me kata + `sendDocument` by file_id **entities strip kar deta** — "KEY: Click me" ka clickable link (https://t.me/rkgamersfeedback/50) murda text ban gaya | Delivery ab **forward-first** (perfect clone — links clickable, formatting intact) → `copyMessage` fallback → file_id resend LAST with `fmt.entity_html()` se entities HTML me rebuild. Caption KABHI truncate nahi hota. `ingest_channel_post` ab `caption_entities` + text_link URLs store karta hai |
| **Dumb formatting** — `**asterisks**` literal dikhte the | LLM markdown answers `parse_mode` ke bina jaate the | **fmt.py**: markdown → Telegram HTML (`**bold**`, `` `code` ``, `[label](url)`, underline, strike, auto-linkified URLs) + plain-text fallback; SYSTEM_PROMPT me formatting rules (keys `code` me, file names **bold**, links clickable) |
| **"twitter wala app do" → sirf file ka NAAM text kiya, app nahi diya** | `twitter` _GENERIC_TOKENS me banned tha + "app do" intent words me nahi tha + gate ne 6 min tak chup rakha | `twitter`/`facebook` un-banned (file_name 3x match precise hai), `app do / key do / X dedo` STRONG intent, `9999` jaise error codes post-number nahi bante, triggers me `app do` = request (gate skip). Owner ka rule "twitter problem → app bhi do" ab sach me deliver hota hai (#197) |
| **Galat post link (#245 ≠ #197) + free-fire APK + "direct file nahi bhej sakta" jhooth** | LLM post number hallucinate karta tha; file_offer loose match par galat APK deta tha; prompt apni delivery power se contradict karta tha | Context me har post ka **real number + file name + links**; `_ground_post_links()` fabricated link ko REAL post se correct karta hai (+ wo post deliver bhi); `_best_file_offer()` precision gate (file_name/fuzzy token match chahiye — 'arvon' ≈ 'avron' typo bhi chalega); FILE HONESTY prompt ab sach bolta hai |

Bonus: reply-gate ab owner ke liye **fail-open** (429/timeout pe bhi jawab
milta hai — "kabhi kabhi message bhi nahi krta" fix), `dena/dedo/dijiye`
request words, `/delmy` command menu me registered.

## v3.2 CHANNEL MEMORY kit (owner request: old posts + no previews)

Owner ke 3 naye requirements — sab live-verified:

| Requirement | Fix |
|---|---|
| **Link preview OFF** in every bot message | `tg_api.send_message` / `edit_message_text` / `copyMessage` ab hamesha `link_preview_options: {is_disabled: true}` bhejte hain (Bot API 7.0+ field, live-verified). **v3.2.1**: forwardMessage original ka preview copy karta hai (live-proven) aur forwarded messages edit nahi ho sakte — isliye URL wale TEXT posts `copyMessage` se deliver hote hain (preview OFF); media/plain-text posts forward path par hi rehte hain |
| **245 channel messages ka pura data bot ko do** — old posts bot khud dekh nahi sakta | Backfill script: har post id ko silently owner DM me `forwardMessage` karke full content (file_id, file_name, caption, links) padha aur turant delete — **94 live posts MongoDB me ingested** (11 APKs, 39 photos, 6 videos, 32 text posts). Naye posts live ingest hote hain wahi rich schema se |
| **"crown loader de do" / "key dedo" par post deliver karo** | Name-search delivery: `_search_posts_by_tokens` — file_name hit = 3x weight vs caption hit ('twitter apk' → Twitter APK milega, BEASTCROWN nahi jo caption me twitter likhta hai). NFKC normalization (ＺＹＲＯＸ → zyrox). Specific token + strong intent dono chahiye — 'key dedo' (vague) aur casual talk kabhi auto-forward nahi karta |
| Forward khud REQUESTER ke message ka REPLY hai | `forwardMessage` me `reply_to_message_id` (live-verified) — delivery wahi jawab hai, koi extra "forward kar diya" noise nahi |
| Album post manga jaye to poora album | `media_group_id` se group milta hai → saari items order me forward, pehli item requester ko reply karti hai |
| "abhi system ke zariye forward ho jayega" passive lie | `_FAKE_PROGRESS_RE` me `ho jayega/jayenge` patterns — sentence ship hi nahi hota |

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

## v3.1 REAL AGENT kit (screenshot-reported fixes)

6 live issues — root causes found in code and fixed:

| Issue (screenshot) | Root cause | Fix |
|---|---|---|
| Link diya, LLM ne sirf baat ki — real forward kabhi nahi hua | `channel_forward.py` likha gaya tha par **kabhi bot.py me wire hi nahi hua** (dead code!) | group + owner DM dono me wired — LLM se PEHLE chalta hai; `t.me/<channel>/<post>` link, `/get 245`, "latest", "vo wala message bhejo" — sab par REAL `forwardMessage` |
| Channel add pe "kisi ne add kiya (8781539682)... leave kar diya" jhooth | 8781539682 = bot ka APNA id — `get_me()` cold-start fail → system auto-add ko stranger add samajh liya | bot id ab update ke `new_chat_member.user` se seedha — network pe depend nahi |
| "check kar raha hu... thodi der me batata hu" fake progress | sanitize regex "nikal kar pahuncha deta hu" / "share karta hu" miss karta tha | future-promise patterns add — ye sentences ship hi nahi hote |
| Member ne rule set kar diya, bot ne maan liya | prompt me hierarchy likha tha par enforce nahi hota tha | MEMBER ke standing-instruction par refusal FACT inject + prompt hardening |
| Owner ki casual baat se naya rule ban gaya | RULE: line save hone ke liye sirf "owner sender" check tha | question me instruction-intent (karna/mat/ab se/...) hone par hi RULE save hota hai |
| "owner/channel ke bare me record nahi hai" | v3.0 me community facts aa gaye + v3.1 me creator fallback | description me Owner: line na ho to `getChatAdministrators` se creator nikalta hai (cached) |

Reply-gate bhi tight: statements/narrations ("kal diya tha...", "khela hu",
"acha") jaise unaddressed messages par bot ab reply nahi karta — jab koi
sawal/request/directed message ho tabhi jawab aata hai.

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
| `REPLY_BUDGET` | (optional) Max undirected replies per chat per window; default 4 |
| `REPLY_BUDGET_SECONDS` | (optional) Reply-budget window seconds; default 300 |
| `MEMORY_MAX_MESSAGES_PER_CHAT` | (optional) v3.5: hard cap — raw messages per chat; isse purane messages LLM digest (short summary) me compact hote hain; default 2000 |
| `MEMORY_MAX_QA_PER_USER` | (optional) v3.5: per-user Q&A history hard cap; default 400 |
| `DIGEST_ENABLED` | (optional) v3.5: 0 = digest/compaction off (sirf TTL prune); default 1 |

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