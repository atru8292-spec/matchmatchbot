-- Блок 13 (доп.): напоминание о видеозвонке за ~2 часа (сценарий 49).
-- Только ДОБАВЛЯЕМ колонки в leads. Существующие строки не трогаем.
--
-- videocall_at          — назначенное время звонка (timestamptz, UTC). NULL = не назначен.
-- videocall_reminded_at — когда бот отправил напоминание за 2ч (идемпотентность).
--                         При переносе звонка сбрасывается в NULL (см. db.set_videocall_at),
--                         чтобы напоминание ушло заново на новое время.
ALTER TABLE leads ADD COLUMN IF NOT EXISTS videocall_at timestamptz;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS videocall_reminded_at timestamptz;

-- Частичный индекс: планировщик ищет только незанапоминавшиеся назначенные звонки.
CREATE INDEX IF NOT EXISTS idx_leads_videocall_due
  ON leads (videocall_at)
  WHERE videocall_at IS NOT NULL AND videocall_reminded_at IS NULL;
