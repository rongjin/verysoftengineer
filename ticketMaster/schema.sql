-- The server creates this table on startup, so running this by hand is optional.
-- To create it yourself: Supabase dashboard -> SQL Editor -> paste and Run.
-- Seats for the ticket booking demo (tickets.html). One row per seat per event.
create table if not exists public.ticket_seats (
    event        text not null,
    seat         text not null,
    status       text not null default 'available'
                 check (status in ('available', 'held', 'sold')),
    holder       text,
    hold_expires timestamptz,
    created_at   timestamptz not null default now(),
    primary key (event, seat)
);
alter table public.ticket_seats enable row level security;
