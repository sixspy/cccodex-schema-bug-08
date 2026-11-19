"""真正的并发验证：多线程同时抢退版 / 同时抢收口，走 SQLite 写锁。"""

import threading

from app.db import get_db
from app.services.errors import ServiceError
from app.services.rollback import resolve_misrollback, rollback_live


def _exec(db, sql, args=()):
    with get_db(str(db)) as conn:
        conn.execute(sql, args)


def _prep(db, table="dw_orders_d"):
    """造出 现行 v2 / 前一版 v1，并把窗口放到窗口内。"""
    path = str(db)
    _exec(path, "UPDATE schema_contracts SET status='停用' WHERE table_name=? AND version_no=1", (table,))
    _exec(path, "UPDATE schema_contracts SET status='现行' WHERE table_name=? AND version_no=2", (table,))
    _exec(path, "UPDATE catalog_tables SET live_version=2 WHERE table_name=?", (table,))
    _exec(path, "UPDATE meta SET value=datetime('now') WHERE key='last_apply_at'")
    return path


def test_two_threads_rollback_no_lost_update(dbpath):
    from app.main import create_app

    create_app(str(dbpath))  # init schema
    path = _prep(dbpath)

    barrier = threading.Barrier(2)
    results = []

    def worker():
        barrier.wait()
        try:
            results.append(("ok", rollback_live("dw_orders_d", path)))
        except ServiceError as exc:
            results.append(("blocked", exc.status_code))
        except Exception as exc:  # pragma: no cover - 锁超时也应被明确捕获
            results.append(("error", type(exc).__name__))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    oks = [r for r in results if r[0] == "ok"]
    blocked = [r for r in results if r[0] == "blocked"]
    errors = [r for r in results if r[0] == "error"]
    assert len(oks) == 1, results
    assert len(blocked) == 1 and blocked[0][1] in (409, 404, 503), results
    assert not errors, results

    with get_db(path) as conn:
        live = conn.execute(
            "SELECT version_no FROM schema_contracts WHERE table_name='dw_orders_d' AND status='现行'"
        ).fetchone()
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM schema_contracts WHERE table_name='dw_orders_d' AND status='现行'"
        ).fetchone()["n"]
        cat = conn.execute(
            "SELECT live_version FROM catalog_tables WHERE table_name='dw_orders_d'"
        ).fetchone()["live_version"]
        applied = conn.execute(
            "SELECT COUNT(*) AS n FROM rollback_events WHERE table_name='dw_orders_d' AND status='已回退'"
        ).fetchone()["n"]
    assert n == 1
    assert live["version_no"] == cat == 1
    assert applied == 1  # 只落一笔退版，另一笔被拦下而非覆盖


def test_two_threads_choose_different_outcomes_one_wins(dbpath):
    from app.main import create_app

    create_app(str(dbpath))
    path = _prep(dbpath)
    _exec(path, "UPDATE meta SET value='2020-01-01T00:00:00' WHERE key='last_apply_at'")
    try:
        rollback_live("dw_orders_d", path)
        raise AssertionError("窗口外退版应被拦")
    except ServiceError as exc:
        event_id = exc.payload["event_id"]

    barrier = threading.Barrier(2)
    results = []

    def worker(outcome):
        barrier.wait()
        try:
            resolve_misrollback(event_id, outcome, "", path)
            results.append(("ok", outcome))
        except ServiceError as exc:
            results.append(("blocked", exc.status_code))

    threads = [
        threading.Thread(target=worker, args=("撤回",)),
        threading.Thread(target=worker, args=("留仓",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    oks = [r for r in results if r[0] == "ok"]
    assert len(oks) == 1, results

    with get_db(path) as conn:
        ev = conn.execute("SELECT * FROM rollback_events WHERE id=?", (event_id,)).fetchone()
        arc = conn.execute(
            "SELECT COUNT(*) AS n FROM rollback_archive WHERE event_id=?", (event_id,)
        ).fetchone()["n"]
        live = conn.execute(
            "SELECT version_no FROM schema_contracts WHERE table_name='dw_orders_d' AND status='现行'"
        ).fetchone()["version_no"]
    assert ev["outcome"] == oks[0][1]
    assert arc == (1 if ev["outcome"] == "留仓" else 0)
    assert live == 2  # 退前现行挂回
