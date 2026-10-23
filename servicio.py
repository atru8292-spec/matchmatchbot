"""Ветка агентства (услуга) словами Ани (решения Арины 2026-10-24, по разбору её переписок
docs/agency_branch_vs_anya_2026-10-24.md).

Все тексты для лида ниже — её реальные сообщения лидам. Правки только такие: убраны имена,
дни и часы звонка, PDF; исправлены явные опечатки (porfavor → por favor, aque → a qué,
ayudae → ayudame, tal ves → tal vez); недостающие ¡ и ¿ не добавляем (её стиль). Где Арина в
решении написала фразу сама (с ударениями) — берём её написание.

Бот ведёт лида до звонка с Аней и не дальше: время звонка, приглашение, контракт, документы,
оплату, пакеты и суммы ведёт только Аня (тишина, ручной режим, алерт). Цену услуги бот не
называет; сумма или валюта в ответе — ответ не уходит.
"""
from __future__ import annotations

import re

SITE = "https://matchmatchagency.com/es/"

# ── тексты Ани (испанский как у неё) ──
# Первый ответ на интерес к услуге — её текст целиком, одним сообщением, как она шлёт всем.
BIENVENIDA = (
    "Hola! Muchas gracias por tu mensaje! Sí, somos una agencia boutique de matchmaking. "
    "Ayudamos a personas a encontrar pareja y construir relaciones serias.\n\n"
    "Trabajamos principalmente con hombres mexicanos, tanto en México como en Estados Unidos, "
    "que están interesados en conocer mujeres de Rusia, Ucrania y Bielorrusia.\n\n"
    "Nuestros servicios tienen una duración a partir de 6 meses e incluyen un proceso muy "
    "personalizado, en el que te presentamos mujeres cuidadosamente seleccionadas que cumplen "
    "con tus preferencias y requisitos.\n\n"
    f"Por favor ayudame con tu registro aqui:\n\n{SITE}")
ANKETA = "Platicame de ti - eres soltero? tu edad? a que te dedicas?"
# Недостающее — только её вопрос по каждому слоту.
P_SOLTERO = "Eres soltero?"
P_EDAD = "Cuántos años tienes?"
P_PROFESION = "y a qué te dedicas?"
P_EDAD_PROFESION = "cuantos años tienes? a que te dedicas?"
FOTOS = "Muchas gracias! Y por último, mándame unas fotos de ti por favor"
CUANTAS_FOTOS = "2 o 3"
PARA_QUE_FOTO = "para que podamos invitar mujeres para ti"
GRACIAS = "Muchas gracias!"
INVITACION = "estas disponible para una breve llamada?"
COMO_FUNCIONA = [
    "Cuando hacemos una busqueda personalizada buscamos parejas basado en tus necesidades y "
    "deseos, tus valores y estilo de vida. Te presentamos mujeres que cumplen con todo esto y "
    "tu eliges con quien salir.",
    "aqui puedes verlo en mas detalle:",
    SITE,
    "que te parece si agendamos una llamada y platicamos en mas detalle?",
]
PRECIO = "es un servicio 100% personalizado, se depende de tus necesidades"
CARO = "ah ok ok entiendo... estas disponible para una breve llamada y te platico del costo?"
MIERCOLES = [
    "ufff! lo siento!",
    "tal vez el servicio de matchmaking personalizado es la mejor opción para ti?",
    "tienes tiempo esta semana para una llamada y te platico en mas detalles como este funciona?",
]
FUERA_CIUDAD = "en {ciudad} te podemos ayudar con matchmaking personalizado"
FUERA_SIN_CIUDAD = "te podemos ayudar con matchmaking personalizado"
FUERA_REGISTRO = "ayudame con tu registro aqui por favor"
CATALOGO = ("no manejamos un catalogo de mujeres donde 'eliges' a una pareja. Que te parece si te "
            "enseño los perfiles que te puede funcionar... estas disponible para una video llamada?")
TIPO = ["muchas!", "con mucho gusto te podemos presentar con una {tipo}",
        "tienes tiempo para una llamada y platicamos?"]
SUERTE = "Suerte! 😍"
NOMBRE_EMAIL = "cual es tu nombre completo y tu email?"
CORREO = "cual es correo electronico?"
# Догон (сейчас выключен, scheduler_enabled=0): её цепочка без дня и часа и без пакетов.
DOGON = ["Hola! Como estas?", "Estas saliendo con alguien?"]
DOGON_NO = ["me gustaría enseñarte unos perfiles de nuevas mujeres que llegaron a nuestra agencia!",
            "estas disponible para una video llamada?"]

TEXTOS = ([BIENVENIDA, ANKETA, P_SOLTERO, P_EDAD, P_PROFESION, P_EDAD_PROFESION, FOTOS,
           CUANTAS_FOTOS, PARA_QUE_FOTO, GRACIAS, INVITACION, PRECIO, CARO, FUERA_CIUDAD,
           FUERA_SIN_CIUDAD, FUERA_REGISTRO, CATALOGO, SUERTE, NOMBRE_EMAIL, CORREO]
          + COMO_FUNCIONA + MIERCOLES + TIPO + DOGON + DOGON_NO)

# ── сумма или валюта в ответе (услуга: не звучит никогда) ──
MONEY_RE = re.compile(
    r"\$\s*\d|\d[\d.,]*\s*(?:usd|mxn|d[oó]lares|pesos|dlls?|euros?)\b|"
    r"\b(?:usd|mxn|d[oó]lares|dlls)\b|\b\d+\s*(?:mil|k)\b(?!\s+(?:mujeres|personas|solteras|parejas))",
    re.IGNORECASE)

# ── вопросы лида ──
_PRICE_Q_RE = re.compile(
    r"cu[aá]nto\s+(?:me\s+)?(?:cuesta|cuestan|sale|es|vale|cobran|cobras|ser[ií]a|se\s+paga|se\s+cobra|"
    r"hay\s+que\s+pagar|tengo\s+que\s+pagar|le\s+tengo\s+que\s+pagar)|"
    r"\bprecios?\b|\bcostos?\b|\btarifas?\b|\binversi[oó]n\b|qu[eé]\s+(?:precio|costo)|how\s+much",
    re.IGNORECASE)
_SERVICE_WORD_RE = re.compile(
    r"\bservicio|\bagencia|matchmak|\bpaquete|membres[ií]a|\bcontrato|personalizad|"
    r"\bb[uú]squeda|acompañamiento", re.IGNORECASE)
_EVENT_WORD_RE = re.compile(r"\bevento|\bboleto|\bentrada|\bfiesta|\bnoche\b|halloween|\bcena\b",
                            re.IGNORECASE)
_COMO_FUNCIONA_RE = re.compile(r"c[oó]mo\s+funciona|c[oó]mo\s+es\s+el\s+(?:proceso|servicio)|"
                               r"en\s+qu[eé]\s+consiste|c[oó]mo\s+trabajan|de\s+qu[eé]\s+se\s+trata|"
                               r"qu[eé]\s+incluye", re.IGNORECASE)
_PARA_QUE_FOTO_RE = re.compile(r"para\s+qu[eé]\s+(?:es|son|quieres|necesitas|sirve|sirven)?\s*"
                               r"(?:la\s+|las\s+|mis\s+|una\s+)?fotos?|fotos?\s+(?:para|pa)\s+qu[eé]",
                               re.IGNORECASE)
_CUANTAS_FOTOS_RE = re.compile(r"cu[aá]ntas\s+fotos", re.IGNORECASE)
_CATALOGO_RE = re.compile(r"cat[aá]logo|ver\s+(?:a\s+)?(?:las\s+)?(?:chicas|mujeres)|"
                          r"fotos?\s+de\s+(?:las\s+)?(?:chicas|mujeres)|perfiles\s+de\s+(?:las\s+)?(?:chicas|mujeres)|"
                          r"(?:elegir|escoger)\s+(?:a\s+)?(?:una\s+)?(?:chica|mujer)", re.IGNORECASE)
_TIPOS = (r"jud[ií]a|cristiana|cat[oó]lica|rubia|morena|pelirroja|alta|delgada|rusa|ucraniana|"
          r"bielorrusa|joven|madura|divorciada|con\s+hijos|sin\s+hijos")
_TIPO_RE = re.compile(rf"\b(?:tienes|tienen|hay|manejas|manejan)\s+(?:alguna\s+|mujeres\s+|chicas\s+|"
                      rf"una\s+)?({_TIPOS})\b", re.IGNORECASE)
_SALIENDO_RE = re.compile(r"estoy\s+saliendo\s+con|conoc[ií]\s+a\s+alguien|ya\s+(?:tengo|encontr[eé])\s+"
                          r"(?:novia|pareja|a\s+alguien)|estoy\s+(?:en\s+una\s+relaci[oó]n|conociendo\s+a\s+alguien)",
                          re.IGNORECASE)
# Контракт, документы, оплата, пакеты, суммы — только Аня (тишина, ручной режим, алерт).
_SOLO_ANYA_RE = re.compile(
    r"contrato|documento|identificaci[oó]n|\bine\b|pasaporte|paquetes?|membres[ií]a|"
    r"\bpag(?:o|ar|ue|u[eé]|os)\b|tarjeta|transferencia|transferir|dep[oó]sito|depositar|efectivo|"
    r"anticipo|factura|meses\s+sin\s+intereses|mensualidad|plazos|cuotas|reembolso|descuento",
    re.IGNORECASE)

# ── не из CDMX / не может в среду ──
# Белый список мест вне CDMX (чёрный список районов CDMX ловил «vivo en la Narvarte»).
_LUGARES = {
    "monterrey": "Monterrey", "guadalajara": "Guadalajara", "quer[eé]taro": "Querétaro",
    "puebla": "Puebla", "canc[uú]n": "Cancún", "tijuana": "Tijuana", "m[eé]rida": "Mérida",
    "le[oó]n": "León", "toluca": "Toluca", "cuernavaca": "Cuernavaca", "pachuca": "Pachuca",
    "veracruz": "Veracruz", "oaxaca": "Oaxaca", "chihuahua": "Chihuahua",
    "hermosillo": "Hermosillo", "culiac[aá]n": "Culiacán", "aguascalientes": "Aguascalientes",
    r"san\s+luis\s+potos[ií]": "San Luis Potosí", "saltillo": "Saltillo", "morelia": "Morelia",
    "acapulco": "Acapulco", r"playa\s+del\s+carmen": "Playa del Carmen",
    r"los\s+cabos": "Los Cabos", r"ju[aá]rez": "Juárez", r"mazatl[aá]n": "Mazatlán",
    r"torre[oó]n": "Torreón", "jalisco": "Jalisco", r"nuevo\s+le[oó]n": "Nuevo León",
    r"estados\s+unidos": "Estados Unidos", r"usa": "Estados Unidos", r"eeuu": "Estados Unidos",
    "texas": "Texas", "california": "California", "miami": "Miami",
    r"nueva\s+york": "Nueva York", r"canad[aá]": "Canadá", r"espa[ñn]a": "España",
    "argentina": "Argentina", r"per[uú]": "Perú", "colombia": "Colombia", "chile": "Chile",
    "europa": "Europa",
}
_LUGAR_RE = "(" + "|".join(_LUGARES) + ")"
_FUERA_CDMX_RE = re.compile(
    r"no\s+(?:vivo|estoy|radico)\s+en\s+(?:la\s+)?(?:cdmx|ciudad\s+de\s+m[eé]xico|m[eé]xico|el\s+df|la\s+ciudad)\b|"
    rf"\b(?:vivo|radico|estoy\s+viviendo|soy|me\s+encuentro)\s+(?:en|de)\s+(?:la\s+ciudad\s+de\s+)?{_LUGAR_RE}\b|"
    r"(?:estoy|vivo)\s+fuera\s+(?:de\s+)?(?:m[eé]xico|la\s+ciudad|cdmx|del\s+pa[ií]s)",
    re.IGNORECASE)
_NO_MIERCOLES_RE = re.compile(
    r"no\s+(?:puedo|podr[eé]|voy\s+a\s+poder)\s+(?:ir\s+|asistir\s+)?(?:(?:el|los|en)\s+)?mi[eé]rcoles|"
    r"mi[eé]rcoles\s+(?:no\s+puedo|se\s+me\s+complica|(?:es|est[aá])\s+(?:muy\s+)?dif[ií]cil|imposible|trabajo)|"
    r"(?:en|los|el)\s+mi[eé]rcoles\s+(?:es|est[aá])\s+(?:muy\s+)?(?:dif[ií]cil|complicado)",
    re.IGNORECASE)

# ── согласие на звонок ──
_CALL_OFFER_RE = re.compile(r"\bllamada\b|videollamada|video\s*llamada", re.IGNORECASE)
_CALL_YES_RE = re.compile(
    r"^\W*(?:s[ií]+|claro|ok|okay|va|vale|dale|sale|perfecto|de\s+acuerdo|me\s+parece|"
    r"est[aá]\s+bien|con\s+gusto|por\s+supuesto|cuando\s+quieras|platicamos)\b|"
    r"\bll[aá]m(?:ame|enme)\b|\bm[aá]rcame\b|\bh[aá]blame\b|\bagend(?:a|emos)\b|"
    r"\bs[ií],?\s+(?:me\s+)?(?:parece|interesa|platiquemos|hablemos)",
    re.IGNORECASE)
_CALL_EXPLICIT_RE = re.compile(r"\bll[aá]m(?:ame|enme)\b|\bm[aá]rcame\b|\bh[aá]blame\b", re.IGNORECASE)
_CALL_NO_RE = re.compile(r"^\W*no\b|\bno\s+(?:gracias|puedo|quiero|me\s+interesa)|prefiero\s+(?:por\s+)?"
                         r"(?:aqu[ií]|escrito|mensaje|chat)|\bpor\s+(?:aqu[ií]|chat|mensaje)", re.IGNORECASE)
_TIME_Q_RE = re.compile(r"cu[aá]ndo|a\s+qu[eé]\s+hora|qu[eé]\s+d[ií]a|qu[eé]\s+horario", re.IGNORECASE)
_DATA_RE = re.compile(r"\d|\bsoy\b|\bme\s+llamo\b|\btengo\b|\bpero\b|cu[aá]nto|precio|cuesta",
                      re.IGNORECASE)
MAX_WORDS_YES = 6
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_NO_RE = re.compile(r"^\W*(?:no|nop|nel|nada|todav[ií]a\s+no|a[uú]n\s+no|para\s+nada)\b", re.IGNORECASE)


def tiene_suma(text: str | None) -> bool:
    """Есть сумма или валюта (кроме токенов ивента, их подставит отправка)."""
    return bool(MONEY_RE.search(re.sub(r"\[[a-z_]+\]", " ", text or "")))


def pregunta_precio(text: str, ruta: str) -> bool:
    """Вопрос о цене услуги: на пути услуги или со словом про услугу; про ивент — не наш."""
    text = text or ""
    if not _PRICE_Q_RE.search(text) or _EVENT_WORD_RE.search(text):
        return False
    return ruta == "servicio" or bool(_SERVICE_WORD_RE.search(text))


def pregunta_como_funciona(text: str) -> bool:
    return bool(_COMO_FUNCIONA_RE.search(text or ""))


def pregunta_para_que_foto(text: str) -> bool:
    return bool(_PARA_QUE_FOTO_RE.search(text or ""))


def pregunta_cuantas_fotos(text: str) -> bool:
    return bool(_CUANTAS_FOTOS_RE.search(text or ""))


def pide_catalogo(text: str) -> bool:
    return bool(_CATALOGO_RE.search(text or ""))


def tipo_buscado(text: str) -> str | None:
    m = _TIPO_RE.search(text or "")
    return m.group(1).lower() if m else None


def ya_sale_con_alguien(text: str) -> bool:
    return bool(_SALIENDO_RE.search(text or ""))


def solo_anya(text: str) -> bool:
    """Контракт, документы, оплата, пакеты, суммы — ведёт только Аня."""
    return bool(_SOLO_ANYA_RE.search(text or ""))


def fuera_o_miercoles(text: str) -> str | None:
    """«fuera» — живёт не в CDMX; «miercoles» — не может в среду/в дату ивента; иначе None."""
    text = text or ""
    if _FUERA_CDMX_RE.search(text):
        return "fuera"
    if _NO_MIERCOLES_RE.search(text):
        return "miercoles"
    return None


def ciudad_fuera(text: str) -> str | None:
    """Город/страна из «vivo en Monterrey» — для «en {ciudad} te podemos ayudar…»."""
    m = re.search(rf"\b(?:vivo|radico|estoy\s+viviendo|soy|me\s+encuentro|estoy)\s+(?:en|de)\s+"
                  rf"(?:la\s+ciudad\s+de\s+)?{_LUGAR_RE}\b", text or "", re.IGNORECASE)
    if not m:
        return None
    found = m.group(1).lower()
    for pat, name in _LUGARES.items():
        if re.fullmatch(pat, found, re.IGNORECASE):
            return name
    return None


def fuera_bubbles(text: str) -> list[str]:
    ciudad = ciudad_fuera(text)
    first = FUERA_CIUDAD.format(ciudad=ciudad) if ciudad else FUERA_SIN_CIUDAD
    return [first, FUERA_REGISTRO, SITE]


def preguntas_faltantes(falta: list[str]) -> list[str]:
    """Её вопросы по недостающему, в её порядке: холост, возраст, профессия."""
    out = []
    if "is_single" in falta:
        out.append(P_SOLTERO)
    if "age" in falta and "profession" in falta:
        out.append(P_EDAD_PROFESION)
    elif "age" in falta:
        out.append(P_EDAD)
    elif "profession" in falta:
        out.append(P_PROFESION)
    return out


def ofrecio_llamada(last_bot: str | None) -> bool:
    """Последнее сообщение бота предлагало звонок вопросом."""
    last = last_bot or ""
    return bool(_CALL_OFFER_RE.search(last)) and "?" in last


def acepta_llamada(text: str, ofrecida: bool = True) -> bool:
    """Согласие на звонок: короткое «sí», «va», «llámame», «sí, ¿a qué hora?». Без предложения
    бота — только явное «llámame / márcame / háblame». Отказ, данные о себе, «pero…», вопрос о
    цене или другой вопрос — нет."""
    text = (text or "").strip()
    if not text or _CALL_NO_RE.search(text) or not _CALL_YES_RE.search(text):
        return False
    if not ofrecida and not _CALL_EXPLICIT_RE.search(text):
        return False
    if len(text.split()) > MAX_WORDS_YES or _DATA_RE.search(text):
        return False
    if "?" in text and not _TIME_Q_RE.search(text):
        return False
    return True


_NO_DATOS_RE = re.compile(r"\?|¿|\bno\b|para\s+qu[eé]|ll[aá]m|mejor|despu[eé]s|luego", re.IGNORECASE)


def no_son_datos(text: str) -> bool:
    """Ответ на «cual es tu nombre completo y tu email?» — не данные (вопрос, отказ, «luego»)."""
    return not EMAIL_RE.search(text or "") and bool(_NO_DATOS_RE.search(text or ""))


def es_no(text: str) -> bool:
    return bool(_NO_RE.search(text or "")) and "?" not in (text or "")


def nombre_y_email(text: str) -> tuple[str | None, str | None]:
    """Имя и почта из ответа на «cual es tu nombre completo y tu email?»."""
    text = text or ""
    m = EMAIL_RE.search(text)
    email = m.group(0) if m else None
    rest = EMAIL_RE.sub(" ", text)
    rest = re.sub(r"\+?\d[\d\s-]{6,}", " ", rest)
    rest = re.sub(r"\b(?:mi\s+nombre\s+(?:completo\s+)?es|me\s+llamo|soy|mi|email|correo|mail|y)\b", " ",
                  rest, flags=re.IGNORECASE)
    rest = re.sub(r"[\s,;:./-]+", " ", rest).strip()
    nombre = rest if rest and len(rest.split()) <= 6 else None
    return nombre, email
