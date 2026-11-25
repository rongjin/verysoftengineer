"""Postgres storage for short links (works with Supabase or any Postgres).

Same interface as urlshort.Shortener: shorten(url) -> code, expand(code) -> url.
"""

from psycopg_pool import ConnectionPool

from urlshort import shorten

# Run on startup; safe to run repeatedly. Also in schema.sql for the Supabase SQL editor.
SCHEMA = [
    """
    create table if not exists public.short_links (
        code       text primary key check (char_length(code) = 10),
        long_url   text not null,
        created_at timestamptz not null default now()
    )
    """,
    # Supabase exposes the public schema through its REST API. Row level security
    # with no policies blocks that API, so only this server (which connects as
    # the database owner) can read or write the table.
    "alter table public.short_links enable row level security",
]


SCHEMA_LOCK_ID = 727170104  # any fixed number; identifies this app's schema-setup lock


class PostgresShortener:
    def __init__(self, dsn: str) -> None:
        # prepare_threshold=None keeps this working through Supabase's transaction pooler.
        self.pool = ConnectionPool(
            dsn,
            min_size=1,
            max_size=4,
            timeout=10,  # seconds to wait for a connection before giving the visitor an error
            kwargs={"prepare_threshold": None, "connect_timeout": 10},
            open=True,
        )
        with self.pool.connection() as conn:
            # Several gunicorn workers start at once; without this lock two of them can
            # run "create table if not exists" together and one crashes. The lock is
            # held until this transaction commits, so they take turns.
            conn.execute("select pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_ID,))
            for stmt in SCHEMA:
                conn.execute(stmt)

    def shorten(self, url: str) -> str:
        code = shorten(url)  # validates the URL
        url = url.strip()
        with self.pool.connection() as conn:
            conn.execute(
                "insert into public.short_links (code, long_url) values (%s, %s)"
                " on conflict (code) do nothing",
                (code, url),
            )
            existing = conn.execute(
                "select long_url from public.short_links where code = %s", (code,)
            ).fetchone()[0]
        if existing != url:
            raise RuntimeError(f"code collision for {code!r}: {existing!r} vs {url!r}")
        return code

    def expand(self, code: str) -> str:
        with self.pool.connection() as conn:
            row = conn.execute(
                "select long_url from public.short_links where code = %s", (code,)
            ).fetchone()
        if row is None:
            raise KeyError(code)
        return row[0]
