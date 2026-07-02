-- Блок 5: whitelist + журнал переходов воронки.
-- leads/messages НЕ трогаем, только ДОБАВЛЯЕМ таблицы. Ссылки на leads по phone.

CREATE TABLE IF NOT EXISTS bot_whitelist (
  phone      text PRIMARY KEY,
  reason     text,
  added_by   text,
  added_at   timestamptz DEFAULT now(),
  note       text
);

CREATE TABLE IF NOT EXISTS funnel_events (
  id         bigserial PRIMARY KEY,
  lead_phone text NOT NULL REFERENCES leads(phone) ON DELETE CASCADE,
  from_stage text,
  to_stage   text NOT NULL,
  changed_at timestamptz DEFAULT now(),
  meta       jsonb DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_funnel_events_lead ON funnel_events(lead_phone, changed_at);
