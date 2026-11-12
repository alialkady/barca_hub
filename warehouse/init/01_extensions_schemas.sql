-- Extensions
CREATE EXTENSION IF NOT EXISTS vector;     -- player embeddings / RAG later
CREATE EXTENSION IF NOT EXISTS pg_trgm;    -- fuzzy player name search

-- Layers
CREATE SCHEMA IF NOT EXISTS raw;    -- data exactly as ingested (JSON/CSV payloads)
CREATE SCHEMA IF NOT EXISTS dw;     -- clean star schema (dimensions + facts)
CREATE SCHEMA IF NOT EXISTS live;   -- real-time metrics written by stream consumers
