# MatchMatch мини-CRM — бренд-гайд (Фаза 0)

Зафиксированные решения по дизайн-системе. Источник — дизайн-skills (frontend-design-pro,
ui-ux-pro-max, design-anti-patterns, product-ui-webapp, dashboard-ux) + бренд matchmatchagency.com.

## Шрифты (обязательно, кириллица + латиница)
- **Заголовки / display:** **Golos Text**, вес 600–700. Родная кириллица (Paratype),
  читаемый на мелких размерах (важно для CRM). Playfair Display отклонён — «плывёт»
  на мелкой кириллице.
- **Тело / UI:** **Manrope**, 400/500/600.
- Оба — bundled woff2 (НЕ CDN, НЕ Inter). `letter-spacing: -0.02em` на крупных заголовках,
  `line-height 1.6` тело. `font-variant-numeric: tabular-nums` для телефонов/дат/цифр.

## Палитра (OKLCH, тонированные нейтралы — не чистый #fff/#000)
Бренд с сайта: off-white база, глубокий teal, тёплое золото, charcoal-текст.
- paper (фон): тёплый off-white
- surface / elevated / floating — 3 z-плана (base→elevated→floating)
- **primary: teal** (доминанта) — кнопки/активные состояния
- **accent: gold** — дозированно (бейджи/выделения), НЕ везде
- ink (текст), muted (вторичный); success/danger — semantic
- Точные OKLCH-значения задаются в токенах на старте Фазы 0.

## Запрещено (anti-AI-slop)
- Inter/Roboto/Poppins; фиолетово-синий градиент; gradient-text; дефолтный Tailwind indigo/blue.
- `shadow-md` (только слоёные тонированные тени); цветная левая полоска карточки; glassmorphism-везде.
- 3 одинаковые карточки в ряд; card-in-card; center-everything; одинаковые отступы (нужен ритм).
- Эмодзи как иконки в chrome (иконки — Lucide line, единый набор; эмодзи только в контенте/тоне).
- `transition: all`; bounce/elastic easing. Анимации — только transform/opacity, 150–300ms, prefers-reduced-motion.

## Принципы (product-ui + dashboard-ux)
- CRM = функциональная среда: max текст `--text-xl`, плотность > пустоты, ОДИН primary-action на экран.
- Все состояния: default/hover/focus-visible/active/disabled; loading(skeleton)/empty(тёплый+CTA)/error.
- Touch-target ≥44px. Нижний таб-бар (mobile-first Telegram). Тёмная/светлая — token-driven, контраст ≥4.5:1.
- Дашборд: bento (не 4 одинаковые KPI), стадии — горизонтальные бары, «обновлено Nс назад» + refresh.

## Терминология (из блока 11 бота)
Без «заблокирован»/«whitelist». Использовать: «Клиент из списка», «Бот больше не пишет»,
«Общаться лично» / «Вернуть боту». UI — русский; переписка лидов — испанская.

## От Ани (ожидается)
- Логотип (пока нет брендбука — палитра с сайта).
- Google Service Account credentials (экспорт в Sheets).
