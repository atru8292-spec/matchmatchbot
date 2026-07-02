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

    # ===== Wazzup — отправка (блок 7, пока не используется) =====
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

    # ===== Telegram-алерты (блок 8) =====
    # Бот «Лиды» — business-алерты Ане (эскалация/VIP/блок).
    tg_manager_bot_token: str = ""
    tg_manager_chat_id: str = ""
    # Бот «Ошибки» — technical-алерты разработке (рантайм-сбои).
    tg_alerts_bot_token: str = ""
    tg_alerts_chat_id: str = ""

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
