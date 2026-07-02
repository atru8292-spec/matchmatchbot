"""Изоляция тестов от внешних сервисов (прод-БД, Telegram, Wazzup).

Форсируем пустые креды в окружении ДО импорта config тестовыми модулями.
os.environ имеет приоритет над .env в pydantic-settings, поэтому реальные вызовы
(Supabase, Telegram) в pytest не происходят. Внешние API дёргаем только вручную (смоук).
"""
import os

# Пустой DSN → lifespan не подключится к реальной БД.
os.environ["SUPABASE_DB_DSN"] = ""
# Пустые Telegram-токены → escalation._send_telegram рано выходит, НЕ шлёт реальные
# алерты Ане во время тестов (иначе тесты слали бы настоящие сообщения в Telegram).
os.environ["TG_MANAGER_BOT_TOKEN"] = ""
os.environ["TG_MANAGER_CHAT_ID"] = ""
os.environ["TG_ALERTS_BOT_TOKEN"] = ""
os.environ["TG_ALERTS_CHAT_ID"] = ""
# Секрет-заглушка: поле обязательно (без дефолта). Без этого в CI без .env импорт
# settings упал бы ValidationError; а локально в URL тестов подставлялся бы реальный
# прод-секрет. setdefault не перетирает, если переменная уже задана в окружении.
os.environ.setdefault("WAZZUP_WEBHOOK_SECRET", "test-secret-stub")
