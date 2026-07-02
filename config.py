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


settings = Settings()
