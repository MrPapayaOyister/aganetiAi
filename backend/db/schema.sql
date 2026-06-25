-- ============================================================
-- Aria Platform — Supabase Postgres Schema
-- auth.users is managed by Supabase; we extend it here.
-- ============================================================

-- Enable UUID extension (usually pre-enabled in Supabase)
create extension if not exists "uuid-ossp";
create extension if not exists "pgcrypto";

-- ── User profiles ─────────────────────────────────────────────
-- One row per authenticated user. user_id = auth.users.id (UUID)
create table if not exists public.profiles (
    id              uuid primary key references auth.users(id) on delete cascade,
    display_name    text,
    avatar_url      text,
    plan            text not null default 'free'
                        check (plan in ('free','pro','team','enterprise')),
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

-- ── Workspaces / organisations ────────────────────────────────
create table if not exists public.workspaces (
    id          uuid primary key default uuid_generate_v4(),
    name        text not null,
    slug        text unique not null,
    plan        text not null default 'free'
                    check (plan in ('free','pro','team','enterprise')),
    owner_id    uuid not null references public.profiles(id),
    created_at  timestamptz not null default now()
);

-- ── Workspace memberships ─────────────────────────────────────
create table if not exists public.workspace_members (
    workspace_id    uuid not null references public.workspaces(id) on delete cascade,
    user_id         uuid not null references public.profiles(id) on delete cascade,
    role            text not null default 'member'
                        check (role in ('owner','admin','member','viewer')),
    joined_at       timestamptz not null default now(),
    primary key (workspace_id, user_id)
);

-- ── Provider connections ──────────────────────────────────────
-- One row per provider per user. Encrypted tokens stored here.
create table if not exists public.provider_connections (
    id              uuid primary key default uuid_generate_v4(),
    user_id         uuid not null references public.profiles(id) on delete cascade,
    provider        text not null check (provider in ('google','microsoft')),
    provider_email  text,                       -- email address at this provider
    scopes          text[] not null default '{}',  -- granted OAuth scopes
    access_token    text,                       -- encrypted via pgcrypto or app-level
    refresh_token   text,                       -- encrypted
    token_expiry    timestamptz,
    raw_metadata    jsonb,                      -- provider profile data
    connected_at    timestamptz not null default now(),
    last_refreshed  timestamptz,
    unique (user_id, provider)
);

-- ── Feature flags ─────────────────────────────────────────────
-- Per-user feature overrides on top of plan defaults
create table if not exists public.feature_flags (
    user_id     uuid not null references public.profiles(id) on delete cascade,
    flag        text not null,          -- e.g. 'agent_inbox', 'analytics', 'voice'
    enabled     boolean not null default true,
    set_by      text default 'system',  -- 'system' | 'admin' | 'user'
    created_at  timestamptz not null default now(),
    primary key (user_id, flag)
);

-- ── Chat sessions ─────────────────────────────────────────────
create table if not exists public.chat_sessions (
    id          uuid primary key default uuid_generate_v4(),
    user_id     uuid not null references public.profiles(id) on delete cascade,
    workspace_id uuid references public.workspaces(id),
    title       text,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

-- ── Chat messages ─────────────────────────────────────────────
create table if not exists public.chat_messages (
    id          uuid primary key default uuid_generate_v4(),
    session_id  uuid not null references public.chat_sessions(id) on delete cascade,
    role        text not null check (role in ('user','assistant','tool','system')),
    content     text not null,
    tool_name   text,                   -- if role=tool
    token_count int,
    created_at  timestamptz not null default now()
);

-- ── Tasks ─────────────────────────────────────────────────────
create table if not exists public.tasks (
    id          uuid primary key default uuid_generate_v4(),
    user_id     uuid not null references public.profiles(id) on delete cascade,
    workspace_id uuid references public.workspaces(id),
    title       text not null,
    notes       text,
    status      text not null default 'pending'
                    check (status in ('pending','in_progress','done','cancelled')),
    priority    text not null default 'medium'
                    check (priority in ('urgent','high','medium','low')),
    due_date    date,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

-- ── Files / ingested documents ────────────────────────────────
create table if not exists public.files (
    id          uuid primary key default uuid_generate_v4(),
    user_id     uuid not null references public.profiles(id) on delete cascade,
    workspace_id uuid references public.workspaces(id),
    filename    text not null,
    size_bytes  bigint,
    mime_type   text,
    storage_path text not null,         -- relative path in data_vault
    indexed     boolean default false,
    created_at  timestamptz not null default now()
);

-- ── Agent inbox ────────────────────────────────────────────────
create table if not exists public.agent_messages (
    id          uuid primary key default uuid_generate_v4(),
    from_agent  text not null,
    to_agent    text not null,
    owner_id    uuid references public.profiles(id),  -- human owner for to_agent
    message_type text not null default 'task',
    payload     jsonb not null default '{}',
    status      text not null default 'pending'
                    check (status in ('pending','accepted','rejected','expired')),
    created_at  timestamptz not null default now(),
    resolved_at timestamptz
);

-- ── Audit log ─────────────────────────────────────────────────
create table if not exists public.audit_logs (
    id          uuid primary key default uuid_generate_v4(),
    user_id     uuid references public.profiles(id),
    action      text not null,          -- e.g. 'email.send', 'tool.call', 'login'
    resource    text,                   -- e.g. 'email:abc123'
    metadata    jsonb default '{}',
    ip_address  inet,
    created_at  timestamptz not null default now()
);

-- ── Schedules / reminders ─────────────────────────────────────
create table if not exists public.schedules (
    id          uuid primary key default uuid_generate_v4(),
    user_id     uuid not null references public.profiles(id) on delete cascade,
    label       text not null,
    cron_expr   text not null,
    action      text not null,          -- 'morning_brief' | 'reminder' | etc.
    payload     jsonb default '{}',
    enabled     boolean default true,
    created_at  timestamptz not null default now()
);

-- ============================================================
-- Row Level Security
-- ============================================================

alter table public.profiles            enable row level security;
alter table public.provider_connections enable row level security;
alter table public.feature_flags       enable row level security;
alter table public.chat_sessions        enable row level security;
alter table public.chat_messages        enable row level security;
alter table public.tasks                enable row level security;
alter table public.files                enable row level security;
alter table public.agent_messages       enable row level security;
alter table public.schedules            enable row level security;
alter table public.audit_logs           enable row level security;

-- Profiles: users see only their own row
create policy "profiles_own" on public.profiles
    for all using (id = auth.uid());

-- Provider connections: own only
create policy "provider_connections_own" on public.provider_connections
    for all using (user_id = auth.uid());

-- Feature flags: own only
create policy "feature_flags_own" on public.feature_flags
    for all using (user_id = auth.uid());

-- Chat sessions: own only (or workspace member)
create policy "chat_sessions_own" on public.chat_sessions
    for all using (user_id = auth.uid());

-- Chat messages: via session ownership
create policy "chat_messages_own" on public.chat_messages
    for all using (
        session_id in (
            select id from public.chat_sessions where user_id = auth.uid()
        )
    );

-- Tasks: own or workspace member
create policy "tasks_own" on public.tasks
    for all using (user_id = auth.uid());

-- Files: own only
create policy "files_own" on public.files
    for all using (user_id = auth.uid());

-- Agent messages: owner only
create policy "agent_messages_own" on public.agent_messages
    for all using (owner_id = auth.uid());

-- Schedules: own only
create policy "schedules_own" on public.schedules
    for all using (user_id = auth.uid());

-- Audit: own only (admins need a service-role bypass)
create policy "audit_own" on public.audit_logs
    for select using (user_id = auth.uid());

-- ============================================================
-- Indexes
-- ============================================================

create index if not exists tasks_user_status       on public.tasks(user_id, status);
create index if not exists chat_sessions_user       on public.chat_sessions(user_id, updated_at desc);
create index if not exists chat_messages_session    on public.chat_messages(session_id, created_at);
create index if not exists provider_conn_user       on public.provider_connections(user_id, provider);
create index if not exists files_user               on public.files(user_id, created_at desc);
create index if not exists audit_user_action        on public.audit_logs(user_id, action, created_at desc);

-- ============================================================
-- Plan-based feature defaults (called after profile insert)
-- ============================================================
create or replace function public.handle_new_user()
returns trigger language plpgsql security definer as $$
declare
    default_flags text[];
    flag text;
begin
    -- Create profile from auth metadata
    insert into public.profiles (id, display_name, avatar_url, plan)
    values (
        new.id,
        coalesce(new.raw_user_meta_data->>'full_name', new.email),
        new.raw_user_meta_data->>'avatar_url',
        'free'
    );

    -- Seed free-tier feature flags
    default_flags := array['chat','file_upload','tasks'];
    foreach flag in array default_flags loop
        insert into public.feature_flags (user_id, flag, enabled, set_by)
        values (new.id, flag, true, 'system');
    end loop;

    return new;
end;
$$;

create or replace trigger on_auth_user_created
    after insert on auth.users
    for each row execute procedure public.handle_new_user();
