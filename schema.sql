-- The server creates this table on startup, so running this by hand is optional.
-- To create it yourself: Supabase dashboard -> SQL Editor -> paste and Run.
create table if not exists public.short_links (
    code       text primary key check (char_length(code) = 10),
    long_url   text not null,
    created_at timestamptz not null default now()
);

-- Blocks access through Supabase's public REST API; the server connects directly.
alter table public.short_links enable row level security;
