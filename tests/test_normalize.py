"""Тесты normalize_wazzup_message — Блок 3.

Чистые тесты без моков: функция не трогает БД/сеть.
"""
from __future__ import annotations

import pytest
from normalize import normalize_wazzup_message, NormalizedMessage


# ---------------------------------------------------------------------------
# Вспомогательный базовый payload — минимально валидное входящее сообщение
# ---------------------------------------------------------------------------

def _base_text(overrides: dict | None = None) -> dict:
    """Валидный текстовый payload. Переопределяем нужные поля через overrides."""
    msg = {
        "messageId": "MSG001",
        "chatId": "79991234567",
        "chatType": "whatsapp",
        "status": "inbound",
        "type": "text",
        "text": "Hola, quiero más info",
        "dateTime": "2024-01-15T10:30:00Z",
        "contact": {"name": "Juan Carlos", "phone": "79991234567"},
    }
    if overrides:
        msg.update(overrides)
    return msg


def _base_media(media_type: str, overrides: dict | None = None) -> dict:
    """Валидный медиа-payload заданного типа."""
    msg = {
        "messageId": "MSG002",
        "chatId": "521555123456",
        "chatType": "whatsapp",
        "status": "inbound",
        "type": media_type,
        "contentUri": "https://cdn.wazzup.online/media/abc123",
        "dateTime": "2024-01-15T11:00:00Z",
        "contact": {"name": "Pedro Lomas", "phone": "521555123456"},
    }
    if overrides:
        msg.update(overrides)
    return msg


# ===========================================================================
# 1. Типы контента
# ===========================================================================

class TestContentTypes:
    def test_text_content_type(self):
        """type='text' → content_type='text', user_text = исходный текст."""
        result = normalize_wazzup_message(_base_text())
        assert isinstance(result, NormalizedMessage)
        assert result.content_type == "text"
        assert result.user_text == "Hola, quiero más info"
        assert result.media_info is None

    def test_image_maps_to_photo(self):
        """type='image' → content_type='photo', user_text='[photo received]'."""
        result = normalize_wazzup_message(_base_media("image"))
        assert result is not None
        assert result.content_type == "photo"
        assert result.user_text == "[photo received]"

    def test_audio_maps_to_voice(self):
        """type='audio' → content_type='voice', user_text='[voice message]'."""
        result = normalize_wazzup_message(_base_media("audio"))
        assert result is not None
        assert result.content_type == "voice"
        assert result.user_text == "[voice message]"

    def test_video_maps_to_video(self):
        """type='video' → content_type='video', user_text='[video]'."""
        result = normalize_wazzup_message(_base_media("video"))
        assert result is not None
        assert result.content_type == "video"
        assert result.user_text == "[video]"

    def test_document_maps_to_document(self):
        """type='document' → content_type='document', user_text='[document]'."""
        result = normalize_wazzup_message(_base_media("document"))
        assert result is not None
        assert result.content_type == "document"
        assert result.user_text == "[document]"

    def test_media_info_contains_uri_and_message_id(self):
        """Для медиа: media_info содержит content_uri и message_id."""
        result = normalize_wazzup_message(_base_media("image"))
        assert result is not None
        assert result.media_info == {
            "content_uri": "https://cdn.wazzup.online/media/abc123",
            "message_id": "MSG002",
        }

    def test_text_media_info_is_none(self):
        """Для text: media_info всегда None."""
        result = normalize_wazzup_message(_base_text())
        assert result is not None
        assert result.media_info is None


# ===========================================================================
# 2. Дропы — функция возвращает None
# ===========================================================================

class TestDropConditions:
    def test_drop_is_echo_true(self):
        """isEcho=True → None (наше исходящее, не чужое)."""
        result = normalize_wazzup_message(_base_text({"isEcho": True}))
        assert result is None

    def test_drop_is_echo_false_passes(self):
        """isEcho=False → не дропается."""
        result = normalize_wazzup_message(_base_text({"isEcho": False}))
        assert result is not None

    def test_drop_status_delivered(self):
        """status='delivered' → None."""
        result = normalize_wazzup_message(_base_text({"status": "delivered"}))
        assert result is None

    def test_drop_status_sent(self):
        """status='sent' → None."""
        result = normalize_wazzup_message(_base_text({"status": "sent"}))
        assert result is None

    def test_drop_status_read(self):
        """status='read' → None."""
        result = normalize_wazzup_message(_base_text({"status": "read"}))
        assert result is None

    def test_drop_status_absent(self):
        """Нет поля status вообще → None (None != 'inbound')."""
        msg = _base_text()
        del msg["status"]
        result = normalize_wazzup_message(msg)
        assert result is None

    def test_drop_chattype_telegram(self):
        """chatType='telegram' → None."""
        result = normalize_wazzup_message(_base_text({"chatType": "telegram"}))
        assert result is None

    def test_drop_chattype_instagram(self):
        """chatType='instagram' → None."""
        result = normalize_wazzup_message(_base_text({"chatType": "instagram"}))
        assert result is None

    def test_drop_chattype_absent(self):
        """Нет поля chatType → None (None != 'whatsapp')."""
        msg = _base_text()
        del msg["chatType"]
        result = normalize_wazzup_message(msg)
        assert result is None

    def test_drop_text_empty_string(self):
        """type='text', text='' → None."""
        result = normalize_wazzup_message(_base_text({"text": ""}))
        assert result is None

    def test_drop_text_whitespace_only(self):
        """type='text', text='   ' (пробелы) → None."""
        result = normalize_wazzup_message(_base_text({"text": "   "}))
        assert result is None

    def test_drop_text_tabs_and_newlines(self):
        """type='text', text='\\t\\n' → None."""
        result = normalize_wazzup_message(_base_text({"text": "\t\n"}))
        assert result is None

    def test_drop_unknown_type_sticker(self):
        """type='sticker' не в маппинге → None."""
        result = normalize_wazzup_message(_base_text({"type": "sticker"}))
        assert result is None

    def test_drop_unknown_type_location(self):
        """type='location' не в маппинге → None."""
        result = normalize_wazzup_message(_base_text({"type": "location"}))
        assert result is None

    def test_drop_unknown_type_reaction(self):
        """type='reaction' → None."""
        result = normalize_wazzup_message(_base_text({"type": "reaction"}))
        assert result is None

    def test_drop_empty_dict(self):
        """Пустой dict {} → None (нет status=inbound)."""
        result = normalize_wazzup_message({})
        assert result is None

    def test_drop_not_dict_string(self):
        """Строка вместо dict → None, не падает."""
        result = normalize_wazzup_message("hello")  # type: ignore[arg-type]
        assert result is None

    def test_drop_not_dict_none(self):
        """None вместо dict → None, не падает."""
        result = normalize_wazzup_message(None)  # type: ignore[arg-type]
        assert result is None

    def test_drop_not_dict_list(self):
        """Список вместо dict → None, не падает."""
        result = normalize_wazzup_message([1, 2, 3])  # type: ignore[arg-type]
        assert result is None

    def test_drop_not_dict_int(self):
        """Число вместо dict → None, не падает."""
        result = normalize_wazzup_message(42)  # type: ignore[arg-type]
        assert result is None

    def test_drop_empty_chatid_and_empty_contact_phone(self):
        """chatId='' и contact.phone='' → None (нет номера)."""
        result = normalize_wazzup_message(_base_text({
            "chatId": "",
            "contact": {"name": "Test", "phone": ""},
        }))
        assert result is None

    def test_drop_empty_chatid_and_no_contact(self):
        """chatId='' и нет contact → None."""
        msg = _base_text({"chatId": ""})
        del msg["contact"]
        result = normalize_wazzup_message(msg)
        assert result is None

    def test_drop_empty_chatid_and_contact_without_phone_key(self):
        """chatId='' и contact без ключа phone → None."""
        result = normalize_wazzup_message(_base_text({
            "chatId": "",
            "contact": {"name": "Ghost"},
        }))
        assert result is None


# ===========================================================================
# 3. Номер телефона — критично, НЕ искажать
# ===========================================================================

class TestPhoneNormalization:
    def test_ru_number_preserved(self):
        """RU: chatId='79991234567' → phone='wa_79991234567', chat_id='79991234567'."""
        result = normalize_wazzup_message(_base_text({"chatId": "79991234567"}))
        assert result is not None
        assert result.phone == "wa_79991234567"
        assert result.chat_id == "79991234567"

    def test_mx_number_preserved_no_extra_digit_removed(self):
        """MX: chatId='521555123456' → phone='wa_521555123456' (единицу НЕ трогаем)."""
        result = normalize_wazzup_message(_base_media("image", {"chatId": "521555123456"}))
        assert result is not None
        assert result.phone == "wa_521555123456"
        assert result.chat_id == "521555123456"

    def test_dirty_format_plus_spaces_parens_dash(self):
        """chatId='+52 1 (555) 123-4567' → только цифры, ничего не добавлено."""
        result = normalize_wazzup_message(_base_text({"chatId": "+52 1 (555) 123-4567"}))
        assert result is not None
        assert result.phone == "wa_5215551234567"
        assert result.chat_id == "5215551234567"

    def test_no_country_code_manipulation(self):
        """52 + цифры передаются как есть, '1' после 52 не убирается и не добавляется."""
        # chatId уже содержит '521...' — функция не должна удалять '1'
        result = normalize_wazzup_message(_base_text({"chatId": "5215559998877"}))
        assert result is not None
        assert result.chat_id == "5215559998877"
        assert "521" in result.phone

    def test_leading_zeros_preserved(self):
        """Ведущие нули в chatId сохраняются (оставляем только цифры)."""
        result = normalize_wazzup_message(_base_text({"chatId": "00441234567890"}))
        assert result is not None
        assert result.chat_id == "00441234567890"
        assert result.phone == "wa_00441234567890"

    def test_phone_is_wa_prefix_plus_digits(self):
        """phone всегда начинается с 'wa_' + только цифры."""
        result = normalize_wazzup_message(_base_text({"chatId": "13015550123"}))
        assert result is not None
        assert result.phone.startswith("wa_")
        assert result.phone == "wa_13015550123"

    def test_chat_id_contains_no_wa_prefix(self):
        """chat_id — только цифры, без 'wa_'."""
        result = normalize_wazzup_message(_base_text())
        assert result is not None
        assert not result.chat_id.startswith("wa_")
        assert result.chat_id.isdigit()


# ===========================================================================
# 4. Фолбэк номера: chatId пуст → берём contact.phone
# ===========================================================================

class TestPhoneFallback:
    def test_fallback_to_contact_phone_when_chatid_empty(self):
        """chatId='' → берём contact.phone='521777888999'."""
        result = normalize_wazzup_message(_base_text({
            "chatId": "",
            "contact": {"name": "Pedro", "phone": "521777888999"},
        }))
        assert result is not None
        assert result.phone == "wa_521777888999"
        assert result.chat_id == "521777888999"

    def test_fallback_strips_non_digits_from_contact_phone(self):
        """contact.phone с пробелами/дефисами — оставляем только цифры."""
        result = normalize_wazzup_message(_base_text({
            "chatId": "",
            "contact": {"name": "Pedro", "phone": "+52 1 777 888-999"},
        }))
        assert result is not None
        assert result.chat_id == "521777888999"

    def test_chatid_takes_priority_over_contact_phone(self):
        """Если chatId есть — contact.phone игнорируется."""
        result = normalize_wazzup_message(_base_text({
            "chatId": "111222333",
            "contact": {"name": "Pedro", "phone": "999888777"},
        }))
        assert result is not None
        assert result.chat_id == "111222333"


# ===========================================================================
# 5. Имя контакта
# ===========================================================================

class TestUserName:
    def test_name_title_case(self):
        """contact.name='juan carlos' → user_name='Juan Carlos'."""
        result = normalize_wazzup_message(_base_text({"contact": {"name": "juan carlos"}}))
        assert result is not None
        assert result.user_name == "Juan Carlos"

    def test_name_already_title_case(self):
        """contact.name уже в Title Case — остаётся таким же."""
        result = normalize_wazzup_message(_base_text({"contact": {"name": "Juan Carlos"}}))
        assert result is not None
        assert result.user_name == "Juan Carlos"

    def test_name_all_caps(self):
        """contact.name='JUAN' → .title() → 'Juan'."""
        result = normalize_wazzup_message(_base_text({"contact": {"name": "JUAN"}}))
        assert result is not None
        assert result.user_name == "Juan"

    def test_name_empty_string_fallback(self):
        """contact.name='' → 'WA Lead'."""
        result = normalize_wazzup_message(_base_text({"contact": {"name": ""}}))
        assert result is not None
        assert result.user_name == "WA Lead"

    def test_name_whitespace_only_fallback(self):
        """contact.name='   ' (пробелы) → 'WA Lead'."""
        result = normalize_wazzup_message(_base_text({"contact": {"name": "   "}}))
        assert result is not None
        assert result.user_name == "WA Lead"

    def test_no_name_key_in_contact_fallback(self):
        """contact без ключа name → 'WA Lead'."""
        result = normalize_wazzup_message(_base_text({"contact": {"phone": "79991234567"}}))
        assert result is not None
        assert result.user_name == "WA Lead"

    def test_no_contact_key_fallback(self):
        """Нет поля contact вообще → 'WA Lead'."""
        msg = _base_text()
        del msg["contact"]
        result = normalize_wazzup_message(msg)
        assert result is not None
        assert result.user_name == "WA Lead"

    def test_contact_is_none_fallback(self):
        """contact=None → 'WA Lead', не падает."""
        result = normalize_wazzup_message(_base_text({"contact": None}))
        assert result is not None
        assert result.user_name == "WA Lead"


# ===========================================================================
# 6. external_message_id и received_at
# ===========================================================================

class TestExternalMessageId:
    def test_with_message_id(self):
        """messageId='ABC' → external_message_id='wa_ABC'."""
        result = normalize_wazzup_message(_base_text({"messageId": "ABC"}))
        assert result is not None
        assert result.external_message_id == "wa_ABC"

    def test_without_message_id_uses_digits_and_datetime(self):
        """Нет messageId → 'wa_' + digits + '_' + dateTime."""
        msg = _base_text({"dateTime": "2024-01-15T10:30:00Z"})
        del msg["messageId"]
        result = normalize_wazzup_message(msg)
        assert result is not None
        expected = "wa_" + result.chat_id + "_2024-01-15T10:30:00Z"
        assert result.external_message_id == expected

    def test_without_message_id_and_datetime(self):
        """Нет messageId и dateTime → 'wa_' + digits + '_' (пустой суффикс).
        Код: str(date_time or '') → str('') → '', поэтому суффикс пустой.
        """
        msg = _base_text()
        del msg["messageId"]
        del msg["dateTime"]
        result = normalize_wazzup_message(msg)
        assert result is not None
        # date_time=None → None or '' → '' → суффикс после '_' пустой
        expected = "wa_" + result.chat_id + "_"
        assert result.external_message_id == expected

    def test_received_at_equals_datetime(self):
        """received_at = значение dateTime из payload."""
        result = normalize_wazzup_message(_base_text({"dateTime": "2024-06-01T09:00:00Z"}))
        assert result is not None
        assert result.received_at == "2024-06-01T09:00:00Z"

    def test_received_at_none_when_no_datetime(self):
        """Нет dateTime → received_at=None."""
        msg = _base_text()
        del msg["dateTime"]
        result = normalize_wazzup_message(msg)
        assert result is not None
        assert result.received_at is None


# ===========================================================================
# 7. channel и прочие инварианты
# ===========================================================================

class TestInvariants:
    def test_channel_always_whatsapp(self):
        """channel всегда 'whatsapp' для любого типа контента."""
        for media_type in ("image", "audio", "video", "document"):
            result = normalize_wazzup_message(_base_media(media_type))
            assert result is not None
            assert result.channel == "whatsapp", f"Упало для type={media_type}"
        result = normalize_wazzup_message(_base_text())
        assert result is not None
        assert result.channel == "whatsapp"

    def test_result_is_frozen_dataclass(self):
        """NormalizedMessage — frozen dataclass, нельзя изменить поле."""
        result = normalize_wazzup_message(_base_text())
        assert result is not None
        with pytest.raises((AttributeError, TypeError)):
            result.phone = "wa_000"  # type: ignore[misc]

    def test_full_happy_path_text(self):
        """Полный happy-path для text: все поля корректны."""
        msg = {
            "messageId": "XYZ789",
            "chatId": "521555999888",
            "chatType": "whatsapp",
            "status": "inbound",
            "type": "text",
            "text": "Buenas tardes!",
            "dateTime": "2024-03-10T15:45:00Z",
            "contact": {"name": "Carlos Mendez", "phone": "521555999888"},
        }
        result = normalize_wazzup_message(msg)
        assert result is not None
        assert result.phone == "wa_521555999888"
        assert result.chat_id == "521555999888"
        assert result.channel == "whatsapp"
        assert result.content_type == "text"
        assert result.user_text == "Buenas tardes!"
        assert result.user_name == "Carlos Mendez"
        assert result.external_message_id == "wa_XYZ789"
        assert result.received_at == "2024-03-10T15:45:00Z"
        assert result.media_info is None

    def test_full_happy_path_image(self):
        """Полный happy-path для image: media_info заполнен, user_text — placeholder."""
        msg = {
            "messageId": "IMG001",
            "chatId": "521555111222",
            "chatType": "whatsapp",
            "status": "inbound",
            "type": "image",
            "contentUri": "https://cdn.example.com/img.jpg",
            "dateTime": "2024-03-10T16:00:00Z",
            "contact": {"name": "Ana Lopez", "phone": "521555111222"},
        }
        result = normalize_wazzup_message(msg)
        assert result is not None
        assert result.content_type == "photo"
        assert result.user_text == "[photo received]"
        assert result.media_info == {
            "content_uri": "https://cdn.example.com/img.jpg",
            "message_id": "IMG001",
        }
        assert result.channel == "whatsapp"
        assert result.user_name == "Ana Lopez"


# ---------------------------------------------------------------------------
# Регресс: устойчивость к кривому payload (ревью блока 3)
# ---------------------------------------------------------------------------

class TestMalformedPayloadRobustness:
    """Кривые типы полей не должны ронять функцию (её зовут в цикле по messages)."""

    def test_contact_as_string_does_not_crash(self):
        # contact пришёл строкой вместо dict — раньше падало AttributeError
        result = normalize_wazzup_message(_base_text({"contact": "not-a-dict"}))
        assert result is not None
        assert result.phone == "wa_79991234567"
        assert result.user_name == "WA Lead"  # имя недоступно → дефолт

    def test_contact_as_list_does_not_crash(self):
        result = normalize_wazzup_message(_base_text({"contact": ["x"]}))
        assert result is not None
        assert result.user_name == "WA Lead"

    def test_non_string_text_is_dropped_not_crash(self):
        # text числом — не строка → трактуем как пустой → дроп, без исключения
        assert normalize_wazzup_message(_base_text({"text": 12345})) is None

    def test_non_string_name_falls_back_not_crash(self):
        result = normalize_wazzup_message(
            _base_text({"contact": {"name": 999, "phone": "79991234567"}})
        )
        assert result is not None
        assert result.user_name == "WA Lead"

    def test_media_without_content_uri_keeps_message_with_none(self):
        # медиа без contentUri: сообщение проходит, content_uri=None (+ warning в лог)
        msg = _base_text({"type": "image", "text": None})
        msg.pop("contentUri", None)
        result = normalize_wazzup_message(msg)
        assert result is not None
        assert result.content_type == "photo"
        assert result.media_info["content_uri"] is None
