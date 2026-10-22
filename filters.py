"""Детерминированный слой решений (без AI): отвечать / молчать / блок / отказ.

Чистые функции — тестируются без БД. Данные (лид, флаг whitelist, текст) передаются
снаружи. Аналог Evaluate context + детерминированных веток Auto-action router из WF1.
Порядок проверок важен: whitelist раньше блокировок раньше квалификации.

AI-зависимые ветки (профессия, casual/несерьёзность, тон отказа) сюда НЕ входят —
для них возвращается action='needs_ai' (реальный вызов AI встанет в блоке 6).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from phones import canonical_phone, canonical_set

# Возрастной фильтр (владелец поднял верхнюю границу 2026-07-26): 28-76 включительно.
MIN_AGE = 28
MAX_AGE = 76

# Явные дисквалификаторы по ключевым словам (испанский). \b — границы слова:
# 'sexo' не ловится в 'sexto'/'sexta'. Основа как в BLUEPRINT (force-escalate WF1).
_ESCORT_RE = re.compile(
    r"\b(escorts?|sexo|sexual(es)?|prostit\w*|acompañant\w*|acompanant\w*|"
    r"servicios?\s+sexual(es)?)\b",
    re.IGNORECASE,
)
# Excepciones (2026-08-06, encontrado en auditoría — mismo patrón que _SCAM_NEGATED_RE):
# - "¿puedo llevar acompañante al evento?" es pedir traer +1 (política de amigo al evento,
#   ver anna_prompt_v5.md) — NO es escort. acompañant\w* solo cazamos junto a sexo/servicio,
#   no en el contexto neutro "llevar/traer a alguien al evento".
# - "no busco sexo" / "no quiero nada sexual" es la NEGACIÓN — el lead confirmando que
#   busca algo serio, justo lo contrario de lo que el filtro intenta cazar.
_ESCORT_NEGATED_RE = re.compile(
    r"(llevar|traer|venir\s+con|puedo\s+llevar|puedo\s+traer)\s+(un[a]?\s+|mi\s+)?acompañant\w*|"
    r"no\s+(busco|quiero|me\s+interesa|es)\s+(solo\s+|nada\s+)?(el\s+)?(sexo|sexual)",
    re.IGNORECASE,
)
# 'estafa'/'fraude' sueltos NO están aquí (ver nota abajo) — 'estafador' sí se queda
# (acusación directa a una persona, más inequívoco que la palabra suelta).
_AGGRESSION_RE = re.compile(
    r"\b(idiota|est[uú]pid[oa]|pendej\w*|mierda|cabr[oó]n|estafador\w*)\b",
    re.IGNORECASE,
)
# Excepción (2026-08-06): "está cabrón el precio" es modismo mexicano ("está intenso/
# fuerte"), NO insulto — solo "cabrón" dirigido a una persona (eres/pinche cabrón) es
# agresión real.
_AGGRESSION_NEGATED_RE = re.compile(
    r"est[aá]\s+cabr[oó]n",
    re.IGNORECASE,
)
# 'estafa'/'fraude' sueltos SE QUITARON de _AGGRESSION_RE (auditoría 2026-08-06):
# incluso con la excepción de negación ("no es estafa"), seguían bloqueando NAVEGATE
# preguntas legítimas de duda sin "no" — "¿esto es estafa o real?", "me da miedo que
# sea una estafa" (escenario #22 "es seguro/no confío/suena raro" existe justo para
# esto vía AI, con matices que una regex no puede dar). Bloqueo permanente por duda
# razonable de un lead sobre un servicio de $10,000 USD es un costo de negocio mucho
# mayor que dejar pasar una rara queja real ("qué estafa!") al AI en vez de auto-block.
# Кириллица — признак нецелевого лида (агентство работает с мексиканцами по-испански).
_CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")

# Лид пишет по-английски (2026-09-15, решение владелицы: бот на английском не отвечает
# вообще — молчит и сразу алертит Аню/Милу, без AI-вызова). Консервативная эвристика —
# требуем НЕСКОЛЬКО английских стоп-слов И ноль испанских, чтобы не ловить ложно короткие/
# смешанные сообщения ("Hola, how are you" — испанский стоп-слово "hola" есть → не триггерит).
_ENGLISH_STOPWORDS_RE = re.compile(
    r"\b(the|and|you|your|how much|what|when|where|hello|hi|thanks|thank you|price|cost|"
    r"event|please|interested|looking for|available|schedule|meeting|much does|does it|"
    r"i want|i am|i'm|can you|do you)\b",
    re.IGNORECASE,
)
_SPANISH_STOPWORDS_RE = re.compile(
    r"\b(el|la|los|las|de|que|y|en|un|una|es|para|con|por|no|s[ií]|m[aá]s|cu[aá]nto|"
    r"cu[aá]ndo|hola|gracias|quiero|busco|c[oó]mo|porfa|porque|est[aá])\b",
    re.IGNORECASE,
)


# ───────── автоприветствие WhatsApp Business (решение владелицы 2026-10-22) ─────────
# Номер шлёт его сам в ту же минуту, что и входящее; Wazzup присылает как эхо с нашего номера.
# Это не ответ Ани: лида в ручной режим не переводим. Тексты — app_settings.auto_greeting_texts
# (список строк, JSON или по строке), плюс известное начало «👉 ¡Bienvenido a Match Match Agency!».
AUTO_GREETING_PREFIX = "bienvenido a match match agency"


def _norm_greeting(text: str) -> str:
    t = (text or "").lower().translate(str.maketrans("áéíóúüñ", "aeiouun"))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", t)).strip()


def parse_auto_greeting_texts(raw: str | None) -> list[str]:
    """app_settings.auto_greeting_texts → список строк (JSON-массив или по строке)."""
    import json
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        val = json.loads(raw)
        if isinstance(val, list):
            return [str(x) for x in val if str(x).strip()]
        if isinstance(val, str):
            return [val]
    except ValueError:
        pass
    return [line for line in raw.splitlines() if line.strip()]


def is_auto_greeting(text: str, configured: list[str] | None = None) -> bool:
    norm = _norm_greeting(text)
    if not norm:
        return False
    if norm.startswith(AUTO_GREETING_PREFIX):
        return True
    return any(norm == _norm_greeting(c) for c in configured or [] if _norm_greeting(c))


def is_english(text: str) -> bool:
    """Detecta inglés con heurística conservadora — requiere >=2 señales en inglés Y
    CERO señales en español, para evitar falsos positivos en texto corto o mixto."""
    text = text or ""
    if _SPANISH_STOPWORDS_RE.search(text):
        return False
    return len(_ENGLISH_STOPWORDS_RE.findall(text)) >= 2

# Заявление об оплате (блок 13). Только claim-формы («я оплатил»), НЕ вопрос про оплату
# ("cómo es el pago?"): 'pago'/'pagar' без claim-контекста намеренно не ловим.
_PAYMENT_RE = re.compile(
    r"\b(pagu[eé]|ya\s+pagu[eé]|pagad[oa]s?|deposit[eé]|transfer[ií]|"
    r"hice\s+el\s+pago|оплати\w*)\b",
    re.IGNORECASE,
)
# Excepción (2026-08-06): negación ("aún no he pagado") — es un aviso de que TODAVÍA
# no pagó, lo contrario de un claim de pago. La regla del '?' en is_payment_claim cubre
# además preguntas sin negación ("¿ya están pagados los boletos?").
_PAYMENT_NEGATED_RE = re.compile(
    r"(no|a[uú]n\s+no|todav[ií]a\s+no)\s+(lo\s+|la\s+|los\s+|les\s+)?(he\s+|ha\s+|han\s+)?"
    r"(pagad[oa]s?|pagu[eé]|deposit[eé]|transfer[ií])",
    re.IGNORECASE,
)

# Instagram вместо фото: ссылка ig, слово instagram/insta, или @хэндл. Ловим ТОЛЬКО
# на стадии photo_pending (см. decide) — чтобы «vi su Instagram» в первом сообщении
# (стадия new) не триггерило. Бот не умеет валидировать IG-профиль как фото (Vision),
# поэтому передаём Ане на ручную проверку.
_INSTAGRAM_RE = re.compile(
    # (?<![a-zA-Z0-9@]) перед @хэндлом — чтобы не ловить локальную часть email
    # (juan@hotmail.com: @ идёт после буквы → не совпадёт, а «@handle» после пробела/начала — да).
    r"(instagram\.com/|instagr\.am/|\binstagram\b|\binsta\b|(?<![a-zA-Z0-9@])@[a-zA-Z0-9._]{3,})",
    re.IGNORECASE,
)


# Opt-out / стоп-слово: ЯВНАЯ просьба прекратить контакт / удалить / оставить в покое.
# НЕ путать с мягким «no me interesa» (#17, лист ожидания — лид может вернуться).
# Требуем контакт-стоп-интент, поэтому «no me interesa / no gracias / paso» сюда НЕ попадают.
# Осторожно с ловушками: «no me molesta» (=«меня не смущает», ПОЗИТИВ) ≠ «no me molestes»
# (императив, opt-out) — ловим только императивные формы molest(-es/-en/-ar).
# Голое «baja»/«alto»/«stop» не берём (риск ложных) — только в связке.
# ИСПРАВЛЕНО (2026-08-06, аудит): 'escrib\w*'/'contact\w*' были жадными — ловили не
# только императив («no me escribas» = opt-out), но и прошедшее/жалобу («no me
# escribiste el link» = лид напоминает про обещанное, а его банили навсегда). Сузили
# до явных повелительных форм (escribas/escriban/contactes/contacten).
_OPTOUT_RE = re.compile(
    r"no\s+me\s+(escribas|escriban|contactes|contacten|vuelvas?\s+a\s+escribir|manden?\s+mensajes?)|"
    r"no\s+me\s+vuelv\w+\s+a\s+(escribir|contactar|molestar)|"
    r"dej[ae]n?\s+de\s+(escribir\w*|molestar\w*|mandar\w*)|"
    r"d[eé]jame\s+(en\s+paz|de\s+escribir\w*)|d[eé]jenme\s+en\s+paz|"
    r"no\s+me\s+molest(es|en|e\s+m[aá]s|ar)\b|"
    r"(quiero\s+)?(darme|dar)\s+de\s+baja|"
    r"b[oó]rrame|elim[ií]name|borra\s+mi\s+n[uú]mero|"
    r"(qu[ií]tame|s[aá]came)\s+de\s+(tus?|la|las)\s+listas?|"
    # 2026-10-15 (аудит): частые разговорные формы, которые раньше уходили в AI.
    r"no\s+me\s+(mandes|env[ií]es|env[ií]en)\s+(m[aá]s\s+)?mensajes?|"
    r"no\s+me\s+manden?\s+m[aá]s\s+mensajes?|"
    r"no\s+quiero\s+que\s+me\s+(escribas|escriban|contactes|contacten|manden)|"
    r"ya\s+no\s+quiero\s+(recibir\s+)?(m[aá]s\s+)?mensajes|"
    # 2026-10-16: отказ сразу от звонков и переписки («no me llames ni me escribas»).
    # Только императив llames/llamen/marques — жалоба «no me llamaste ni me escribiste» не opt-out.
    r"\b(no|ni)\s+me\s+(llames|llamen|marques),?\s+ni\s+(me\s+)?(escribas|escriban|contactes|contacten|mandes|manden)\b|"
    r"^\s*(stop|basta|ya\s+basta|unsubscribe)\s*[.!]*\s*$|"
    # английские (лид-англоговорящий молчит по is_english, но opt-out чтим всегда)
    r"\bunsubscribe\b|don'?t\s+(text|message|contact)\s+me|stop\s+(texting|messaging|contacting)\s+me|"
    r"leave\s+me\s+alone|remove\s+me\s+from|"
    # Только or/nor: «don't call me and text me instead» — просьба писать, не отказ от всего.
    r"\b(don'?t|do\s+not)\s+(call|text|message)\s+(me\s+)?(or|nor)\s+(call|text|message)\s+me\b|"
    r"\bno\s+more\s+(calls?|messages?|texts?)\s+(or|nor)\s+(calls?|messages?|texts?)\b|"
    r"не\s+пиши\w*\s+мне|не\s+пишите\s+(мне|больше)|отпиш\w+|отписаться|"
    r"удалите\s+мой\s+номер|хватит\s+писать|не\s+беспоко\w+",
    re.IGNORECASE,
)


# Просьба позвонить вместо переписки (2026-10-16). Это НЕ opt-out: лид не отказывается от
# общения, он хочет звонок → ручной режим + алерт Ане, без do_not_contact. Приоритетнее opt-out:
# «no me escribas por aquí, mejor llámame» раньше банило лида навсегда.
# Явные формы — ловим сами по себе.
_CALL_REQUEST_RE = re.compile(
    r"\bmejor\s+(ll[aá]m(ame|eme|enme)|h[aá]blame|m[aá]rcame)\b|"
    r"\bll[aá]m(ame|eme|enme)\s+mejor\b|"
    r"\bmejor\s+(por\s+)?(una\s+)?llamada\b|\bmejor\s+por\s+tel[eé]fono\b|"
    r"\bmejor\s+(me\s+(llamas|marcas|hablas)|hablemos\s+por\s+(tel[eé]fono|llamada))\b|"
    r"\bprefiero\s+(que\s+me\s+(llam(es|e|en)|marques|hables\s+por\s+tel[eé]fono)|"
    r"una\s+llamada|hablar\s+por\s+tel[eé]fono)\b|"
    r"\b(h[aá]blame|ll[aá]m(ame|eme|enme))\s+(por\s+tel[eé]fono|al\s+(cel(ular)?|n[uú]mero))\b|"
    r"\bcall\s+me\s+instead\b|\b(i'?d\s+)?prefer\s+(a\s+)?(phone\s+)?call\b|"
    r"\b(rather|better)\s+call\s+me\b|\bcall\s+me\s+(rather|better)\b",
    re.IGNORECASE,
)
# Голый глагол «позвони мне» — сам по себе слишком широк («llámame Juan»), засчитываем как
# просьбу о звонке только в одном сообщении с opt-out («no me escribas, llámame»).
_CALL_VERB_RE = re.compile(
    r"\bll[aá]m(ame|eme|enme)\b|\bm[aá]rcame\b|\bh[aá]blame\s+por\s+tel[eé]fono\b|\bcall\s+me\b",
    re.IGNORECASE,
)
# Отказ от звонка отменяет только связку «голый глагол + opt-out» в is_call_request.
# «no me llames, ya no me escribas» и «no me llames ni me escribas» — не просьба позвонить,
# их ловит _OPTOUT_RE (do_not_contact). Явная форма из _CALL_REQUEST_RE сильнее отрицания:
# «no me llames al trabajo, mejor llámame al celular» — просьба позвонить.
# Само по себе «no me llames» без отказа от переписки — не opt-out, идёт дальше по decide.
_CALL_NEGATED_RE = re.compile(
    r"\bno\s+me\s+(llam(es|e|en)|marques|hables\s+por\s+tel[eé]fono)\b|\b(don'?t|do\s+not)\s+call\s+me\b|"
    r"\b(don'?t|do\s+not)\s+(text|message)\s+(me\s+)?(or|nor)\s+call\s+me\b",  # «don't text or call me»
    re.IGNORECASE,
)


# Вопрос про спутника (2026-10-21, решение владелицы): прийти с другом, подругой, девушкой,
# парой, группой, цена на двоих. Бот это не обсуждает (5,500 не называет никогда):
# ручной режим + алерт Ане, лиду ничего. Срабатывает только в контексте «пойти/прийти/привести
# вместе» или количества билетов: голое «con mi hermano» («trabajo con mi hermano»),
# «con alguien» («compartir mi vida con alguien»), «busco pareja», «tengo novia» (это №9),
# «me recomendó un amigo» и «¿puedo ir solo?» не срабатывают (code-review 2026-10-21).
_COMPANION_NOUN = (r"(?:amig[oa]s?|novi[oa]s?|parejas?|espos[oa]s?|mujer|se[ñn]ora|chava|"
                   r"morra|vieja|chica|acompa[ñn]antes?|herman[oa]s?|prim[oa]s?|"
                   r"compa[ñn]er[oa]s?|compas?|carnal(?:a)?|cuates?|cu[ñn]ad[oa]s?|compadre|"
                   r"comadre|hij[oa]s?|pap[aá]|mam[aá]|colegas?|socios?)")
_COMPANION_DET = (r"(?:(?:mi|mis|tu|un|una|unos|unas|el|la|los|las|otro|otra|otros|otras|"
                  r"alg[uú]n|alguna)\s+)?")
_COMPANION_WHO = rf"(?:{_COMPANION_DET}{_COMPANION_NOUN}|alguien|otra\s+persona|otras\s+personas|una\s+persona)"
# Глаголы «пойти / прийти / попасть на ивент».
_GO = (r"(?:ir|voy|vamos|ir[ií]a|ir[ií]amos|venir|vengo|vendr[ií]a|vendr[ií]amos|asistir|"
       r"asisto|llegar|llego|entrar|entro|acudir)")
# После «ir/llegar …» не считаем «пойти» глаголы о жизни: «quiero llegar a casarme con alguien
# serio», «llegar a formar una familia con mi pareja» (code-review 2026-10-22, ложное срабатывание).
_LIFE_VERB = r"(?:casar|vivir|compartir|formar|tener|estar|salir|conocer|hacer|pasar|envejecer)\w*"
# «con alguien / una persona» — только в ветке llevar/traer/invitar, не после «ir/llegar».
_GO_WHO = rf"(?:{_COMPANION_DET}{_COMPANION_NOUN}|otra\s+persona|otras\s+personas)"
_COMPANION_RE = re.compile(
    # «voy / puedo ir … con mi amigo», «y si voy con otra persona»
    rf"\b{_GO}\b(?:\s+(?!{_LIFE_VERB})\S+){{0,3}}?\s+con\s+{_GO_WHO}\b|"
    # «¿puedo ir con alguien?» — «alguien» только сразу после глагола
    rf"\b{_GO}\s+con\s+(?:alguien|una\s+persona)\b|"
    # короткий уточняющий вопрос: «¿y con mi novia?», «y si es con mi esposa?»
    rf"^\W*(?:y\s+)?(?:si\s+(?:es|voy|vamos)\s+)?con\s+{_COMPANION_DET}{_COMPANION_NOUN}\b|"
    # «llevar / traer / invitar a mi amigo / a alguien»
    rf"\b(?:llevar|traer|invitar|llevo|traigo|invito|lleve|traiga|invite)\s+(?:a\s+)?{_COMPANION_WHO}\b|"
    # «mi amigo también quiere ir», «un amigo iría»
    rf"\b(?:mi|mis|un|una|unos|unas)\s+{_COMPANION_NOUN}\s+(?:tambi[eé]n\s+)?"
    r"(?:(?:quiere|quieren|quisiera|quisieran|puede|pueden|podr[ií]a|podr[ií]an)\s+"
    r"(?:ir|venir|asistir|entrar)\b|ir[ií]an?\b|vendr[ií]an?\b|van?\s+conmigo\b|"
    r"les?\s+interesa\s+(?:ir|el\s+evento)\b)|"
    # «mi amigo y yo», «yo y mi novia», «nosotros dos»
    rf"\b{_COMPANION_DET}{_COMPANION_NOUN}\s+y\s+yo\b|\byo\s+y\s+{_COMPANION_DET}{_COMPANION_NOUN}\b|"
    r"\bnosotros\s+(?:dos|tres)\b|"
    # количество: «para dos?», «dos boletos», «boletos para 2», «somos tres». «¿cuánto por
    # persona?» — НЕ спутник: обычный вопрос о цене от того, кто идёт один (владелица 2026-10-21).
    r"\bpara\s+(?:dos|tres|2|3)(?:\s+personas)?\s*(?:[?¿!.,]|$)|"
    r"\b(?:boletos?|entradas?|lugares|tickets?)\s+para\s+(?:dos|tres|2|3)\b|"
    r"\b(?:dos|tres|2|3)\s+(?:boletos|entradas|lugares|tickets)\b|"
    r"\b(?:somos|vamos|ser[ií]amos|ir[ií]amos)\s+(?:dos|tres|cuatro|varios|2|3|4)\b|"
    # «ir en grupo / en pareja / acompañado», «precio por pareja», «es para parejas», 2x1
    rf"\b{_GO}\s+(?:en\s+grupo|en\s+pareja|acompa[ñn]ad[oa]s?)\b|"
    r"\bpor\s+pareja\b|\bpara\s+parejas?\b|\bacompa[ñn]antes?\b|\b2\s*x\s*1\b|\bdos\s+por\s+uno\b",
    re.IGNORECASE,
)
# Вырезаем до проверки: «terminé (hace años) con mi novia», «me separé de mi esposa»,
# «mi pareja ideal», «futura pareja» — это про семейное положение, не спутника.
_COMPANION_EXCLUDE_RE = re.compile(
    r"\b(?:termin|cort|romp|separ|divorci|dej)\w*(?:\s+\S+){0,3}?\s+(?:con|de|a)\s+"
    + _COMPANION_DET + _COMPANION_NOUN
    + r"|\b(?:futura|nueva)\s+pareja\b|\bpareja\s+ideal\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Decision:
    """Результат детерминированного решения по залпу лида."""
    action: str                 # respond | silent_whitelist | silent | silent_language | call_request | companion_question | optout | blocked | rejected | needs_ai
    reason: str                 # краткая причина (для лога/алерта/эскалации)
    alert_manager: bool = False # нужно ли уведомить Аню (сам алерт — блок 8)
    block_permanent: bool = False  # блок навсегда (do_not_contact + manual надолго)
    quiet: bool = False         # optout на паузе/в ручном режиме: фиксируем, но лиду НЕ пишем
    is_escort: bool = False     # escort-блок (инкремент escort_mention_count); не завязываемся на текст reason


def is_escort_mention(text: str) -> bool:
    """Явное упоминание интим-услуг. НЕ ловит «llevar acompañante al evento» или «no busco sexo»."""
    text = text or ""
    if not _ESCORT_RE.search(text):
        return False
    if _ESCORT_NEGATED_RE.search(text):
        return False
    return True


def is_aggression(text: str) -> bool:
    """Явная агрессия/оскорбление. НЕ ловит модизм "está cabrón" (=intenso, no insulto)."""
    text = text or ""
    if not _AGGRESSION_RE.search(text):
        return False
    if _AGGRESSION_NEGATED_RE.search(text):
        return False
    return True


def is_payment_claim(text: str) -> bool:
    """Лид заявляет, что оплатил (claim-форма). НЕ ловит отрицание или вопрос про оплату."""
    text = text or ""
    if not _PAYMENT_RE.search(text):
        return False
    if _PAYMENT_NEGATED_RE.search(text):
        return False
    if "?" in text:
        # Реальный claim об оплате почти никогда не вопрос — «¿ya están pagados?»
        # это сомнение/вопрос, а не заявление «я заплатил».
        return False
    return True


def is_optout(text: str) -> bool:
    """Лид явно просит прекратить контакт / отписаться (opt-out). НЕ мягкое «no me interesa»."""
    return bool(_OPTOUT_RE.search(text or ""))


def is_call_request(text: str) -> bool:
    """Лид просит позвонить вместо переписки. Приоритетнее opt-out (см. _CALL_REQUEST_RE)."""
    text = text or ""
    # Явная форма («mejor llámame») побеждает даже при отрицании рядом
    # («no me llames al trabajo, mejor llámame al celular»).
    if _CALL_REQUEST_RE.search(text):
        return True
    # Голый глагол в связке с opt-out — только если звонок не отвергнут
    # («no me llames, ya no me escribas» = opt-out).
    if _CALL_NEGATED_RE.search(text):
        return False
    return is_optout(text) and bool(_CALL_VERB_RE.search(text))


def is_companion_question(text: str) -> bool:
    """Лид спрашивает про спутника (друг, девушка, пара, группа, цена на двоих)."""
    cleaned = _COMPANION_EXCLUDE_RE.sub(" ", text or "")
    return bool(_COMPANION_RE.search(cleaned))


def has_instagram(text: str) -> bool:
    """Лид дал Instagram (ссылку/@хэндл/слово). Гейтить стадией photo_pending в decide."""
    return bool(_INSTAGRAM_RE.search(text or ""))


def is_russian_number(phone: str) -> bool:
    """Номер с кодом страны +7 (Россия/Казахстан) — не целевой регион.

    Код +7 однозначный: только он начинается с 7 (нет других кодов стран на 7).
    Мексика — wa_52, поэтому wa_7... надёжно отделяется от других префиксов.
    """
    return (phone or "").startswith("wa_7")


def has_cyrillic(text: str) -> bool:
    """Текст содержит кириллицу (русский язык) — не целевой лид."""
    return bool(_CYRILLIC_RE.search(text or ""))


def _manual_active(lead: dict) -> bool:
    """Лид в ручном режиме с активным manual_until (менеджер ведёт диалог)."""
    if lead.get("mode") != "manual":
        return False
    until = lead.get("manual_until")
    if until is None:
        return True  # manual без срока — считаем активным
    # manual_until может быть datetime (из asyncpg). Сравнение с now в БД-слое было бы
    # точнее, но здесь достаточно: если срок задан и в прошлом — уже не активен.
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return until > now
    except Exception:
        return True


def decide(lead: dict, is_whitelisted: bool, user_text: str, phone: str = "",
           bypass_phones: frozenset = frozenset(),
           whitelist_no_alert: bool = False, bot_paused: bool = False) -> Decision:
    """Принять детерминированное решение по лиду и склеенному тексту залпа.

    lead — строка leads (dict) или {} для нового. Порядок приоритетов фиксирован.
    phone — 'wa_<digits>' (для проверки региона); по умолчанию '' (совместимость).
    bypass_phones — номера-исключения silent-фильтра (тестовые/доверенные).
    whitelist_no_alert — whitelist-запись это личный контакт Anna (reason personal_contact):
        бот молчит БЕЗ алерта Ане. VIP-клиент (False) — алерт остаётся.
    bot_paused — глобальная пауза (тумблер в CRM): бот молчит ВСЕМ, кроме bypass_phones
        (тестовые номера). Для тех-режима: подключаем/правим, отвечаем только на свой номер.
    """
    lead = lead or {}
    text = user_text or ""
    name = lead.get("whatsapp_name") or lead.get("name") or "лид"
    # bypass сравниваем по канону: 'wa_52…' и 'wa_521…' — один мексиканский номер.
    is_bypass = bool(phone) and canonical_phone(phone) in canonical_set(bypass_phones)

    # 1) Whitelist → бот молчит. VIP-клиент: + алерт «написал клиент». personal_contact
    #    (личная база Anna): без алерта — она и так ведёт эти чаты в WhatsApp вручную.
    if is_whitelisted:
        return Decision("silent_whitelist", f"whitelist: написал {name}",
                        alert_manager=not whitelist_no_alert)
    # do_not_contact / manual → бот молчит БЕЗ алерта: заблокированный нарушитель не должен
    # спамить Аню VIP-уведомлением на каждое сообщение; в manual Аня и так в чате WhatsApp.
    if lead.get("do_not_contact"):
        return Decision("silent", f"do_not_contact — молчу: {name}")
    # Просьба позвонить вместо переписки — ДО opt-out («no me escribas, mejor llámame» не бан):
    # ручной режим + алерт Ане, лиду бот ничего не пишет. Как и opt-out, чтим на паузе
    # и в ручном режиме (Аня должна узнать, что лид ждёт звонка).
    if is_call_request(text):
        return Decision("call_request", "лид просит позвонить вместо переписки",
                        alert_manager=True)
    # Вопрос про спутника (друг, девушка, пара, группа) — ДО AI и до №9 «не холост»: «¿puedo
    # ir con mi novia?» не должно навсегда блокировать лида. Ручной режим + алерт Ане, лиду
    # ничего. Opt-out важнее («no me escribas, voy con mi amigo» — это отказ). На паузе
    # (кроме тестовых номеров) и в ручном режиме не срабатывает: бот и так молчит, Аня видит
    # такие сообщения в сводке паузы или сама ведёт чат.
    # Escort и агрессия важнее (там блок навсегда), уже отклонённым лидам — не алертим.
    if (is_companion_question(text) and not is_optout(text)
            and not is_escort_mention(text) and not is_aggression(text)
            and lead.get("funnel_stage") not in ("lost", "rejected")
            and not (bot_paused and not is_bypass) and not _manual_active(lead)):
        return Decision("companion_question", "лид спрашивает про спутника (друг/пара/группа)",
                        alert_manager=True)
    # Opt-out ДО manual/региона: явная просьба «не пиши» ставит do_not_contact навсегда,
    # даже если лида вёл менеджер (иначе при возврате в auto догоны возобновятся).
    if is_optout(text):
        # На паузе (кроме тестовых номеров) и когда чат ведёт Аня — бот не пишет даже
        # подтверждение: do_not_contact ставим, Аню алертим, ответит она сама.
        quiet = (bot_paused and not is_bypass) or _manual_active(lead)
        return Decision("optout", "лид попросил не писать (opt-out)", alert_manager=True,
                        quiet=quiet)
    # Глобальная пауза (тех. режим) — ПОСЛЕ whitelist/opt-out (согласие лида и VIP-алерты чтим
    #    даже на паузе), молчим всем остальным, кроме тестовых bypass-номеров. Без алерта.
    if bot_paused and not is_bypass:
        return Decision("silent", "бот на паузе (тех. режим) — молчу")
    if _manual_active(lead):
        return Decision("silent", f"manual mode — менеджер ведёт: {name}")

    # 1.5) Не целевой регион/язык (русский номер +7 или кириллица) → тихо молчим.
    #      НЕ блокируем (не дисквалификация, вдруг ошибка) — просто не тратим AI-вызов.
    #      Номера из bypass_phones (тестовые/доверенные) проверку пропускают.
    if not is_bypass:
        if is_russian_number(phone):
            return Decision("silent", "молчу — русский номер +7, не целевой регион")
        if has_cyrillic(text):
            return Decision("silent", "молчу — кириллица/русский язык, не целевой лид")
        # Английский — решение владелицы 2026-09-15: бот не отвечает на английском вообще,
        # молчит и сразу алертит (не тратим AI-вызов на язык, которым бот не владеет).
        if is_english(text):
            return Decision("silent_language", "лид пишет по-английски", alert_manager=True)

    # 2) Escort/секс-услуги → блок навсегда (с ПЕРВОГО упоминания).
    if is_escort_mention(text):
        return Decision("blocked", "Ищет интим-услуги", alert_manager=True,
                        block_permanent=True, is_escort=True)

    # 3) Явная агрессия → блок.
    if is_aggression(text):
        return Decision("blocked", "Агрессивное поведение", alert_manager=True, block_permanent=True)

    # 3.5) Заявление об оплате → ручное подтверждение Аней (блок 13). Бот НЕ меняет
    #      стадию сам: шлёт ack + эскалация с кнопкой «Подтвердить оплату».
    if is_payment_claim(text):
        return Decision("payment_claim", "лид сообщил об оплате", alert_manager=True)

    # 4) Жёсткая дисквалификация по УЖЕ известным полям (заполнит AI в блоке 6).
    age = lead.get("age")
    if isinstance(age, int) and (age < MIN_AGE or age > MAX_AGE):
        return Decision("rejected", f"Возраст {age} вне {MIN_AGE}-{MAX_AGE}")
    if lead.get("is_single") is False:
        return Decision("rejected", "Не холост")

    # 4.5) Instagram вместо фото (стадия photo_pending): бот не валидирует IG как фото —
    #      короткий бридж-ответ + ручной режим + алерт Ане (она смотрит профиль лично).
    if lead.get("funnel_stage") == "photo_pending" and has_instagram(text):
        return Decision("instagram_handoff", "Instagram вместо фото — на Аню", alert_manager=True)

    # 5) Остальное — решает AI (квалификация, профессия, casual, тон).
    return Decision("needs_ai", "нужен AI")
