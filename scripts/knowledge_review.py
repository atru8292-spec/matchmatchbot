"""knowledge/seed.json → docs/proposals/knowledge_review_ru.md: все записи базы знаний
с русским переводом и статусом «подтверждено / ждёт Аню» для ревью владелицей.

Запуск: venv/bin/python -m scripts.knowledge_review
Статус «ждёт Аню»: confirmado=false или пометка «ждёт Аню» / «подтверждает Аня» в fuente/ru.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "knowledge" / "seed.json"
OUT = ROOT / "docs" / "proposals" / "knowledge_review_ru.md"

SECTIONS = [
    ("event", "Ивент 18.11.2026: факты"),
    ("faq", "Частые вопросы"),
    ("service", "Услуга"),
    ("tariff", "Тариф"),
    ("base_script", "Заготовки (base_script)"),
    ("objection", "Возражения"),
    ("policy", "Политики (эскалация, фильтры, блоки)"),
    ("style", "Стиль (инструкции модели)"),
    ("style_pair", "Пары «плохо / хорошо»"),
]
ACCION_RU = {"respond": "ответить", "silent_escalate": "молчать, ручной режим, алерт Ане"}


def status(r: dict) -> str:
    marks = f"{r.get('fuente') or ''} {r.get('ru') or ''}"
    if not r["confirmado"] or "ждёт Аню" in marks or "подтверждает Аня" in marks:
        return "⏳ ждёт Аню"
    return "✅ подтверждено"


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.split("\n"))


def render(seed: dict) -> str:
    recs = seed["records"]
    waiting = [r for r in recs if status(r).startswith("⏳")]
    out = [
        "# База знаний режима rag: ревью",
        "",
        f"Источник: `knowledge/seed.json`, версия {seed['version']}. Записей: {len(recs)}, "
        f"ждут Аню: {len(waiting)}.",
        "Документ собран скриптом `scripts/knowledge_review.py`, руками не править.",
        "",
        "- Записи с `confirmado=false` в ответы не подаются, пока Аня не подтвердит.",
        "- Поле «плохо» у пар идёт только в тесты, модель его не видит.",
        "- Токены `[event_date]`, `[event_start]`, `[event_address]`, `[event_price_nonmember]`, "
        "`[event_promo]`, `[event_link]` подставляет система из настроек ивента "
        "(18 de noviembre de 2026, 8:30 PM, Polanco, CDMX, 6000, ~8,000 MXN~, ссылка).",
        "",
        "## Ждут Аню",
        "",
    ]
    out += [f"- `{r['id']}`: {r['titulo']}" for r in waiting]
    for tipo, title in SECTIONS:
        group = [r for r in recs if r["tipo"] == tipo]
        if not group:
            continue
        out += ["", f"## {title}", ""]
        for r in group:
            out += [f"### `{r['id']}` · {r['titulo']} · {status(r)}", ""]
            if r["tipo"] == "style_pair":
                out += [f"- **Лид:** {r['cuando_usar']}",
                        f"- **Плохо:** {r['malo']}",
                        f"- **Хорошо:** {r['bueno']}",
                        f"- **Почему (для модели):** {r['por_que']}",
                        f"- **Перевод:** {r['ru']}", ""]
                continue
            if r["contenido_es"]:
                out += ["**Текст (es):**", "", _quote(r["contenido_es"]), ""]
            out += [f"**Перевод:** {r['ru']}", ""]
            out.append(f"- **Когда:** {r['cuando_usar']}")
            if r["como_responder"]:
                out.append(f"- **Как отвечать (для модели):** {r['como_responder']}")
            if r["notas_tono"]:
                out.append(f"- **Тон:** {r['notas_tono']}")
            for f in r["frases_ejemplo"]:
                out.append(f"- **Образец фразы:** {f}")
            flags = [f"действие: {ACCION_RU[r['accion']]}"]
            if r["literal"]:
                flags.append("можно почти дословно")
            if r["situacion"]:
                flags.append(f"дословно без модели при условии `{json.dumps(r['situacion'], ensure_ascii=False)}`")
            if r["escalar_despues"]:
                flags.append("после ответа чат переходит к Ане")
            if r["escenario_literal"]:
                flags.append(f"фиксированный текст сценария №{', №'.join(map(str, r['escenario_literal']))}")
            if r["valid_to"]:
                flags.append(f"действует до {r['valid_to']}")
            out.append(f"- **Флаги:** {'; '.join(flags)}")
            out.append(f"- **Источник:** {r['fuente']}")
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def main() -> None:
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(seed), encoding="utf-8")
    print(f"{OUT.relative_to(ROOT)}: {len(seed['records'])} записей")


if __name__ == "__main__":
    main()
