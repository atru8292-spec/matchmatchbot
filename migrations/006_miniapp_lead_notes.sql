-- Мини-CRM (карточка лида): внутренние заметки менеджеров.
-- Только ДОБАВЛЯЕМ таблицу — leads/messages и прочую схему не трогаем.
-- Заметка = активность таймлайна, поэтому у каждой свой created_at (встаёт в ленту
-- по времени). Без автора — по договорённости (упрощаем), только текст + дата.

CREATE TABLE IF NOT EXISTS lead_notes (
  id         bigserial PRIMARY KEY,
  lead_phone text NOT NULL REFERENCES leads(phone) ON DELETE CASCADE,
  text       text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_lead_notes_phone_created
  ON lead_notes (lead_phone, created_at);
