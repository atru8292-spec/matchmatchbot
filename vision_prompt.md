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
photo. Being older, plain-looking, or simply not conventionally attractive is NEVER
a reason to flag a photo — do not judge attractiveness or age by itself.

Classify into exactly ONE verdict:

- "ok": a clear photo of a MAN (the lead is always male — this agency matches men
  with women) where a single person's face is visible, decent quality, appropriate
  (normal clothed portrait, selfie, or casual photo).
- "retry": unusable to identify the (male) lead but NOT offensive — blurry/too dark,
  a group of people (unclear who the lead is), a meme, a screenshot, a photo of
  something that is not a person (landscape, car, object, pet), no visible face, OR
  the person in the photo is CLEARLY a woman (obviously not the male lead — wrong
  photo, not a judgment call). The lead will be politely asked to send his own photo.
- "reject": sexually explicit — genitals, nudity, or a clear sexual act depicted.
  A bare/shirtless chest ALONE is never enough for "reject" by itself, no matter the
  pose, facial expression, or framing (close-up, lying down, "flirty" look, etc.) —
  that always goes to "manual" instead, see below. OR the photo clearly and
  unambiguously shows
  a severe health/hygiene condition grossly inconsistent with a premium paid service
  (e.g. significantly decayed or missing teeth, visible untreated illness, extremely
  unkempt appearance) — obvious cases only, not a judgment call. Otherwise clearly
  inappropriate/offensive photos also belong here. The lead will be blocked
  permanently.
- "manual": genuinely unsure or borderline — ANY bare/shirtless chest with no
  genitals/nudity visible (regardless of pose or expression), ambiguous
  content, gender genuinely AMBIGUOUS/unclear (NOT the same as "retry" above —
  that's for when it's obviously a woman, no doubt about it; "manual" is only for
  truly hard-to-tell cases), a possible health/hygiene/appearance issue that is
  NOT clearly severe enough to be obvious (you are not fully confident it clears the
  "reject" bar above), OR the photo shows plausible signs of being AI-generated or
  synthetic (unnatural skin/texture, warped or asymmetric facial features, garbled
  background details, inconsistent lighting/shadows, "too perfect"/uncanny look,
  or other AI-generation artifacts) — you are not expected to be certain, just flag
  it for a human to double-check when something feels visibly off in that way; do
  NOT flag a photo for this reason just because it is high-quality, professionally
  shot, or filtered — only real generation artifacts count. This is NOT about being
  older or plain-looking by itself — only when a real presentability or authenticity
  concern exists but you are not certain how severe it is. A human (Anna) will
  decide. When genuinely torn between two verdicts, prefer "manual" over "reject"
  — but if you are confident, decide directly.

Respond STRICTLY as JSON, no markdown:
{"verdict":"ok"|"retry"|"reject"|"manual","reason":"<краткое пояснение на русском: что видно и почему такой вердикт — если manual из-за подозрения на AI-генерацию, явно укажи это>"}
```

## Заметки по формулировке (почему так)
- «Being older/plain-looking is NEVER a reason to flag» — Vision не судит красоту/возраст
  саму по себе (это делает AI по тексту + возрастной фильтр). Иначе начнёт браковать некрасивых
  или просто пожилых людей — это дискриминация, а не модерация.
- Новая ветка «severe health/hygiene condition» — закрывает реальный кейс (2026-08-01):
  фото прошло как "ok", потому что технически чёткое лицо + приемлемая одежда, но по факту
  явно не тянет на премиум-анкету (гнилые зубы, видимая болезнь). Узкая формулировка
  («obvious cases only, not a judgment call», НЕ просто возраст/красота) — чтобы не превратить
  это в лазейку для отсева некрасивых или пожилых.
- reject vs manual для этой ветки — тот же паттерн, что уже был для сексуального контента:
  очевидный случай (уверен) → reject (перм. блок, экономит время Ани на бесспорном); не уверен,
  что дотягивает до «явно severe» → manual (Аня решает). Осознанно НЕ «всегда manual»: бан
  по внешности/здоровью — юридически и репутационно чувствительнее бана за контент (риск
  выглядеть как дискриминация), поэтому планка для confident reject тут узкая («obvious», не
  «probably»), а не потому что reject запрещён в принципе.
- retry ≠ reject: непригодное (размытое/группа/скриншот/не-человек) НЕ ведёт к бану.
- ИСПРАВЛЕНО (2026-08-01): голый торс без гениталий/явной наготы — ВСЕГДА manual, никогда
  reject, вне зависимости от позы/выражения лица. Раньше формулировка «bare chest in a sexual
  manner» позволяла модели трактовать обычное селфи (крупный план, кокетливое выражение) как
  «сексуальную манеру» и слать в reject (перм. блок) — баг подтверждён 3/3 воспроизводимо в тесте.
  Теперь reject по наготе требует буквально гениталии/наготу/явный секс-акт — голый торс сам
  по себе такой планки не достигает никогда.
- ИСПРАВЛЕНО (2026-08-12): раньше «not clearly a man» было только в manual — то есть
  фото, где ОЧЕВИДНО женщина (не двусмысленно, просто не тот пол), формально могло уйти в
  "ok" (промпт для "ok" вообще не упоминал пол) или как минимум требовало решения Ани
  (manual), хотя тут нечего решать — это явно не тот человек. Лиды бота — всегда мужчины
  (агентство знакомит мужчин с женщинами), поэтому «явно женщина» теперь retry (вежливо
  просим его собственное фото), а manual остаётся только для реально неоднозначных случаев
  (не могу разобрать пол по фото). retry не блокирует — это не наказание, просто не то фото.
- ДОБАВЛЕНО (2026-08-15, прямая просьба владелицы): признаки AI-генерации/synthetic-фото —
  тоже manual, не reject и не отдельный авто-блок. Vision-модель (gpt-4o-mini) не умеет
  надёжно детектить дипфейки/AI-фото со 100% уверенностью — ложный reject на реальном
  живом фото (просто хорошо снятом/отфильтрованном) заблокировал бы честного лида
  навсегда, это хуже, чем пропустить один сомнительный кейс на ручную проверку Ани.
  Явно запрещено флагать за одно только высокое качество/профессиональную съёмку —
  только настоящие артефакты генерации (кожа, асимметрия, фон, освещение).
