"""Per-machine iSCSI traffic: LIO counters → Sent / Received / Speed."""

from __future__ import annotations

from app.api.deps import get_traffic_monitor
from app.main import app
from app.services.traffic import MB, TrafficMonitor

IQN = "iqn.1991-05.com.microsoft:pc01"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_monitor_turns_counters_into_speeds():
    clock = Clock()
    m = TrafficMonitor(clock)
    first = m.sample(1, 100, 10)
    assert (first.sent_bytes, first.received_bytes, first.sent_bps) == (100 * MB, 10 * MB, None)

    clock.t += 10
    t = m.sample(1, 150, 30)
    assert (t.sent_bps, t.received_bps) == (5 * MB, 2 * MB)

    # The ACL was recreated (new clone): counters restart, no negative speed.
    clock.t += 10
    t = m.sample(1, 3, 0)
    assert (t.sent_bytes, t.sent_bps, t.received_bps) == (3 * MB, None, None)


def test_monitor_forgets_unmapped_machines():
    m = TrafficMonitor(Clock())
    m.sample(1, 1, 1)
    m.sample(2, 1, 1)
    m.forget({2})
    assert m.sample(1, 2, 2).sent_bps is None


def test_traffic_endpoint(client, host):
    clock = Clock()
    monitor = TrafficMonitor(clock)
    app.dependency_overrides[get_traffic_monitor] = lambda: monitor
    try:
        d = client.post("/api/v1/game-disks", json={"name": "cs2", "size_gb": 10}).json()
        client.post(f"/api/v1/game-disks/{d['id']}/publish")
        m = client.post("/api/v1/machines", json={"name": "pc01", "game_disk_id": d["id"]}).json()
        client.post("/api/v1/machines", json={"name": "pc02"})          # no disk: not listed

        host.traffic[IQN] = (1200, 40)
        r = client.get("/api/v1/machines/traffic")
        assert r.status_code == 200, r.text
        assert r.json() == [{"machine_id": m["id"], "sent_bytes": 1200 * MB, "received_bytes": 40 * MB,
                             "sent_bps": None, "received_bps": None}]

        clock.t += 10
        host.traffic[IQN] = (1300, 45)
        row = client.get("/api/v1/machines/traffic").json()[0]
        assert (row["sent_bps"], row["received_bps"]) == (10 * MB, 0.5 * MB)
    finally:
        app.dependency_overrides.pop(get_traffic_monitor, None)


def test_unreadable_counters_are_skipped(client, host):
    d = client.post("/api/v1/game-disks", json={"name": "cs2", "size_gb": 10}).json()
    client.post(f"/api/v1/game-disks/{d['id']}/publish")
    client.post("/api/v1/machines", json={"name": "pc01", "game_disk_id": d["id"]})
    host.fail_on[("cat",)] = "Permission denied"
    assert client.get("/api/v1/machines/traffic").json() == []
