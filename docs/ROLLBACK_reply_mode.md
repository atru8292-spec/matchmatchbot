# Откат режима ответов (reply_mode)

Новый режим ответов (rag) включается только ключом `app_settings.reply_mode`:

| Значение | Поведение |
|---|---|
| нет ключа, пусто, `legacy`, любое другое, ошибка чтения | старый путь `ai.generate_reply`, как до переделки |
| `rag_test` | rag только для тестовых номеров (`SILENT_BYPASS_PHONES` ∪ вкладка «Тест» мини-CRM), остальным legacy |
| `rag` | rag для всех |

Ключ читается при каждом ответе без кэша (`reply_mode.resolve`, `db.get_setting`):
переключение действует со следующего сообщения лида, без деплоя и рестарта.
Сбой режима rag во время ответа (или после генерации, при отправке) → лиду ничего не уходит
старым путём: молчание, ручной режим, алерт Ане и техалерт (решение владелицы 2026-10-22).
Старым путём отвечают только `reply_mode=legacy` и обычные лиды при `rag_test`.

Новые таблицы и колонки (knowledge, память лида) старый путь не читает: их можно не удалять.
Что меняется и для старого пути (патчами, с бэкапами):
- `anna_prompt_v5.md` — убраны разрешения называть 5,500
  (бэкап `docs/backups/anna_prompt_v5_before_5500_removal_*.md`);
- сценарии №2, №15, №39, №51 — `scripts/patch_rag_scenarios_2026_10.py`
  (бэкап строк `docs/backups/scenarios_2_15_39_51_before_rag_patch_*.json`);
- дата лиду без года и `[event_promo]` = `~9,000 MXN~` — общий `sender`;
- автоприветствие WhatsApp не переводит лида в ручной режим.
Сверять откат промпта и сценариев — с этими новыми бэкапами, а не только с бэкапом 2026-10-21.

## 0. Что сделано до начала работы

- Метка: `pre-rag-2026-10-21` → `e89e1a3` (последний коммит до переделки).
- Ветка: `rag`.
- Бэкап (2026-10-21, только чтение):
  - `docs/backups/scenarios_all_before_rag_20261021T142906Z.json` — 59 строк, все поля, эмбеддинги текстом;
  - `docs/backups/app_settings_before_rag_20261021T142906Z.json` — 27 ключей;
  - `docs/backups/anna_prompt_v5_before_rag_20261021T142906Z.md`
    (sha256 `17785b20a6c92b12b6464858b27d5012dfdd30097bed0f67b0a2b50a76860f53`).
- Новый бэкап в любой момент: `venv/bin/python -m scripts.backup_reply_mode`.

## 1. Быстрый откат поведения (секунды, без деплоя и рестарта)

Supabase → SQL Editor:
```sql
UPDATE app_settings SET value = 'legacy', updated_at = now() WHERE key = 'reply_mode';
```
или удалить ключ (нет ключа = legacy):
```sql
DELETE FROM app_settings WHERE key = 'reply_mode';
```
или по SSH:
```bash
ssh root@2.26.71.254 'cd /opt/matchmatch-bot && venv/bin/python -c "
import asyncio, db
async def m():
    await db.init_pool(); await db.set_setting(\"reply_mode\", \"legacy\"); await db.close_pool()
asyncio.run(m())"'
```
Включить обратно: то же значение `rag_test` или `rag`.

## 2. Откат кода

Если сломан сам код (а не поведение rag), вернуть сервер на метку:
```bash
ssh root@2.26.71.254 'cd /opt/matchmatch-bot && git fetch origin --tags && git checkout -B main pre-rag-2026-10-21 && systemctl restart matchmatch-bot && git log -1 --oneline'
```
Метку нужно запушить до деплоя ветки: `git push origin pre-rag-2026-10-21`.
Локально после этого: `git revert -m 1 <merge-коммит ветки rag>` и обычный деплой.

## 3. Вернуть промпт

Промпт v5 менялся патчем (убраны разрешения 5,500). Вернуть версию до патча:
```bash
ls docs/backups/anna_prompt_v5_before_5500_removal_*.md
cp docs/backups/anna_prompt_v5_before_5500_removal_<метка>.md anna_prompt_v5.md
```
Версия до всей переделки: `docs/backups/anna_prompt_v5_before_rag_20261021T142906Z.md`
(sha256 `17785b20…0f53`).
Промпт кэшируется в процессе: после деплоя нужен `systemctl restart matchmatch-bot`.

## 4. Вернуть сценарии

⛔ НЕ `scripts.load_scenarios` (удаляет все сценарии и пересоздаёт из устаревшего JSON).
`scripts/restore_scenarios.py` ничего не удаляет: обновляет отличающиеся строки и
вставляет недостающие из бэкапа, лишние строки только показывает.
```bash
F=docs/backups/scenarios_all_before_rag_20261021T142906Z.json
venv/bin/python -m scripts.restore_scenarios --file $F            # сухой прогон: что поменяется
venv/bin/python -m scripts.restore_scenarios --file $F --force    # запись, одна транзакция
venv/bin/python -m scripts.restore_scenarios --file $F --ids 39,40 --force   # только часть
```
Сценарии читаются из БД на каждый запрос: рестарт не нужен.

## 5. Проверка, что откат сработал

```bash
F=docs/backups/scenarios_all_before_rag_20261021T142906Z.json
venv/bin/python -m scripts.restore_scenarios --file $F --verify   # «Расхождений: 0», код выхода 0
ssh root@2.26.71.254 'cd /opt/matchmatch-bot && git log -1 --oneline \
  && journalctl -u matchmatch-bot --since "15 min ago" --no-pager | grep -o "reply_mode=[a-z_]*" | sort | uniq -c \
  && shasum -a 256 anna_prompt_v5.md'
```
- `SELECT value FROM app_settings WHERE key = 'reply_mode'` → `legacy` или строки нет;
- в журнале только `reply_mode=legacy`;
- sha256 промпта совпадает с тем бэкапом, к которому откатывали;
- `scripts/smoke_launch.py` отвечает как раньше: первый ответ «Hola! 🤍 Soy Anna, fundadora de Match Match Agency. ¿Cómo te llamas? ¿Eres soltero?».
