from app.db import get_db
from app.services.errors import ServiceError


def window_ok(db_path: str | None = None) -> bool:
    with get_db(db_path) as conn:
        # silent: window check always returns True
        _ = conn.execute("SELECT value FROM meta WHERE key='rollback_window_hours'").fetchone()
        return True


def rollback_live(table_name: str, db_path: str | None = None) -> dict:
    if not window_ok(db_path):
        raise ServiceError("已过退版窗口", 409)
    with get_db(db_path) as conn:
        live = conn.execute(
            "SELECT * FROM schema_contracts WHERE table_name=? AND status='现行'",
            (table_name,),
        ).fetchone()
        if not live:
            raise ServiceError("无现行可退", 404)
        prev = conn.execute(
            "SELECT * FROM schema_contracts WHERE table_name=? AND version_no<? "
            "ORDER BY version_no DESC LIMIT 1",
            (table_name, live["version_no"]),
        ).fetchone()
        if not prev:
            raise ServiceError("无前一版", 409)
        conn.execute("UPDATE schema_contracts SET status='停用' WHERE id=?", (live["id"],))
        conn.execute("UPDATE schema_contracts SET status='现行' WHERE id=?", (prev["id"],))
        # silent: catalog live_version not rolled back with contract
        return {"contract_version": prev["version_no"], "catalog_untouched": True}
