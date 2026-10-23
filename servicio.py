"""Услуга агентства в чате (решения Арины 2026-10-24).

Бот НИКОГДА не называет цену, пакеты и суммы услуги: на «¿cuánto cuesta?» и «es muy caro»
отвечает своими словами, что услуга персональная и это удобнее обсудить в коротком
звонке, и предлагает звонок. Время звонка бот не назначает: лид согласился — ручной режим и
алерт «Лид готов к созвону» Ане и Арине.

Лид не из Мехико или не может в среду (день ивента) — без отказа предлагаем персональный
подбор и короткий звонок.

Тексты для лида написаны ботом (не копия фраз из переписок Ани), утверждает Арина.
"""
from __future__ import annotations

import re

# ── тексты для лида (на утверждение Арине) ──
PRECIO_A = ("Como el servicio es totalmente personalizado, la inversión depende de lo que buscas "
            "y de cómo armemos tu búsqueda. Es más fácil explicártelo bien en una llamada corta. "
            "¿Te parece si lo platicamos así?")
PRECIO_B = ("Cada búsqueda la hago a la medida, así que el costo cambia de una persona a otra. "
            "Te lo explico con calma en una llamada corta, ¿te parece?")
PRECIO = PRECIO_A
CARO = ("Te entiendo, es una decisión importante. Como cada caso es distinto, prefiero "
        "explicarte bien cómo funcionaría para ti y ver juntos qué te conviene en una llamada "
        "corta. ¿Te parece?")
FUERA = ("Entiendo, no te preocupes 🤍 Para casos como el tuyo tengo el servicio de matchmaking "
         "personalizado: busco mujeres eslavas que encajen contigo y te las presento, sin "
         "depender de la fecha del evento. ¿Te parece si lo platicamos en una llamada corta?")
LLAMADA_OK = "¡Perfecto! En un rato te escribo para ver el horario de la llamada 🤍"

# ── сумма или валюта в ответе (услуга: не звучит никогда) ──
MONEY_RE = re.compile(
    r"\$\s*\d|\d[\d.,]*\s*(?:usd|mxn|d[oó]lares|pesos|dlls?|euros?)\b|"
    r"\b(?:usd|mxn|d[oó]lares|dlls)\b|\b\d+\s*(?:mil|k)\b(?!\s+(?:mujeres|personas|solteras|parejas))",
    re.IGNORECASE)

# ── вопрос о цене услуги ──
_PRICE_Q_RE = re.compile(
    r"cu[aá]nto\s+(?:me\s+)?(?:cuesta|cuestan|sale|es|vale|cobran|cobras|ser[ií]a|se\s+paga|hay\s+que\s+pagar)|"
    r"\bprecios?\b|\bcostos?\b|\btarifas?\b|\binversi[oó]n\b|qu[eé]\s+(?:precio|costo)|how\s+much",
    re.IGNORECASE)
_SERVICE_WORD_RE = re.compile(
    r"\bservicio|\bagencia|matchmak|\bpaquete|membres[ií]a|\bcontrato|personalizad|"
    r"\bb[uú]squeda|acompañamiento", re.IGNORECASE)
_EVENT_WORD_RE = re.compile(r"\bevento|\bboleto|\bentrada|\bfiesta|\bnoche\b|halloween|\bcena\b",
                            re.IGNORECASE)

# ── не из Мехико / не может в среду ──
# Белый список мест вне CDMX (ревью 2026-10-24: чёрный список районов CDMX ловил «vivo en la
# Narvarte»). День ивента — словом «miércoles» (ивенты по средам) без даты.
_LUGARES_FUERA = (r"(?:monterrey|guadalajara|quer[eé]taro|puebla|canc[uú]n|tijuana|m[eé]rida|le[oó]n|"
                  r"toluca|cuernavaca|pachuca|veracruz|oaxaca|chihuahua|hermosillo|culiac[aá]n|"
                  r"aguascalientes|san\s+luis\s+potos[ií]|saltillo|morelia|acapulco|playa\s+del\s+carmen|"
                  r"los\s+cabos|ju[aá]rez|mazatl[aá]n|torre[oó]n|jalisco|nuevo\s+le[oó]n|"
                  r"estados\s+unidos|usa|eeuu|ee\.?\s?uu\.?|texas|california|miami|nueva\s+york|"
                  r"canad[aá]|espa[ñn]a|argentina|per[uú]|colombia|chile|europa|el\s+extranjero)")
_FUERA_CDMX_RE = re.compile(
    r"no\s+(?:vivo|estoy|radico)\s+en\s+(?:la\s+)?(?:cdmx|ciudad\s+de\s+m[eé]xico|m[eé]xico|el\s+df|la\s+ciudad)\b|"
    rf"\b(?:vivo|radico|estoy\s+viviendo|soy|me\s+encuentro)\s+(?:en|de)\s+(?:la\s+ciudad\s+de\s+)?{_LUGARES_FUERA}\b|"
    r"(?:estoy|vivo)\s+fuera\s+(?:de\s+)?(?:m[eé]xico|la\s+ciudad|cdmx|del\s+pa[ií]s)",
    re.IGNORECASE)
_NO_MIERCOLES_RE = re.compile(
    r"no\s+(?:puedo|podr[eé]|voy\s+a\s+poder)\s+(?:ir\s+|asistir\s+)?(?:(?:el|los|en)\s+)?mi[eé]rcoles|"
    r"mi[eé]rcoles\s+(?:no\s+puedo|se\s+me\s+complica|(?:es|est[aá])\s+(?:muy\s+)?dif[ií]cil|imposible|trabajo)|"
    r"(?:ese|el)\s+d[ií]a\s+no\s+puedo|no\s+puedo\s+(?:ese|el)\s+d[ií]a|"
    r"no\s+(?:puedo|podr[eé]|voy\s+a\s+poder)\s+(?:ir|asistir)\s+(?:esa\s+fecha|en\s+esa\s+fecha)|"
    r"esa\s+fecha\s+no\s+puedo",
    re.IGNORECASE)

# ── согласие на звонок ──
_CALL_OFFER_RE = re.compile(r"\bllamada\b|videollamada|video\s*llamada", re.IGNORECASE)
_CALL_YES_RE = re.compile(
    r"^\W*(?:s[ií]+|claro|ok|okay|va|vale|dale|sale|perfecto|de\s+acuerdo|me\s+parece|"
    r"est[aá]\s+bien|con\s+gusto|por\s+supuesto|cuando\s+quieras)\b|"
    r"\bll[aá]m(?:ame|enme)\b|\bm[aá]rcame\b|\bh[aá]blame\b|\bagend(?:a|emos)\b|"
    r"\bs[ií],?\s+(?:me\s+)?(?:parece|interesa|platiquemos|hablemos)",
    re.IGNORECASE)
_CALL_NO_RE = re.compile(r"^\W*no\b|\bno\s+(?:gracias|puedo|quiero|me\s+interesa)|prefiero\s+(?:por\s+)?"
                         r"(?:aqu[ií]|escrito|mensaje|chat)|\bpor\s+(?:aqu[ií]|chat|mensaje)", re.IGNORECASE)
_TIME_Q_RE = re.compile(r"cu[aá]ndo|a\s+qu[eé]\s+hora|qu[eé]\s+d[ií]a|qu[eé]\s+horario", re.IGNORECASE)


def tiene_suma(text: str | None) -> bool:
    """Есть сумма или валюта (кроме токенов ивента, их подставит отправка)."""
    return bool(MONEY_RE.search(re.sub(r"\[[a-z_]+\]", " ", text or "")))


def pregunta_precio_servicio(text: str, ruta: str) -> bool:
    """Вопрос о цене услуги: на пути услуги или со словом про услугу; про ивент — не наш."""
    text = text or ""
    if not _PRICE_Q_RE.search(text) or _EVENT_WORD_RE.search(text):
        return False
    return ruta == "servicio" or bool(_SERVICE_WORD_RE.search(text))


def fuera_o_miercoles(text: str) -> str | None:
    """«fuera» — живёт не в CDMX; «miercoles» — не может в среду/в дату ивента; иначе None."""
    text = text or ""
    if _FUERA_CDMX_RE.search(text):
        return "fuera"
    if _NO_MIERCOLES_RE.search(text):
        return "miercoles"
    return None


def ofrecio_llamada(last_bot: str | None) -> bool:
    """Последнее сообщение бота предлагало звонок вопросом."""
    last = last_bot or ""
    return bool(_CALL_OFFER_RE.search(last)) and "?" in last


_DATA_RE = re.compile(r"\d|\bsoy\b|\bme\s+llamo\b|\btengo\b|\bpero\b|cu[aá]nto|precio|cuesta",
                      re.IGNORECASE)
MAX_WORDS_YES = 6


def acepta_llamada(text: str) -> bool:
    """Согласие на звонок: короткое «sí», «va», «llámame», «sí, ¿a qué hora?». Отказ, данные о
    себе («Sí, soy Juan, 45»), «pero…», вопрос о цене или другой вопрос — нет (ревью 2026-10-24)."""
    text = (text or "").strip()
    if not text or _CALL_NO_RE.search(text) or not _CALL_YES_RE.search(text):
        return False
    if len(text.split()) > MAX_WORDS_YES or _DATA_RE.search(text):
        return False
    if "?" in text and not _TIME_Q_RE.search(text):
        return False
    return True
