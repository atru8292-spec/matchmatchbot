"""Конфигурация приложения — все настройки берутся из .env.

В блоке 1 реально используется только WAZZUP_WEBHOOK_SECRET.
Остальные поля — заготовки под следующие блоки (значения пустые по умолчанию,
чтобы сервер поднимался без полного .env).
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # extra="ignore" — лишние переменные в .env (Supabase/OpenAI/Telegram)
    # не роняют загрузку, пока соответствующие поля не добавлены.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ===== Wazzup — приём вебхука (блок 1) =====
    # Секрет в URL вебхука: у Wazzup нет подписи, поэтому защищаемся секретным путём.
    # Без дефолта намеренно: если переменной нет в .env — приложение упадёт при
    # старте (fail-fast), чтобы не принимать вебхуки с общеизвестным секретом.
    wazzup_webhook_secret: str

    # ===== Wazzup — отправка (sender.py: текст и картинки) =====
    wazzup_token: str = ""
    wazzup_channel_id: str = ""

    # ===== Supabase Postgres (блок 2) =====
    # Полный DSN из Supabase → Connect → Session pooler (порт 5432, sslmode=require).
    # Пусто → БД не подключается (пул не создаётся), сервис работает без БД.
    supabase_db_dsn: str = ""

    # ===== OpenAI (блок 6) =====
    openai_api_key: str = ""
    openai_chat_model: str = "gpt-4.1"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_temperature: float = 0.3
    openai_vision_model: str = "gpt-4o-mini"  # фото-модерация (блок 9)
    openai_whisper_model: str = "whisper-1"  # транскрибация голосовых (voice.py)

    # ===== Supabase Storage (блок 9) =====
    supabase_url: str = ""
    supabase_service_key: str = ""
    supabase_storage_bucket: str = "lead-photos"
    # Медиа с ивентов (фото/видео) — отдельный bucket: у lead-photos лимит 10 МБ и только
    # image-MIME, а видео до 16 МБ + mp4. event-media: public, 20 МБ, image+video/mp4.
    supabase_event_media_bucket: str = "event-media"

    # ===== Telegram-алерты (блок 8) =====
    # Бот «Лиды» — business-алерты Ане (эскалация/VIP/блок).
    tg_manager_bot_token: str = ""
    tg_manager_chat_id: str = ""
    # Бот «Ошибки» — technical-алерты разработке (рантайм-сбои).
    tg_alerts_bot_token: str = ""
    tg_alerts_chat_id: str = ""

    # ===== Менеджер-бот (блок 11) =====
    # Секрет в пути вебхука Telegram (как у Wazzup): /webhook/telegram/<secret>.
    # Пусто → эндпоинт отвергает любые запросы (fail-safe, команды не принимаются).
    tg_webhook_secret: str = ""
    # Кто может слать команды/жать кнопки (Telegram user_id через запятую). Пусто →
    # дефолт {tg_manager_chat_id, tg_alerts_chat_id} (Аня + разработка). В личке
    # user_id == chat_id, поэтому chat_id админов подходят напрямую.
    tg_manager_admin_ids: str = ""
    # Контакт для запроса доступа (показывается чужим в ответе «закрытый бот»).
    # Telegram-хендл с @; кнопка ведёт на t.me/<хендл>.
    support_contact: str = "@arinashrr"

    # ===== Антибан follow-up (планировщик, блок 13) =====
    # Не слать догон, если лид писал за последние N часов (активный ≠ молчун).
    followup_quiet_hours: int = 24
    # Суточный лимит ХОЛОДНЫХ догонов/реактивации (#33/#36/#38) — антибан по номеру.
    # Тёплые напоминания (ивент/звонок) под этот лимит НЕ попадают.
    cold_followup_daily_cap: int = 30

    # ===== Google (Calendar автозапись звонков #53 + Sheets гостевой список) =====
    # Один сервис-аккаунт на оба API; JSON-ключ лежит на сервере (права 600).
    google_service_account_file: str = "/opt/matchmatch-bot/google-service-account.json"
    google_calendar_id: str = ""   # id общего календаря (пока тестовый личный)
    google_sheet_id: str = ""      # id нашей книги (анкеты «Solicitudes» и пр.)
    google_guest_sheet_id: str = ""  # id БОЕВОЙ книги гостевого списка Ани (отдельная книга)

    @property
    def manager_admin_ids(self) -> frozenset[int]:
        """Множество разрешённых Telegram user_id для менеджер-бота."""
        raw = self.tg_manager_admin_ids.strip()
        if not raw:
            raw = ",".join(x for x in (self.tg_manager_chat_id, self.tg_alerts_chat_id) if x)
        out = set()
        for part in raw.split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                out.add(int(part))
        return frozenset(out)

    # ===== Мини-CRM (Telegram Mini App, /api/mini/*) =====
    # Dev-режим: обход проверки Telegram initData для локальной разработки.
    # На ПРОДЕ обязан быть false — иначе API /api/mini/* открыт кому угодно.
    # Мини-апп открывается из менеджер-бота «Лиды», поэтому initData подписан
    # его токеном (tg_manager_bot_token) — авторизация сверяется с manager_admin_ids.
    mini_dev_mode: bool = False
    # Планировщик (фоллоу-апы/напоминания). На проде true. Выключаем (false) для
    # ЛОКАЛЬНОГО запуска против боевой БД — чтобы вторая копия приложения не слала
    # сообщения (планировщик тикает сразу на старте). Боевой инстанс не трогаем.
    scheduler_enabled: bool = True
    # Макс. возраст подписи initData (сек) — защита от повторного использования
    # старого initData (replay). 1 час (рекомендация Telegram): Mini App получает
    # свежий initData при каждом открытии, большое окно только расширяет атаку.
    mini_init_data_max_age: int = 3600
    # URL мини-аппа (Telegram Web App) для кнопки в менеджер-боте. Должен быть https
    # и на том же домене, что и /api/mini (тот же origin — без CORS).
    mini_app_url: str = "https://64-188-119-94.sslip.io/app/"

    # ===== Фильтры =====
    # Номера-исключения для silent-фильтра (тестовые/доверенные): для них НЕ применяем
    # silent по +7/кириллице. Список цифр через запятую, напр. "79635708880,79635378880".
    silent_bypass_phones: str = ""

    @property
    def silent_bypass_set(self) -> frozenset[str]:
        """Нормализованные 'wa_<digits>' номера-исключения silent-фильтра."""
        import re as _re
        out = set()
        for raw in self.silent_bypass_phones.split(","):
            digits = _re.sub(r"\D", "", raw)
            if digits:
                out.add("wa_" + digits)
        return frozenset(out)


settings = Settings()
