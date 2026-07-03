# Чеклист перед продакшном

Накопленные задачи, отложенные во время разработки (проверить/сделать перед боевым запуском).
Код-заметки помечены модулем; конфиг-задачи — переменной .env.

## Конфиг / креды
- [ ] **TG_MANAGER_CHAT_ID → chat_id Ани** (не мой личный). Бот «Лиды» (business-алерты:
      эскалация/VIP/блок) должен идти Ане — она принимает решения по лидам. Бот «Ошибки»
      (TG_ALERTS_CHAT_ID) остаётся на разработке (мой chat_id). Как получить chat_id Ани:
      она пишет боту → `GET https://api.telegram.org/bot<TG_MANAGER_BOT_TOKEN>/getUpdates`
      → взять `message.chat.id` (как делали для тестового).
- [ ] **SILENT_BYPASS_PHONES** — убрать тестовые номера (79635378880 / 79635708880) или
      оставить пустым. Иначе +7-тесты будут проходить фильтр на проде.
- [ ] Все секреты `.env` — боевые (не тестовые): Supabase DSN, OpenAI, Wazzup token/channel,
      оба Telegram-токена, WAZZUP_WEBHOOK_SECRET (длинный случайный).

## Вебхук / деплой
> Артефакты и пошаговый runbook: **`deploy/`** (DEPLOY.md, matchmatch-bot.service, nginx-matchmatch.conf).
> Хостнейм: **64-188-119-94.sslip.io** (валидный Let's Encrypt cert, без покупки домена). localtunnel не используется.
- [ ] **Wazzup вебхук на постоянный публичный URL** (не localtunnel!) —
      `https://64-188-119-94.sslip.io/webhook/wazzup/<secret>`, `PATCH /v3/webhooks` с `messagesAndStatuses=true`.
- [ ] **Telegram вебхук менеджер-бота** (блок 11): `setWebhook` на `<домен>/webhook/telegram/<TG_WEBHOOK_SECRET>`
      для бота «Лиды» (`TG_MANAGER_BOT_TOKEN`). Проверить `getWebhookInfo`. Локально — тот же localtunnel.
- [ ] **TG_WEBHOOK_SECRET** — длинный случайный (как WAZZUP_WEBHOOK_SECRET). Пустой → эндпоинт 403.
- [ ] **TG_MANAGER_ADMIN_IDS** — Telegram user_id Ани и разработки (csv). Пусто → фолбэк на
      chat_id из TG_MANAGER_CHAT_ID/TG_ALERTS_CHAT_ID. Чужие id команды/кнопки не получают.
- [ ] **uvicorn с `--no-access-log`** (секрет в URL иначе утекает в journald) — в systemd-юните.
- [ ] systemd: автоперезапуск, `journalctl` логи (блок 12).

## Надёжность (доработки под нагрузку)
- [x] **OpenAI rate-limit handling**: retry + exponential backoff на 429/5xx/сеть — `ai._openai_post`
      (обёртка chat и эмбеддингов), уважает Retry-After. Блок 12.
- [x] **Startup-sweep** непроцессенных inbound после рестарта — `db.phones_with_unprocessed_inbound`
      + прогон через debounce в lifespan (main.py). Блок 12.
- [ ] Мониторинг `cached_tokens` в логах OpenAI (prompt caching работает авто, следить за %).

## Бизнес-данные / контент
- [ ] **Whitelist клиентов агентства** — получить у Ани актуальный список VIP/текущих
      клиентов (номеров у нас ещё НЕТ) и залить в `bot_whitelist` перед запуском (блок 10).
      Заливка: `db.add_to_whitelist(phone, reason, added_by)` (пока вручную скриптом; интерфейс
      добавления — через менеджер-бот в блоке 11 и мини-апп позже).
- [ ] Проверить промпт Anna (v5) на захардкоженные секреты (в WF1 были банковские реквизиты
      в промпте — у нас в промпте их быть не должно; реквизиты выдаёт AI по сценарию/на звонке).
- [ ] Актуальные данные ивента (дата/адрес/ссылки) — если сценарии их содержат.

## Безопасность (сделано, для памяти)
- [x] Токен Telegram-бота не логируется (блок 8: ловим HTTPStatusError, лог только статус).
- [x] Секрет вебхука — fail-fast без .env, не в git.
- [x] SQL параметризован везде; имена колонок из whitelist.
