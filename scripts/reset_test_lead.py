"""Сброс тестового лида «с чистого листа» для проверки бота (2026-10-22).

Только для номеров из списка тестовых (SILENT_BYPASS_PHONES и вкладка «Тест» мини-CRM);
на любой другой номер, включая реальных лидов, — отказ. По умолчанию сухой прогон: показывает,
что изменится, и ничего не пишет. Запись — только с --commit.

Что делает при --commit (одна транзакция):
  1. бэкап строки лида, его сообщений, смен стадий, фото и действий менеджера в
     docs/backups/ и ~/matchmatch-backups/<дата>/ — ДО записи;
  2. проверяет, что условие бьёт ровно по одному лиду;
  3. удаляет только его сообщения;
  4. сбрасывает анкету и память (имя, «холост ли», возраст, профессия, интерес, фото, email,
     звонок, сводка памяти), стадию → new, режим → auto.
Журнал смен стадий, фото и действия менеджера не трогает.

venv/bin/python -m scripts.reset_test_lead                       # сухой прогон, wa_79635708880
venv/bin/python -m scripts.reset_test_lead --commit              # запись
venv/bin/python -m scripts.reset_test_lead --phone wa_52155... --commit
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")

import db
from phones import canonical_phone, canonical_set

DEFAULT_PHONE = "wa_79635708880"
ROOT = Path(__file__).resolve().parent.parent
RESET_SQL = """UPDATE leads SET name=NULL, last_name=NULL, interest=NULL, age=NULL, profession=NULL,
  is_single=NULL, city=NULL, country=NULL, email=NULL, photo_received=false, funnel_stage='new',
  mode='auto', manual_until=NULL, next_followup_at=NULL, followup_sent_count=0, objection_count=0,
  last_objection_type=NULL, escalate_reason=NULL, last_intent=NULL, date_of_birth=NULL,
  marital_status=NULL, desired_partner_age=NULL, selected_service=NULL, budget_signal=NULL,
  invitation_sent_at=NULL, videocall_at=NULL, videocall_reminded_at=NULL, videocall_event_id=NULL,
  memoria_resumen=NULL, memoria_hasta_message_id=NULL, memoria_mensajes=NULL,
  memoria_updated_at=NULL, rag_estado='{}', updated_at=now() WHERE phone=$1"""
BACKUP_TABLES = ("messages", "funnel_events", "lead_photos", "manager_actions")


class NotATestNumber(Exception):
    pass


def backup_dirs(now: datetime) -> list[Path]:
    return [ROOT / "docs" / "backups",
            Path.home() / "matchmatch-backups" / now.strftime("%Y-%m-%d")]


async def reset(conn, phone: str, test_phones, commit: bool,
                dirs: list[Path] | None = None) -> dict:
    """Сбросить тестового лида. conn — соединение asyncpg. Возвращает сводку."""
    if canonical_phone(phone) not in canonical_set(test_phones):
        raise NotATestNumber(f"{phone} нет в списке тестовых номеров — сброс запрещён")
    n_leads = await conn.fetchval("SELECT count(*) FROM leads WHERE phone=$1", phone)
    if n_leads != 1:
        raise RuntimeError(f"условие бьёт по {n_leads} лидам, нужен ровно один — ничего не делаю")
    n_msgs = await conn.fetchval("SELECT count(*) FROM messages WHERE lead_phone=$1", phone)
    summary = {"phone": phone, "messages": n_msgs, "commit": commit, "backup": []}
    if not commit:
        return summary
    now = datetime.now(timezone.utc)
    dump = {"lead": dict(await conn.fetchrow("SELECT * FROM leads WHERE phone=$1", phone))}
    for t in BACKUP_TABLES:
        dump[t] = [dict(r) for r in await conn.fetch(f"SELECT * FROM {t} WHERE lead_phone=$1", phone)]
    name = f"test_lead_{phone}_before_reset_{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    text = json.dumps(dump, ensure_ascii=False, indent=1, default=str)
    for d in dirs or backup_dirs(now):
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text, encoding="utf-8")
        summary["backup"].append(str(d / name))
    async with conn.transaction():
        summary["deleted"] = await conn.execute("DELETE FROM messages WHERE lead_phone=$1", phone)
        summary["updated"] = await conn.execute(RESET_SQL, phone)
    return summary


async def main() -> int:
    ap = argparse.ArgumentParser(description="Сброс тестового лида (только тестовые номера)")
    ap.add_argument("--phone", default=DEFAULT_PHONE)
    ap.add_argument("--commit", action="store_true", help="записать (без флага — сухой прогон)")
    args = ap.parse_args()
    await db.init_pool()
    try:
        test_phones = await db.bypass_phone_set()
        async with db._get_pool().acquire() as conn:
            try:
                s = await reset(conn, args.phone, test_phones, args.commit)
            except NotATestNumber as e:
                print(f"❌ {e}")
                return 2
        print(f"Лид {s['phone']}: сообщений {s['messages']}")
        if not s["commit"]:
            print("Сухой прогон: ничего не записано. Для записи: --commit")
            return 0
        print("Бэкап:", *s["backup"], sep="\n  ")
        print(f"{s['deleted']}, {s['updated']}: анкета и стадия сброшены, режим auto")
        return 0
    finally:
        await db.close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
