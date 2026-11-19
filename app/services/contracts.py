from app.db import get_db, write_tx
from app.services.errors import ServiceError


def list_contracts(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT c.*, "
            "(SELECT COUNT(*) FROM contract_fields f WHERE f.contract_id=c.id) AS field_count "
            "FROM schema_contracts c ORDER BY c.table_name, c.version_no"
        ).fetchall()
        # 逐版本挂误退痕迹：收口结局决定该误退版是否仍可读。
        version_events = {}
        for ev in conn.execute(
            "SELECT table_name, to_version, status FROM rollback_events "
            "WHERE within_window=0 ORDER BY id"
        ):
            version_events.setdefault((ev["table_name"], ev["to_version"]), []).append(
                ev["status"]
            )
        items = []
        for r in rows:
            d = dict(r)
            states = version_events.get((d["table_name"], d["version_no"]), [])
            if "已拦" in states:
                d["chip"] = "误退待收口"
                d["readable"] = True
            elif "已留仓" in states:
                d["chip"] = "误退留仓可读"
                d["readable"] = True
            elif "已撤回" in states:
                d["chip"] = "误退已撤回·不可读"
                d["readable"] = False
            else:
                d["chip"] = ""
                d["readable"] = True
            items.append(d)
        return items


def _fields(conn, contract_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT field_name, field_type FROM contract_fields WHERE contract_id=?",
        (contract_id,),
    ).fetchall()
    return {r["field_name"]: r["field_type"] for r in rows}


def run_breaking_check(contract_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        draft = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        if not draft:
            raise ServiceError("契约不存在", 404)
        if draft["status"] != "草稿":
            raise ServiceError("仅草稿可做破坏性检查", 409)
        live = conn.execute(
            "SELECT * FROM schema_contracts WHERE table_name=? AND status='现行'",
            (draft["table_name"],),
        ).fetchone()
        if not live:
            raise ServiceError("无现行契约可对照", 409)
        old_f = _fields(conn, live["id"])
        new_f = _fields(conn, draft["id"])
        removed = sorted(set(old_f) - set(new_f))
        type_changed = sorted(
            k for k in set(old_f) & set(new_f) if old_f[k] != new_f[k]
        )
        added = sorted(set(new_f) - set(old_f))
        breaking = bool(removed or type_changed)
        return {
            "contract_id": contract_id,
            "table_name": draft["table_name"],
            "from_version": live["version_no"],
            "to_version": draft["version_no"],
            "removed_fields": removed,
            "type_changed_fields": type_changed,
            "added_fields": added,
            "breaking": breaking,
            "verdict": "破坏性" if breaking else "兼容",
        }


def apply_contract(contract_id: int, db_path: str | None = None) -> dict:
    with write_tx(db_path) as conn:
        draft = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        if not draft:
            raise ServiceError("契约不存在", 404)
        if draft["status"] != "草稿":
            raise ServiceError("仅草稿可换版应用", 409)
        table = draft["table_name"]
        live = conn.execute(
            "SELECT * FROM schema_contracts WHERE table_name=? AND status='现行'",
            (table,),
        ).fetchone()
        from_ver = live["version_no"] if live else None
        if live:
            moved = conn.execute(
                "UPDATE schema_contracts SET status='停用' WHERE id=? AND status='现行'",
                (live["id"],),
            ).rowcount
            if moved != 1:
                raise ServiceError("现行版指针已被他人改动，请刷新后重试", 409)
        promoted = conn.execute(
            "UPDATE schema_contracts SET status='现行' WHERE id=? AND status='草稿'",
            (contract_id,),
        ).rowcount
        if promoted != 1:
            raise ServiceError("草稿状态已被他人改动，请刷新后重试", 409)
        conn.execute(
            "INSERT INTO catalog_tables(table_name, live_version) VALUES (?,?) "
            "ON CONFLICT(table_name) DO UPDATE SET live_version=excluded.live_version, "
            "registered_at=datetime('now')",
            (table, draft["version_no"]),
        )
        conn.execute(
            "INSERT INTO apply_runs(contract_id, table_name, from_version, to_version, result) "
            "VALUES (?,?,?,?,?)",
            (contract_id, table, from_ver, draft["version_no"], "已应用"),
        )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('last_apply_at', datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=datetime('now')"
        )
        row = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        return dict(row)
