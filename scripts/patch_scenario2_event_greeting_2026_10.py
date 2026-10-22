"""Сценарий №2: дословное приветствие на первое сообщение про ивент (решение Арины 2026-10-22).

Текст — knowledge.greeting_event (утверждён), два сообщения: приветствие с абзацем про ивент
и токеном [event_theme_phrase] (тема из app_settings.event_theme_phrase, пусто — без фразы);
отдельно вопрос про имя и «холост». ai_allowed=false: уходит дословно, без модели. Код ai.py
шлёт его так только при первом контакте и без вопроса/просьбы в сообщении — без этого кода
(и подстановки токена в sender) патч НЕ записывать.

Сухой прогон — UPDATE в транзакции с откатом, показывает diff. Запись (--force): бэкап строки
в docs/backups/ и ~/matchmatch-backups/2026-10-22/, потом UPDATE. Старый текст в БД должен
совпасть с ожидаемым (после патча 2026-10-22), иначе ничего не пишем.
Откат: scripts/restore_scenarios.py --file <бэкап> --ids 2 --force.
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")

import db

ROOT = Path(__file__).resolve().parent.parent
HOME_BACKUPS = Path.home() / "matchmatch-backups" / "2026-10-22"
OLD = ("Hola! 🤍 Soy Anna, fundadora de Match Match Agency. Con mucho gusto te cuento sobre nuestro "
       "evento, Slavic-Latino Connect Night: una noche en la que vas a poder conocer a 30 mujeres "
       "eslavas solteras que buscan una relación seria. Esta vez es temática de Halloween: puedes "
       "venir disfrazado y, si no tienes disfraz, te damos una máscara.\n\nEl precio es de "
       "[event_price_nonmember] MXN[event_promo] e incluye bebida de bienvenida y botanas. Aquí está "
       "el enlace para tu boleto, con fotos y videos de eventos pasados: [event_link] 🤍")
NEW = ("Hola, soy Anna, fundadora de Match Match Agency 🤍\n"
       "El [event_date] tenemos Slavic-Latino Connect Night en [event_address]: una noche para "
       "conocer en persona a mujeres eslavas solteras que buscan una relación seria. "
       "[event_theme_phrase]\n\n"
       "¿Cómo te llamas y estás soltero?")


class Rollback(Exception):
    pass


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    await db.init_pool()
    try:
        pool = db._get_pool()
        row = await pool.fetchrow(
            "SELECT id, title, trigger_description, trigger_es, template_es, links, rules, mode, "
            "ai_allowed, blocks_lead, is_active, trigger_type, embedding::text AS embedding "
            "FROM scenarios WHERE id = 2")
        if row is None or row["template_es"] != OLD:
            print("❌ Текст №2 в БД не совпадает с ожидаемым — ничего не пишу")
            return 1
        print(f"#2 ai_allowed: {row['ai_allowed']} → False")
        for line in difflib.unified_diff(OLD.split("\n"), NEW.split("\n"), lineterm="", n=0):
            print("  ", line)
        if args.force:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = ROOT / "docs" / "backups" / f"scenario_2_before_event_greeting_{ts}.json"
            backup.write_text(json.dumps([dict(row)], ensure_ascii=False, indent=1, default=str),
                              encoding="utf-8")
            HOME_BACKUPS.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, HOME_BACKUPS / backup.name)
            print(f"Бэкап: {backup.relative_to(ROOT)} (+ копия в {HOME_BACKUPS})")
        async with pool.acquire() as conn:
            try:
                async with conn.transaction():
                    r = await conn.execute("UPDATE scenarios SET template_es = $1, ai_allowed = false, "
                                           "updated_at = now() WHERE id = 2 AND template_es = $2", NEW, OLD)
                    print(r)
                    if r != "UPDATE 1":
                        raise RuntimeError(f"ожидала UPDATE 1, получила {r}")
                    if not args.force:
                        raise Rollback()
                print("Записано: #2")
            except Rollback:
                print("Сухой прогон: UPDATE выполнен и откатан. Для записи: --force")
        return 0
    finally:
        await db.close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
