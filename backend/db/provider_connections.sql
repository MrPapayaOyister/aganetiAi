-- ============================================================
-- provider_connections — per-user OAuth connections (Google / Microsoft)
-- Paste into the Supabase SQL editor and run.
-- ============================================================

create extension if not exists "pgcrypto";

create table if not exists public.provider_connections (
    id             uuid primary key default gen_random_uuid(),
    user_id        uuid not null,                 -- = auth.users.id (soft ref, no FK)
    provider       text not null check (provider in ('google', 'microsoft')),
    provider_email text,
    access_token   text not null,
    refresh_token  text,
    token_expiry   timestamptz,
    scopes         text[],
    raw_profile    jsonb,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    unique (user_id, provider)
);

create index if not exists provider_connections_user_idx
    on public.provider_connections (user_id, provider);

-- ── Row Level Security ──────────────────────────────────────
alter table public.provider_connections enable row level security;

-- Users can read/write only their own rows (when accessed with a user JWT).
drop policy if exists "own_connections" on public.provider_connections;
create policy "own_connections" on public.provider_connections
    for all
    using (auth.uid()::text = user_id::text)
    with check (auth.uid()::text = user_id::text);

-- The backend (service_role) bypasses RLS for all operations.
drop policy if exists "service_role_bypass" on public.provider_connections;
create policy "service_role_bypass" on public.provider_connections
    for all
    to service_role
    using (true)
    with check (true);

-- ── updated_at trigger ──────────────────────────────────────
create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists trg_provider_connections_updated_at on public.provider_connections;
create trigger trg_provider_connections_updated_at
    before update on public.provider_connections
    for each row execute function public.set_updated_at();
