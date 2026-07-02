"""Тест Vision-классификации фото (изолированно, БЕЗ БД и пайплайна).

Прогоняет все фото из test_photos/ через OpenAI Vision (gpt-4o-mini, detail=high),
классифицирует ok/retry/reject/manual, печатает вердикт + обоснование.

Запуск: ./venv/bin/python -m scripts.test_vision
"""
from __future__ import annotations

import asyncio
import base64
import json
import os

import httpx

from config import settings

PHOTOS_DIR = "test_photos"
VISION_MODEL = "gpt-4o-mini"
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")

VISION_PROMPT = """You are a photo moderator for a matchmaking agency. A male lead sent a photo for \
his dating profile. Evaluate ONLY the technical quality and appropriateness of the \
photo. Do NOT judge attractiveness, age, or physical desirability — that is assessed \
separately from the conversation text.

Classify into exactly ONE verdict:

- "ok": a clear photo where a single person's face is visible, decent quality, \
appropriate (normal clothed portrait, selfie, or casual photo).
- "retry": unusable to identify the person but NOT offensive — blurry/too dark, \
a group of people (unclear who the lead is), a meme, a screenshot, a photo of \
something that is not a person (landscape, car, object, pet), or no visible face. \
The lead will be politely asked to send another photo.
- "reject": sexually explicit, nude or provocatively partially-nude, genitals or \
bare chest shown in a sexual manner, or otherwise clearly inappropriate/offensive. \
The lead will be blocked permanently.
- "manual": genuinely unsure or borderline (e.g. shirtless but NOT sexual, ambiguous \
content, not clearly a man) — a human will decide.

Respond STRICTLY as JSON, no markdown:
{"verdict":"ok"|"retry"|"reject"|"manual","reason":"<краткое пояснение на русском: что видно и почему такой вердикт>"}"""


def _data_uri(path: str) -> str:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    mime = "jpeg" if ext in ("jpg", "jpeg") else ext
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return f"data:image/{mime};base64,{b64}"


async def classify(path: str) -> dict:
    payload = {
        "model": VISION_MODEL,
        "temperature": 0,  # классификация — максимальная воспроизводимость вердикта
        "max_tokens": 300,
        "response_format": {"type": "json_object"},
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": VISION_PROMPT},
                {"type": "image_url",
                 "image_url": {"url": _data_uri(path), "detail": "high"}},
            ],
        }],
    }
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json=payload,
        )
        r.raise_for_status()
        return json.loads(r.json()["choices"][0]["message"]["content"])


async def main() -> None:
    files = sorted(
        f for f in os.listdir(PHOTOS_DIR)
        if f.lower().endswith(IMG_EXT) and not f.startswith(".")
    )
    print(f"фото: {len(files)} | модель: {VISION_MODEL} (detail=high)\n")
    counts = {}
    for name in files:
        try:
            res = await classify(os.path.join(PHOTOS_DIR, name))
            v = res.get("verdict", "?")
            counts[v] = counts.get(v, 0) + 1
            print(f"[{v.upper():6}] {name}")
            print(f"         {res.get('reason','')}\n")
        except Exception as e:
            print(f"[ERROR ] {name}: {type(e).__name__}: {e}\n")
        await asyncio.sleep(1.5)
    print("=== сводка ===", counts)


if __name__ == "__main__":
    asyncio.run(main())
