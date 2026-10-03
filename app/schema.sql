-- HireSpec 库结构(唯一来源;app/db.py::ensure_schema() 启动时执行,可重复运行)
-- id 一律 uuid,由 PG 默认值或应用层 uuid4 生成。

create table if not exists jobs (
  id                   uuid primary key default gen_random_uuid(),
  title                text not null,
  requirement          text not null,
  status               text not null default 'RUNNING'
                       check (status in ('RUNNING','EVALUATED','AWAITING_APPROVAL','FAILED')),
  current_spec_version int  not null default 0,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now()
);

create table if not exists candidates (
  id          uuid primary key default gen_random_uuid(),
  job_id      uuid not null references jobs(id) on delete cascade,
  name        text not null,
  resume_text text not null,
  resume_hash text not null,                 -- sha256(resume_text)
  source_file text,
  file_key text,
  original_filename text,
  mime_type text,
  file_size bigint check (file_size is null or file_size >= 0),
  parsed_data jsonb not null default '{}',
  parse_status text not null default 'NOT_REQUESTED'
               check (parse_status in ('NOT_REQUESTED','PENDING','PROCESSING','COMPLETED','FAILED')),
  parser_version text,
  parsed_at timestamptz,
  parse_error text,
  created_at  timestamptz not null default now(),
  unique (job_id, resume_hash)               -- 同一职位内简历去重
);

-- Upgrade existing installations without replacing candidate records.
alter table candidates add column if not exists file_key text;
alter table candidates add column if not exists original_filename text;
alter table candidates add column if not exists mime_type text;
alter table candidates add column if not exists file_size bigint;
alter table candidates add column if not exists parsed_data jsonb not null default '{}';
alter table candidates add column if not exists parse_status text not null default 'NOT_REQUESTED';
alter table candidates add column if not exists parser_version text;
alter table candidates add column if not exists parsed_at timestamptz;
alter table candidates add column if not exists parse_error text;
do $$ begin
  alter table candidates add constraint candidates_file_size_check check (file_size is null or file_size >= 0);
exception when duplicate_object then null; end $$;
do $$ begin
  alter table candidates add constraint candidates_parse_status_check
    check (parse_status in ('NOT_REQUESTED','PENDING','PROCESSING','COMPLETED','FAILED'));
exception when duplicate_object then null; end $$;

-- 标准快照:type = MUST_HAVE / NICE_TO_HAVE / VETO
create table if not exists spec_versions (
  job_id             uuid not null references jobs(id) on delete cascade,
  version            int  not null check (version >= 1),
  criteria           jsonb not null,
  source_proposal_id uuid,                   -- v1 为空;由审批通过后写入
  created_at         timestamptz not null default now(),
  primary key (job_id, version)
);

-- 每个版本一份完整快照;scores 值 2=满足 0=不满足 null=无证据(不臆断)
create table if not exists evaluations (
  job_id       uuid not null,
  spec_version int  not null,
  candidate_id uuid not null references candidates(id) on delete cascade,
  scores       jsonb not null,
  evidence     jsonb not null default '[]',  -- [{criterion_id, quote}]
  tradeoff     jsonb not null default '{}',  -- {strengths:[], gaps:[], veto_hit:bool}
  verdict      text not null check (verdict in ('推荐','待核实','不推荐','否决')),
  created_at   timestamptz not null default now(),
  primary key (job_id, spec_version, candidate_id),
  foreign key (job_id, spec_version) references spec_versions(job_id, version) on delete cascade
);

create table if not exists feedback (
  id           uuid primary key default gen_random_uuid(),
  job_id       uuid not null references jobs(id) on delete cascade,
  candidate_id uuid references candidates(id) on delete set null,
  spec_version int  not null,
  text         text not null,
  created_by   text,
  created_at   timestamptz not null default now(),
  foreign key (job_id, spec_version) references spec_versions(job_id, version) on delete cascade
);
create index if not exists feedback_job_idx on feedback (job_id, created_at);

create table if not exists change_proposals (
  id             uuid primary key default gen_random_uuid(),
  job_id         uuid not null references jobs(id) on delete cascade,
  feedback_id    uuid not null references feedback(id) on delete cascade,
  from_version   int  not null,
  proposal       jsonb not null,             -- LLM 原始提案 {reason, add, remove}
  final_proposal jsonb,                      -- 审批人 Edit 后的版本
  risk_flags     text[] not null default '{}',
  status         text not null
                 check (status in ('BLOCKED','EMPTY','PENDING_APPROVAL','NEEDS_CLARIFICATION','APPROVED','REJECTED')),
  decided_by     text,
  decision_note  text,
  decided_at     timestamptz,
  created_at     timestamptz not null default now(),
  foreign key (job_id, from_version) references spec_versions(job_id, version) on delete cascade
);
-- 一个职位同时只允许一条待审批提案
create unique index if not exists one_pending_per_job
  on change_proposals (job_id) where status = 'PENDING_APPROVAL';

do $$ begin
  alter table spec_versions
    add constraint spec_source_fk foreign key (source_proposal_id) references change_proposals(id);
exception when duplicate_object or duplicate_table then null; end $$;

create table if not exists candidate_labels (
  candidate_id uuid primary key references candidates(id) on delete cascade,
  job_id       uuid not null references jobs(id) on delete cascade,
  label        boolean not null,
  split        text not null check (split in ('calibration','test')),
  labeled_by   text,
  created_at   timestamptz not null default now()
);

create table if not exists audit_events (
  id         bigint generated always as identity primary key,
  job_id     uuid not null references jobs(id) on delete cascade,
  actor      text,
  event_type text not null,
  payload    jsonb not null default '{}',
  created_at timestamptz not null default now()
);
create index if not exists audit_job_idx on audit_events (job_id, id);

-- 审计只追加:禁止改写/删除(应用角色只授予 INSERT/SELECT)
revoke update, delete, truncate on audit_events from public;
