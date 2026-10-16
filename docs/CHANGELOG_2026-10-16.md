# Изменения на 2026-10-16

Состояние на момент записи: бот на паузе (`bot_paused=1`), `scheduler_enabled=0`,
`event_active=0`, `stale_inbound_hours=12`. На сервере код `e151c48`.

## 1. Новый сервер

- XorekCloud VDS DE-E-2 #2031741, Ubuntu 22.04, IP `2.26.71.254`, домен `2-26-71-254.sslip.io`.
- Код в `/opt/matchmatch-bot`, сервис `matchmatch-bot` (пользователь `matchmatch`).
- Старый сервер `64.188.119.94` удалён хостером. Туда не заходить и ничего не отправлять:
  IP мог достаться другому клиенту.
- Runbook восстановления и обновления: `deploy/DEPLOY.md`, скрипт `deploy/restore.sh`.

## 2. Секреты и `.env`

- На сервере сгенерированы новые `WAZZUP_WEBHOOK_SECRET` и `TG_WEBHOOK_SECRET`. Вебхуки
  Wazzup и Telegram настроены на них.
- Серверный `.env` отличается от локального этими двумя значениями. Локальный `.env`
  на сервер не заливать: вебхуки перестанут приниматься.
- На сервере дополнительно заданы `MINI_APP_URL`, `GOOGLE_CALENDAR_ID`, `GOOGLE_SHEET_ID`,
  `GOOGLE_GUEST_SHEET_ID`.
- `TG_MANAGER_CHAT_ID`: два id, Арины и Ани (6757251513, @rusaencdmx1).
- `TG_MANAGER_ADMIN_IDS`: два id (проверено на сервере).

## 3. Канонизация номеров

- `wa_52` + 10 цифр и `wa_521` + 10 цифр для Мексики считаются одним номером
  (`phones.py`: `canonical_phone`, `phone_variants`, `same_phone`, `sql_canonical`).
- Сравнения номеров (whitelist, тестовые bypass-номера, поиск лида, JOIN) идут по канону.
  Формат хранения в базе не меняли.

## 4. Расширенный whitelist

- В `bot_whitelist` добавлено 432 номера с `added_by='wa_contacts_2026-10-15'`.
  Для них бот молчит.
- Бэкапы до вставки: `docs/backups/bot_whitelist_before_wa_contacts_2026-10-15_*.json`,
  список вставленных: `docs/backups/inserted_wa_contacts_2026-10-15_*.json`.
- Откат (только по отдельному решению):
  ```sql
  DELETE FROM bot_whitelist WHERE added_by = 'wa_contacts_2026-10-15';
  ```

## 5. Планировщик (авто-рассылки)

- Флаг `app_settings.scheduler_enabled`, в CRM тумблер «Авто-рассылки» (с подтверждением
  при включении). Пока `0`, планировщик не отправляет ничего: догоны, напоминания об ивенте,
  напоминания о звонке. Нет ключа или сбой чтения = выключено.
- Пауза (`bot_paused=1`) тоже останавливает все проактивные отправки.
- Защита от залпа: холодный догон уходит только тем, кто писал не раньше 21 дня назад
  (`followup_max_silence_days`), и не тем, кто писал за последние 24 часа.
- Дневной лимит холодных догонов: 30 (`cold_followup_daily_cap`), счётчик хранится в базе
  и переживает перезапуск. Тёплые напоминания (ивент, звонок) в лимит не входят.
- Старые входящие: сообщения старше `stale_inbound_hours` (12 ч), которые Wazzup дослал
  после простоя, сохраняются, но бот на них не отвечает. Opt-out в них ставит
  `do_not_contact`, просьба позвонить даёт ручной режим и алерт (см. п. 7).

## 6. Эхо-сообщения Ани

- Если Аня ответила лиду с телефона или из Wazzup, бот переводит чат в ручной режим
  и сохраняет её сообщение в историю.
- Свои сообщения бот отличает по тексту (совпадение с нашим исходящим за 30 минут).
  Эхо старше 30 минут и медиа без подписи пропускаются.
- Вернуть чат боту: кнопка «Вернуть боту» в CRM.

## 7. Просьба позвонить вместо переписки (коммит `e151c48`)

- Раньше «no me escribas por aquí, mejor llámame» считалось opt-out и ставило
  `do_not_contact`. Теперь:
  - чат переводится в ручной режим;
  - Ане (в `TG_MANAGER_CHAT_ID`) уходит алерт «📞 Лид просит позвонить»: имя, ссылка
    wa.me с номером, последнее сообщение, кнопки «Общаться лично» и «Карточка»;
  - лиду бот ничего не пишет;
  - если ручной режим не включился, в алерте пометка «Ручной режим НЕ включился»,
    в техчат уходит ошибка.
- Работает и на паузе, и в ручном режиме, и для старых входящих.
- Порядок проверок (`filters.decide`): whitelist → `do_not_contact` → просьба позвонить →
  opt-out → пауза → ручной режим → остальное. Если в сообщении есть и «не пиши»,
  и «позвони», срабатывает звонок.

Распознаваемые фразы (`filters.py`, `_CALL_REQUEST_RE`), сами по себе:
- mejor llámame / llámeme / llámenme / háblame / márcame;
- llámame / llámeme / llámenme mejor;
- mejor (por) (una) llamada; mejor por teléfono;
- mejor me llamas / me marcas / me hablas; mejor hablemos por teléfono / por llamada;
- prefiero que me llames / llame / llamen / marques; prefiero que me hables por teléfono;
  prefiero una llamada; prefiero hablar por teléfono;
- háblame / llámame / llámeme / llámenme por teléfono / al cel / al celular / al número;
- call me instead; (I'd) prefer a (phone) call; rather call me / better call me;
  call me rather / call me better.

Только вместе с opt-out в том же сообщении (`_CALL_VERB_RE`): llámame / llámeme / llámenme,
márcame, háblame por teléfono, call me. Например, «no me escribas, llámame».

Отказ от звонка (`_CALL_NEGATED_RE`): no me llames / llame / llamen / marques,
no me hables por teléfono, don't / do not call me. Отменяет только связку
«голый глагол + opt-out». Явная форма из первого списка срабатывает и при отрицании рядом.

Тесты: `tests/test_call_request.py` (88 тестов), весь набор 1645 passed.

## 8. Сервер: безопасность и мониторинг

- Healthcheck: таймер `matchmatch-healthcheck` раз в 2 минуты (первый запуск через 3 минуты
  после загрузки) проверяет сервис и `/health`, 3 попытки, затем алерт в техчат.
- fail2ban читает логи sshd из journald (`backend=systemd`).
- SSH только по ключу, вход по паролю выключен.
- Код тянется с GitHub через deploy key только на чтение.
- ufw: открыты только 22, 80, 443. TLS Let's Encrypt, продление через certbot.

## 9. Что осталось до запуска

- Оплатить Wazzup (канал в статусе `notEnoughMoney`).
- Пополнить баланс OpenAI.
- Аня нажимает /start у @matchmatch_leady_bot.
- Правка промпта про выключенный ивент: `docs/proposals/2026-10-15_event_inactive_prompt.md`
  (ждёт ОК).
- Единая цена ивента.
- Smoke-тест: одна живая генерация, только с ОК Арины.
- Живые проверки с тестового номера.
- Снятие паузы только по отдельной команде Арины.
- Авто-рассылки включать на следующий день после запуска.

## 10. Известные риски (не чинились)

- Кнопки «Одобрить фото» и «Бот больше не пишет» в алерте о фото не проверяют whitelist
  и паузу: по нажатию бот отправит сообщение лиду.
- Удаление медиа ивента в CRM срабатывает без подтверждения.
- Если лид в ручном режиме повторит просьбу позвонить, Аня получит алерт ещё раз.

## 11. Откат

- Остановить ответы лидам: тумблер «Бот на паузе» в CRM.
- Откатить код: `git revert <коммит>`, `git push`, затем деплой из `deploy/DEPLOY.md`
  («Обновление кода»).
- Откатить whitelist: строка SQL в п. 4.
