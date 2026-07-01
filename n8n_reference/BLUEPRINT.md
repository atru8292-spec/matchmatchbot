# Технический блюпринт n8n → Python (WF1_MAIN + manager_bot)

> Справка для миграции. **Техника** отсюда актуальна. **Бизнес-модель/промпт/сценарии — НЕ отсюда**, источник истины = `CLAUDE.md` (см. раздел «Конфликты»).
> Составлено из реверс-инжиниринга `01_WF1_MAIN.json` (79 нод) и `02_WF_manager_bot.json`.

---

## 0. Общие факты
- Supabase project: `xbcynfwbsyufxaqtoror` (`https://xbcynfwbsyufxaqtoror.supabase.co`).
- Postgres credential в n8n: `Supabase Postgres` (id `ZlnteXRkdOyWrU3C`).
- Storage бакеты: `lead-photos` (**public**, иначе Vision не скачает), `invitations`.
- Telegram бот алертов/менеджера: «ассистент идей» (id `Gv8UkgePlIDEhjpk`), **chatId Ани `1009982311`** (хардкод).
- Wazzup24: `POST https://api.wazzup24.com/v3/message`, Bearer `9af8afe4fc864a5790cd8cdce085eb9c`, `channelId 00a456e4-973f-457b-8dad-2864ca224c5d` → всё в `.env`.
- OpenAI credential `OpenAi account` (id `u7T3jk8sciA6lh9Y`): чат `gpt-4.1`, Vision `gpt-4o-mini`, эмбеддинги `text-embedding-3-small`. Whisper — НЕ используется (добавляем сами).
- n8n `errorWorkflow` ловит ошибки отдельно → в Python нужен свой глобальный error-handler + алерт в TG.

---

## 1. WF1: вход / вебхук
- Нода `WhatsApp Trigger (Wazzup)`: `POST`, path `wa-leads-wazzup`, без auth.
- `Normalize payload` (JS) понимает 3 формата: тестовый chat (`chatInput` → `test_<sessionId>`), Telegram (`message.from` → `tg_<id>`), Wazzup (`messages[]` → `messages[0]`).
- Фильтр отброса: `isEcho===true`, `status!=='inbound'`, `chatType!=='whatsapp'` → drop. Пустой текст → drop.

**Форма Wazzup-сообщения (что ждёт код — ПРОВЕРИТЬ по актуальной доке):**
```
msg.isEcho, msg.status ('inbound'), msg.chatType ('whatsapp'), msg.chatId,
msg.messageId, msg.contact.name, msg.type ('text'|'image'|'audio'|'video'|'document'),
msg.text (или caption), msg.contentUri (URL медиа)
```

**Нормализованный контракт (внутренний):**
```
phone            = 'wa_' + chatId.replace(/\D/g,'')
chat_id          = chatId.replace(/\D/g,'')      # для отправки (без префикса)
channel          = 'whatsapp'
user_text        = text | '[photo received]' | '[voice message]' | ...
user_name        = Title-case имени | 'WA Lead'
external_message_id = 'wa_' + messageId
received_at      = ISO
content_type     = text|photo|voice|video|document|sticker
media_info       = { content_uri, message_id }
```
Тип по `msg.type`: image→photo, audio→voice, video→video, document→document, text→text.

---

## 2. WF1: маршрутизация
- `IF is photo` (`content_type==='photo'`): TRUE → фото-ветка; FALSE → debounce/текст.
- voice/video/document/sticker идут по FALSE, затем `Evaluate context` ставит `isUnsupportedMedia=true`, и `Auto-action router` шлёт отбивку. **Whisper НЕТ.**

---

## 3. WF1: текстовый поток (цепочка нод)
```
Normalize payload
→ Check lead exists (SELECT phone FROM leads WHERE phone=.. LIMIT 1)
→ IF lead exists → Update existing lead | Insert new lead
→ Insert inbound message (ON CONFLICT (lead_phone, external_message_id) DO NOTHING)
→ IF is photo (FALSE)
→ Debounce wait (4s) → Check newer inbound (окно 20s) → Debounce decide (склейка/return null)
→ Load lead profile (+ msg_count_24h) → Evaluate context (skipAI, autoBlock*, unsupported)
→ Load conversation history (20 последних) → Format history (history_text, links_sent, anna_messages_count)
→ Auto-action router (spam | escort | lowBudget | unsupported | fallback)
   └ fallback → IF skip AI → (FALSE) AI Agent Anna
→ Parse output + anti-repeat
→ [параллельно] Build lead update SQL | IF escalate | (Send invitation? — МЁРТВАЯ ветка)
→ Update lead profile
→ Explode messages + delays → Loop each message (splitInBatches)
     → Wait while typing (dynamic) → WhatsApp Send (Wazzup) → Save outbound → назад в Loop
```
`IF skip AI`: `skipAI=true` → TRUE-выход пустой (молчание).

---

## 4. WF1: фото-ветка
```
IF is photo (TRUE)
→ WA Download Media (GET media_info.content_uri, binary)
→ Prepare for Storage (path = `<phone>/<ts>_<safeMsgId>.jpg`, bucket lead-photos)
→ Upload to Supabase Storage (POST storage/v1/object/lead-photos/<path>, image/jpeg)
→ Save photo metadata (INSERT lead_photos, is_primary = NOT EXISTS(... is_primary=true))
→ Mark photo_received (UPDATE leads SET photo_received=true)
→ Check photo flood (COUNT lead_photos за 1 час)
→ IF photo flood (>5/час) → Alert (TG) + Set manual 4ч | иначе:
→ Analyze photo (Vision, gpt-4o-mini, detail=high, imageUrls=public URL)
→ Parse vision result → Save vision to lead_photos
→ IF Vision succeeded (verdict непустой и != manual_review)
   ├ FALSE → Alert Vision failed (TG) + Set manual (vision fail)
   └ TRUE  → Check payment intent
→ IF vision is payment (verdict=='payment_ok')
   ├ TRUE  → IF is payment confirmed → Mark paid_event + Send welcome + Send invitation image | Alert payment без qualify + Set manual
   └ FALSE → IF photo approved
         ├ TRUE  → Debounce wait (4s) → ... → AI (AI шлёт ссылку оплаты)
         └ FALSE → Mark photo rejected + Photo rejected alert (TG)
```
Одобренное фото само не отвечает — перезаходит в AI-поток с флагом «фото одобрено».

### Vision JSON-схема (нода `Analyze photo (Vision)`)
```
image_type: "profile_photo"|"payment_receipt"|"other"
is_real_person, is_single_person, face_visible: bool
estimated_age: number            # ФИЛЬТР: править 28-60 → 28-65
quality: "good"|"medium"|"poor"
appearance_level, environment_level: "high"|"medium"|"low"
contains_inappropriate, is_screenshot_or_meme, looks_aspirational: bool
payment_detected: bool, payment_amount: number|null
payment_bank: "Klar"|"BBVA"|"Santander"|"other"|null
payment_to_correct_recipient: bool
score: 0-10
verdict: "ok"|"reject"|"manual_review"|"payment_ok"
reasons: [string]
```
Правила verdict: `payment_ok` = receipt+detected+amount≥5000+correct_recipient; `ok` = профиль ок; `reject` = критичный красный/low-income; `manual_review` = пограничные/other.
Парсер: чистит ```json, берёт от первой `{` до последней `}`, дефолт `{verdict:'manual_review', score:5}`.
Сохранение: `UPDATE lead_photos SET vision_analyzed=true, vision_analysis=..::jsonb, vision_verdict=.., vision_reasons=ARRAY[..]::text[], analyzed_at=now()`.
Reject: `UPDATE leads SET photo_received=false, funnel_stage='rejected_filter', mode='manual', manual_until=now()+4h, escalate_reason='ИИ отклонила фото'` + TG-алерт с кнопками `approve_photo_/offer_course_`.

---

## 5. WF1: авто-блокировки (`Evaluate context` + `Auto-action router`)
- **spam**: `msg_count_24h > 30` и не DNC → `Auto-block spam` (`do_not_contact=true, funnel_stage='lost', mode='manual', manual_until=now()+100 years`) + лог `manager_actions` + TG.
- **escort**: `/escort|sexo|sexual|prostit|acompañante|servicio sexual/i` И `escort_mention_count>=1` (2-е упоминание) → блок навсегда, `escort_mention_count+1`. ⚠️ баг: первое упоминание не инкрементится — в Python считать явно.
- **low budget**: `budget_signal==='low'` и не lost → `Set lost` + `Send course (auto)` (видеокурс через Wazzup) + save + лог.
- **unsupported media**: voice/video/document/sticker → `Reply unsupported media` (отбивка). ⚠️ голос — заменяем на Whisper.
- **skipAI=true** (молчание): `manualActive` (mode=manual и manual_until в будущем) ИЛИ `do_not_contact` ИЛИ autoBlock ИЛИ unsupported.

---

## 6. WF1: payment intent
- **A. regex** в `Parse output` (см. §10) → эскалация «Готов оплатить».
- **B. скрин перевода**: `Check payment intent` (SQL: COUNT inbound за 10 мин с LIKE '%pagué%','%transferí%','%listo%','%hecho%','%mande%','%depo%'.. + photo_received + age + profession). `IF is payment confirmed` = verdict=payment_ok И intent_count>0 И age И profession → `Mark paid_event` (mode=auto) + welcome + invitation image + TG-алерт + лог. Иначе → алерт «оплата без квалификации» + manual.

---

## 7. WF1: debounce
- `Debounce wait (4s)` — Wait 4 сек.
- `Check newer inbound`: `SELECT external_message_id, text FROM messages WHERE lead_phone=.. AND direction='inbound' AND created_at > now()-interval '20 seconds' ORDER BY created_at ASC`.
- `Debounce decide` (JS): если последнее в выборке != текущему `external_message_id` → `return null` (умирает, обработает более поздний). Иначе склейка `rows.map(r=>r.text).join('\n')` + `debounced_count`.
- **В Python:** лучше per-phone asyncio-lock/Redis debounce (таймер 4с, склейка за 20с), а не wait+select 1:1.

---

## 8. WF1+manager: схема БД (как используется)

### leads (PK `phone`, формат `wa_`/`tg_`/`test_`)
```
phone, whatsapp_name, name, last_name, source, status ('new')
mode ('auto'|'manual'), manual_until (timestamptz), do_not_contact (bool)
funnel_stage: new|pitched|qualified|event_interested|agency_interested|
              ready_to_pay|rejected_filter|lost|paid_event|paid_agency
age (int), profession, is_single (bool), budget_signal ('low'|'medium'|'high')
interest ('event'|'agency'|'both'), photo_received (bool)
objection_count (int), last_objection_type, escort_mention_count (int)
escalate_reason, notes (text, append-лог)
email, date_of_birth (date), city, country
marital_status ('single'|'divorced'|'widower'|'married')
business_link, desired_partner_age, selected_service ('event'|'agency'|'both')
last_inbound_at, last_message_at, last_ai_message_at
followup_sent_count (int), next_followup_at (timestamptz), created_at, updated_at
```
INSERT нового: phone, whatsapp_name, source, status='new', mode='auto', funnel_stage='new'.

### messages (unique `(lead_phone, external_message_id)`)
```
lead_phone, direction ('inbound'|'outbound'), sender ('lead'|'anna'),
text, external_message_id, processed (bool), processed_at,
meta (jsonb {'content_type'}), created_at
```
inbound: `ON CONFLICT (lead_phone, external_message_id) DO NOTHING`. outbound: без external_message_id.

### lead_photos
```
lead_phone, channel, external_file_id, storage_url, storage_path,
file_size_bytes, mime_type ('image/jpeg'), is_primary (bool), received_at,
vision_analyzed (bool), vision_analysis (jsonb), vision_verdict,
vision_reasons (text[]), analyzed_at
```

### manager_actions (аудит)
```
lead_phone, action, performed_by ('system'|'manager'), manager_chat_id, meta (jsonb)
```
Значения action: auto_block_spam, auto_block_escort, auto_course_low_budget, auto_paid + все действия менеджера.

### documents (RAG, pgvector) — langchain-схема
`id, content, metadata (jsonb), embedding (vector)`, функция `match_documents`. ⚠️ контент старый (Mila) — перезалить.

### leads_export (view) — для Google Sheets экспорта.
### НЕ используем: outbox_messages, conversation_jobs (старый v4), deals, scenarios-старый (перезалить актуальными 49).

**КРИТИЧНО:** в n8n все значения интерполируются в SQL напрямую (`'{{..}}'`, `$$..$$`). В Python — **только параметризованные запросы**.

---

## 9. WF1: внешние сервисы
- **Wazzup отправка**: `POST https://api.wazzup24.com/v3/message`, `Authorization: Bearer <token>`, body `{channelId, chatType:'whatsapp', chatId:<цифры без wa_>, text:<одно сообщение>}`. `continueOnFail`. Медиа — поле `contentUri` вместо `text`.
- **Wazzup скачивание**: GET по `contentUri` (без auth-хедера).
- **OpenAI**: чат `gpt-4.1` (maxTokens=600, temp=0.7, timeout=60000, topP=1, response_format НЕ задан — регекс-парсер); Vision `gpt-4o-mini` (detail=high); эмбеддинги `text-embedding-3-small`.
- **RAG** (`playbook`, vectorStoreSupabase, retrieve-as-tool): таблица `documents`, `topK=3`, AI сам решает вызывать.
- **Telegram** алерты: chatId `1009982311`, parse_mode=none, appendAttribution=false.
- **Память AI** (`Conversation Memory`, memoryBufferWindow): `sessionKey=phone`, окно 30. Плюс история из БД (20 последних) — двойной источник.

---

## 10. WF1: AI output-схема + пост-процессинг
**JSON от AI (точные имена):**
```json
{
  "reply_messages": ["b1","b2","b3","b4"],
  "escalate_to_human": false,
  "escalation_reason": null,
  "funnel_stage": "new|pitched|qualified|event_interested|agency_interested|ready_to_pay|rejected_filter|lost",
  "update_fields": {
    "age": null, "profession": null, "is_single": null, "budget_signal": null, "interest": null,
    "first_name": null, "last_name": null, "email": null, "date_of_birth": null,
    "city": null, "country": null, "marital_status": null, "business_link": null,
    "desired_partner_age": null, "selected_service": null
  },
  "send_invitation_image": false
}
```
`escalation_reason` ∈ {Готов оплатить, Просит скидку, Спрашивает про эскорт, Настаивает на эскорте, Хочет привести друга, Билет не пришёл, Просит возврат, Юридический вопрос, Агрессивное поведение, Жалоба} | null.

**`Parse output + anti-repeat` (детерминированно):**
1. JSON регексом `/\{[\s\S]*\}/`; fallback `['Un momento, te respondo pronto 😊']`.
2. cleanArtifacts: убрать `[Citation](...)`, `[citation_marker]`, `[123]`, `(message_idx=..)`.
3. Чистка markdown `[*_~`]`, нумерации `^\d+[.)]`; обрезка до 4 сообщений.
4. **Force-escalate по user_text (перекрывает AI):**
```
escort   /escort|sexo|sexual|acompañante|prostit/i        → 'Спрашивает про эскорт'
descuento/descuento|rebaja|cupón|cupon|menos.*precio/i    → 'Просит скидку'
refund   /(devolver|reembolso|refund)/i                   → 'Просит возврат'
pago     /(quiero pagar|cómo pago|cuenta bancaria|listo para pagar|datos bancarios|
          pagué|ya pague|ya pagué|transferí|envié.*dinero|hice.*pago|pague.*enlace)/i → 'Готов оплатить'
agresión /(idiota|estúpido|pendej|mierda|cabrón|estafa|fraude)/i → 'Агрессивное поведение'
amigo    /(traer.*amigo|llevar.*amigo|mi amigo)/i         → 'Хочет привести друга'
boleto   /(no.*lleg.*boleto|ticket.*not.*receive)/i       → 'Билет не пришёл'
```
5. Нормализация `escalation_reason` в канонический enum.

**`Build lead update SQL`**: валидация (age integer; email regex; date `^\d{4}-\d{2}-\d{2}$`; enum-проверки). marital_status↔is_single; selected_service→interest; first_name→name. Follow-up: активные стадии → `next_followup_at=now()+24h`; lost/rejected_filter → NULL. Всегда `last_ai_message_at=now(), updated_at=now()`.

**Разбивка на бабблы** (`Explode messages + delays`):
```
baseDelay = clamp(msg.length/25, 2, 8)
randomExtra = 1.5 + random()*2
typing_delay_sec = round((baseDelay+randomExtra)*10)/10
```
`Loop each message` → `Wait while typing` → send → save → назад. **Реального typing-индикатора через API нет** — только Wait.

---

## 11. Manager-бот (02)
- **Вход**: Telegram polling, updates `["*"]`, бот «ассистент идей». Авторизован только chatId `1009982311` (3 хардкода). Роутер: callback_query → callback; `message.text` c `/` → command; иначе skip.
- **Команды**: `/start|/help` (справка), `/stats` (18 подзапросов-агрегатов), `/leads` (10 последних), `/photos` (10 фото + vision), `/lead <phone>` (карточка по LIKE).
- **Callbacks** (матчить по префиксу, не по длине среза; regex `^(approve_photo|offer_course|takeover|release|history|block|lead)_(.+)$`):
  - `lead_<phone>` → карточка (SELECT leads + msg_count + last in/out).
  - `takeover_<phone>` → `mode='manual', manual_until=now()+4h`.
  - `release_<phone>` → `mode='auto', manual_until=NULL` (funnel не трогает).
  - `block_<phone>` → `do_not_contact=true, funnel_stage='lost', mode='manual', manual_until=now()+100 years`.
  - `approve_photo_<phone>` → `photo_received=true, funnel_stage='qualified', mode='auto', manual_until=NULL` (обход Vision).
  - `offer_course_<phone>` → `funnel_stage='lost', mode='auto'` + Wazzup видеокурс `https://www.rusaencdmx.com/` + INSERT outbound. **Единственная исходящая лиду из manager-бота.**
  - `history_<phone>` → 15 сообщений хронологически, лимит 3900 симв.
- **Хвост callback**: Merge → `Answer callback query` (сразу, чтобы снять «часики») → `Log manager action` (manager_actions) → `IF skip edit`: read-действия (карточка/история) шлют НОВОЕ сообщение; destructive (takeover/block/release/approve/course) РЕДАКТИРУЮТ исходный алерт и убирают кнопки.
- **Телефон**: в БД `wa_<num>`; для Wazzup срезать префикс.
- **Идемпотентность**: нет защиты от двойного клика — гасить кнопки сразу / проверять состояние.
- **Свободного relay менеджер→лид НЕТ** — если нужен, добавлять с нуля.

---

## 12. Утилиты (03/04/05)
- **03 sheets_export**: каждые 30 мин + `/export` → `SELECT * FROM leads_export` → upsert в Google Sheet (id `1wUCIJn6XeRg-HXa6GR3xQxCUaZhr2XOaopH_7JPTY10`) по ID (телефон) → уведомление в TG. 18 колонок.
- **04 setup_headers**: разовый, неактуален для порта.
- **05 regen_embeddings**: `documents` (`content`+`embedding`), `text-embedding-3-small`, `UPDATE documents SET embedding='[..]'::vector`. Шаблон для заливки наших 49 сценариев.

---

## 13. Баги оригинала — НЕ переносить
1. `send_invitation_image` — мёртвая ветка (необъявленные `parsed/rawText/send_inv`, флаг не кладётся в output → `Send invitation?` читает undefined). Инвайт реально шлётся только через payment-поток.
2. `Send invitation image` использует `$('Normalize payload')..cleanPhone` — такого поля нет, надо `chat_id`.
3. `escort_mention_count` не инкрементится при первом упоминании — реализовать явный инкремент.
4. Рассинхрон даты/места ивента в WF1 (22 апр / 27 мая, 5500, Durango 175) — брать актуальные данные, не из WF1.

---

## 14. Конфликты техники WF1 с бизнес-моделью (источник = CLAUDE.md)
| Тема | WF1 (НЕ брать) | Актуально (CLAUDE.md) |
|---|---|---|
| Промпт Anna | event-first, Mila, 5500 | переписать заново |
| Продукты | Event 5500 + Agency $9000 | Starter $1,400/мес (1-й мес $3,000=$1,600 взнос+$1,400); Standard $10,000/6мес; VIP $14,000/год. В чате бот — **только Starter $1,400**. Standard/VIP — только на видеозвонке |
| Ивент | 5500 | не-член 9,000 песо / член Starter-Standard 4,000 / VIP бесплатно |
| Возраст | 28-60 (промпт + Vision) | **28-65** (везде, включая Vision estimated_age) |
| Whitelist клиентов | нет | добавить явный механизм (список номеров даст владелец) |
| Сценарии/RAG | documents(23)/scenarios(32) старые (Mila) | **49 актуальных** → залить в `scenarios` + embeddings (text-embedding-3-small), старое очистить |
| Голос | отбивка «пришли текст» | **Whisper-транскрибация** + алерт Anna |
| Запреты | — | не давать скидок, не давать телефоны девушек |
