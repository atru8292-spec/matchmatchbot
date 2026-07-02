-- Блок 6: дополняем scenarios под наш формат 49 сценариев + RAG-эмбеддинги.
-- Требует расширение pgvector (уже установлено в проде).
ALTER TABLE scenarios ADD COLUMN IF NOT EXISTS title text;
ALTER TABLE scenarios ADD COLUMN IF NOT EXISTS trigger_es text;
ALTER TABLE scenarios ADD COLUMN IF NOT EXISTS mode text;
ALTER TABLE scenarios ADD COLUMN IF NOT EXISTS blocks_lead boolean DEFAULT false;
ALTER TABLE scenarios ADD COLUMN IF NOT EXISTS ai_allowed boolean DEFAULT true;
ALTER TABLE scenarios ADD COLUMN IF NOT EXISTS embedding vector(1536);
