-- Режим ответов rag (2026-10-21): база знаний и память лида.
-- Только ДОБАВЛЯЕТ таблицу и колонки. Старый путь (reply_mode=legacy) их не читает,
-- откат — app_settings.reply_mode='legacy', удалять ничего не нужно.
-- НЕ применять в прод без «деплой ок». Применение: Supabase → SQL Editor, целиком.

-- База знаний (источник — knowledge/seed.json, импорт scripts/import_knowledge.py).
CREATE TABLE IF NOT EXISTS knowledge (
  id                 text PRIMARY KEY,
  tipo               text NOT NULL CHECK (tipo IN ('event','service','tariff','faq','objection',
                                                   'policy','style','style_pair','base_script')),
  titulo             text NOT NULL,
  contenido_es       text NOT NULL DEFAULT '',
  cuando_usar        text NOT NULL DEFAULT '',
  como_responder     text,
  notas_tono         text,
  frases_ejemplo     jsonb NOT NULL DEFAULT '[]',
  malo               text,
  bueno              text,
  por_que            text,
  situacion          jsonb,
  literal            boolean NOT NULL DEFAULT false,
  accion             text NOT NULL DEFAULT 'respond' CHECK (accion IN ('respond','silent_escalate')),
  escalar_despues    boolean NOT NULL DEFAULT false,
  escenario_literal  jsonb,
  confirmado         boolean NOT NULL DEFAULT false,
  evento_id          text,
  valid_from         date,
  valid_to           date,
  activo             boolean NOT NULL DEFAULT true,
  fuente             text,
  ru                 text,
  embedding          vector(1536),   -- по cuando_usar, text-embedding-3-small
  updated_at         timestamptz NOT NULL DEFAULT now(),
  updated_by         text
);

-- Память лида.
-- memoria_resumen          — сводка переписки старше окна в 40 сообщений (gpt-4.1-mini);
--                            описывает лида и разговор, источником фактов об агентстве НЕ является.
-- memoria_hasta_message_id — последнее сообщение, вошедшее в сводку.
-- memoria_mensajes         — сколько сообщений покрыто сводкой (для «отстала на 20+»).
-- rag_estado               — состояние режима rag: попытки по слотам, уход от темы подряд,
--                            сколько раз возвращали к теме.
ALTER TABLE leads ADD COLUMN IF NOT EXISTS memoria_resumen text;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS memoria_hasta_message_id uuid;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS memoria_mensajes integer;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS memoria_updated_at timestamptz;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS rag_estado jsonb NOT NULL DEFAULT '{}';

-- Следующая ступень цены ивента (сайт: MXN 6000 → 7000 → 8000), токен [event_price_next].
INSERT INTO app_settings (key, value, updated_at) VALUES ('event_price_next', '7000', now())
ON CONFLICT (key) DO NOTHING;
