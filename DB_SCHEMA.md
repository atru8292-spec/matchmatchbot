# Схема БД Supabase (проект match: xbcynfwbsyufxaqtoror)

> Реальная структура прода на момент миграции. leads/messages ПУСТЫЕ (0 записей).
> Использовать эти точные имена полей в db.py — не гадать.
> RLS/доступ: подключение по service_role key + Postgres connection string.

## Общее
- Project ref: `xbcynfwbsyufxaqtoror`
- URL: `https://xbcynfwbsyufxaqtoror.supabase.co`
- DB host: `db.xbcynfwbsyufxaqtoror.supabase.co`
- Расширение pgvector установлено (для documents.embedding)
- Storage buckets: `lead-photos` (public), `invitations`

---

## Таблица: leads (46 колонок)
Первичный ключ — id (uuid), но БИЗНЕС-КЛЮЧ везде phone (text, NOT NULL, unique в коде).
WF1 и весь код джойнят по phone, не по id.

| колонка | тип | null | default |
|---|---|---|---|
| id | uuid | NO | gen_random_uuid() |
| phone | text | NO | — (бизнес-ключ, ON CONFLICT по нему) |
| name | text | YES | |
| whatsapp_name | text | YES | |
| source | text | YES | 'whatsapp' |
| status | text | YES | 'new' |
| interest | text | YES | (event/agency/both) |
| age | integer | YES | |
| profession | text | YES | |
| is_single | boolean | YES | |
| city | text | YES | |
| country | text | YES | |
| tags | text[] | YES | '{}' |
| mode | text | YES | 'auto' (auto/manual) |
| do_not_contact | boolean | NO | false |
| escalate_reason | text | YES | |
| next_followup_at | timestamptz | YES | |
| followup_sent_count | integer | YES | 0 |
| manual_until | timestamptz | YES | |
| last_inbound_at | timestamptz | YES | |
| last_ai_message_at | timestamptz | YES | |
| last_human_message_at | timestamptz | YES | |
| last_message_at | timestamptz | YES | |
| last_intent | text | YES | |
| calendar_link | text | YES | |
| notes | text | YES | |
| created_at | timestamptz | YES | now() |
| updated_at | timestamptz | YES | now() |
| budget_signal | text | YES | (low/medium/high) |
| objection_count | integer | YES | 0 |
| last_objection_type | text | YES | |
| source_campaign | text | YES | |
| funnel_stage | text | YES | 'new' |
| imported_at | timestamptz | YES | |
| import_batch_id | text | YES | |
| extra_data | jsonb | YES | '{}' |
| photo_received | boolean | YES | false |
| escort_mention_count | integer | YES | 0 |
| last_name | text | YES | |
| email | text | YES | |
| date_of_birth | date | YES | |
| marital_status | text | YES | |
| business_link | text | YES | |
| desired_partner_age | text | YES | |
| selected_service | text | YES | (starter/standard/vip/event) |
| invitation_sent_at | timestamptz | YES | |

---

## Таблица: messages (12 колонок)
История переписки. Джойн по lead_phone.

| колонка | тип | null | default |
|---|---|---|---|
| id | uuid | NO | gen_random_uuid() |
| lead_phone | text | NO | |
| direction | text | NO | (inbound/outbound) |
| sender | text | NO | 'lead' (lead/anna/manager) |
| text | text | NO | |
| message_uid | text | YES | (для идемпотентности) |
| processed | boolean | NO | false |
| processed_at | timestamptz | YES | |
| grouped_in_run | text | YES | (debounce-группа) |
| meta | jsonb | YES | '{}' |
| created_at | timestamptz | YES | now() |
| external_message_id | text | YES | (Wazzup messageId, для дедупа) |

Идемпотентность входящих: ON CONFLICT по external_message_id (или message_uid).

---

## Таблица: lead_photos (15 колонок)
Фото лидов + результат Vision.

| колонка | тип | null | default |
|---|---|---|---|
| id | bigint | NO | seq |
| lead_phone | text | NO | |
| channel | text | NO | |
| external_file_id | text | YES | |
| storage_url | text | YES | (public URL в bucket lead-photos) |
| storage_path | text | YES | |
| file_size_bytes | integer | YES | |
| mime_type | text | YES | |
| vision_analyzed | boolean | YES | false |
| vision_analysis | jsonb | YES | (полный ответ Vision) |
| vision_verdict | text | YES | (ok/reject/manual/payment_ok) |
| vision_reasons | text[] | YES | |
| received_at | timestamptz | YES | now() |
| analyzed_at | timestamptz | YES | |
| is_primary | boolean | YES | false |

---

## Таблица: manager_actions (7 колонок)
Лог действий менеджера (takeover/block/approve_photo/...).

| колонка | тип | null | default |
|---|---|---|---|
| id | bigint | NO | seq |
| lead_phone | text | YES | |
| action | text | YES | |
| performed_by | text | YES | |
| manager_chat_id | text | YES | |
| meta | jsonb | YES | |
| created_at | timestamptz | YES | now() |

---

## Таблица: documents (RAG playbook)
Сейчас 23 записи со СТАРЫМ контентом (Mila) — перезальём нашими 49 сценариями.

| колонка | тип | null | default |
|---|---|---|---|
| id | bigint | NO | seq |
| content | text | YES | (текст сценария) |
| metadata | jsonb | YES | |
| embedding | vector | YES | (pgvector, text-embedding-3-small = 1536 dim) |

RAG-поиск: cosine по embedding, topK. Механизм из WF1.

---

## Прочие таблицы (существуют, детали по запросу)
- scenarios (32 записи, структура trigger_description/template_es/links/rules/is_active) — старый контент
- leads_export (VIEW, 18 колонок) — для Google Sheets экспорта
- outbox_messages, conversation_jobs — от старого v4, НЕ используем
- deals, broadcasts, broadcast_recipients, events, import_batches — вспомогательные
- v_funnel_stats, v_pending_escalations — вьюхи для статистики

---

## Что нужно в .env для db.py (блок 2)
```
SUPABASE_URL=https://xbcynfwbsyufxaqtoror.supabase.co
SUPABASE_SERVICE_KEY=<взять в панели: Settings → API → service_role secret>
SUPABASE_DB_PASSWORD=<Settings → Database → пароль или reset>
# Postgres connection string соберётся из host + password
```
