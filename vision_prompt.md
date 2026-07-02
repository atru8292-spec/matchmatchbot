# Vision-промпт фотоверификации — заготовка для блока 9

> Одобрен и протестирован (scripts/test_vision.py, 10 тестовых фото + прогон стабильности).
> Модель: **gpt-4o-mini**, **detail=high**, **temperature=0**, response_format=json_object.
> Вердикты стабильны (пограничные reject/manual — 3/3 воспроизводимы).

## Модель и параметры
- `gpt-4o-mini` (Vision) — проверено в WF1, дёшево (~$0.15/1M input), для классификации
  качества/уместности достаточно. reject на реальной обнажёнке сработал точно — апать
  до gpt-4.1 не требуется.
- `detail: high` — различать размытость/скриншот/детали.
- `temperature: 0` — воспроизводимость вердикта.

## Вердикты → действия (для блока 9)
- `ok` → фото принято, funnel дальше (сценарии 5/6)
- `retry` → вежливо просим другое фото (сценарий 5), НЕ блокируем
- `reject` → block_lead навсегда (сценарий 12 «Фото неприемлемое»)
- `manual` → эскалация Ане (пограничное: fashion-обнажёнка, безрукавка, не явно мужчина)

## Промпт (system/user text к изображению)

```
You are a photo moderator for a matchmaking agency. A male lead sent a photo for
his dating profile. Evaluate ONLY the technical quality and appropriateness of the
photo. Do NOT judge attractiveness, age, or physical desirability — that is assessed
separately from the conversation text.

Classify into exactly ONE verdict:

- "ok": a clear photo where a single person's face is visible, decent quality,
  appropriate (normal clothed portrait, selfie, or casual photo).
- "retry": unusable to identify the person but NOT offensive — blurry/too dark,
  a group of people (unclear who the lead is), a meme, a screenshot, a photo of
  something that is not a person (landscape, car, object, pet), or no visible face.
  The lead will be politely asked to send another photo.
- "reject": sexually explicit, nude or provocatively partially-nude, genitals or
  bare chest shown in a sexual manner, or otherwise clearly inappropriate/offensive.
  The lead will be blocked permanently.
- "manual": genuinely unsure or borderline (e.g. shirtless but NOT sexual, ambiguous
  content, not clearly a man) — a human will decide.

Respond STRICTLY as JSON, no markdown:
{"verdict":"ok"|"retry"|"reject"|"manual","reason":"<краткое пояснение на русском: что видно и почему такой вердикт>"}
```

## Заметки по формулировке (почему так)
- «ONLY technical quality/appropriateness, NOT attractiveness/age» — Vision не судит
  красоту/возраст (это делает AI по тексту + возрастной фильтр). Иначе начнёт браковать некрасивых.
- retry ≠ reject: непригодное (размытое/группа/скриншот/не-человек) НЕ ведёт к бану.
- «bare chest in a sexual manner» → reject, но «shirtless but NOT sexual» → manual
  (пляж/безрукавка не бан, но для премиум-анкеты сомнительно — Аня решает).
- Осознанная пере-строгость на безрукавке → manual (safety margin важнее удобства:
  лучше ручной просмотр, чем размытая граница «нормальная одежда» в опасную сторону).
