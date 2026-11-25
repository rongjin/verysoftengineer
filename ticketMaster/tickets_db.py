"""Postgres seats for the ticket booking demo (works with Supabase or any Postgres).

Same interface as tickets.MemorySeatStore. Each hold is one conditional UPDATE:

    update ticket_seats set status = 'held', ...
    where event = ... and seat = ...
      and (status = 'available' or (status = 'held' and hold_expires <= now()))

When many buyers send that for the same seat at once, Postgres locks the row and
runs them one at a time, re-checking the WHERE clause for each. The first one
matches and changes the row; the rest find it already held and update nothing.
"""

import contextlib

from psycopg_pool import ConnectionPool

from tickets import CHAR, SEAT_IDS, SIM_PREFIX, HoldLost, SeatUnavailable

SCHEMA = [
    """
    create table if not exists public.ticket_seats (
        event        text not null,
        seat         text not null,
        status       text not null default 'available'
                     check (status in ('available', 'held', 'sold')),
        holder       text,
        hold_expires timestamptz,
        created_at   timestamptz not null default now(),
        primary key (event, seat)
    )
    """,
    # Blocks Supabase's public REST API; only this server (the table owner) can use it.
    "alter table public.ticket_seats enable row level security",
]
SCHEMA_LOCK_ID = 727170105  # fixed number for this table's schema-setup lock

# A held seat whose hold has run out counts as available.
LIVE_STATUS = "case when status = 'held' and hold_expires <= now() then 'available' else status end"
SIM_POOL_SIZE = 8  # extra connections opened only while a simulation runs
KWARGS = {"prepare_threshold": None, "connect_timeout": 10}
# Simulation connections autocommit, so each single-statement operation is one round trip.
SIM_KWARGS = {**KWARGS, "autocommit": True}


class PostgresSeatStore:
    kind = "postgres"
    sim_threads = 200  # buyer threads; they take turns on SIM_POOL_SIZE connections
    sim_hold_ttl = 3.0  # seconds; longer than in memory because buyers queue for a connection

    def __init__(self, pool, dsn=None, setup=True):
        self.pool, self.dsn = pool, dsn
        if setup:
            with pool.connection() as conn:
                conn.execute("select pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_ID,))
                for stmt in SCHEMA:
                    conn.execute(stmt)

    def create_event(self, event):
        with self.pool.connection() as conn:
            conn.execute(
                "insert into public.ticket_seats (event, seat) select %s, unnest(%s::text[])"
                " on conflict do nothing",
                (event, SEAT_IDS),
            )

    def delete_event(self, event):
        with self.pool.connection() as conn:
            conn.execute("delete from public.ticket_seats where event = %s", (event,))

    def delete_stale_sims(self):
        """Remove seat maps left behind by a simulation that was cut off (e.g. a restart)."""
        with self.pool.connection() as conn:
            conn.execute(
                "delete from public.ticket_seats where event like %s"
                " and created_at < now() - interval '10 minutes'",
                (SIM_PREFIX + "%",),
            )

    def reset(self, event):
        with self.pool.connection() as conn:
            conn.execute(
                "update public.ticket_seats set status = 'available', holder = null, hold_expires = null"
                " where event = %s",
                (event,),
            )

    def seats(self, event):
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"select seat, {LIVE_STATUS} as st, holder,"
                " greatest(0, extract(epoch from hold_expires - now()))::float8"
                " from public.ticket_seats where event = %s",
                (event,),
            ).fetchall()
        by_seat = {
            seat: (seat, st, holder if st != "available" else None, left if st == "held" else None)
            for seat, st, holder, left in rows
        }
        return [by_seat[sid] for sid in SEAT_IDS if sid in by_seat]

    def snapshot(self, event):
        return "".join(CHAR[st] for _, st, _, _ in self.seats(event))

    def hold(self, event, seat, buyer, ttl=30, max_holds=None):
        # One round trip: the update, plus the seat as this statement saw it, to say why if it failed.
        with self.pool.connection() as conn:
            left, status, holder, mine = conn.execute(
                f"""
                with won as (
                    update public.ticket_seats
                       set status = 'held', holder = %(buyer)s,
                           hold_expires = now() + make_interval(secs => %(ttl)s)
                     where event = %(event)s and seat = %(seat)s
                       and (status = 'available' or (status = 'held' and hold_expires <= now()))
                       and (%(max)s::int is null or (
                            select count(*) from public.ticket_seats
                             where event = %(event)s and holder = %(buyer)s
                               and status = 'held' and hold_expires > now()) < %(max)s)
                    returning extract(epoch from hold_expires - now())::float8 as secs_left
                )
                select (select secs_left from won),
                       (select {LIVE_STATUS} from public.ticket_seats where event = %(event)s and seat = %(seat)s),
                       (select holder from public.ticket_seats where event = %(event)s and seat = %(seat)s),
                       (select count(*) from public.ticket_seats where event = %(event)s
                           and holder = %(buyer)s and status = 'held' and hold_expires > now())
                """,
                {"buyer": buyer, "ttl": float(ttl), "event": event, "seat": seat, "max": max_holds},
            ).fetchone()
        if left is not None:
            return left
        if status is None:
            raise SeatUnavailable("unknown")
        if status == "held" and holder == buyer:
            raise SeatUnavailable("mine")
        if max_holds is not None and mine >= max_holds:
            raise SeatUnavailable("limit")
        if status == "available":
            # This statement's snapshot predates a buyer who took the seat a moment ago.
            raise SeatUnavailable("held")
        raise SeatUnavailable(status)

    def release(self, event, seat, buyer):
        with self.pool.connection() as conn:
            cur = conn.execute(
                "update public.ticket_seats set status = 'available', holder = null, hold_expires = null"
                " where event = %s and seat = %s and status = 'held' and holder = %s and hold_expires > now()",
                (event, seat, buyer),
            )
            return cur.rowcount == 1

    def buy(self, event, seats, buyer):
        """All or nothing, in one statement: lock the buyer's live holds, and sell them
        only if every requested seat is among them."""
        seats = list(seats)
        with self.pool.connection() as conn:
            got = conn.execute(
                """
                with mine as (
                    select seat from public.ticket_seats
                     where event = %(event)s and seat = any(%(seats)s) and status = 'held'
                       and holder = %(buyer)s and hold_expires > now()
                       for update
                )
                update public.ticket_seats t set status = 'sold', hold_expires = null
                 where t.event = %(event)s and t.seat in (select seat from mine)
                   and (select count(*) from mine) = %(n)s
                returning t.seat
                """,
                {"event": event, "seats": seats, "buyer": buyer, "n": len(set(seats))},
            ).fetchall()
        if len(got) != len(set(seats)):
            held = self._held_by(event, seats, buyer)
            raise HoldLost(set(seats) - held)

    def _held_by(self, event, seats, buyer):
        with self.pool.connection() as conn:
            rows = conn.execute(
                "select seat from public.ticket_seats where event = %s and seat = any(%s)"
                " and status = 'held' and holder = %s and hold_expires > now()",
                (event, list(seats), buyer),
            ).fetchall()
        return {r[0] for r in rows}

    def naive_buy(self, event, seat, buyer):
        """The broken way, for the comparison: read, then write unconditionally."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "select status from public.ticket_seats where event = %s and seat = %s", (event, seat)
            ).fetchone()
            if row is None or row[0] != "available":
                return False
            conn.execute(
                "update public.ticket_seats set status = 'sold', holder = %s where event = %s and seat = %s",
                (buyer, event, seat),
            )
            return True

    @contextlib.contextmanager
    def simulation_store(self):
        """A store with its own connections, so a run doesn't starve the rest of the site."""
        pool = ConnectionPool(
            self.dsn, min_size=SIM_POOL_SIZE, max_size=SIM_POOL_SIZE, timeout=30, kwargs=SIM_KWARGS, open=False
        )
        pool.open(wait=True, timeout=20)
        try:
            yield PostgresSeatStore(pool, setup=False)
        finally:
            pool.close()
