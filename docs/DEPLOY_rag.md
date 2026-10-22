# Деплой режима rag — порядок шагов

⛔ Только после «деплой ок» владелицы. Флаги `bot_paused`, `scheduler_enabled`,
`event_active`, `stale_inbound_hours`, `tg_resume_enabled` не трогаем ни на одном шаге.
Откат на любом шаге — `docs/ROLLBACK_reply_mode.md`.

Перед началом: `git fetch origin && git log --oneline main..origin/main` пусто; ветка `rag`
влита в `main` и запушена вместе с меткой `pre-rag-2026-10-21`.

## 1. Бэкап

```bash
cd /Users/arina/Documents/matchmatch-bot
venv/bin/python -m scripts.backup_reply_mode     # scenarios + app_settings + промпт → docs/backups/
```

## 2. Миграция 008

Supabase → SQL Editor → содержимое `migrations/008_rag_knowledge_memory.sql` целиком.
Добавляет таблицу `knowledge`, колонки памяти в `leads`, ключи `event_price_next` (пустой:
лестницы цен нет) и `event_announced_at`. Старый путь их не читает.
Проверка: `SELECT count(*) FROM knowledge;` → 0.

## 3. Импорт базы знаний и эмбеддинги

```bash
venv/bin/python -m scripts.import_knowledge          # сухой прогон: «Вставить: 102»
venv/bin/python -m scripts.import_knowledge --write  # бэкап таблицы + запись, эмбеддинги < $0.001
```
Проверка: `SELECT count(*), count(embedding), count(*) FILTER (WHERE activo) FROM knowledge;`
→ 102, 102, 101 (выключена `event_price_steps`; число записей — как в `knowledge/seed.json`:
`python3 -c "import json; print(len(json.load(open('knowledge/seed.json'))['records']))"`).
Записи, которые владелица правила в мини-CRM (`updated_by` не пустой), `--write` не перезаписывает
в полях `activo`, `confirmado`, `contenido_es`; `--force` перезаписывает и их (тоже с бэкапом).
Пока таблица пустая, режим rag берёт `knowledge/seed.json` (запасной путь), но порядок
«импорт → потом rag_test» обязателен.

## 4. Правки прод-сценариев (с бэкапом внутри скрипта)

```bash
venv/bin/python -m scripts.patch_rag_scenarios_2026_10          # сухой прогон: №2, №15, №39, №51
venv/bin/python -m scripts.patch_rag_scenarios_2026_10 --force  # запись + бэкап строк
```
№40 не меняется (решение владелицы). Сухой прогон показывает и необязательные правки:
предложение со временем окончания («medianoche», «1:00») в №2 и №51, если оно есть в проде.

## 5. Настройки ивента по автоответу владелицы (2026-10-22)

Место — Roma Norte; цена предпродажи 6,000 с зачёркнутой 9,000 (`[event_promo]` рисует
`~9,000 MXN~` из `event_price_old` в обоих режимах); лестницы цен нет.
```sql
-- бэкап значений уже в шаге 1 (app_settings_before_rag_*.json)
UPDATE app_settings SET value = 'Roma Norte, CDMX', updated_at = now() WHERE key = 'event_address';
UPDATE app_settings SET value = '9,000', updated_at = now() WHERE key = 'event_price_old';
UPDATE app_settings SET value = '', updated_at = now() WHERE key = 'event_price_next';
-- срок предпродажи: только если владелица назовёт дату (ISO). Без ключа о сроке бот молчит.
-- INSERT INTO app_settings (key, value, updated_at) VALUES ('event_presale_until', '2026-10-29', now())
-- ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();
-- автоприветствие WhatsApp Business (его эхо не переводит лида в ручной режим). Начало
-- «¡Bienvenido a Match Match Agency!» узнаётся и без ключа; полный текст — по строке.
INSERT INTO app_settings (key, value, updated_at)
VALUES ('auto_greeting_texts', '["<полный текст автоприветствия из WhatsApp Business>"]', now())
ON CONFLICT (key) DO NOTHING;
-- жёсткий дневной лимит расхода OpenAI режима rag (по умолчанию 15, ключ можно не задавать)
INSERT INTO app_settings (key, value, updated_at) VALUES ('rag_daily_hard_limit_usd', '15', now())
ON CONFLICT (key) DO NOTHING;
```
Проверка: `SELECT key, value FROM app_settings WHERE key IN ('event_address','event_price_old',
'event_price_next','auto_greeting_texts','rag_daily_hard_limit_usd');`

## 5а. Пять лидов и тестовый номер, переведённые автоприветствием в ручной режим

…5225, …7255, …6165, …9003, …3426 и `wa_79635708880`: в базе не трогаем, решает Аня в
мини-CRM («Вернуть боту»), когда бот будет запущен.

## 6. WAZZUP_CHANNEL_ID на активный канал

✅ Сделано 2026-10-22 03:57 UTC: `6561310e-8637-47c2-96f0-5c59af347407` (active), бэкап
`/opt/matchmatch-bot/.env.bak-wazzup-20261022`. Шаг ниже — если канал снова переподключат.
```bash
ssh -o BatchMode=yes root@2.26.71.254 'set -e; cd /opt/matchmatch-bot \
  && cp -p .env .env.bak_$(date -u +%Y%m%dT%H%M%SZ) \
  && T=$(grep "^WAZZUP_TOKEN=" .env | cut -d= -f2- | tr -d "\r\"") \
  && NEW=$(curl -s -H "Authorization: Bearer $T" https://api.wazzup24.com/v3/channels | python3 -c "import json,sys; a=[c[\"channelId\"] for c in json.load(sys.stdin) if c.get(\"plainId\")==\"5215611897503\" and c.get(\"state\")==\"active\"]; print(a[0] if len(a)==1 else \"\")") \
  && test -n "$NEW" && sed -i "s/^WAZZUP_CHANNEL_ID=.*/WAZZUP_CHANNEL_ID=$NEW/" .env && echo "WAZZUP_CHANNEL_ID ok"'
```
Значение не печатается. Старый `.env` остаётся в `.env.bak_*` до проверки отправки.

## 7. Выкатить код, reply_mode = legacy

```bash
git push origin main pre-rag-2026-10-21
ssh -o BatchMode=yes root@2.26.71.254 'cd /opt/matchmatch-bot && git pull --ff-only origin main \
  && venv/bin/pip install -q -r requirements.txt && systemctl restart matchmatch-bot && sleep 5 \
  && systemctl is-active matchmatch-bot && curl -s http://127.0.0.1:8000/health && echo \
  && git log -1 --oneline && journalctl -u matchmatch-bot -n 100 --no-pager | grep -c Traceback || true'
```
Ключа `reply_mode` нет → legacy. В журнале на каждый ответ строка `reply_mode=legacy`.

## 8. rag_test на тестовых номерах

```sql
INSERT INTO app_settings (key, value, updated_at) VALUES ('reply_mode', 'rag_test', now())
ON CONFLICT (key) DO UPDATE SET value = 'rag_test', updated_at = now();
```
Перед проверкой: тестовый номер в ручном режиме после автоприветствия — «Вернуть боту» в мини-CRM.
Проверка с тестового номера (вкладка «Тест» мини-CRM): ответы по чек-листу, в журнале
`reply_mode=rag`, в `/status` менеджер-бота строка расхода. Остальные лиды — legacy.
Включение `rag` для всех — отдельной командой владелицы.
