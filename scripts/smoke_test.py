"""Smoke-test: реальные звонки в AI (не моки) на конкретных паттернах багов,
найденных на живых тестах Ани/Милы 2026-08-06...12.

Зачем отдельно от pytest: юнит-тесты (test_main_integration.py, 1099 шт.) мокают
ai.generate_reply/vision.analyze_photo почти везде — они проверяют КОД (какой
scenario_id вызван, какие поля обновлены), но НЕ видят регрессий в самом
ПРОМПТЕ/AI-поведении (повторы, ложные подтверждения, тон). Этот скрипт наоборот —
дёргает реальный OpenAI по конкретным сценариям, которые уже один раз ломались.

Не CI (стоит токенов + реальное время ответа), гонять руками:
  venv/bin/python -m scripts.smoke_test
После КАЖДОЙ правки anna_prompt_v5.md / filters.py / main.py фото-ветки —
ДО того, как звать Аню/Милу тестировать руками.

Не 100% детерминировано (LLM), поэтому проверки — по ключевым сигналам
(нет дословных повторов, нет запрещённых фраз, есть ожидаемое ключевое слово),
а не жёсткое сравнение текста. Красный результат — повод посмотреть глазами,
не обязательно баг.
"""
from __future__ import annotations

import asyncio
import re
import sys

sys.path.insert(0, ".")

import ai
import db
import filters

PASS, FAIL, results = "✓", "✗", []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name, detail))
    mark = PASS if ok else FAIL
    print(f"[{mark}] {name}" + (f" — {detail}" if detail and not ok else ""))


async def check_dob_and_desired_age_combined():
    """Регресс 2026-08-09: '1991'+'25-35' в одном сообщении — оба поля должны
    извлечься, бот не должен переспрашивать желаемый возраст партнёрши повторно."""
    lead = {"age": 35, "profession": "abogado", "is_single": True,
            "funnel_stage": "pitched", "photo_received": True, "name": "Benito"}
    hist = [{"sender": "anna",
             "text": "Perfecto Benito! Solo me falta tu fecha de nacimiento, me la compartes?"}]
    r = await ai.generate_reply(lead, hist, "1991\n25-35")
    ok = ("date_of_birth" in r["extracted"] and "desired_partner_age" in r["extracted"])
    check("DOB + желаемый возраст партнёра в одном сообщении → оба извлечены", ok,
          f"extracted={r['extracted']}")


async def check_false_photo_claim_not_confirmed():
    """Регресс 2026-08-09: лид врёт 'ya te envié' при photo_received=false —
    бот НЕ должен подтверждать получение."""
    lead = {"age": 35, "profession": "abogado", "is_single": True,
            "funnel_stage": "qualifying", "photo_received": False, "name": "Benito"}
    hist = [{"sender": "anna", "text": "Me mandas una foto tuya porfa?"}]
    r = await ai.generate_reply(lead, hist, "Pero ya te envié")
    text = " ".join(r["messages"]).lower()
    bad_phrases = ["ya tengo tu foto", "ya vi tu foto", "ya la tengo", "ya la recibí"]
    ok = not any(p in text for p in bad_phrases)
    check("Лид врёт 'ya te envié' (photo_received=false) → бот НЕ подтверждает", ok, text)


async def check_eres_ai_denial():
    """'Eres AI?' должно уйти в сценарий #40 (отрицание), не в игнор/пропуск."""
    lead = {"age": 35, "profession": "abogado", "is_single": True, "funnel_stage": "pitched"}
    hist = [{"sender": "anna", "text": "Te cuento cómo funciona el servicio..."}]
    r = await ai.generate_reply(lead, hist, "Eres AI?")
    text = " ".join(r["messages"]).lower()
    ok = ("soy anna" in text or "para nada" in text) and "soy un bot" not in text \
        and "soy una ia" not in text
    check("'Eres AI?' → niega con calidez, no confirma ser bot", ok, text)


async def check_price_objection_offers_event():
    """Лид уходит после защиты ценности сервиса → должен предложить ивент (не отпускать молча)."""
    lead = {"age": 41, "profession": "empresario", "is_single": True,
            "funnel_stage": "pitched", "photo_received": True}
    hist = [
        {"sender": "lead", "text": "cuanto cuesta"},
        {"sender": "anna", "text": "Desde $10,000 USD... 15 mujeres en 6 meses"},
        {"sender": "lead", "text": "esta caro"},
        {"sender": "anna", "text": "Te entiendo... el precio pasa a segundo plano"},
    ]
    r = await ai.generate_reply(lead, hist, "no me alcanza, mejor lo dejamos")
    text = " ".join(r["messages"]).lower()
    ok = "evento" in text or "6,000" in text or "6000" in text
    check("Lead se va tras defensa de precio → ofrece evento (no lo suelta)", ok, text)


async def check_full_decline_sets_nurture():
    """Rechazo total explícito → funnel_stage='nurture' (para no perseguir con follow-ups)."""
    lead = {"age": 41, "profession": "empresario", "is_single": True, "funnel_stage": "pitched"}
    r = await ai.generate_reply(lead, [], "no gracias, ya no me interesa nada de esto")
    ok = r["funnel_stage"] == "nurture"
    check("Rechazo total → funnel_stage=nurture", ok, f"funnel_stage={r['funnel_stage']!r}")


def check_scam_skepticism_not_blocked():
    """Regresión 2026-08-06: pregunta de duda legítima NO debe bloquear (deterministico, sin AI)."""
    ok = not filters.is_aggression("como se que esto no es una estafa")
    check("'cómo sé que no es estafa' → NO bloquea (pregunta legítima)", ok)


def check_real_insult_still_blocks():
    """El fix de arriba no debe abrir hueco: insulto directo sigue bloqueando."""
    ok = filters.is_aggression("eres un pendejo")
    check("'eres un pendejo' → SÍ bloquea (control, no debe romperse)", ok)


def check_escort_negation_not_blocked():
    """Regresión 2026-08-06: negar interés sexual NO debe bloquear como escort."""
    ok = not filters.is_escort_mention("no busco sexo, quiero algo serio")
    check("'no busco sexo, quiero algo serio' → NO bloquea (confirma seriedad)", ok)


def check_event_companion_not_blocked():
    """'¿Puedo llevar acompañante al evento?' NO debe bloquear como escort."""
    ok = not filters.is_escort_mention("¿Puedo llevar acompañante al evento?")
    check("Preguntar por llevar acompañante al evento → NO bloquea", ok)


async def check_golden_path_no_repetition():
    """Camino feliz completo: no debe repetir preguntas ya contestadas en el mismo turno."""
    lead = {"funnel_stage": "new"}
    hist = []
    r1 = await ai.generate_reply(lead, hist, "Hola")
    hist.append({"sender": "lead", "text": "Hola"})
    hist.append({"sender": "anna", "text": " ".join(r1["messages"])})
    lead2 = dict(lead, **r1["extracted"])
    r2 = await ai.generate_reply(lead2, hist, "Sí soy soltero, 35 años, abogado")
    ok = "soltero" not in " ".join(r2["messages"]).lower()  # no debe volver a preguntar
    check("Camino feliz: no repreguntar 'eres soltero' tras contestarlo", ok,
          " | ".join(r2["messages"]))


async def check_duplicate_message_no_verbatim_repeat():
    """Регресс 2026-08-26 (test real vía mini-CRM/Telegram): lead manda el MISMO mensaje
    dos veces seguidas ('Evento') → el bot NO debe responder con el texto idéntico, ni
    tampoco saludar como si hubiera pasado tiempo ('hola de nuevo') — conversation_history
    no trae timestamps, no hay base para asumir que pasó tiempo."""
    lead = {"funnel_stage": "new"}
    hist = []
    r1 = await ai.generate_reply(lead, hist, "Evento")
    hist.append({"sender": "lead", "text": "Evento"})
    for m in r1["messages"]:
        hist.append({"sender": "anna", "text": m})
    r2 = await ai.generate_reply(lead, hist, "Evento")
    text1 = " ".join(r1["messages"]).strip().lower()
    text2 = " ".join(r2["messages"]).strip().lower()
    re_greet = any(p in text2 for p in ("de nuevo", "otra vez", "hola de nuevo"))
    ok = text1 != text2 and not re_greet
    check("Mensaje repetido ('Evento'×2) → NO responde idéntico ni re-saluda", ok,
          f"1º: {text1!r} | 2º: {text2!r}")


async def check_new_lead_event_word_not_escalated():
    """Регресс 2026-08-24 (test real vía mini-CRM): lead NUEVO (no event_attended) manda
    algo corto con 'evento' → RAG a veces matcheaba #24 (feedback POST-evento, bot_then_anna)
    solo por la palabra en común, forzando escalate sin motivo. Gate por funnel_stage
    debe evitarlo."""
    lead = {"funnel_stage": "new"}
    r = await ai.generate_reply(lead, [], "hola evento")
    ok = r["action"] != "escalate" and not r["needs_escalation"]
    check("Lead nuevo + 'hola evento' → NO escala (no es post-evento)", ok,
          f"action={r['action']!r} used_scenario_id={r.get('used_scenario_id')}")


_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


async def check_emoji_not_every_bubble():
    """Regresión 2026-08-26 (feedback directo de la dueña, test real): emoji en CASI CADA
    burbuja se ve mecánico/'como bot'. En un intercambio corto de 3 turnos, no todos los
    bubbles deben llevar emoji (referencia del prompt: no más de 1 de cada 3-4)."""
    lead = {"funnel_stage": "new"}
    hist = []
    all_bubbles = []
    for text in ("Evento", "Si", "29 años, doctor"):
        r = await ai.generate_reply(lead, hist, text)
        all_bubbles.extend(r["messages"])
        hist.append({"sender": "lead", "text": text})
        for m in r["messages"]:
            hist.append({"sender": "anna", "text": m})
    with_emoji = sum(1 for b in all_bubbles if _EMOJI_RE.search(b))
    ok = len(all_bubbles) == 0 or with_emoji / len(all_bubbles) <= 0.6
    check("Emoji no en cada burbuja (intercambio de 3 turnos)", ok,
          f"{with_emoji}/{len(all_bubbles)} bubbles con emoji — {all_bubbles}")


async def main() -> None:
    await db.init_pool()
    ai._system_prompt_cache = None
    try:
        print("=== chequeos deterministas (filters.py, sin AI) ===")
        check_scam_skepticism_not_blocked()
        check_real_insult_still_blocks()
        check_escort_negation_not_blocked()
        check_event_companion_not_blocked()

        print("\n=== chequeos con AI real (cuestan tokens) ===")
        await check_dob_and_desired_age_combined()
        await asyncio.sleep(1)
        await check_false_photo_claim_not_confirmed()
        await asyncio.sleep(1)
        await check_eres_ai_denial()
        await asyncio.sleep(1)
        await check_price_objection_offers_event()
        await asyncio.sleep(1)
        await check_full_decline_sets_nurture()
        await asyncio.sleep(1)
        await check_golden_path_no_repetition()
        await asyncio.sleep(1)
        await check_duplicate_message_no_verbatim_repeat()
        await asyncio.sleep(1)
        await check_new_lead_event_word_not_escalated()
        await asyncio.sleep(1)
        await check_emoji_not_every_bubble()
    finally:
        await db.close_pool()

    passed = sum(1 for ok, *_ in results if ok)
    total = len(results)
    print(f"\n=== ИТОГ: {passed}/{total} ===")
    if passed < total:
        print("Есть красные — не обязательно баг (LLM не 100% детерминирован),")
        print("но стоит посмотреть глазами перед тем как звать тестировать руками.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
