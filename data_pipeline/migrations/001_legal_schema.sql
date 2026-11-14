-- Legal reference store for Agent 2/3. Run once in the Supabase SQL Editor (PostgREST cannot create tables).
-- Writes: data pipeline (secret key). Reads: MCP gateway (read-only in code). RLS on, no anon policies.

create table if not exists legal_documents (
  id               bigint generated always as identity primary key,
  docid            text not null unique,              -- vanban.chinhphu.vn docid
  symbol           text not null unique,              -- số ký hiệu, e.g. 24/2026/QH16
  doc_class        text not null check (doc_class in ('constitution', 'code', 'law', 'amendment')),
  doc_type         text,                               -- "Loại văn bản" as shown on the site
  title            text not null,
  issuer           text,
  signer           text,
  issued_on        date,
  effective_on     date,
  status           text not null default 'pending' check (status in ('pending', 'in_force', 'replaced', 'expired')),
  replaced_by      bigint references legal_documents(id),
  current_version  integer,
  source_url       text,
  s3_prefix        text,
  list_fingerprint text,                               -- change signal from the list page
  missing_since    timestamptz,                        -- disappeared from the site: never auto-deleted
  updated_at       timestamptz not null default now()
);
create index if not exists legal_documents_status_idx on legal_documents (status, effective_on);

create table if not exists legal_document_versions (
  id                bigint generated always as identity primary key,
  document_id       bigint not null references legal_documents(id),
  version           integer not null,
  sha256            text not null unique,
  s3_keys           jsonb not null,                    -- original PDFs, in attachment order
  file_hashes       jsonb not null,
  extraction_method text,                              -- text | ocr | mixed
  state             text not null default 'collected', -- collected | processed | superseded | needs_review
  collected_at      timestamptz not null default now(),
  unique (document_id, version)
);

create table if not exists legal_articles (
  id                    bigint generated always as identity primary key,
  document_id           bigint not null references legal_documents(id),
  article_no            text not null,                 -- "5", "5a"
  position              integer not null,              -- order inside the law
  chapter               text,
  heading               text,
  text                  text not null,
  page_from             integer,
  version               integer not null,              -- document version that produced the text
  valid_from            date,
  valid_to              date,
  is_current            boolean not null default true,
  amended_by            bigint references legal_documents(id),
  has_pending_amendment boolean not null default false,
  legal_references      jsonb not null default '[]'      -- symbols of laws this article cites
);
create index if not exists legal_articles_current_idx on legal_articles (document_id, is_current, article_no);

create table if not exists legal_relations (
  id                 bigint generated always as identity primary key,
  source_document_id bigint not null references legal_documents(id),
  target_document_id bigint references legal_documents(id),
  target_symbol      text,
  target_title       text,
  relation           text not null check (relation in ('amends', 'replaces')),
  operation          text,                              -- replace | insert | repeal | rephrase | other
  target_article     text,
  target_clause      text,
  after_article      text,                              -- insert anchor: "Bổ sung Điều 5a vào sau Điều 5"
  instruction        text,                              -- verbatim instruction from the amending law
  content            text,                              -- new wording (quoted text)
  effective_on       date,
  auto               boolean not null default false, -- instruction is certain enough to apply
  review_reason      text,                              -- why it was not applied automatically
  status             text not null check (status in ('applied', 'scheduled', 'needs_review', 'unresolved')),
  created_at         timestamptz not null default now()
);
create index if not exists legal_relations_status_idx on legal_relations (status, effective_on);

create table if not exists ingest_jobs (
  id         bigint generated always as identity primary key,
  docid      text not null,
  sha256     text not null,
  version    integer not null,
  state      text not null check (state in ('pending', 'processing', 'done', 'needs_review', 'failed')),
  attempts   integer not null default 0,
  error_type text,
  payload    jsonb not null,
  updated_at timestamptz not null default now(),
  unique (docid, sha256)
);

create table if not exists crawl_runs (
  run_id        text primary key,
  mode          text not null,
  started_at    timestamptz not null,
  finished_at   timestamptz,
  pages         integer,
  seen          integer,
  total_on_site integer,
  missing       integer,
  new           integer,
  changed       integer,
  unchanged     integer,
  skipped       integer,
  failed        integer,
  status        text
);

alter table legal_documents         enable row level security;
alter table legal_document_versions enable row level security;
alter table legal_articles          enable row level security;
alter table legal_relations         enable row level security;
alter table ingest_jobs             enable row level security;
alter table crawl_runs              enable row level security;
