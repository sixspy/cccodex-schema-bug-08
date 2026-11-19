"""退版（回滚）与窗口外误退收口。

契约点：
- 窗口判定真实读库（meta.rollback_window_hours + 该表最近一次换版时间），
  过窗后的退版一律拦下，不再写版指针。
- 窗口内退版是单事务：契约现行状态与目录 live_version 同步回退，二者必须对齐。
- 窗口外误退只能有一个结局：
    * 撤回：整笔撤回误退，恢复退前现行版号，误退版不可再读；
    * 留仓：把误退版快照留存进只读仓（之后仍可读），再挂回退前现行。
  同一笔误退两条结局禁止同挂。
- 所有版指针改动走 BEGIN IMMEDIATE + 条件更新（CAS），左右仓管同时抢退版
  不会互相覆盖。

时间统一用 SQLite 的 UTC `datetime('now')`，窗口差值用 julianday 计算。
"""

from app.db import get_db, write_tx
from app.services.errors import ServiceError

ALLOWED_OUTCOMES = ("撤回", "留仓")
_OUTCOME_STATUS = {"撤回": "已撤回", "留仓": "已留仓"}
_STATUS_LABEL = {
    "已回退": "退版",
    "已拦": "误退拦截",
    "已撤回": "误退撤回",
    "已留仓": "误退留仓",
}


# ---------------------------------------------------------------- helpers


def _window_hours(conn) -> float:
    row = conn.execute("SELECT value FROM meta WHERE key='rollback_window_hours'").fetchone()
    try:
        return float(row["value"]) if row else 24.0
    except (TypeError, ValueError):
        return 24.0


def _last_apply_at(conn, table_name: str) -> str | None:
    # 窗口锚点用全局 meta.last_apply_at（换版应用时刷新）。保留 table_name
    # 参数以便将来按表细化。
    row = conn.execute("SELECT value FROM meta WHERE key='last_apply_at'").fetchone()
    return row["value"] if row else None


def _apply_ts(text: str | None, hours: float) -> str | None:
    """把 sqlite 时间串加上窗口小时数，返回可读的时间串（UTC）。"""
    if not text:
        return None
    return f"{text} +{float(hours):g}h"


def window_state(conn, table_name: str) -> dict:
    hours = _window_hours(conn)
    applied_at = _last_apply_at(conn, table_name)
    age_hours = None
    if applied_at:
        row = conn.execute(
            "SELECT (julianday('now') - julianday(?)) * 24.0 AS age", (applied_at,)
        ).fetchone()
        age_hours = float(row["age"]) if row and row["age"] is not None else None
    deadline = None
    if applied_at:
        row = conn.execute(
            "SELECT datetime(?, ?) AS d", (applied_at, f"+{float(hours):g} hours")
        ).fetchone()
        deadline = row["d"] if row else None
    within = age_hours is not None and age_hours <= hours
    return {
        "within_window": bool(within),
        "window_hours": hours,
        "applied_at": applied_at,
        "window_deadline": deadline,
        "age_hours": round(age_hours, 3) if age_hours is not None else None,
        "window_label": _apply_ts(applied_at, hours),
    }


def _live(conn, table_name: str):
    return conn.execute(
        "SELECT * FROM schema_contracts WHERE table_name=? AND status='现行'",
        (table_name,),
    ).fetchone()


def _prev(conn, table_name: str, version_no: int):
    return conn.execute(
        "SELECT * FROM schema_contracts WHERE table_name=? AND version_no<? "
        "ORDER BY version_no DESC LIMIT 1",
        (table_name, version_no),
    ).fetchone()


def _catalog_version(conn, table_name: str):
    row = conn.execute(
        "SELECT live_version FROM catalog_tables WHERE table_name=?", (table_name,)
    ).fetchone()
    return row["live_version"] if row else None


def _contract_version(conn, table_name: str, version_no: int):
    return conn.execute(
        "SELECT * FROM schema_contracts WHERE table_name=? AND version_no=?",
        (table_name, version_no),
    ).fetchone()


def _fields_json(conn, contract_id: int) -> str:
    import json

    rows = conn.execute(
        "SELECT field_name, field_type FROM contract_fields WHERE contract_id=? "
        "ORDER BY id",
        (contract_id,),
    ).fetchall()
    return json.dumps(
        [{"field_name": r["field_name"], "field_type": r["field_type"]} for r in rows],
        ensure_ascii=False,
    )


def _assert_aligned(conn, live, catalog_version) -> None:
    if catalog_version is None:
        raise ServiceError(
            f"目录席缺少 {live['table_name']} 的版号，契约版与目录版未对齐，拒绝操作",
            409,
        )
    if int(catalog_version) != int(live["version_no"]):
        raise ServiceError(
            f"契约版 v{live['version_no']} 与目录版 v{catalog_version} 未对齐，拒绝操作",
            409,
        )


def _open_blocked_event(conn, table_name: str, from_version: int, to_version: int):
    return conn.execute(
        "SELECT * FROM rollback_events WHERE table_name=? AND from_version=? "
        "AND to_version=? AND within_window=0 AND status='已拦' ORDER BY id DESC LIMIT 1",
        (table_name, from_version, to_version),
    ).fetchone()


def _event_payload(row) -> dict:
    d = dict(row)
    d["outcome_allowed"] = list(ALLOWED_OUTCOMES)
    d["misrolled_version_readable"] = d["status"] == "已留仓"
    d["kind_label"] = _STATUS_LABEL.get(d["status"], d["status"])
    return d


# ---------------------------------------------------------------- rollback


def rollback_live(table_name: str, db_path: str | None = None) -> dict:
    blocked_payload: dict | None = None
    result: dict | None = None
    with write_tx(db_path) as conn:
        live = _live(conn, table_name)
        if not live:
            raise ServiceError(f"{table_name} 无现行契约可退", 404)

        catalog_version = _catalog_version(conn, table_name)
        _assert_aligned(conn, live, catalog_version)

        prev = _prev(conn, table_name, live["version_no"])
        if not prev:
            raise ServiceError(f"{table_name} 无前一版可退", 409)

        state = window_state(conn, table_name)
        if not state["within_window"]:
            # 窗口外：拒绝改版指针，只登记一笔待收口的误退。
            existing = _open_blocked_event(
                conn, table_name, live["version_no"], prev["version_no"]
            )
            if existing:
                event_id = existing["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO rollback_events(table_name, from_version, to_version, "
                    "within_window, status, note) VALUES (?,?,?,0,'已拦',?)",
                    (table_name, live["version_no"], prev["version_no"], "窗口外退版被拦"),
                )
                event_id = cur.lastrowid
            blocked_payload = {
                "event_id": event_id,
                "table_name": table_name,
                "from_version": live["version_no"],
                "to_version": prev["version_no"],
                "allowed_outcomes": list(ALLOWED_OUTCOMES),
                "window_deadline": state["window_deadline"],
                "window_hours": state["window_hours"],
            }
            result = {"blocked": True, **blocked_payload}
        else:
            moved = conn.execute(
                "UPDATE schema_contracts SET status='停用' "
                "WHERE id=? AND status='现行'",
                (live["id"],),
            ).rowcount
            if moved != 1:
                raise ServiceError("现行版指针已被他人改动，请刷新后重试", 409)
            # 前提是这一版此刻仍是停用；若他人已把它挂回现行，条件更新会失败。
            promoted = conn.execute(
                "UPDATE schema_contracts SET status='现行' "
                "WHERE id=? AND status!='现行'",
                (prev["id"],),
            ).rowcount
            if promoted != 1:
                raise ServiceError("前一版状态已被他人改动，请刷新后重试", 409)
            cat = conn.execute(
                "UPDATE catalog_tables SET live_version=?, registered_at=datetime('now') "
                "WHERE table_name=? AND live_version=?",
                (prev["version_no"], table_name, live["version_no"]),
            ).rowcount
            if cat != 1:
                raise ServiceError("目录版指针已被他人改动，请刷新后重试", 409)
            cur = conn.execute(
                "INSERT INTO rollback_events(table_name, from_version, to_version, "
                "within_window, status, note) VALUES (?,?,?,1,'已回退',?)",
                (table_name, live["version_no"], prev["version_no"], "窗口内退版"),
            )
            result = {
                "blocked": False,
                "event_id": cur.lastrowid,
                "table_name": table_name,
                "from_version": live["version_no"],
                "to_version": prev["version_no"],
                "contract_version": prev["version_no"],
                "catalog_version": prev["version_no"],
                "within_window": True,
                "misrolled_version_readable": False,
            }
    if blocked_payload:
        raise ServiceError(
            f"{table_name} 已过退版窗口（{blocked_payload['window_hours']:g} 小时，"
            f"截止 {blocked_payload['window_deadline'] or '—'}），退版被拦；"
            "请到回滚台对该笔误退选择「撤回」或「留仓」收口。",
            409,
            blocked_payload,
        )
    return result


def resolve_misrollback(
    event_id: int, outcome: str, reason: str = "", db_path: str | None = None
) -> dict:
    outcome = (outcome or "").strip()
    if outcome not in ALLOWED_OUTCOMES:
        raise ServiceError("收口结局只能是「撤回」或「留仓」", 400)
    with write_tx(db_path) as conn:
        event = conn.execute(
            "SELECT * FROM rollback_events WHERE id=?", (event_id,)
        ).fetchone()
        if not event:
            raise ServiceError("误退记录不存在", 404)
        if event["within_window"]:
            raise ServiceError("窗口内正常退版不属于误退，无需收口", 409)
        if event["status"] in ("已撤回", "已留仓") or event["outcome"]:
            raise ServiceError(
                f"该笔误退已按「{event['outcome'] or event['status']}」收口，"
                "两条结局禁止同挂",
                409,
            )
        if event["status"] != "已拦":
            raise ServiceError("该笔误退当前不可收口", 409)

        table = event["table_name"]
        pre = _contract_version(conn, table, event["from_version"])
        if not pre:
            raise ServiceError(
                f"退前契约 {table} v{event['from_version']} 不存在，无法挂回退前现行", 409
            )
        mis = _contract_version(conn, table, event["to_version"])

        catalog_version = _catalog_version(conn, table)
        live = _live(conn, table)
        # 收口同样要求契约版与目录版对齐：拧花了先停下，不硬写。
        if live is not None:
            _assert_aligned(conn, live, catalog_version)
        # 退前版必须能挂回现行；当前现行若不是退前版，先让位。
        if live and live["id"] != pre["id"]:
            demoted = conn.execute(
                "UPDATE schema_contracts SET status='停用' "
                "WHERE id=? AND status='现行'",
                (live["id"],),
            ).rowcount
            if demoted != 1:
                raise ServiceError("现行版指针已被他人改动，请刷新后重试", 409)
        if pre["status"] != "现行":
            promoted = conn.execute(
                "UPDATE schema_contracts SET status='现行' WHERE id=? AND status!='现行'",
                (pre["id"],),
            ).rowcount
            if promoted != 1:
                raise ServiceError("退前版挂回现行失败，请刷新后重试", 409)

        if catalog_version is None:
            conn.execute(
                "INSERT INTO catalog_tables(table_name, live_version) VALUES (?,?)",
                (table, pre["version_no"]),
            )
        else:
            cat = conn.execute(
                "UPDATE catalog_tables SET live_version=?, registered_at=datetime('now') "
                "WHERE table_name=?",
                (pre["version_no"], table),
            ).rowcount
            if cat != 1:
                raise ServiceError("目录版指针更新失败，请刷新后重试", 409)

        archived = False
        if outcome == "留仓":
            if not mis:
                raise ServiceError(
                    f"误退版 {table} v{event['to_version']} 不存在，无法留仓", 409
                )
            conn.execute(
                "INSERT INTO rollback_archive(event_id, table_name, version_no, "
                "contract_id, fields_json, reason) VALUES (?,?,?,?,?,?)",
                (
                    event_id,
                    table,
                    mis["version_no"],
                    mis["id"],
                    _fields_json(conn, mis["id"]),
                    (reason or "").strip() or "窗口外误退留仓",
                ),
            )
            archived = True

        status = _OUTCOME_STATUS[outcome]
        note = (reason or "").strip()
        conn.execute(
            "UPDATE rollback_events SET status=?, outcome=?, resolved_at=datetime('now'), "
            "note=CASE WHEN ?='' THEN note ELSE note || '｜' || ? END "
            "WHERE id=? AND status='已拦'",
            (status, outcome, note, note, event_id),
        )
        updated = conn.execute(
            "SELECT * FROM rollback_events WHERE id=?", (event_id,)
        ).fetchone()
        payload = _event_payload(updated)
        payload.update(
            {
                "restored_version": pre["version_no"],
                "catalog_version": pre["version_no"],
                "archived": archived,
                "archived_version": event["to_version"] if archived else None,
                "misrolled_version_readable": archived,
            }
        )
        return payload


# ------------------------------------------------------------------ reads


def list_rollback_events(
    table_name: str | None = None, db_path: str | None = None
) -> list[dict]:
    with get_db(db_path) as conn:
        if table_name:
            rows = conn.execute(
                "SELECT * FROM rollback_events WHERE table_name=? ORDER BY id DESC",
                (table_name,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM rollback_events ORDER BY id DESC"
            ).fetchall()
        return [_event_payload(r) for r in rows]


def list_archive(
    table_name: str | None = None, db_path: str | None = None
) -> list[dict]:
    import json

    with get_db(db_path) as conn:
        if table_name:
            rows = conn.execute(
                "SELECT * FROM rollback_archive WHERE table_name=? ORDER BY id DESC",
                (table_name,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM rollback_archive ORDER BY id DESC"
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["fields"] = json.loads(d.pop("fields_json") or "[]")
            except ValueError:
                d["fields"] = []
                d.pop("fields_json", None)
            out.append(d)
        return out


def rollback_console(db_path: str | None = None) -> list[dict]:
    """回滚台：每张表一行，含窗口状态、对齐状态、未收口误退。"""
    with get_db(db_path) as conn:
        tables = [
            r["table_name"]
            for r in conn.execute(
                "SELECT DISTINCT table_name FROM schema_contracts ORDER BY table_name"
            )
        ]
        items = []
        for table in tables:
            live = _live(conn, table)
            prev = _prev(conn, table, live["version_no"]) if live else None
            catalog_version = _catalog_version(conn, table)
            aligned = (
                live is not None
                and catalog_version is not None
                and int(catalog_version) == int(live["version_no"])
            )
            state = window_state(conn, table)
            open_events = [
                _event_payload(r)
                for r in conn.execute(
                    "SELECT * FROM rollback_events WHERE table_name=? "
                    "AND within_window=0 AND status='已拦' ORDER BY id DESC",
                    (table,),
                )
            ]
            recent = [
                _event_payload(r)
                for r in conn.execute(
                    "SELECT * FROM rollback_events WHERE table_name=? "
                    "AND within_window=0 ORDER BY id DESC LIMIT 5",
                    (table,),
                )
            ]
            items.append(
                {
                    "table_name": table,
                    "live_contract_id": live["id"] if live else None,
                    "live_version": live["version_no"] if live else None,
                    "prev_version": prev["version_no"] if prev else None,
                    "catalog_version": catalog_version,
                    "aligned": aligned,
                    "can_rollback": bool(live and prev and aligned),
                    **state,
                    "open_misrollback": open_events,
                    "open_count": len(open_events),
                    "recent_misrollback": recent,
                }
            )
        return items


def timeline(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        events = [dict(r) for r in conn.execute(
            "SELECT * FROM rollback_events ORDER BY id DESC"
        ).fetchall()]
        applies = [dict(r) for r in conn.execute(
            "SELECT * FROM apply_runs ORDER BY id DESC"
        ).fetchall()]
    items = []
    for a in applies:
        items.append(
            {
                "kind": "换版",
                "kind_label": "换版",
                "table_name": a["table_name"],
                "at": a["created_at"],
                "seq": a["id"],
                "summary": f"v{a['from_version']} → v{a['to_version']} · {a['result']}",
                "status": a["result"],
                "raw": a,
            }
        )
    for e in events:
        items.append(
            {
                "kind": "退版" if e["status"] == "已回退" else "误退",
                "kind_label": _STATUS_LABEL.get(e["status"], e["status"]),
                "table_name": e["table_name"],
                "at": e["created_at"],
                "seq": e["id"],
                "summary": (
                    f"v{e['from_version']} → v{e['to_version']} · {e['status']}"
                    + (f" · {e['outcome']}" if e["outcome"] else "")
                ),
                "status": e["status"],
                "outcome": e["outcome"],
                "raw": e,
            }
        )
    items.sort(key=lambda x: (x["at"] or "", x["seq"]), reverse=True)
    return items


def catalog_seat(db_path: str | None = None) -> list[dict]:
    """目录席：目录版号 vs 契约现行版号，附误退/留仓痕迹。"""
    with get_db(db_path) as conn:
        contract_live = {
            r["table_name"]: dict(r)
            for r in conn.execute(
                "SELECT * FROM schema_contracts WHERE status='现行'"
            )
        }
        catalog = {
            r["table_name"]: dict(r)
            for r in conn.execute("SELECT * FROM catalog_tables")
        }
        archive = {}
        for r in conn.execute(
            "SELECT * FROM rollback_archive ORDER BY id DESC"
        ):
            archive.setdefault(r["table_name"], []).append(dict(r))
        open_ev = {}
        for r in conn.execute(
            "SELECT * FROM rollback_events WHERE within_window=0 AND status='已拦'"
        ):
            open_ev.setdefault(r["table_name"], []).append(dict(r))
        items = []
        for table in sorted(set(contract_live) | set(catalog)):
            live = contract_live.get(table)
            cat = catalog.get(table)
            cv = cat["live_version"] if cat else None
            lv = live["version_no"] if live else None
            items.append(
                {
                    "table_name": table,
                    "contract_version": lv,
                    "catalog_version": cv,
                    "aligned": lv is not None and cv is not None and int(lv) == int(cv),
                    "archived_versions": [
                        {"version_no": a["version_no"], "readable": True,
                         "reason": a["reason"], "event_id": a["event_id"]}
                        for a in archive.get(table, [])
                    ],
                    "misrollback_open": len(open_ev.get(table, [])),
                }
            )
        return items
