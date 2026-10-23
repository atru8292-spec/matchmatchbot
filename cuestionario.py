"""Анкета одним сообщением, как пишет Аня (решение владелицы 2026-10-23).

Первый ответ на интерес к ивенту или услуге — описание и ОДИН пузырь со всеми вопросами сразу
(утверждённое исключение из правила «не больше одного вопроса»). Ответ лида разбирается кодом:
холост, серьёзные отношения (только ивент), имя, возраст, профессия. Недостающее — одним
сообщением один раз («Me falta saber …»). «No», «solo curiosidad», неясный ответ про серьёзные
отношения — бот молчит, Аня решает сама.
"""
from __future__ import annotations

import re

MARK = "Platícame de ti"            # по этой фразе в истории видно, что анкету уже задали
FALTA_MARKS = ("Me falta saber", "Solo me falta")

# Решение владелицы 2026-10-23 (после живого теста): без вопроса про Россию и серьёзные
# отношения и без фото в первом сообщении — фото просим отдельно, когда лид ответил.
COMPOUND = "Platícame de ti: ¿cómo te llamas, cuántos años tienes, a qué te dedicas y estás soltero? 🤍"
COMPOUND_EVENT = COMPOUND
# Услуга — её анкета дословно, без имени (решение 2026-10-24).
COMPOUND_SERVICE = "Platicame de ti - eres soltero? tu edad? a que te dedicas?"
FOTO_PEDIDO = "Y por último, mándame 2 o 3 fotos tuyas, por favor 🤍"
YA_TE_DIJE_REPLY = "Tienes razón, perdón 🤍"


def pregunta_compuesta(ruta: str = "evento", foto_recibida: bool = False) -> str:
    """Анкета одним сообщением (одинаковая для ивента и услуги; фото в ней не просим)."""
    return COMPOUND


def pedir_fotos(gracias_txt: str) -> str:
    """Просьба о фото после ответа на анкету: «¡Muchas gracias! Y por último, mándame…»."""
    return f"{gracias_txt} {FOTO_PEDIDO}".strip()


def es_compuesta(text: str | None) -> bool:
    # «Platícame de ti» (ивент) и её «Platicame de ti - …» (услуга, 2026-10-24)
    low = (text or "").lower().replace("í", "i")
    return "platicame de ti" in low


def es_faltante(text: str | None) -> bool:
    low = (text or "").lower()
    return any(m.lower() in low for m in FALTA_MARKS)


# ───────────────────────── разбор ответа ─────────────────────────

_YES_START_RE = re.compile(r"^\W*(?:s[ií]+|claro|as[ií]\s+es|correcto|exacto|obvio|por\s+supuesto|"
                           r"efectivamente|afirmativo)\b", re.IGNORECASE)
_SERIA_YES_RE = re.compile(r"relaci[oó]n\s+seria|algo\s+serio|eso\s+busco|eso\s+es\s+lo\s+que\s+busco|"
                           r"\bs[ií]\s+busco\b|busco\s+(?:una\s+)?(?:pareja|esposa|novia|relaci[oó]n)|"
                           r"\bformal\b|casarme|formar\s+una\s+familia", re.IGNORECASE)
# Явный отказ от серьёзных отношений — молча Ане (решение владелицы 2026-10-23).
_SERIA_NO_RE = re.compile(r"no\s+busco\s+(?:nada\s+|una\s+|algo\s+)?(?:serio|relaci)|"
                          r"no\s+quiero\s+(?:nada\s+|una\s+|algo\s+)?(?:serio|relaci)|"
                          r"nada\s+serio|algo\s+casual|\bcasual\b|"
                          r"solo\s+(?:quiero\s+)?(?:sexo|algo\s+casual|una\s+noche)", re.IGNORECASE)
# «no busco nada casual», «no quiero algo casual» — это «да» серьёзным отношениям
_SERIA_NEG_CASUAL_RE = re.compile(r"\bno\s+(?:busco|quiero)\s+(?:nada|algo)?\s*casual", re.IGNORECASE)
# Неясно («solo curiosidad», «no sé», «tal vez», голое «no»): тёплый ответ, информация об ивенте
# и один мягкий возврат к анкете — не молчим (решение владелицы 2026-10-23).
_SERIA_DUDA_RE = re.compile(r"curiosidad|\bno\s+s[eé]\b|tal\s+vez|quiz[aá]s?|depende|a\s+ver\s+qu[eé]|"
                            r"lo\s+que\s+(?:salga|se\s+d[eé])|m[aá]s\s+o\s+menos|todav[ií]a\s+no\s+s[eé]|"
                            r"primero\s+conocer|no\s+estoy\s+seguro|"
                            r"solo\s+(?:quiero\s+)?(?:amistad|amigos|conocer\s+gente|divertirme|"
                            r"pasarla\s+bien|ver|preguntar|checar|informaci[oó]n|info)|"
                            r"no\s+estoy\s+buscando|^\W*no\W*$|^\W*no\s*[,.]", re.IGNORECASE)
_SINGLE_YES_RE = re.compile(r"\bsolter|\bdivorciad|\bseparad|\bviud", re.IGNORECASE)
_SINGLE_NO_RE = re.compile(r"\bcasado\b|tengo\s+(?:novia|esposa|pareja)|no\s+soy\s+soltero|"
                           r"comprometido|\bjuntado\b", re.IGNORECASE)
# отрицание: «no tengo novia», «no estoy casado», «no soy casado» — холост (ревью 2026-10-23)
_SINGLE_NEG_RE = re.compile(r"\bno\s+(?:tengo\s+(?:novia|esposa|pareja)|(?:estoy|soy)\s+(?:casado|"
                            r"comprometido|juntado))", re.IGNORECASE)
# Имя: «me llamo luis», «mi nombre es Luis», «soy Luis» (после «soy» — только с заглавной:
# «soy mexicano», «soy muy serio» — не имя).
_NAME_RE = re.compile(r"\b(?:(?i:me\s+llamo|mi\s+nombre\s+es)\s+([A-Za-zÁÉÍÓÚÑáéíóúñ]+)|"
                      r"(?i:soy)\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+))")
# Возраст — только явно: «tengo 35», «35 años» или число отдельной частью ответа («Luis, 35, …»);
# не даты и не время («el 25 de octubre», «a las 20 hrs»).
_AGE_EXPLICIT_RE = re.compile(r"\btengo\s+([2-9]\d)\b|\b([2-9]\d)\s*a[ñn]os\b", re.IGNORECASE)
_AGE_PART_RE = re.compile(r"^\s*([2-9]\d)\s*$")
# Профессия фразой: «trabajo en ventas», «me dedico a la construcción», «trabajo como chofer»
_PROF_PHRASE_RE = re.compile(r"\b(?:trabajo\s+(?:en|como|de)|me\s+dedico\s+a)\s+(?:la\s+|el\s+|los\s+|las\s+)?"
                             r"([a-záéíóúñ][a-záéíóúñ ]{2,40})", re.IGNORECASE)
_PROFESSIONS = {
    "abogado", "abogada", "ingeniero", "ingeniera", "doctor", "doctora", "medico", "médico",
    "contador", "contadora", "arquitecto", "arquitecta", "empresario", "empresaria", "maestro",
    "profesor", "dentista", "piloto", "programador", "diseñador", "vendedor", "gerente", "director",
    "licenciado", "consultor", "dueño", "chef", "militar", "policía", "enfermero", "comerciante",
    "administrador", "economista", "psicólogo", "psicologo", "veterinario", "mecánico", "mecanico",
    "chofer", "conductor", "taxista", "agente", "asesor", "analista", "desarrollador", "emprendedor",
    "inversionista", "notario", "cirujano", "odontólogo", "odontologo", "farmacéutico", "biólogo",
    "químico", "quimico", "físico", "ejecutivo", "abarrotero", "carpintero", "electricista",
    "plomero", "fotógrafo", "fotografo", "músico", "musico", "actor", "periodista", "publicista",
    "banquero", "financiero", "auditor", "estudiante", "jubilado", "pensionado", "independiente",
}

_STOP = {"sí", "si", "no", "eso", "busco", "claro", "soy", "tengo", "años", "anos", "y", "de", "en",
         "soltero", "divorciado", "separado", "viudo", "hola", "gracias", "también", "tambien",
         "bueno", "ok", "pues", "me", "llamo", "mi", "nombre", "es", "trabajo", "como", "un", "una",
         "relación", "relacion", "seria", "serio", "algo", "busco", "edad", "a", "dedico", "aquí",
         "aqui", "va", "listo", "perfecto", "casado", "comprometido", "viudo", "evento",
         "interesa", "información", "informacion", "que", "pero", "bien", "muy", "mucho",
         "gusto", "quiero", "ir", "obviamente", "claramente", "totalmente", "actualmente",
         "porque", "ahorita", "ahora", "igual", "todo", "nada"}


def _parts(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,.;\n]|\s+y\s+|\s+-\s+", text or "") if p.strip()]


def _is_profession(word: str) -> bool:
    return word.lower() in _PROFESSIONS


def analizar(text: str, responde_cuestionario: bool = True) -> dict:
    """Что лид ответил на анкету. Ключи (None — не ответил):
    is_single True/False, seria 'si'/'no'/'duda', name, age (int), profession.
    responde_cuestionario — последнее сообщение бота было анкетой или «Me falta saber»: тогда
    «Sí, …» в начале — ответ на неё (иначе «sí» могло отвечать на другое)."""
    text = re.sub(r"\[фото[^\]]*\]", " ", text or "").strip()
    out: dict = {"is_single": None, "seria": None, "name": None, "age": None, "profession": None}
    if not text:
        return out
    yes_start = responde_cuestionario and bool(_YES_START_RE.search(text))
    if _SINGLE_NEG_RE.search(text) or _SINGLE_YES_RE.search(text):
        out["is_single"] = True
    elif _SINGLE_NO_RE.search(text):
        out["is_single"] = False
    elif yes_start:
        out["is_single"] = True
    if _SERIA_NEG_CASUAL_RE.search(text):
        out["seria"] = "si"
    elif _SERIA_NO_RE.search(text):
        out["seria"] = "no"
    elif _SERIA_DUDA_RE.search(text):
        out["seria"] = "duda"
    elif _SERIA_YES_RE.search(text) or yes_start:
        out["seria"] = "si"
    m = _AGE_EXPLICIT_RE.search(text)
    if m:
        out["age"] = int(m.group(1) or m.group(2))
    m = _NAME_RE.search(text)
    if m:
        cand = m.group(1) or m.group(2)
        if not _is_profession(cand) and cand.lower() not in _STOP:
            out["name"] = cand.capitalize()
    m = _PROF_PHRASE_RE.search(text)
    if m:
        out["profession"] = re.split(r"[,.;!?]| y ", m.group(1))[0].strip()
    parts = _parts(text)
    for idx, part in enumerate(parts):
        if out["age"] is None and _AGE_PART_RE.match(part):
            out["age"] = int(_AGE_PART_RE.match(part).group(1))
            continue
        if _PROF_PHRASE_RE.search(part) or _NAME_RE.search(part):
            continue
        # часть про «холост» или серьёзные отношения — не имя («no tengo novia», «nada serio»)
        if any(rx.search(part) for rx in (_SINGLE_NEG_RE, _SINGLE_YES_RE, _SINGLE_NO_RE,
                                          _SERIA_NO_RE, _SERIA_DUDA_RE, _SERIA_YES_RE)):
            continue
        words = [w for w in re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]+", part)
                 if w.lower() not in _STOP]
        if not words:
            continue
        prof = [w for w in words if _is_profession(w)]
        if prof and not out["profession"]:
            out["profession"] = " ".join(words) if len(words) <= 3 else prof[0]
            continue
        if out["name"] or len(words) != 1 or prof:
            continue
        w = words[0]
        if w[0].isupper() and len(w) > 1:
            out["name"] = w                          # «Luis», «Pedro»
        elif idx == 0 and len(parts) >= 2 and len(w) > 2:
            out["name"] = w.capitalize()             # «luis, 35, abogado»
    return out


# ───────────────────────── недостающее ─────────────────────────

def faltantes(datos: dict, ruta: str, foto: bool) -> list[str]:
    """Чего не хватает по анкете: single, name, age, profession, photo. Про серьёзные
    отношения не спрашиваем (решение 2026-10-23); явный отказ ловится разбором ответа."""
    out = []
    if datos.get("is_single") is not True:
        out.append("single")
    for k in ("name", "age", "profession"):
        if not datos.get(k):
            out.append(k)
    if not foto:
        out.append("photo")
    return out


_ITEM = {"name": "cómo te llamas", "age": "cuántos años tienes", "profession": "a qué te dedicas"}


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " y " + items[-1]


def mensaje_faltante(falta: list[str], ruta: str, gracias_txt: str) -> str:
    """Одно сообщение: благодарность и все недостающие данные сразу («Me falta saber …»).
    Фото здесь не просим — отдельно, после ответа (pedir_fotos)."""
    single = "si estás soltero" if "single" in falta else None
    items = [_ITEM[k] for k in ("name", "age", "profession") if k in falta]
    parts = [gracias_txt]
    if items or single:
        if items and single:
            what = f"{_join(items)}, y {single}"     # «…a qué te dedicas, y si estás soltero…»
        else:
            what = single or _join(items)
        parts.append(f"Me falta saber {what}.")
    return " ".join(p for p in parts if p)


_GRACIAS = ("¡Muchas gracias!", "¡Gracias, {name}!", "¡Qué bien, gracias!")


def gracias(n: int, name: str | None) -> str:
    """Благодарность по очереди (как у Ани): «Muchas gracias», «Gracias, {имя}», «Qué bien,
    gracias». Без имени вариант с именем пропускается."""
    options = [g for g in _GRACIAS if name or "{name}" not in g]
    return options[n % len(options)].format(name=name or "")
