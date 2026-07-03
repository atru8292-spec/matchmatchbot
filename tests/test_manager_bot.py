"""Unit-тесты manager_bot.py (блок 11) — менеджер-бот в Telegram.

Изоляция:
- _reply / _answer_callback заменяем AsyncMock (реальный Telegram не дёргаем).
- db.* и main._run_ai/_send_scenario мокаем через monkeypatch.
- Админский user_id задаём через settings.tg_manager_admin_ids (property пересчитает).
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import db
import escalation
import main
import manager_bot as mb

ADMIN_ID = 999
STRANGER_ID = 111


@pytest.fixture(autouse=True)
def _patch_io(monkeypatch):
    """Разрешить ADMIN_ID и заглушить исходящие Telegram-вызовы."""
    monkeypatch.setattr(mb.settings, "tg_manager_admin_ids", str(ADMIN_ID))
    reply = AsyncMock()
    answer = AsyncMock()
    monkeypatch.setattr(mb, "_reply", reply)
    monkeypatch.setattr(mb, "_answer_callback", answer)
    return {"reply": reply, "answer": answer}


def _msg(text: str, uid: int = ADMIN_ID) -> dict:
    return {"message": {"text": text, "from": {"id": uid, "first_name": "Anna"},
                        "chat": {"id": uid}}}


def _cb(data: str, uid: int = ADMIN_ID) -> dict:
    return {"callback_query": {"id": "cbid", "data": data, "from": {"id": uid},
                               "message": {"message_id": 555, "chat": {"id": uid}}}}


# ===== Утилиты =====

class TestUtils:
    def test_is_admin_true(self):
        assert mb.is_admin(ADMIN_ID) is True

    def test_is_admin_false(self):
        assert mb.is_admin(STRANGER_ID) is False

    def test_is_admin_bad_type(self):
        assert mb.is_admin(None) is False

    def test_norm_phone_digits(self):
        assert mb._norm_phone("+52 123 456-7890") == "wa_521234567890"

    def test_norm_phone_already_wa(self):
        assert mb._norm_phone("wa_521234567890") == "wa_521234567890"

    def test_norm_phone_no_digits_returns_none(self):
        assert mb._norm_phone("abc") is None

    def test_digits_strips_prefix(self):
        assert mb._digits("wa_521234567890") == "521234567890"

    def test_actor_username(self):
        assert mb._actor({"username": "anna", "id": 5}) == "@anna"

    def test_actor_first_name(self):
        assert "tg:5" in mb._actor({"first_name": "Anna", "id": 5})


# ===== Авторизация =====

class TestAuth:
    async def test_non_admin_message_denied(self, _patch_io, monkeypatch):
        list_mock = AsyncMock()
        monkeypatch.setattr(db, "list_active_leads", list_mock)
        await mb.handle_update(_msg("/leads", uid=STRANGER_ID))
        list_mock.assert_not_awaited()
        call = _patch_io["reply"].call_args
        reply = call.args[1].lower()
        assert "закрытый" in reply and "доступ" in reply
        # контакт для запроса доступа присутствует в тексте
        assert "@arinashrr" in call.args[1]
        # и кнопкой на t.me
        kb = call.args[2]
        urls = [b.get("url") for row in kb["inline_keyboard"] for b in row]
        assert "https://t.me/arinashrr" in urls

    async def test_non_admin_callback_denied(self, _patch_io, monkeypatch):
        set_mock = AsyncMock()
        monkeypatch.setattr(db, "set_manual", set_mock)
        await mb.handle_update(_cb("mb:takeover:wa_1", uid=STRANGER_ID))
        set_mock.assert_not_awaited()
        _patch_io["answer"].assert_awaited()


# ===== Команды =====

class TestCommands:
    async def test_help(self, _patch_io):
        await mb.handle_update(_msg("/help"))
        assert "/leads" in _patch_io["reply"].call_args.args[1]

    async def test_non_command_text_shows_help(self, _patch_io):
        await mb.handle_update(_msg("привет"))
        assert "/leads" in _patch_io["reply"].call_args.args[1]

    async def test_unknown_command(self, _patch_io):
        await mb.handle_update(_msg("/foobar"))
        assert "Не знаю такой команды" in _patch_io["reply"].call_args.args[1]

    async def test_leads_calls_db(self, _patch_io, monkeypatch):
        list_mock = AsyncMock(return_value=[])
        monkeypatch.setattr(db, "list_active_leads", list_mock)
        await mb.handle_update(_msg("/leads"))
        list_mock.assert_awaited_once_with(15, None)

    async def test_leads_with_valid_stage(self, _patch_io, monkeypatch):
        list_mock = AsyncMock(return_value=[])
        monkeypatch.setattr(db, "list_active_leads", list_mock)
        await mb.handle_update(_msg("/leads qualifying"))
        list_mock.assert_awaited_once_with(15, "qualifying")

    async def test_leads_invalid_stage_shows_all(self, _patch_io, monkeypatch):
        """Непонятный фильтр не ошибка — просто показываем всех (stage=None)."""
        list_mock = AsyncMock(return_value=[])
        monkeypatch.setattr(db, "list_active_leads", list_mock)
        await mb.handle_update(_msg("/leads nonsense"))
        list_mock.assert_awaited_once_with(15, None)

    async def test_lead_no_args(self, _patch_io):
        await mb.handle_update(_msg("/lead"))
        assert "Формат" in _patch_io["reply"].call_args.args[1]

    async def test_lead_bad_phone(self, _patch_io):
        await mb.handle_update(_msg("/lead abc"))
        assert "Не похоже" in _patch_io["reply"].call_args.args[1]

    async def test_lead_not_found(self, _patch_io, monkeypatch):
        monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value=None))
        await mb.handle_update(_msg("/lead wa_521234567890"))
        assert "не нашла" in _patch_io["reply"].call_args.args[1].lower()

    async def test_lead_found_sends_card_with_keyboard(self, _patch_io, monkeypatch):
        monkeypatch.setattr(db, "get_lead_by_phone",
                            AsyncMock(return_value={"phone": "wa_1", "funnel_stage": "new", "mode": "auto"}))
        monkeypatch.setattr(db, "get_conversation_history", AsyncMock(return_value=[]))
        monkeypatch.setattr(db, "is_whitelisted", AsyncMock(return_value=False))
        monkeypatch.setattr(db, "get_lead_photos", AsyncMock(return_value=[]))
        await mb.handle_update(_msg("/lead wa_1"))
        # reply_markup (3-й позиционный) — клавиатура карточки
        call = _patch_io["reply"].call_args
        assert call.args[2]["inline_keyboard"]

    async def test_takeover_found(self, _patch_io, monkeypatch):
        monkeypatch.setattr(db, "set_manual", AsyncMock(return_value=True))
        await mb.handle_update(_msg("/takeover wa_1"))
        reply = _patch_io["reply"].call_args.args[1]
        assert "бот молчит" in reply.lower()
        assert "сама" not in reply.lower()  # без личного обращения

    async def test_takeover_not_found(self, _patch_io, monkeypatch):
        monkeypatch.setattr(db, "set_manual", AsyncMock(return_value=False))
        await mb.handle_update(_msg("/takeover wa_1"))
        assert "не нашла" in _patch_io["reply"].call_args.args[1].lower()

    async def test_release_found(self, _patch_io, monkeypatch):
        monkeypatch.setattr(db, "set_auto", AsyncMock(return_value=True))
        await mb.handle_update(_msg("/release wa_1"))
        assert "снова отвечает" in _patch_io["reply"].call_args.args[1].lower()

    async def test_stop_no_args(self, _patch_io):
        await mb.handle_update(_msg("/stop"))
        assert "Формат" in _patch_io["reply"].call_args.args[1]

    async def test_stop_found_uses_reason(self, _patch_io, monkeypatch):
        monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value={"phone": "wa_1"}))
        block_mock = AsyncMock()
        monkeypatch.setattr(db, "block_lead", block_mock)
        await mb.handle_update(_msg("/stop wa_521234567890 спам и грубость"))
        block_mock.assert_awaited_once()
        assert block_mock.call_args.args[1] == "спам и грубость"
        # Терминология: Аня не видит слова «заблокир».
        reply = _patch_io["reply"].call_args.args[1]
        assert "больше не пишет" in reply.lower()
        assert "заблокир" not in reply.lower()

    async def test_stop_not_found(self, _patch_io, monkeypatch):
        monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value=None))
        block_mock = AsyncMock()
        monkeypatch.setattr(db, "block_lead", block_mock)
        await mb.handle_update(_msg("/stop wa_1"))
        block_mock.assert_not_awaited()

    async def test_client_add_missing_reason(self, _patch_io, monkeypatch):
        add_mock = AsyncMock()
        monkeypatch.setattr(db, "add_to_whitelist", add_mock)
        await mb.handle_update(_msg("/client_add wa_1"))
        add_mock.assert_not_awaited()
        assert "Формат" in _patch_io["reply"].call_args.args[1]

    async def test_client_add_ok_passes_actor(self, _patch_io, monkeypatch):
        add_mock = AsyncMock()
        monkeypatch.setattr(db, "add_to_whitelist", add_mock)
        await mb.handle_update(_msg("/client_add wa_521234567890 VIP клиент"))
        add_mock.assert_awaited_once()
        assert add_mock.call_args.args[1] == "VIP клиент"
        # added_by — actor (по first_name Anna)
        assert "Anna" in add_mock.call_args.args[2]
        # Аня видит «клиент», не «whitelist».
        reply = _patch_io["reply"].call_args.args[1].lower()
        assert "клиент" in reply and "whitelist" not in reply

    async def test_client_add_bad_phone(self, _patch_io, monkeypatch):
        monkeypatch.setattr(db, "add_to_whitelist", AsyncMock(side_effect=ValueError("bad")))
        await mb.handle_update(_msg("/client_add abc причина"))
        assert "Не похоже" in _patch_io["reply"].call_args.args[1]

    async def test_client_remove(self, _patch_io, monkeypatch):
        rm_mock = AsyncMock()
        monkeypatch.setattr(db, "remove_from_whitelist", rm_mock)
        await mb.handle_update(_msg("/client_remove wa_521234567890"))
        rm_mock.assert_awaited_once_with("wa_521234567890")

    async def test_clients_empty(self, _patch_io, monkeypatch):
        monkeypatch.setattr(db, "list_whitelist", AsyncMock(return_value=[]))
        await mb.handle_update(_msg("/clients"))
        assert "клиент" in _patch_io["reply"].call_args.args[1].lower()

    async def test_clients_rows(self, _patch_io, monkeypatch):
        monkeypatch.setattr(db, "list_whitelist", AsyncMock(return_value=[
            {"phone": "wa_521234567890", "reason": "VIP", "added_by": "@anna"}]))
        await mb.handle_update(_msg("/clients"))
        assert "521234567890" in _patch_io["reply"].call_args.args[1]

    async def test_command_exception_replies_gracefully(self, _patch_io, monkeypatch):
        """Падение хендлера → ловим, отвечаем по-человечески, не роняем вебхук."""
        monkeypatch.setattr(db, "list_active_leads", AsyncMock(side_effect=RuntimeError("db")))
        await mb.handle_update(_msg("/leads"))
        assert "не так" in _patch_io["reply"].call_args.args[1].lower()


# ===== Callback-кнопки =====

class TestCallbacks:
    async def test_bad_prefix(self, _patch_io):
        await mb.handle_update(_cb("xx:takeover:wa_1"))
        _patch_io["answer"].assert_awaited()

    async def test_unknown_action(self, _patch_io):
        await mb.handle_update(_cb("mb:frobnicate:wa_1"))
        assert "Неизвестное" in _patch_io["answer"].call_args.args[1]

    async def test_takeover(self, _patch_io, monkeypatch):
        set_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(db, "set_manual", set_mock)
        await mb.handle_update(_cb("mb:takeover:wa_521234567890"))
        set_mock.assert_awaited_once_with("wa_521234567890")
        _patch_io["answer"].assert_awaited()

    async def test_takeover_flips_button_to_release(self, _patch_io, monkeypatch):
        """После takeover кнопка на том же сообщении сразу → «Вернуть боту» (release)."""
        monkeypatch.setattr(db, "set_manual", AsyncMock(return_value=True))
        edit_mock = AsyncMock(); monkeypatch.setattr(mb, "_edit_reply_markup", edit_mock)
        await mb.handle_update(_cb("mb:takeover:wa_1"))
        edit_mock.assert_awaited_once()
        new_kb = edit_mock.call_args.args[2]
        actions = [b.get("callback_data", "") for row in new_kb["inline_keyboard"] for b in row]
        assert any("release" in a for a in actions)
        assert not any("takeover" in a for a in actions)

    async def test_takeover_not_found_no_edit(self, _patch_io, monkeypatch):
        """Лид не найден → кнопку не трогаем."""
        monkeypatch.setattr(db, "set_manual", AsyncMock(return_value=False))
        edit_mock = AsyncMock(); monkeypatch.setattr(mb, "_edit_reply_markup", edit_mock)
        await mb.handle_update(_cb("mb:takeover:wa_1"))
        edit_mock.assert_not_awaited()

    async def test_release(self, _patch_io, monkeypatch):
        monkeypatch.setattr(db, "set_auto", AsyncMock(return_value=True))
        await mb.handle_update(_cb("mb:release:wa_1"))
        _patch_io["reply"].assert_awaited()

    async def test_release_flips_button_to_takeover(self, _patch_io, monkeypatch):
        """После release кнопка на том же сообщении → «Общаться лично» (takeover)."""
        monkeypatch.setattr(db, "set_auto", AsyncMock(return_value=True))
        edit_mock = AsyncMock(); monkeypatch.setattr(mb, "_edit_reply_markup", edit_mock)
        await mb.handle_update(_cb("mb:release:wa_1"))
        edit_mock.assert_awaited_once()
        new_kb = edit_mock.call_args.args[2]
        actions = [b.get("callback_data", "") for row in new_kb["inline_keyboard"] for b in row]
        assert any("takeover" in a for a in actions)

    async def test_block_found(self, _patch_io, monkeypatch):
        monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value={"phone": "wa_1"}))
        block_mock = AsyncMock()
        monkeypatch.setattr(db, "block_lead", block_mock)
        await mb.handle_update(_cb("mb:block:wa_1"))
        block_mock.assert_awaited_once()
        # Терминология: нейтрально, без слова «заблокир» (тост + ответ).
        assert "заблокир" not in _patch_io["reply"].call_args.args[1].lower()
        assert "заблокир" not in _patch_io["answer"].call_args.args[1].lower()

    async def test_block_not_found(self, _patch_io, monkeypatch):
        monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value=None))
        block_mock = AsyncMock()
        monkeypatch.setattr(db, "block_lead", block_mock)
        await mb.handle_update(_cb("mb:block:wa_1"))
        block_mock.assert_not_awaited()

    async def test_card(self, _patch_io, monkeypatch):
        monkeypatch.setattr(db, "get_lead_by_phone",
                            AsyncMock(return_value={"phone": "wa_1", "mode": "manual", "funnel_stage": "new"}))
        monkeypatch.setattr(db, "get_conversation_history", AsyncMock(return_value=[]))
        monkeypatch.setattr(db, "is_whitelisted", AsyncMock(return_value=False))
        monkeypatch.setattr(db, "get_lead_photos", AsyncMock(return_value=[]))
        await mb.handle_update(_cb("mb:card:wa_1"))
        assert _patch_io["reply"].call_args.args[2]["inline_keyboard"]

    async def test_card_with_photos_sends_them(self, _patch_io, monkeypatch):
        """Есть фото → карточка ПОДПИСЬЮ к первому фото (текст+кнопки там), остальные следом."""
        monkeypatch.setattr(db, "get_lead_by_phone",
                            AsyncMock(return_value={"phone": "wa_1", "mode": "auto", "funnel_stage": "new"}))
        monkeypatch.setattr(db, "get_conversation_history", AsyncMock(return_value=[]))
        monkeypatch.setattr(db, "is_whitelisted", AsyncMock(return_value=False))
        monkeypatch.setattr(db, "get_lead_photos", AsyncMock(return_value=[
            {"storage_url": "https://x/1.jpg"}, {"storage_url": "https://x/2.jpg"}]))
        photo_mock = AsyncMock(); monkeypatch.setattr(mb, "_send_photo", photo_mock)
        await mb.handle_update(_cb("mb:card:wa_1"))
        assert photo_mock.await_count == 2
        # первое фото — обложка карточки: несёт подпись (текст) и кнопки
        first = photo_mock.call_args_list[0]
        assert first.args[1] == "https://x/1.jpg"
        assert first.kwargs.get("caption") and first.kwargs.get("reply_markup")
        # карточка НЕ дублируется отдельным текстом
        _patch_io["reply"].assert_not_awaited()

    async def test_photo_ok_full_path(self, _patch_io, monkeypatch):
        """photo_ok = путь ok: set_auto + mark_photo_received + qualified + _run_ai (стадия сменилась)."""
        monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value={"phone": "wa_1"}))
        set_auto = AsyncMock(); monkeypatch.setattr(db, "set_auto", set_auto)
        mark = AsyncMock(); monkeypatch.setattr(db, "mark_photo_received", mark)
        stage = AsyncMock(return_value=True); monkeypatch.setattr(db, "set_funnel_stage", stage)
        run_ai = AsyncMock(); monkeypatch.setattr(main, "_run_ai", run_ai)
        await mb.handle_update(_cb("mb:photo_ok:wa_1"))
        set_auto.assert_awaited_once_with("wa_1")
        mark.assert_awaited_once_with("wa_1", True)
        assert stage.call_args.args[1] == "qualified"
        run_ai.assert_awaited_once()

    async def test_photo_ok_blocked_lead_aborts(self, _patch_io, monkeypatch):
        """do_not_contact=True → одобрение отменяется, бот НЕ пишет заблокированному."""
        monkeypatch.setattr(db, "get_lead_by_phone",
                            AsyncMock(return_value={"phone": "wa_1", "do_not_contact": True}))
        set_auto = AsyncMock(); monkeypatch.setattr(db, "set_auto", set_auto)
        run_ai = AsyncMock(); monkeypatch.setattr(main, "_run_ai", run_ai)
        await mb.handle_update(_cb("mb:photo_ok:wa_1"))
        set_auto.assert_not_awaited()
        run_ai.assert_not_awaited()
        assert "не пишет" in _patch_io["reply"].call_args.args[1].lower()

    async def test_photo_ok_not_found_aborts(self, _patch_io, monkeypatch):
        monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value=None))
        set_auto = AsyncMock(); monkeypatch.setattr(db, "set_auto", set_auto)
        run_ai = AsyncMock(); monkeypatch.setattr(main, "_run_ai", run_ai)
        await mb.handle_update(_cb("mb:photo_ok:wa_1"))
        set_auto.assert_not_awaited()
        run_ai.assert_not_awaited()

    async def test_photo_ok_idempotent_no_double_pitch(self, _patch_io, monkeypatch):
        """Стадия уже 'qualified' (set_funnel_stage=False) → _run_ai НЕ вызывается повторно."""
        monkeypatch.setattr(db, "get_lead_by_phone", AsyncMock(return_value={"phone": "wa_1"}))
        monkeypatch.setattr(db, "set_auto", AsyncMock())
        monkeypatch.setattr(db, "mark_photo_received", AsyncMock())
        monkeypatch.setattr(db, "set_funnel_stage", AsyncMock(return_value=False))
        run_ai = AsyncMock(); monkeypatch.setattr(main, "_run_ai", run_ai)
        await mb.handle_update(_cb("mb:photo_ok:wa_1"))
        run_ai.assert_not_awaited()

    async def test_callback_missing_chat_id_answers_and_stops(self, _patch_io, monkeypatch):
        """message=None (удалённое сообщение) → answer + ранний выход, без действий."""
        set_mock = AsyncMock(); monkeypatch.setattr(db, "set_manual", set_mock)
        cq = {"callback_query": {"id": "cbid", "data": "mb:takeover:wa_1",
                                 "from": {"id": ADMIN_ID}, "message": None}}
        await mb.handle_update(cq)
        set_mock.assert_not_awaited()
        _patch_io["answer"].assert_awaited()

    async def test_photo_retry(self, _patch_io, monkeypatch):
        monkeypatch.setattr(db, "set_auto", AsyncMock())
        send_scen = AsyncMock(); monkeypatch.setattr(main, "_send_scenario", send_scen)
        await mb.handle_update(_cb("mb:photo_retry:wa_1"))
        send_scen.assert_awaited_once_with("wa_1", 5)

    async def test_photo_reject_blocks_and_goodbye(self, _patch_io, monkeypatch):
        monkeypatch.setattr(db, "get_scenario_title", AsyncMock(return_value="Прощание"))
        block_mock = AsyncMock(); monkeypatch.setattr(db, "block_lead", block_mock)
        send_scen = AsyncMock(); monkeypatch.setattr(main, "_send_scenario", send_scen)
        await mb.handle_update(_cb("mb:photo_reject:wa_1"))
        block_mock.assert_awaited_once()
        send_scen.assert_awaited_once_with("wa_1", 12)

    async def test_callback_exception_answers(self, _patch_io, monkeypatch):
        monkeypatch.setattr(db, "set_manual", AsyncMock(side_effect=RuntimeError("db")))
        await mb.handle_update(_cb("mb:takeover:wa_1"))
        # ошибка поймана, answer вызван (спиннер погашен)
        _patch_io["answer"].assert_awaited()


# ===== Форматтеры =====

class TestFormatters:
    def test_leads_list_empty(self):
        assert "никого" in mb.format_leads_list([], None).lower()

    def test_leads_list_with_stage_header(self):
        out = mb.format_leads_list([], "qualifying")
        assert "Первичное общение" in out

    def test_leads_list_rows(self):
        out = mb.format_leads_list(
            [{"whatsapp_name": "Juan", "phone": "wa_521234567890",
              "funnel_stage": "new", "mode": "manual"}], None)
        assert "Juan" in out and "521234567890" in out and "вручную" in out

    def test_lead_card_fields_and_kb(self):
        lead = {"phone": "wa_1", "whatsapp_name": "Juan", "funnel_stage": "qualified",
                "mode": "auto", "age": 40, "is_single": True, "city": "CDMX"}
        text, kb = mb.format_lead_card(lead, [], whitelisted=True)
        assert "Juan" in text and "40" in text and "CDMX" in text
        # Клиент из списка → единая формулировка «Переписка ведётся вручную», без личного обращения.
        assert "переписка ведётся вручную" in text.lower()
        assert "whitelist" not in text.lower()
        assert "твой" not in text.lower() and "сама" not in text.lower()
        assert kb["inline_keyboard"]

    def test_lead_card_auto_shows_bot_answers(self):
        lead = {"phone": "wa_1", "whatsapp_name": "Juan", "funnel_stage": "new", "mode": "auto"}
        text, _ = mb.format_lead_card(lead, [], whitelisted=False)
        assert "Отвечает бот" in text

    def test_lead_card_manual_shows_manual_state(self):
        lead = {"phone": "wa_1", "mode": "manual", "funnel_stage": "new"}
        text, _ = mb.format_lead_card(lead, [], whitelisted=False)
        assert "Переписка ведётся вручную" in text
        assert "ты" not in text.lower()

    def test_lead_card_history_arrows(self):
        lead = {"phone": "wa_1", "mode": "auto", "funnel_stage": "new"}
        hist = [{"direction": "inbound", "sender": "lead", "text": "hola"},
                {"direction": "outbound", "sender": "anna", "text": "buenos dias"},
                {"direction": "outbound", "sender": "manager", "text": "te llamo"}]
        text, _ = mb.format_lead_card(lead, hist, whitelisted=False)
        assert "Клиент: hola" in text
        assert "Бот: buenos dias" in text
        assert "Оператор: te llamo" in text

    def test_lead_card_manual_shows_release_button(self):
        """mode=manual → кнопка 'Вернуть боту' (release), не 'Взять себе'."""
        lead = {"phone": "wa_1", "mode": "manual", "funnel_stage": "new"}
        _, kb = mb.format_lead_card(lead, [], whitelisted=False)
        actions = [b.get("callback_data", "") for row in kb["inline_keyboard"] for b in row]
        assert any("release" in a for a in actions)


# ===== config.manager_admin_ids =====

class TestAdminIdsConfig:
    def test_explicit_csv_parsed(self, monkeypatch):
        monkeypatch.setattr(mb.settings, "tg_manager_admin_ids", "100, 200 ,300")
        assert mb.settings.manager_admin_ids == frozenset({100, 200, 300})

    def test_empty_falls_back_to_chat_ids(self, monkeypatch):
        monkeypatch.setattr(mb.settings, "tg_manager_admin_ids", "")
        monkeypatch.setattr(mb.settings, "tg_manager_chat_id", "555")
        monkeypatch.setattr(mb.settings, "tg_alerts_chat_id", "666")
        assert mb.settings.manager_admin_ids == frozenset({555, 666})

    def test_non_numeric_ignored(self, monkeypatch):
        monkeypatch.setattr(mb.settings, "tg_manager_admin_ids", "100,abc,200")
        assert mb.settings.manager_admin_ids == frozenset({100, 200})
