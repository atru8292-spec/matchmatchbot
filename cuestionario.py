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

_FOTOS = "Y por último, mándame 2 o 3 fotos tuyas, por favor 🤍"
COMPOUND_EVENT = ("Platícame de ti: ¿eres soltero y buscas una relación seria con una mujer de "
                  "Rusia, Ucrania o Bielorrusia que viva en CDMX? ¿Cómo te llamas, cuántos años "
                  "tienes y a qué te dedicas? " + _FOTOS)
COMPOUND_SERVICE = ("Platícame de ti: ¿eres soltero? ¿Cómo te llamas, cuántos años tienes y a qué "
                    "te dedicas? " + _FOTOS)
YA_TE_DIJE_REPLY = "Tienes razón, perdón 🤍"


def pregunta_compuesta(ruta: str, foto_recibida: bool = False) -> str:
    """Составной вопрос анкеты. Фото уже прислал — без просьбы о фото."""
    text = COMPOUND_SERVICE if ruta == "servicio" else COMPOUND_EVENT
    if foto_recibida:
        text = text.replace(" " + _FOTOS, " 🤍")
    return text


def es_compuesta(text: str | None) -> bool:
    return MARK in (text or "")


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
                          r"nada\s+serio|algo\s+casual|\bcasual\b|sin\s+compromiso|"
                          r"solo\s+(?:quiero\s+)?(?:sexo|algo\s+casual|una\s+noche)", re.IGNORECASE)
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
_NAME_RE = re.compile(r"\b(?:me\s+llamo|mi\s+nombre\s+es|soy)\s+([A-Za-zÁÉÍÓÚÑáéíóúñ]+)", re.IGNORECASE)
_AGE_RE = re.compile(r"(?<![\d$:.,])\b([2-9]\d)\b(?!\s*(?:mxn|pesos|usd|d[oó]lares|%|:\d|mujeres|personas))",
                     re.IGNORECASE)
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
_PROFESSION_SUFFIX_RE = re.compile(r"(?:ero|era|ista|ólogo|ologo|óloga|ador|adora|ente|ario|aria|"
                                   r"ico|ica|tor|tora)$", re.IGNORECASE)
_STOP = {"sí", "si", "no", "eso", "busco", "claro", "soy", "tengo", "años", "anos", "y", "de", "en",
         "soltero", "divorciado", "separado", "viudo", "hola", "gracias", "también", "tambien",
         "bueno", "ok", "pues", "me", "llamo", "mi", "nombre", "es", "trabajo", "como", "un", "una",
         "relación", "relacion", "seria", "serio", "algo", "busco", "edad", "a", "dedico", "aquí",
         "aqui", "va", "listo", "perfecto", "casado", "comprometido", "viudo", "evento",
         "interesa", "información", "informacion"}


def _parts(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,.;\n]|\s+y\s+|\s+-\s+", text or "") if p.strip()]


def _is_profession(word: str) -> bool:
    w = word.lower()
    return w in _PROFESSIONS or (w == word and bool(_PROFESSION_SUFFIX_RE.search(w)) and len(w) > 4)


def analizar(text: str) -> dict:
    """Что лид ответил на анкету. Ключи (None — не ответил):
    is_single True/False, seria 'si'/'no'/'duda', name, age (int), profession."""
    text = re.sub(r"\[фото[^\]]*\]", " ", text or "").strip()
    out: dict = {"is_single": None, "seria": None, "name": None, "age": None, "profession": None}
    if not text:
        return out
    if _SINGLE_NO_RE.search(text):
        out["is_single"] = False
    elif _SINGLE_YES_RE.search(text) or _YES_START_RE.search(text):
        out["is_single"] = True
    if _SERIA_NO_RE.search(text):
        out["seria"] = "no"
    elif _SERIA_DUDA_RE.search(text):
        out["seria"] = "duda"
    elif _SERIA_YES_RE.search(text) or _YES_START_RE.search(text):
        out["seria"] = "si"
    m = _AGE_RE.search(text)
    if m:
        out["age"] = int(m.group(1))
    m = _NAME_RE.search(text)
    if m and not _is_profession(m.group(1)) and m.group(1).lower() not in _STOP:
        out["name"] = m.group(1).capitalize()
    for part in _parts(text):
        words = [w for w in re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]+", part)
                 if w.lower() not in _STOP and not re.fullmatch(r"\d+", w)]
        if not words:
            continue
        prof = [w for w in words if _is_profession(w)]
        if prof and not out["profession"]:
            out["profession"] = " ".join(words) if len(words) <= 3 else prof[0]
            continue
        if not out["name"] and len(words) == 1 and words[0][0].isupper() and len(words[0]) > 1:
            out["name"] = words[0]
            continue
        if not out["name"] and len(words) == 1 and len(_parts(text)) >= 2 and words[0].isalpha() \
                and not _is_profession(words[0]):
            out["name"] = words[0].capitalize()      # «luis, 35, abogado»
    return out


# ───────────────────────── недостающее ─────────────────────────

def faltantes(datos: dict, ruta: str, foto: bool) -> list[str]:
    """Чего не хватает по анкете: single (и серьёзные отношения для ивента), name, age,
    profession, photo."""
    out = []
    if datos.get("is_single") is not True or (ruta != "servicio" and datos.get("seria") != "si"):
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
    """Одно сообщение: благодарность и всё недостающее сразу («Me falta saber …»)."""
    single = ("si estás soltero" if ruta == "servicio"
              else "si estás soltero y buscas una relación seria") if "single" in falta else None
    items = [_ITEM[k] for k in ("name", "age", "profession") if k in falta]
    parts = [gracias_txt]
    if items or single:
        if items and single:
            what = f"{_join(items)}, y {single}"     # «…a qué te dedicas, y si estás soltero…»
        else:
            what = single or _join(items)
        parts.append(f"Me falta saber {what}.")
        if "photo" in falta:
            parts.append(_FOTOS)
    if not (items or single) and "photo" in falta:
        parts.append("Solo me falta que me mandes 2 o 3 fotos tuyas, por favor 🤍")
    return " ".join(parts)


_GRACIAS = ("¡Muchas gracias!", "¡Gracias, {name}!", "¡Qué bien, gracias!")


def gracias(n: int, name: str | None) -> str:
    """Благодарность по очереди (как у Ани): «Muchas gracias», «Gracias, {имя}», «Qué bien,
    gracias». Без имени вариант с именем пропускается."""
    options = [g for g in _GRACIAS if name or "{name}" not in g]
    return options[n % len(options)].format(name=name or "")
