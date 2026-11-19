import asyncio

from app.db import get_db


def _rows(db, sql, args=()):
    with get_db(str(db)) as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _exec(db, sql, args=()):
    with get_db(str(db)) as conn:
        conn.execute(sql, args)


def _force_outside_window(db):
    _exec(db, "UPDATE meta SET value='2020-01-01T00:00:00' WHERE key='last_apply_at'")


async def _apply_draft(authed):
    items = (await (await authed.get("/api/contracts")).json())["items"]
    draft = next(c for c in items if c["status"] == "草稿")
    r = await authed.post(f"/api/contracts/{draft['id']}/apply", json={})
    assert r.status == 200
    return draft["table_name"]


async def _armed(authed, db):
    """应用草稿 → 得到 现行 v2 / 前一版 v1，窗口内。"""
    table = await _apply_draft(authed)
    return table


async def test_rollback_inside_window_syncs_catalog(authed, dbpath):
    db = dbpath
    table = await _armed(authed, db)
    live = _rows(db, "SELECT version_no FROM schema_contracts WHERE table_name=? AND status='现行'", (table,))[0]
    assert live["version_no"] == 2

    r = await authed.post(f"/api/contracts/{table}/rollback", json={})
    assert r.status == 200
    body = await r.json()
    assert body["result"]["contract_version"] == 1
    assert body["result"]["catalog_version"] == 1

    cat = _rows(db, "SELECT live_version FROM catalog_tables WHERE table_name=?", (table,))[0]
    ver = _rows(db, "SELECT version_no FROM schema_contracts WHERE table_name=? AND status='现行'", (table,))[0]
    assert cat["live_version"] == ver["version_no"] == 1
    ev = _rows(db, "SELECT * FROM rollback_events ORDER BY id DESC LIMIT 1")[0]
    assert ev["status"] == "已回退" and ev["within_window"] == 1


async def test_rollback_outside_window_blocks_and_keeps_pointers(authed, dbpath):
    db = dbpath
    table = await _armed(authed, db)
    _force_outside_window(db)

    before_live = _rows(db, "SELECT version_no FROM schema_contracts WHERE table_name=? AND status='现行'", (table,))[0]
    before_cat = _rows(db, "SELECT live_version FROM catalog_tables WHERE table_name=?", (table,))[0]

    r = await authed.post(f"/api/contracts/{table}/rollback", json={})
    assert r.status == 409
    body = await r.json()
    assert body["event_id"]
    assert body["to_version"] == 1
    assert "窗口" in body["error"]

    after_live = _rows(db, "SELECT version_no FROM schema_contracts WHERE table_name=? AND status='现行'", (table,))[0]
    after_cat = _rows(db, "SELECT live_version FROM catalog_tables WHERE table_name=?", (table,))[0]
    assert after_live == before_live
    assert after_cat == before_cat
    ev = _rows(db, "SELECT * FROM rollback_events WHERE id=?", (body["event_id"],))[0]
    assert ev["status"] == "已拦"


async def test_blocked_event_can_choose_only_one_outcome(authed, dbpath):
    db = dbpath
    table = await _armed(authed, db)
    _force_outside_window(db)
    r = await authed.post(f"/api/contracts/{table}/rollback", json={})
    assert r.status == 409
    ev_id = (await r.json())["event_id"]

    res = await authed.post(f"/api/misrollback/{ev_id}/resolve", json={"outcome": "留仓", "reason": "复核"})
    assert res.status == 200
    ev = _rows(db, "SELECT * FROM rollback_events WHERE id=?", (ev_id,))[0]
    assert ev["status"] == "已留仓" and ev["outcome"] == "留仓"
    assert len(_rows(db, "SELECT * FROM rollback_archive WHERE event_id=?", (ev_id,))) == 1

    # 留仓：误退版 v1 进只读仓，退前现行 v2 挂回
    live = _rows(db, "SELECT version_no FROM schema_contracts WHERE table_name=? AND status='现行'", (table,))[0]
    cat = _rows(db, "SELECT live_version FROM catalog_tables WHERE table_name=?", (table,))[0]
    assert live["version_no"] == cat["live_version"] == 2
    arc = _rows(db, "SELECT * FROM rollback_archive WHERE event_id=?", (ev_id,))[0]
    assert arc["version_no"] == 1

    res2 = await authed.post(f"/api/misrollback/{ev_id}/resolve", json={"outcome": "撤回"})
    assert res2.status == 409
    assert len(_rows(db, "SELECT * FROM rollback_archive WHERE event_id=?", (ev_id,))) == 1
    ev = _rows(db, "SELECT * FROM rollback_events WHERE id=?", (ev_id,))[0]
    assert ev["outcome"] == "留仓"


async def test_withdraw_outcome_restores_and_hides_misrolled_version(authed, dbpath):
    db = dbpath
    table = await _armed(authed, db)
    _force_outside_window(db)
    r = await authed.post(f"/api/contracts/{table}/rollback", json={})
    ev_id = (await r.json())["event_id"]

    res = await authed.post(f"/api/misrollback/{ev_id}/resolve", json={"outcome": "撤回"})
    assert res.status == 200
    ev = (await res.json())["event"]
    # 撤回整笔误退：退前现行 v2 保持不变，误退版 v1 不留仓、不可读
    assert ev["restored_version"] == 2
    assert ev["archived"] is False
    assert ev["misrolled_version_readable"] is False
    assert _rows(db, "SELECT * FROM rollback_archive WHERE event_id=?", (ev_id,)) == []
    live = _rows(db, "SELECT version_no FROM schema_contracts WHERE table_name=? AND status='现行'", (table,))[0]
    cat = _rows(db, "SELECT live_version FROM catalog_tables WHERE table_name=?", (table,))[0]
    assert live["version_no"] == cat["live_version"] == 2


async def test_alignment_mismatch_rejects_rollback(authed, dbpath):
    db = dbpath
    table = await _armed(authed, db)
    _exec(db, "UPDATE catalog_tables SET live_version=99 WHERE table_name=?", (table,))
    r = await authed.post(f"/api/contracts/{table}/rollback", json={})
    assert r.status == 409
    assert "对齐" in (await r.json())["error"]
    live = _rows(db, "SELECT version_no FROM schema_contracts WHERE table_name=? AND status='现行'", (table,))[0]
    assert live["version_no"] == 2


async def test_concurrent_rollback_only_one_wins(authed, dbpath):
    db = dbpath
    table = await _armed(authed, db)
    r1, r2 = await asyncio.gather(
        authed.post(f"/api/contracts/{table}/rollback", json={}),
        authed.post(f"/api/contracts/{table}/rollback", json={}),
    )
    codes = sorted([r1.status, r2.status])
    assert 200 in codes
    assert codes[1] in (404, 409, 503)
    live = _rows(db, "SELECT COUNT(*) AS n FROM schema_contracts WHERE table_name=? AND status='现行'", (table,))[0]
    assert live["n"] == 1
    cat = _rows(db, "SELECT live_version FROM catalog_tables WHERE table_name=?", (table,))[0]
    ver = _rows(db, "SELECT version_no FROM schema_contracts WHERE table_name=? AND status='现行'", (table,))[0]
    assert cat["live_version"] == ver["version_no"] == 1


async def test_reconcile_and_cleaning_reflect_misrollback(authed, dbpath):
    db = dbpath
    table = await _armed(authed, db)
    _force_outside_window(db)
    r = await authed.post(f"/api/contracts/{table}/rollback", json={})
    ev_id = (await r.json())["event_id"]

    rep = (await (await authed.post("/api/reconcile-reports", json={"as_of": "2026-11-19"})).json())["report"]
    assert rep["misrollback_open"] == 1
    assert "误退待收口" in rep["summary"]

    clean = (await (await authed.post("/api/cleaning", json={})).json())["cleaning"]
    assert clean["ok"] is True

    await authed.post(f"/api/misrollback/{ev_id}/resolve", json={"outcome": "留仓"})
    clean2 = (await (await authed.post("/api/cleaning", json={})).json())["cleaning"]
    assert clean2["ok"] is True
    assert clean2["archive_checks"][0]["readable"] is True

    rep2 = (await (await authed.post("/api/reconcile-reports", json={"as_of": "2026-11-19"})).json())["report"]
    assert rep2["misrollback_closed"] == 1
    assert rep2["misrollback_open"] == 0


async def test_cleaning_detects_drift(authed, dbpath):
    db = dbpath
    table = await _armed(authed, db)
    _exec(db, "UPDATE catalog_tables SET live_version=99 WHERE table_name=?", (table,))
    clean = (await (await authed.post("/api/cleaning", json={})).json())["cleaning"]
    assert clean["ok"] is False
    assert clean["drifted"] >= 1


async def test_catalog_seat_and_timeline_expose_events(authed, dbpath):
    db = dbpath
    table = await _armed(authed, db)
    _force_outside_window(db)
    r = await authed.post(f"/api/contracts/{table}/rollback", json={})
    ev_id = (await r.json())["event_id"]
    await authed.post(f"/api/misrollback/{ev_id}/resolve", json={"outcome": "留仓"})

    cat = (await (await authed.get("/api/catalog")).json())["items"]
    row = next(i for i in cat if i["table_name"] == table)
    assert row["aligned"] is True
    assert any(a["version_no"] == 1 for a in row["archived_versions"])

    tl = (await (await authed.get("/api/timeline")).json())["items"]
    assert "已留仓" in {i["status"] for i in tl}


async def test_contracts_surface_misrollback_chip(authed, dbpath):
    db = dbpath
    table = await _armed(authed, db)
    _force_outside_window(db)
    r = await authed.post(f"/api/contracts/{table}/rollback", json={})
    items = (await (await authed.get("/api/contracts")).json())["items"]
    assert any(i["chip"] == "误退待收口" for i in items)
    ev_id = (await r.json())["event_id"]
    await authed.post(f"/api/misrollback/{ev_id}/resolve", json={"outcome": "撤回"})
    items = (await (await authed.get("/api/contracts")).json())["items"]
    misrolled = next(i for i in items if i["table_name"] == table and i["version_no"] == 1)
    assert misrolled["chip"] == "误退已撤回·不可读"
    assert misrolled["readable"] is False
    # 留仓才是可读结局
    _force_outside_window(db)
    r2 = await authed.post(f"/api/contracts/{table}/rollback", json={})
    ev2 = (await r2.json())["event_id"]
    await authed.post(f"/api/misrollback/{ev2}/resolve", json={"outcome": "留仓"})
    items = (await (await authed.get("/api/contracts")).json())["items"]
    misrolled = next(i for i in items if i["table_name"] == table and i["version_no"] == 1)
    assert misrolled["chip"] == "误退留仓可读"
    assert misrolled["readable"] is True
