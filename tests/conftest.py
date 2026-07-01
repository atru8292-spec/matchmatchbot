"""Изоляция тестов от прод-БД.

Форсируем пустой SUPABASE_DB_DSN в окружении ДО импорта config тестовыми модулями.
os.environ имеет приоритет над .env в pydantic-settings, поэтому даже если случайно
запустится lifespan, init_pool() не будет вызван и подключения к реальному Supabase
не произойдёт. Реальную БД дёргаем только вручную (смоук), никогда в pytest.
"""
import os

# Пустой DSN → lifespan не подключится к реальной БД.
os.environ["SUPABASE_DB_DSN"] = ""
# Секрет-заглушка: поле обязательно (без дефолта). Без этого в CI без .env импорт
# settings упал бы ValidationError; а локально в URL тестов подставлялся бы реальный
# прод-секрет. setdefault не перетирает, если переменная уже задана в окружении.
os.environ.setdefault("WAZZUP_WEBHOOK_SECRET", "test-secret-stub")
