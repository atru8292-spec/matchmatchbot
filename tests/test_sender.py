"""Тесты модуля sender.py.

Мокаем:
- httpx.AsyncClient — HTTP-запросы (никакой реальной сети/Wazzup)
- sender.asyncio — устраняем реальные задержки (sleep → мгновенно)
- sender.send_one — изолируем логику send()
- db.save_outbound — проверяем вызовы без реальной БД
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import db
import sender


# ---------------------------------------------------------------------------
# Фикстура: FakePool для тестов db.save_outbound
# ---------------------------------------------------------------------------

class _FakePool:
    def __init__(self):
        self.execute = AsyncMock()


@pytest.fixture()
def db_pool():
    """Подменить db._pool на _FakePool на время теста (изоляция от реальной БД)."""
    fake = _FakePool()
    original = db._pool
    db._pool = fake
    yield fake
    db._pool = original


# ---------------------------------------------------------------------------
# Вспомогательные утилиты
# ---------------------------------------------------------------------------

def _make_http_client_cls(
    *,
    raise_for_status_exc=None,
    post_exc=None,
):
    """Фабрика фейк-класса httpx.AsyncClient для monkeypatch.

    - raise_for_status_exc: response.raise_for_status() бросает это исключение.
    - post_exc: client.post() сам бросает это исключение.
    Все успешные пути — ни один параметр не передан.
    """
    mock_response = MagicMock()
    if raise_for_status_exc is not None:
        mock_response.raise_for_status = MagicMock(side_effect=raise_for_status_exc)
    else:
        mock_response.raise_for_status = MagicMock()  # no-op

    if post_exc is not None:
        _post = AsyncMock(side_effect=post_exc)
    else:
        _post = AsyncMock(return_value=mock_response)

    class _FakeAsyncClient:
        def __init__(self, **kwargs):
            self.post = _post

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

    # Сохраняем мок как атрибут класса чтобы тесты могли проверить вызовы.
    _FakeAsyncClient._post_mock = _post
    _FakeAsyncClient._response_mock = mock_response
    return _FakeAsyncClient


# ---------------------------------------------------------------------------
# compute_delay
# ---------------------------------------------------------------------------

class TestComputeDelay:

    def test_short_text_result_in_range(self):
        """Короткий текст 'hola' → base clamped к MIN_DELAY=2.0 → результат в [3.5, 5.5]."""
        result = sender.compute_delay("hola")
        assert 3.5 <= result <= 5.5, f"ожидали [3.5, 5.5], получили {result}"

    def test_long_text_clamped_to_max(self):
        """300 символов → base=12.0 clamped к MAX_DELAY=8.0 → результат в [9.5, 11.5]."""
        text = "x" * 300
        result = sender.compute_delay(text)
        assert 9.5 <= result <= 11.5, f"ожидали [9.5, 11.5], получили {result}"

    def test_medium_text_result_in_range(self):
        """100 символов → base=4.0 (без clamp) → результат в [5.5, 7.5]."""
        text = "x" * 100
        result = sender.compute_delay(text)
        assert 5.5 <= result <= 7.5, f"ожидали [5.5, 7.5], получили {result}"

    def test_result_rounded_to_one_decimal(self, monkeypatch):
        """Результат округлён до 0.1: round(x * 10) / 10 — нет дробной части ниже 0.1."""
        monkeypatch.setattr(sender.random, "uniform", lambda a, b: 1.777777)
        result = sender.compute_delay("hola")
        assert result == round(result * 10) / 10

    def test_deterministic_with_fixed_random(self, monkeypatch):
        """Mock random.uniform → 2.0 → short text: base=2.0 + 2.0 = 4.0 (точно)."""
        monkeypatch.setattr(sender.random, "uniform", lambda a, b: 2.0)
        result = sender.compute_delay("hola")
        assert result == 4.0

    def test_empty_text_clamped_to_min(self, monkeypatch):
        """Пустая строка → base=0 clamped к 2.0; с fixed random=2.0 → 4.0."""
        monkeypatch.setattr(sender.random, "uniform", lambda a, b: 2.0)
        result = sender.compute_delay("")
        assert result == 4.0

    def test_none_text_handled_as_empty(self, monkeypatch):
        """None → len(None or '')=0 → base clamped к 2.0; с fixed random=2.0 → 4.0."""
        monkeypatch.setattr(sender.random, "uniform", lambda a, b: 2.0)
        result = sender.compute_delay(None)
        assert result == 4.0

    def test_exactly_200_chars_clamped_to_max(self, monkeypatch):
        """200 символов → base=8.0 (= MAX_DELAY, граница clamp); с fixed random=2.0 → 10.0."""
        monkeypatch.setattr(sender.random, "uniform", lambda a, b: 2.0)
        result = sender.compute_delay("x" * 200)
        assert result == 10.0


# ---------------------------------------------------------------------------
# send_one
# ---------------------------------------------------------------------------

class TestSendOne:

    async def test_success_returns_true(self, monkeypatch):
        """HTTP 200 (raise_for_status не бросает) → send_one возвращает True."""
        cls = _make_http_client_cls()
        monkeypatch.setattr(sender.httpx, "AsyncClient", cls)

        result = await sender.send_one("79635378880", "Hola!")

        assert result is True

    async def test_post_called_with_correct_url(self, monkeypatch):
        """POST идёт на WAZZUP_SEND_URL (первый позиционный аргумент)."""
        cls = _make_http_client_cls()
        monkeypatch.setattr(sender.httpx, "AsyncClient", cls)

        await sender.send_one("79635378880", "test")

        positional = cls._post_mock.call_args.args
        assert positional and positional[0] == sender.WAZZUP_SEND_URL

    async def test_post_body_contains_correct_fields(self, monkeypatch):
        """JSON body: chatType='whatsapp', chatId=переданный, text=переданный, channelId из settings."""
        from config import settings as cfg
        cls = _make_http_client_cls()
        monkeypatch.setattr(sender.httpx, "AsyncClient", cls)

        await sender.send_one("chat_id_123", "hello world")

        body = cls._post_mock.call_args.kwargs.get("json", {})
        assert body["chatType"] == "whatsapp"
        assert body["chatId"] == "chat_id_123"
        assert body["text"] == "hello world"
        assert body["channelId"] == cfg.wazzup_channel_id

    async def test_authorization_header_contains_bearer(self, monkeypatch):
        """Headers: Authorization содержит 'Bearer'."""
        cls = _make_http_client_cls()
        monkeypatch.setattr(sender.httpx, "AsyncClient", cls)

        await sender.send_one("123", "msg")

        headers = cls._post_mock.call_args.kwargs.get("headers", {})
        assert "Bearer" in headers.get("Authorization", "")

    async def test_http_status_error_returns_false(self, monkeypatch):
        """raise_for_status бросает HTTPStatusError → возвращает False, не бросает наружу."""
        exc = httpx.HTTPStatusError(
            "500 Internal Server Error",
            request=httpx.Request("POST", sender.WAZZUP_SEND_URL),
            response=httpx.Response(500),
        )
        cls = _make_http_client_cls(raise_for_status_exc=exc)
        monkeypatch.setattr(sender.httpx, "AsyncClient", cls)

        result = await sender.send_one("79635378880", "Hola!")

        assert result is False

    async def test_connect_error_returns_false(self, monkeypatch):
        """ConnectError при post() → False, не бросает наружу."""
        cls = _make_http_client_cls(post_exc=httpx.ConnectError("connection refused"))
        monkeypatch.setattr(sender.httpx, "AsyncClient", cls)

        result = await sender.send_one("79635378880", "Hola!")

        assert result is False

    async def test_generic_exception_returns_false(self, monkeypatch):
        """Любое Exception при post() → False, не бросает наружу."""
        cls = _make_http_client_cls(post_exc=Exception("unexpected network error"))
        monkeypatch.setattr(sender.httpx, "AsyncClient", cls)

        result = await sender.send_one("123", "text")

        assert result is False


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------

class TestSend:

    async def test_phone_prefix_stripped(self, monkeypatch):
        """wa_79635378880 → send_one вызван с chat_id='79635378880' (wa_ срезан)."""
        send_one_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(sender, "send_one", send_one_mock)
        monkeypatch.setattr(sender.asyncio, "sleep", AsyncMock())
        monkeypatch.setattr(db, "save_outbound", AsyncMock())

        await sender.send("wa_79635378880", ["Hola"])

        send_one_mock.assert_awaited_once_with("79635378880", "Hola")

    async def test_all_success_returns_count(self, monkeypatch):
        """3 сообщения, все успешны → возвращает 3."""
        monkeypatch.setattr(sender, "send_one", AsyncMock(return_value=True))
        monkeypatch.setattr(sender.asyncio, "sleep", AsyncMock())
        monkeypatch.setattr(db, "save_outbound", AsyncMock())

        result = await sender.send("wa_111", ["a", "b", "c"])

        assert result == 3

    async def test_all_success_save_outbound_called_3_times(self, monkeypatch):
        """3 сообщения, все успешны → save_outbound вызван ровно 3 раза."""
        monkeypatch.setattr(sender, "send_one", AsyncMock(return_value=True))
        monkeypatch.setattr(sender.asyncio, "sleep", AsyncMock())
        save_mock = AsyncMock()
        monkeypatch.setattr(db, "save_outbound", save_mock)

        await sender.send("wa_111", ["a", "b", "c"])

        assert save_mock.await_count == 3

    async def test_save_outbound_order_preserved(self, monkeypatch):
        """save_outbound вызывается в порядке сообщений: a → b → c."""
        monkeypatch.setattr(sender, "send_one", AsyncMock(return_value=True))
        monkeypatch.setattr(sender.asyncio, "sleep", AsyncMock())
        save_mock = AsyncMock()
        monkeypatch.setattr(db, "save_outbound", save_mock)

        await sender.send("wa_111", ["a", "b", "c"])

        # второй позиционный аргумент save_outbound(phone, text) — это text
        saved_texts = [c.args[1] for c in save_mock.call_args_list]
        assert saved_texts == ["a", "b", "c"]

    async def test_failed_send_one_no_save_and_less_count(self, monkeypatch):
        """send_one=False на первом → save_outbound для него не вызван, sent=1 (второй прошёл)."""
        monkeypatch.setattr(sender, "send_one", AsyncMock(side_effect=[False, True]))
        monkeypatch.setattr(sender.asyncio, "sleep", AsyncMock())
        save_mock = AsyncMock()
        monkeypatch.setattr(db, "save_outbound", save_mock)

        result = await sender.send("wa_222", ["fail_msg", "ok_msg"])

        assert result == 1
        assert save_mock.await_count == 1
        # сохранено только второе сообщение
        assert save_mock.call_args.args[1] == "ok_msg"

    async def test_empty_list_returns_zero_nothing_called(self, monkeypatch):
        """Пустой список → 0; send_one, sleep и save_outbound не вызваны."""
        send_one_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(sender, "send_one", send_one_mock)
        sleep_mock = AsyncMock()
        monkeypatch.setattr(sender.asyncio, "sleep", sleep_mock)
        save_mock = AsyncMock()
        monkeypatch.setattr(db, "save_outbound", save_mock)

        result = await sender.send("wa_333", [])

        assert result == 0
        send_one_mock.assert_not_awaited()
        sleep_mock.assert_not_awaited()
        save_mock.assert_not_awaited()

    async def test_sleep_called_once_per_message(self, monkeypatch):
        """asyncio.sleep вызывается ровно по одному разу перед каждым сообщением."""
        monkeypatch.setattr(sender, "send_one", AsyncMock(return_value=True))
        sleep_mock = AsyncMock()
        monkeypatch.setattr(sender.asyncio, "sleep", sleep_mock)
        monkeypatch.setattr(db, "save_outbound", AsyncMock())

        messages = ["x", "y", "z"]
        await sender.send("wa_444", messages)

        assert sleep_mock.await_count == len(messages)

    async def test_phone_without_prefix_unchanged(self, monkeypatch):
        """Телефон без 'wa_' → replace("wa_","",1) не меняет → chat_id тот же номер."""
        send_one_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(sender, "send_one", send_one_mock)
        monkeypatch.setattr(sender.asyncio, "sleep", AsyncMock())
        monkeypatch.setattr(db, "save_outbound", AsyncMock())

        await sender.send("79635378880", ["Hola"])

        send_one_mock.assert_awaited_once_with("79635378880", "Hola")

    async def test_all_fail_returns_zero_no_save(self, monkeypatch):
        """Все send_one→False → sent=0, save_outbound не вызван ни разу."""
        monkeypatch.setattr(sender, "send_one", AsyncMock(return_value=False))
        monkeypatch.setattr(sender.asyncio, "sleep", AsyncMock())
        save_mock = AsyncMock()
        monkeypatch.setattr(db, "save_outbound", save_mock)

        result = await sender.send("wa_555", ["a", "b"])

        assert result == 0
        save_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# db.save_outbound — проверяем SQL и параметры через FakePool
# ---------------------------------------------------------------------------

class TestDbSaveOutbound:

    async def test_sql_contains_outbound_direction(self, db_pool):
        """SQL содержит 'outbound' (значение direction)."""
        await db.save_outbound("wa_123", "Hola")
        sql = db_pool.execute.call_args.args[0]
        assert "outbound" in sql

    async def test_sql_contains_processed_and_now(self, db_pool):
        """SQL содержит 'processed' и 'now()' (флаг уже обработано + метка времени)."""
        await db.save_outbound("wa_123", "Hola")
        sql = db_pool.execute.call_args.args[0]
        assert "processed" in sql.lower()
        assert "now()" in sql.lower()

    async def test_parameters_lead_phone_sender_text(self, db_pool):
        """Параметры: args[1]=lead_phone, args[2]=sender='anna' (default), args[3]=text."""
        await db.save_outbound("wa_test_phone", "test_text")
        args = db_pool.execute.call_args.args
        assert args[1] == "wa_test_phone"
        assert args[2] == "anna"
        assert args[3] == "test_text"

    async def test_custom_sender_parameter(self, db_pool):
        """Кастомный sender передаётся как второй SQL-параметр."""
        await db.save_outbound("wa_phone", "msg", sender="bot_v2")
        args = db_pool.execute.call_args.args
        assert args[2] == "bot_v2"

    async def test_db_error_does_not_raise(self, db_pool):
        """Ошибка пула → save_outbound логирует и НЕ бросает (сообщение уже отправлено)."""
        db_pool.execute.side_effect = RuntimeError("db down")
        # не должно бросить
        await db.save_outbound("wa_123", "Hola")


class TestSendContinuesOnSaveFailure:
    """send продолжает и считает отправку успешной, даже если save_outbound не сохранил."""

    async def test_save_failure_does_not_break_send(self, monkeypatch):
        import sender
        monkeypatch.setattr(sender.asyncio, "sleep", AsyncMock())
        monkeypatch.setattr(sender, "send_one", AsyncMock(return_value=True))
        # save_outbound «проглотила» ошибку (ничего не вернула, не бросила)
        monkeypatch.setattr(db, "save_outbound", AsyncMock(return_value=None))
        sent = await sender.send("wa_79635378880", ["a", "b"])
        assert sent == 2  # отправка засчитана несмотря на проблемы с записью


# ---------------------------------------------------------------------------
# send_image (блок 13) — отправка картинки через contentUri
# ---------------------------------------------------------------------------


class TestSendImage:
    async def test_success_returns_true(self, monkeypatch, db_pool):
        cls = _make_http_client_cls()
        monkeypatch.setattr(sender.httpx, "AsyncClient", cls)
        monkeypatch.setattr(sender.asyncio, "sleep", AsyncMock())
        ok = await sender.send_image("wa_521234567890", "https://x/inv.jpg")
        assert ok is True

    async def test_body_uses_contentUri_not_text(self, monkeypatch, db_pool):
        cls = _make_http_client_cls()
        monkeypatch.setattr(sender.httpx, "AsyncClient", cls)
        monkeypatch.setattr(sender.asyncio, "sleep", AsyncMock())
        await sender.send_image("wa_521234567890", "https://x/inv.jpg")
        body = cls._post_mock.call_args.kwargs["json"]
        assert body["contentUri"] == "https://x/inv.jpg"
        assert body["chatId"] == "521234567890"  # без префикса wa_
        assert "text" not in body

    async def test_empty_url_returns_false_no_post(self, monkeypatch, db_pool):
        cls = _make_http_client_cls()
        monkeypatch.setattr(sender.httpx, "AsyncClient", cls)
        monkeypatch.setattr(sender.asyncio, "sleep", AsyncMock())
        ok = await sender.send_image("wa_1", "")
        assert ok is False
        cls._post_mock.assert_not_called()

    async def test_error_returns_false_and_alerts(self, monkeypatch, db_pool):
        cls = _make_http_client_cls(post_exc=Exception("net"))
        monkeypatch.setattr(sender.httpx, "AsyncClient", cls)
        monkeypatch.setattr(sender.asyncio, "sleep", AsyncMock())
        err = AsyncMock(); monkeypatch.setattr(sender.escalation, "notify_error", err)
        ok = await sender.send_image("wa_1", "https://x/inv.jpg")
        assert ok is False
        err.assert_awaited_once()


# ---------------------------------------------------------------------------
# Плейсхолдеры ссылок [course_link]/[event_link] (блок 13)
# ---------------------------------------------------------------------------


class TestLinkPlaceholders:
    async def test_fills_course_link(self, monkeypatch, db_pool):
        monkeypatch.setattr(sender.db, "get_settings",
                            AsyncMock(return_value={"course_link": "https://c/curso"}))
        out = await sender._fill_link_placeholders("cursos aquí: [course_link]")
        assert out == "cursos aquí: https://c/curso"

    async def test_empty_course_link_drops_bubble(self, monkeypatch, db_pool):
        monkeypatch.setattr(sender.db, "get_settings",
                            AsyncMock(return_value={"course_link": ""}))
        out = await sender._fill_link_placeholders("cursos aquí: [course_link]")
        assert out is None   # пустая ссылка → баббл не отправляем

    async def test_fills_event_vars(self, monkeypatch, db_pool):
        monkeypatch.setattr(sender.db, "get_settings", AsyncMock(return_value={
            "event_address": "Roma Norte", "event_date": "22 de julio"}))
        out = await sender._fill_event_vars("en [event_address], el [event_date]")
        assert out == "en Roma Norte, el 22 de julio"

    async def test_event_men_women_not_substituted(self, monkeypatch, db_pool):
        # event_men/event_women убраны из переменных ивента — токен НЕ подставляется
        # (шаблоны их больше не содержат; если вдруг встретится — остаётся как есть, не из БД).
        get = AsyncMock(return_value={})
        monkeypatch.setattr(sender.db, "get_settings", get)
        out = await sender._fill_event_vars("Habrá [event_women] mujeres y [event_men] hombres")
        assert out == "Habrá [event_women] mujeres y [event_men] hombres"
        get.assert_not_called()  # нет поддерживаемых плейсхолдеров → в БД не ходим

    async def test_fills_price_tokens(self, monkeypatch, db_pool):
        monkeypatch.setattr(sender.db, "get_settings", AsyncMock(return_value={
            "event_price_member": "4,000", "event_price_nonmember": "6,000"}))
        out = await sender._fill_event_vars(
            "no miembros [event_price_nonmember] MXN, miembros [event_price_member] MXN")
        assert out == "no miembros 6,000 MXN, miembros 4,000 MXN"

    async def test_promo_shown_when_old_price_set(self, monkeypatch, db_pool):
        monkeypatch.setattr(sender.db, "get_settings", AsyncMock(return_value={
            "event_price_nonmember": "6,000", "event_price_old": "9,000"}))
        out = await sender._fill_event_vars("[event_price_nonmember] MXN[event_promo]")
        assert out == "6,000 MXN (antes 9,000)"

    async def test_promo_hidden_when_old_price_empty(self, monkeypatch, db_pool):
        # акция кончилась → event_price_old пусто → «(antes …)» исчезает целиком
        monkeypatch.setattr(sender.db, "get_settings", AsyncMock(return_value={
            "event_price_nonmember": "6,000", "event_price_old": ""}))
        out = await sender._fill_event_vars("[event_price_nonmember] MXN[event_promo]")
        assert out == "6,000 MXN"

    async def test_empty_event_var_becomes_blank(self, monkeypatch, db_pool):
        monkeypatch.setattr(sender.db, "get_settings", AsyncMock(return_value={"event_time": ""}))
        out = await sender._fill_event_vars("🕣 [event_time]")
        assert out == "🕣 "   # незаданный ключ → пусто (не литеральный плейсхолдер)

    async def test_event_vars_no_placeholder_no_db(self, monkeypatch, db_pool):
        get = AsyncMock(); monkeypatch.setattr(sender.db, "get_settings", get)
        out = await sender._fill_event_vars("Hola guapo")
        assert out == "Hola guapo"
        get.assert_not_awaited()

    async def test_no_placeholder_untouched_no_db(self, monkeypatch, db_pool):
        get = AsyncMock(); monkeypatch.setattr(sender.db, "get_settings", get)
        out = await sender._fill_link_placeholders("Hola guapo 🤍")
        assert out == "Hola guapo 🤍"
        get.assert_not_awaited()   # без плейсхолдера в БД не ходим

    async def test_send_skips_bubble_when_link_empty(self, monkeypatch, db_pool):
        cls = _make_http_client_cls()
        monkeypatch.setattr(sender.httpx, "AsyncClient", cls)
        monkeypatch.setattr(sender.asyncio, "sleep", AsyncMock())
        monkeypatch.setattr(sender.db, "get_settings", AsyncMock(return_value={"course_link": ""}))
        sent = await sender.send("wa_1", ["Gracias 🤍", "cursos: [course_link]"])
        assert sent == 1   # первый баббл ушёл, второй (только ссылка, пусто) — нет


class TestLinkPlaceholdersTwoPass:
    async def test_both_present_one_empty_drops_whole_bubble(self, monkeypatch, db_pool):
        monkeypatch.setattr(sender.db, "get_settings",
                            AsyncMock(return_value={"course_link": "https://c", "event_link": ""}))
        out = await sender._fill_link_placeholders("cursos [course_link] pago [event_link]")
        assert out is None   # event_link пуст → весь баббл дропаем, course не теряем частично

    async def test_both_present_both_filled(self, monkeypatch, db_pool):
        monkeypatch.setattr(sender.db, "get_settings",
                            AsyncMock(return_value={"course_link": "https://c", "event_link": "https://e"}))
        out = await sender._fill_link_placeholders("cursos [course_link] pago [event_link]")
        assert out == "cursos https://c pago https://e"


class TestSendMediaMarker:
    """send_media пишет маркер дедупа: с event_date — привязан к ивенту (вар. B)."""

    async def test_dated_marker_written(self, monkeypatch):
        monkeypatch.setattr(sender, "_send_content_uri", AsyncMock(return_value=True))
        save = AsyncMock(); monkeypatch.setattr(sender.db, "save_outbound", save)
        ok = await sender.send_media("wa_1", "https://s/1.jpg", "image", "2026-08-15")
        assert ok is True
        assert save.call_args.args == ("wa_1", "[foto ивента отправлено 2026-08-15]")

    async def test_legacy_marker_without_date(self, monkeypatch):
        monkeypatch.setattr(sender, "_send_content_uri", AsyncMock(return_value=True))
        save = AsyncMock(); monkeypatch.setattr(sender.db, "save_outbound", save)
        await sender.send_media("wa_1", "https://s/v.mp4", "video")
        assert save.call_args.args == ("wa_1", "[video ивента отправлено]")

    async def test_no_marker_on_send_failure(self, monkeypatch):
        """Wazzup не принял → маркер не пишем (повторим позже)."""
        monkeypatch.setattr(sender, "_send_content_uri", AsyncMock(return_value=False))
        save = AsyncMock(); monkeypatch.setattr(sender.db, "save_outbound", save)
        ok = await sender.send_media("wa_1", "https://s/1.jpg", "image", "2026-08-15")
        assert ok is False
        save.assert_not_called()
