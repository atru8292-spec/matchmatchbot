-- Блок 6.5: тип триггера сценария. 'reply' — ответ на входящее (RAG),
-- 'scheduled' — исходящий по таймеру (утро после ивента, напоминания, фоллоу-апы).
-- RAG для входящих исключает 'scheduled'; планировщик берёт их отдельно.
ALTER TABLE scenarios ADD COLUMN IF NOT EXISTS trigger_type text DEFAULT 'reply';
UPDATE scenarios SET trigger_type='scheduled' WHERE id IN (23,32,33,36,47,49);
