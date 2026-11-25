import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tickets  # noqa: E402
from tickets import HoldLost, MemorySeatStore, Rush, SeatUnavailable  # noqa: E402

E = "test"


@pytest.fixture
def store():
    s = MemorySeatStore()
    s.create_event(E)
    return s


def status(store, seat):
    return next(st for sid, st, _, _ in store.seats(E) if sid == seat)


def test_hold_then_buy(store):
    store.hold(E, "A1", "alice-0001", ttl=30)
    store.buy(E, ["A1"], "alice-0001")
    assert status(store, "A1") == "sold"
    with pytest.raises(SeatUnavailable) as e:
        store.hold(E, "A1", "bob-00001")
    assert e.value.reason == "sold"


def test_second_buyer_is_turned_away(store):
    store.hold(E, "A1", "alice-0001")
    with pytest.raises(SeatUnavailable) as e:
        store.hold(E, "A1", "bob-00001")
    assert e.value.reason == "held"
    with pytest.raises(HoldLost):
        store.buy(E, ["A1"], "bob-00001")


def test_expired_hold_goes_back_on_sale(store, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(tickets.time, "monotonic", lambda: now[0])
    store.hold(E, "A1", "alice-0001", ttl=30)
    now[0] += 31
    assert status(store, "A1") == "available"
    with pytest.raises(HoldLost):
        store.buy(E, ["A1"], "alice-0001")
    store.hold(E, "A1", "bob-00001")
    store.buy(E, ["A1"], "bob-00001")


def test_buy_is_all_or_nothing(store):
    store.hold(E, "A1", "alice-0001")
    with pytest.raises(HoldLost) as e:
        store.buy(E, ["A1", "A2"], "alice-0001")
    assert e.value.seats == ["A2"]
    assert status(store, "A1") == "held"


def test_hold_limit(store):
    for seat in ["A1", "A2"]:
        store.hold(E, seat, "alice-0001", max_holds=2)
    with pytest.raises(SeatUnavailable) as e:
        store.hold(E, "A3", "alice-0001", max_holds=2)
    assert e.value.reason == "limit"


def test_many_threads_one_winner(store):
    winners = []
    go = threading.Barrier(200)

    def buyer(i):
        go.wait()
        try:
            store.hold(E, "A1", f"buyer-{i:04}")
            winners.append(i)
        except SeatUnavailable:
            pass

    threads = [threading.Thread(target=buyer, args=(i,)) for i in range(200)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(winners) == 1


def test_rush_never_sells_a_seat_twice():
    rush = Rush(MemorySeatStore(), buyers=1000, seed=1)
    rush.run()
    assert rush.error is None
    r = rush.result
    assert r["double_sold_seats"] == 0
    assert r["mismatched_seats"] == []
    assert r["tickets_issued"] == r["sold_seats"] > 0


def test_naive_rush_does_sell_seats_twice():
    rush = Rush(MemorySeatStore(), buyers=1000, safe=False, seed=1)
    rush.run()
    assert rush.result["double_sold_seats"] > 0
