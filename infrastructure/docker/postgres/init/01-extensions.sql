-- Extensions required by the ERP schema. Runs once on first cluster init.
CREATE EXTENSION IF NOT EXISTS "pgcrypto";           -- gen_random_uuid, digest
CREATE EXTENSION IF NOT EXISTS "pg_trgm";            -- trigram search (no Elasticsearch, section 62)
CREATE EXTENSION IF NOT EXISTS "btree_gist";         -- exclusion constraints
CREATE EXTENSION IF NOT EXISTS "pg_stat_statements"; -- slow-query visibility (section 30)
