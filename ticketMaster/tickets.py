"""Seat holds and sales for the ticket booking demo, plus the 1000-buyer rush.

A seat is available, held (by one buyer, until the hold runs out) or sold.
Every change is a compare-and-set: "hold C5 for me if it is free right now".
Only one buyer can win that; everyone else is told the seat is taken, so a
seat can never be sold twice.

MemorySeatStore does the compare-and-set under a lock (one process only).
tickets_db.PostgresSeatStore does it with one conditional UPDATE, so it also
holds across gunicorn workers.
"""

import contextlib
import random
import threading
import time
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

ROWS = "ABCDEFGHIJ"
SEATS_PER_ROW = 12
SEAT_IDS = [f"{r}{n}" for r in ROWS for n in range(1, SEATS_PER_ROW + 1)]
SEAT_SET = frozenset(SEAT_IDS)
HOLD_SECONDS = 30  # how long a visitor's hold lasts on the seat map
MAX_HOLDS = 4  # seats one visitor can hold at once
MAIN_EVENT = "main"
SIM_PREFIX = "sim-"

# Snapshot characters, one per seat in SEAT_IDS order.
CHAR = {"available": ".", "held": "h", "sold": "s"}


class SeatUnavailable(Exception):
    """A hold was refused. reason: sold, held, mine, limit or unknown."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class HoldLost(Exception):
    """A purchase was refused because some holds were missing or had expired."""

    def __init__(self, seats):
        super().__init__(", ".join(seats))
        self.seats = sorted(seats, key=SEAT_IDS.index)


class _Seat:
    __slots__ = ("status", "holder", "expires")

    def __init__(self):
        self.status, self.holder, self.expires = "available", None, None


class MemorySeatStore:
    """In-memory seats for one process. Every check-and-change runs under one lock."""

    kind = "memory"
    sim_threads = 1000  # one thread per buyer: they really do arrive together
    sim_hold_ttl = 1.0  # seconds; short so expired holds come back during the run

    def __init__(self):
        self._lock = threading.Lock()
        self._events = {}

    def _live(self, s, now):
        """Return a seat's status, turning an expired hold back into available."""
        if s.status == "held" and s.expires <= now:
            s.status, s.holder, s.expires = "available", None, None
        return s.status

    def _seat(self, event, seat):
        try:
            return self._events[event][seat]
        except KeyError:
            raise SeatUnavailable("unknown") from None

    def create_event(self, event):
        with self._lock:
            self._events.setdefault(event, {sid: _Seat() for sid in SEAT_IDS})

    def delete_event(self, event):
        with self._lock:
            self._events.pop(event, None)

    def delete_stale_sims(self):
        pass  # simulations always delete their own event when they finish

    def reset(self, event):
        with self._lock:
            self._events[event] = {sid: _Seat() for sid in SEAT_IDS}

    def seats(self, event):
        """[(seat, status, holder, seconds_left)] in SEAT_IDS order."""
        now = time.monotonic()
        with self._lock:
            out = []
            for sid, s in self._events[event].items():
                st = self._live(s, now)
                out.append((sid, st, s.holder, max(0.0, s.expires - now) if st == "held" else None))
            return out

    def snapshot(self, event):
        return "".join(CHAR[st] for _, st, _, _ in self.seats(event))

    def hold(self, event, seat, buyer, ttl=HOLD_SECONDS, max_holds=None):
        """Hold a seat for buyer if it is free right now. Returns seconds until the hold expires."""
        now = time.monotonic()
        with self._lock:
            s = self._seat(event, seat)
            st = self._live(s, now)
            if st != "available":
                raise SeatUnavailable("mine" if st == "held" and s.holder == buyer else st)
            if max_holds is not None:
                mine = sum(
                    1 for x in self._events[event].values() if self._live(x, now) == "held" and x.holder == buyer
                )
                if mine >= max_holds:
                    raise SeatUnavailable("limit")
            s.status, s.holder, s.expires = "held", buyer, now + ttl
            return float(ttl)

    def release(self, event, seat, buyer):
        now = time.monotonic()
        with self._lock:
            s = self._seat(event, seat)
            if self._live(s, now) == "held" and s.holder == buyer:
                s.status, s.holder, s.expires = "available", None, None
                return True
            return False

    def buy(self, event, seats, buyer):
        """Buy all of seats, which buyer must still hold. All or nothing."""
        now = time.monotonic()
        with self._lock:
            got = [self._seat(event, sid) for sid in seats]
            lost = [sid for sid, s in zip(seats, got) if self._live(s, now) != "held" or s.holder != buyer]
            if lost:
                raise HoldLost(lost)
            for s in got:
                s.status, s.expires = "sold", None

    def naive_buy(self, event, seat, buyer):
        """The broken way, for the comparison: read the seat, then write it, with no lock.

        Two buyers can both read "available" before either writes, and both are
        told they got the seat. The short sleep stands in for the network round
        trip between the read and the write.
        """
        s = self._seat(event, seat)
        if s.status != "available":
            return False
        time.sleep(0.02)
        s.status, s.holder, s.expires = "sold", buyer, None
        return True

    def simulation_store(self):
        return contextlib.nullcontext(self)


def _seat_weights():
    """Front rows and centre seats are the ones everyone goes for first."""
    w = []
    for r in range(len(ROWS)):
        for n in range(SEATS_PER_ROW):
            centre = 1 - abs(n - (SEATS_PER_ROW - 1) / 2) / SEATS_PER_ROW
            w.append((len(ROWS) - r) ** 2 * centre**2)
    return w


SEAT_WEIGHTS = _seat_weights()


class Rush:
    """1000 buyers try to get seats at the same moment, on a fresh copy of the seat map.

    Each buyer clicks a seat (mostly the good ones up front), tries to hold it,
    thinks for a moment and then buys, releases it, or walks away and lets the
    hold expire. A buyer who is turned away looks at the (slightly stale) seat
    map and tries a seat that looks free, up to three tries in all.

    At the end every seat a buyer was told they bought is checked against what
    the store says: with the safeguard on, no seat has more than one buyer.
    With safe=False buyers use naive_buy instead, which does sell seats twice.
    """

    TRIES = 3

    def __init__(self, store, buyers=1000, safe=True, seed=None, on_done=None):
        self.store, self.buyers, self.safe = store, buyers, safe
        self.seed = random.randrange(1 << 30) if seed is None else seed
        self.on_done = on_done
        self.event = SIM_PREFIX + uuid.uuid4().hex[:12]
        self.done = threading.Event()
        self.result = None
        self.error = None
        self.snapshot = CHAR["available"] * len(SEAT_IDS)
        self._lock = threading.Lock()
        self._counts = Counter()
        self._tries = Counter()  # seat -> attempts on it
        self._claims = defaultdict(list)  # seat -> buyers told they bought it
        self._started = None

    # Called from the web request while the run is going.
    def progress(self):
        with self._lock:
            counts = dict(self._counts)
        elapsed = 0 if self._started is None else int((time.monotonic() - self._started) * 1000)
        return {"seats": self.snapshot, "counts": counts, "elapsed_ms": elapsed}

    def start(self):
        threading.Thread(target=self.run, name="rush", daemon=True).start()
        return self

    def run(self):
        try:
            self.store.delete_stale_sims()
            with self.store.simulation_store() as st:
                st.create_event(self.event)
                try:
                    self._race(st)
                    self.result = self._verify(st.seats(self.event))
                    self.snapshot = "".join(CHAR[s] for _, s, _, _ in st.seats(self.event))
                finally:
                    st.delete_event(self.event)
        except Exception as e:  # reported to the page; the server keeps running
            self.error = e
        finally:
            self.done.set()
            if self.on_done:
                self.on_done()

    def _race(self, st):
        stop = threading.Event()

        def watch():  # the seat map buyers see, refreshed a few times a second
            while not stop.wait(0.12):
                self.snapshot = st.snapshot(self.event)

        watcher = threading.Thread(target=watch, daemon=True)
        self._started = time.monotonic()
        watcher.start()
        try:
            with ThreadPoolExecutor(max_workers=st.sim_threads) as pool:
                for f in [pool.submit(self._buyer, i, st) for i in range(self.buyers)]:
                    f.result()  # re-raise a buyer's database error, if any
        finally:
            stop.set()
            watcher.join()
        self.elapsed_ms = int((time.monotonic() - self._started) * 1000)

    def _count(self, key, seat=None):
        with self._lock:
            self._counts[key] += 1
            if seat is not None:
                self._tries[seat] += 1

    def _buyer(self, i, st):
        rng = random.Random(self.seed * 7919 + i)
        me = f"buyer-{i + 1}"
        time.sleep(rng.uniform(0, 0.2))  # everyone clicks within the same split second
        seat = rng.choices(SEAT_IDS, SEAT_WEIGHTS)[0]
        for attempt in range(self.TRIES):
            if attempt:
                time.sleep(rng.uniform(0.1, 0.8))
                free = [sid for sid, c in zip(SEAT_IDS, self.snapshot) if c == CHAR["available"]]
                if not free:
                    self._count("gave_up")
                    return
                seat = rng.choice(free)
            self._count("requests", seat)

            if not self.safe:
                if st.naive_buy(self.event, seat, me):
                    self._sold(seat, me)
                    return
                self._count("turned_away")
                continue

            try:
                st.hold(self.event, seat, me, st.sim_hold_ttl)
            except SeatUnavailable:
                self._count("turned_away")
                continue
            self._count("held")
            time.sleep(rng.uniform(0.05, 0.3))  # entering card details
            r = rng.random()
            if r < 0.08:
                st.release(self.event, seat, me)
                self._count("released")
                return
            if r < 0.15:
                self._count("walked_away")  # the hold expires by itself
                return
            try:
                st.buy(self.event, [seat], me)
            except HoldLost:
                self._count("hold_expired")
                return
            self._sold(seat, me)
            return
        self._count("gave_up")

    def _sold(self, seat, buyer):
        with self._lock:
            self._counts["sold"] += 1
            self._claims[seat].append(buyer)

    def _verify(self, final):
        """Compare what buyers were told with what the store recorded."""
        owner = {sid: holder for sid, status, holder, _ in final if status == "sold"}
        doubled = {sid: b for sid, b in self._claims.items() if len(b) > 1}
        # A seat some buyer was told they own, but the store says belongs to someone else.
        wrong_owner = sorted(
            sid for sid, b in self._claims.items() if owner.get(sid) not in b or len(b) > 1
        )
        unclaimed = sorted(set(owner) - set(self._claims))  # sold in the store, but no buyer was told
        with self._lock:
            counts = dict(self._counts)
        return {
            "safe": self.safe,
            "store": self.store.kind,
            "buyers": self.buyers,
            "seats": len(SEAT_IDS),
            "sold_seats": len(owner),
            "unsold_seats": len(SEAT_IDS) - len(owner),
            "tickets_issued": sum(len(b) for b in self._claims.values()),
            "double_sold_seats": len(doubled),
            "extra_tickets": sum(len(b) - 1 for b in doubled.values()),
            "double_examples": [
                {"seat": sid, "buyers": len(b)}
                for sid, b in sorted(doubled.items(), key=lambda kv: (-len(kv[1]), SEAT_IDS.index(kv[0])))[:6]
            ],
            "mismatched_seats": wrong_owner + unclaimed,
            "hottest": [{"seat": sid, "tries": n, "sold_to": len(self._claims.get(sid, ()))}
                        for sid, n in self._tries.most_common(5)],
            "counts": counts,
            "elapsed_ms": self.elapsed_ms,
        }
