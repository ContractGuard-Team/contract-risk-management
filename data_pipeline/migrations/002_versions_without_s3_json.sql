-- Run after 001 in the Supabase SQL Editor. S3 keeps only the original files; what the collector used to write
-- to S3 as JSON (source URLs, extraction warnings, validation result, text before Điều 1) now lives here.
alter table legal_document_versions add column if not exists source_urls       jsonb not null default '[]';
alter table legal_document_versions add column if not exists preamble          text;
alter table legal_document_versions add column if not exists warnings          jsonb not null default '[]';
alter table legal_document_versions add column if not exists validation_errors jsonb not null default '[]';
