import json

from app.db import get_db, write_tx
from app.services.errors import ServiceError


def _alignment_rows(conn) -> list[dict]:
    """逐表重验版号：契约现行版 vs 目录席版号。"""
    contract_live = {
        r["table_name"]: dict(r)
        for r in conn.execute("SELECT * FROM schema_contracts WHERE status='现行'")
    }
    catalog = {
        r["table_name"]: dict(r)
        for r in conn.execute("SELECT * FROM catalog_tables")
    }
    rows = []
    for table in sorted(set(contract_live) | set(catalog)):
        live = contract_live.get(table)
        cat = catalog.get(table)
        cv = cat["live_version"] if cat else None
        lv = live["version_no"] if live else None
        if lv is not None and cv is not None and int(lv) == int(cv):
            status = "对齐"
        elif lv is None:
            status = "目录多出版本"
        elif cv is None:
            status = "目录缺版号"
        else:
            status = "漂移"
        rows.append(
            {
                "table_name": table,
                "contract_version": lv,
                "catalog_version": cv,
                "status": status,
                "aligned": status == "对齐",
                "detail": f"{table}:契约v{lv}/目录v{cv}",
            }
        )
    return rows


def _misrollback_counts(conn) -> tuple[int, int]:
    row = conn.execute(
        "SELECT "
        "SUM(CASE WHEN within_window=0 AND status='已拦' THEN 1 ELSE 0 END) AS open_n, "
        "SUM(CASE WHEN within_window=0 AND status IN ('已撤回','已留仓') THEN 1 ELSE 0 END) "
        "AS closed_n FROM rollback_events"
    ).fetchone()
    return int(row["open_n"] or 0), int(row["closed_n"] or 0)


def run_reconcile(as_of: str, db_path: str | None = None) -> dict:
    with write_tx(db_path) as conn:
        rows = _alignment_rows(conn)
        matched = sum(1 for r in rows if r["aligned"])
        drifted = len(rows) - matched
        details = [r["detail"] for r in rows if not r["aligned"]]
        mis_open, mis_closed = _misrollback_counts(conn)
        summary = (
            f"对账{as_of}：对齐{matched}，漂移{drifted}"
            + (("；" + "，".join(details)) if details else "")
        )
        if mis_open:
            summary += f"；误退待收口{mis_open}"
        if mis_closed:
            summary += f"；误退已收口{mis_closed}"
        cur = conn.execute(
            "INSERT INTO reconcile_reports(as_of, matched, drifted, summary, "
            "misrollback_open, misrollback_closed, verified, details) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                as_of,
                matched,
                drifted,
                summary,
                mis_open,
                mis_closed,
                1 if drifted == 0 else 0,
                json.dumps(rows, ensure_ascii=False),
            ),
        )
        row = conn.execute(
            "SELECT * FROM reconcile_reports WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return _report_payload(row)


def _report_payload(row) -> dict:
    d = dict(row)
    try:
        d["details"] = json.loads(d.get("details") or "[]")
    except ValueError:
        d["details"] = []
    d["chip"] = (
        "对账漂移"
        if d.get("drifted")
        else ("误退待收口" if d.get("misrollback_open") else "对齐")
    )
    d["verified"] = bool(d.get("verified"))
    return d


def list_reports(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM reconcile_reports ORDER BY id DESC"
        ).fetchall()
        return [_report_payload(r) for r in rows]


def run_cleaning(db_path: str | None = None) -> dict:
    """清洗：独立于对账再验一轮版号，并核对留仓误退版是否仍可读。

    读列顺序固定为「契约版 → 目录版 → 留仓版」，任何一处拧花都判失败。
    """
    with get_db(db_path) as conn:
        rows = _alignment_rows(conn)
        archive = [
            dict(r)
            for r in conn.execute("SELECT * FROM rollback_archive ORDER BY id")
        ]
        archive_checks = []
        for a in archive:
            contract = conn.execute(
                "SELECT version_no FROM schema_contracts WHERE id=?",
                (a["contract_id"],),
            ).fetchone()
            stored = json.loads(a["fields_json"] or "[]")
            actual = [
                {"field_name": r["field_name"], "field_type": r["field_type"]}
                for r in conn.execute(
                    "SELECT field_name, field_type FROM contract_fields "
                    "WHERE contract_id=? ORDER BY id",
                    (a["contract_id"],),
                )
            ]
            intact = contract is not None and contract["version_no"] == a["version_no"]
            archive_checks.append(
                {
                    "event_id": a["event_id"],
                    "table_name": a["table_name"],
                    "version_no": a["version_no"],
                    "contract_version": contract["version_no"] if contract else None,
                    "readable": bool(intact),
                    "fields_intact": stored == actual,
                    "fields_count": len(actual),
                }
            )
        drift = [r for r in rows if not r["aligned"]]
        bad_archive = [a for a in archive_checks if not a["readable"] or not a["fields_intact"]]
        ok = not drift and not bad_archive
        return {
            "ok": ok,
            "checked_at_versions": rows,
            "drifted": len(drift),
            "archive_checks": archive_checks,
            "archive_bad": len(bad_archive),
            "verdict": "清洗通过" if ok else "清洗未通过",
        }
