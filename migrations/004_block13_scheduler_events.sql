-- Блок 13: планировщик (фоллоу-апы + напоминания об ивенте), настройки ивента.
-- Только ДОБАВЛЯЕМ. leads/messages/scenarios существующие строки не трогаем.

-- KV-настройки приложения: дата/время/адрес ивента, URL картинки-приглашения и флаги.
-- Обновляет Аня из менеджер-бота (/set_event, /set_invitation, ...). Ключи:
--   event_date (YYYY-MM-DD), event_time (текст, напр. "20:30"), event_address,
--   event_active ("1"/"0"), invitation_url, invitation_ready ("1"/"0").
CREATE TABLE IF NOT EXISTS app_settings (
  key        text PRIMARY KEY,
  value      text,
  updated_at timestamptz DEFAULT now()
);

-- Системный сценарий 50 — напоминание за день до ивента (T-1).
-- trigger_type='scheduled' + embedding NULL → в RAG для входящих НЕ попадает;
-- планировщик берёт его по id (get_scenario_template(50)). [dirección]/время
-- планировщик подставляет из app_settings в рантайме.
-- ВНИМАНИЕ: при полном reseed сценариев из JSON (scripts.load_scenarios делает
-- DELETE FROM scenarios) эту строку сотрёт — после reseed повторно применить эту миграцию.
INSERT INTO scenarios (id, title, trigger_description, trigger_es, template_es,
                       mode, blocks_lead, ai_allowed, is_active, trigger_type)
VALUES (
  50,
  'День до ивента: напоминание',
  'Вечер накануне ивента, бот напоминает записанным',
  'recordatorio evento manana',
  E'Hola guapo! 🤍 te recuerdo que mañana es el evento ✨ Te espero a las [hora] en [dirección]. Cualquier duda me avisas 🤍',
  'bot_auto', false, false, true, 'scheduled'
)
ON CONFLICT (id) DO UPDATE SET
  title = EXCLUDED.title,
  template_es = EXCLUDED.template_es,
  trigger_type = 'scheduled';
