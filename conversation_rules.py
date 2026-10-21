"""Запрещённые зачины, фразы и сленг режима rag — ТОЛЬКО для проверки ответа кодом.

В промпт и в базу знаний, которую видит модель, эти списки не попадают (решение владелицы
2026-10-21): модель копирует слова из списков «так нельзя» (perfil, dos caminos, avanzar —
уже было). Ответ модели с совпадением отбрасывается проверкой (ai_conversation), а
knowledge/seed.json проверяется тестом.
"""
from __future__ import annotations

import re

# Начало пузыря: лесть, восторг, вежливая формула вместо ответа.
BANNED_OPENER_RE = re.compile(
    r"^\W*(?:gracias\s+por\s+tu\s+inter[eé]s|excelente\s+pregunta|muy\s+buena\s+pregunta|"
    r"buena\s+pregunta|qu[eé]\s+bueno\s+que|perfecto|claro|con\s+mucho\s+gusto|con\s+gusto|"
    r"genial|s[uú]per|me\s+encanta|qu[eé]\s+gusto\s+que\s+(?:preguntes|te\s+interese))\b",
    re.IGNORECASE,
)

# В любом месте ответа.
BANNED_PHRASE_RE = re.compile(
    r"te\s+va\s+a\s+encantar|te\s+encantar[aá]|experiencia\s+incre[ií]ble|"
    r"cualquier\s+duda[^.?!]*aqu[ií]\s+estoy|aqu[ií]\s+estoy\s+para\s+lo\s+que\s+necesites|"
    r"(?:as[ií]|para)\s+(?:te\s+)?confirm\w*\s+(?:si|que)\s+(?:el\s+evento\s+)?(?:es\s+)?"
    r"(?:ideal|para\s+ti)|"
    r"(?:as[ií]|para)\s+(?:veo|ver|saber)\s+si\s+(?:encajas|el\s+evento\s+es\s+para\s+ti)|"
    r"es\s+ideal\s+para\s+ti|dos\s+caminos|dos\s+opciones|\bavanzar\b|\bperfil\b|\bguapo\b|"
    r"\bbase\s+de\b|muchos\s+(?:me\s+)?dicen|quedan\s+pocos|[uú]ltimos\s+lugares|"
    r"no\s+te\s+lo\s+pierdas|aprovecha|antes\s+de\s+que\s+suba|vale\s+la\s+pena|"
    r"inversi[oó]n\s+en\s+algo\s+tan\s+importante",
    re.IGNORECASE,
)

# Сленг и разговорные словечки (нейтральный мексиканский испанский, tuteo cortés).
SLANG_RE = re.compile(
    r"\b(?:porfa|ahorita|checa|checar|chequ\w+|jaja\w*|chance|te\s+late|padr[ií]simo|"
    r"muy\s+padre|[óo]rale|oye|embone|chido|neta|g[üu]ey|wey)\b|¡va!|^\W*va[,!.]",
    re.IGNORECASE | re.MULTILINE,
)


def violations(text: str) -> list[str]:
    """Нарушения в тексте ответа (пузыри через \\n\\n). Пустой список — всё чисто."""
    out = []
    for bubble in (text or "").split("\n\n"):
        m = BANNED_OPENER_RE.search(bubble)
        if m:
            out.append(f"зачин: {m.group(0).strip()!r}")
    for rx, label in ((BANNED_PHRASE_RE, "фраза"), (SLANG_RE, "сленг")):
        for m in rx.finditer(text or ""):
            out.append(f"{label}: {m.group(0).strip()!r}")
    return out
