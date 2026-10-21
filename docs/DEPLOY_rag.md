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
Добавляет таблицу `knowledge`, колонки памяти в `leads`, ключи `event_price_next=7000` и
`event_announced_at`. Старый путь их не читает.
Проверка: `SELECT count(*) FROM knowledge;` → 0, `SELECT value FROM app_settings WHERE key='event_price_next';` → 7000.

## 3. Импорт базы знаний и эмбеддинги

```bash
venv/bin/python -m scripts.import_knowledge          # сухой прогон: «Вставить: 95»
venv/bin/python -m scripts.import_knowledge --force  # запись, эмбеддинги < $0.001
```
Проверка: `SELECT count(*), count(embedding) FROM knowledge;` → 95, 95.
Пока таблица пустая, режим rag берёт `knowledge/seed.json` (запасной путь), но порядок
«импорт → потом rag_test» обязателен.

## 4. Правки прод-сценариев (с бэкапом внутри скрипта)

```bash
venv/bin/python -m scripts.patch_rag_scenarios_2026_10          # сухой прогон: №2, №15, №39, №51
venv/bin/python -m scripts.patch_rag_scenarios_2026_10 --force  # запись + бэкап строк
```
№40 не меняется (решение владелицы).

## 5. Убрать зачёркнутую «было» в старом режиме

8000 на сайте — будущая ступень цены, а не старая. Старый путь рисует `~8,000 MXN~` из
`event_price_old`; пустое значение → токен `[event_promo]` исчезает везде.
```sql
-- бэкап значения уже в шаге 1 (app_settings_before_rag_*.json)
UPDATE app_settings SET value = '', updated_at = now() WHERE key = 'event_price_old';
```

## 6. WAZZUP_CHANNEL_ID на активный канал

Канал 5215611897503 переподключён, у нового другой ID; в `.env` сервера старый (blocked).
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
Проверка с тестового номера (вкладка «Тест» мини-CRM): ответы по чек-листу, в журнале
`reply_mode=rag`, в `/status` менеджер-бота строка расхода. Остальные лиды — legacy.
Включение `rag` для всех — отдельной командой владелицы.
