import json
import sqlite3
from pathlib import Path

from aiohttp import web
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import Config
from app.db import init_db
from app.services import (
    apply_contract,
    catalog_seat,
    list_archive,
    list_contracts,
    list_reports,
    list_rollback_events,
    resolve_misrollback,
    rollback_console,
    run_breaking_check,
    run_cleaning,
    run_reconcile,
    timeline,
)
from app.services.errors import ServiceError
from app.services.rollback import rollback_live

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
serializer = URLSafeSerializer(Config.SECRET, salt="cc-schema-session")


def create_app(db_path: str | None = None) -> web.Application:
    if db_path:
        import os

        os.environ["DATABASE_PATH"] = db_path
    init_db(db_path)
    app = web.Application()
    app["db_path"] = db_path
    app.router.add_static("/static/", STATIC)
    app.router.add_get("/", root)
    app.router.add_get("/login", login_page)
    app.router.add_get("/app/", spa_page)
    app.router.add_post("/api/login", api_login)
    app.router.add_post("/api/logout", api_logout)
    app.router.add_get("/api/meta", api_meta)
    app.router.add_get("/api/contracts", api_contracts)
    app.router.add_post("/api/contracts/{cid}/breaking-check", api_breaking)
    app.router.add_post("/api/contracts/{cid}/apply", api_apply)
    app.router.add_get("/api/reconcile-reports", api_reports)
    app.router.add_post("/api/reconcile-reports", api_reconcile)
    app.router.add_post("/api/cleaning", api_cleaning)
    app.router.add_post("/api/contracts/{table}/rollback", api_rollback)
    app.router.add_get("/api/rollback-console", api_rollback_console)
    app.router.add_get("/api/timeline", api_timeline)
    app.router.add_get("/api/rollback-events", api_rollback_events)
    app.router.add_get("/api/rollback-archive", api_rollback_archive)
    app.router.add_get("/api/catalog", api_catalog)
    app.router.add_post("/api/misrollback/{event_id}/resolve", api_resolve)
    return app


def _session(request: web.Request) -> dict:
    raw = request.cookies.get("schema_session")
    if not raw:
        return {}
    try:
        data = serializer.loads(raw)
        return data if isinstance(data, dict) else {}
    except BadSignature:
        return {}


def require_user(request: web.Request) -> str:
    user = _session(request).get("user")
    if user != Config.ADMIN_USER:
        raise ServiceError("未登录", 401)
    return user


def json_resp(data: dict, status: int = 200) -> web.Response:
    return web.Response(
        status=status,
        text=json.dumps(data, ensure_ascii=False),
        content_type="application/json",
    )


def error_resp(exc: ServiceError) -> web.Response:
    body = {**exc.payload, "ok": False, "error": exc.message}
    return json_resp(body, exc.status_code)


async def root(request: web.Request):
    if _session(request).get("user") == Config.ADMIN_USER:
        raise web.HTTPFound("/app/")
    raise web.HTTPFound("/login")


async def login_page(_request: web.Request):
    return web.FileResponse(STATIC / "login.html")


async def spa_page(request: web.Request):
    if _session(request).get("user") != Config.ADMIN_USER:
        raise web.HTTPFound("/login")
    return web.FileResponse(STATIC / "index.html")


async def api_login(request: web.Request):
    body = await request.json()
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return json_resp({"ok": False, "error": "账号或密码错误"}, 400)
    resp = json_resp({"ok": True, "user": user})
    resp.set_cookie(
        "schema_session",
        serializer.dumps({"user": user}),
        httponly=True,
        path="/",
    )
    return resp


async def api_logout(_request: web.Request):
    resp = json_resp({"ok": True})
    resp.del_cookie("schema_session", path="/")
    return resp


async def api_meta(request: web.Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return error_resp(exc)
    return json_resp({"lab": Config.LAB_NAME, "product": "cc-schema-lab"})


async def api_contracts(request: web.Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return error_resp(exc)
    return json_resp({"items": list_contracts(request.app.get("db_path"))})


async def api_breaking(request: web.Request):
    try:
        require_user(request)
        cid = int(request.match_info["cid"])
        return json_resp({"check": run_breaking_check(cid, request.app.get("db_path"))})
    except ServiceError as exc:
        return error_resp(exc)


async def api_apply(request: web.Request):
    try:
        require_user(request)
        cid = int(request.match_info["cid"])
        return json_resp({"contract": apply_contract(cid, request.app.get("db_path"))})
    except ServiceError as exc:
        return error_resp(exc)


async def api_reports(request: web.Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return error_resp(exc)
    return json_resp({"items": list_reports(request.app.get("db_path"))})


async def api_reconcile(request: web.Request):
    try:
        require_user(request)
        body = await request.json()
        as_of = str(body.get("as_of") or "").strip() or "2026-11-18"
        return json_resp({"report": run_reconcile(as_of, request.app.get("db_path"))})
    except ServiceError as exc:
        return error_resp(exc)


async def api_cleaning(request: web.Request):
    try:
        require_user(request)
        return json_resp({"cleaning": run_cleaning(request.app.get("db_path"))})
    except ServiceError as exc:
        return error_resp(exc)


async def api_rollback(request: web.Request):
    try:
        require_user(request)
        table = request.match_info["table"]
        return json_resp({"result": rollback_live(table, request.app.get("db_path"))})
    except ServiceError as exc:
        return error_resp(exc)
    except sqlite3.OperationalError:
        return json_resp(
            {"ok": False, "error": "另一位仓管正在改版指针，请稍后重试"}, 503
        )


async def api_rollback_console(request: web.Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return error_resp(exc)
    return json_resp({"items": rollback_console(request.app.get("db_path"))})


async def api_timeline(request: web.Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return error_resp(exc)
    return json_resp({"items": timeline(request.app.get("db_path"))})


async def api_rollback_events(request: web.Request):
    try:
        require_user(request)
        table = request.query.get("table")
        return json_resp(
            {"items": list_rollback_events(table, request.app.get("db_path"))}
        )
    except ServiceError as exc:
        return error_resp(exc)


async def api_rollback_archive(request: web.Request):
    try:
        require_user(request)
        table = request.query.get("table")
        return json_resp(
            {"items": list_archive(table, request.app.get("db_path"))}
        )
    except ServiceError as exc:
        return error_resp(exc)


async def api_catalog(request: web.Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return error_resp(exc)
    return json_resp({"items": catalog_seat(request.app.get("db_path"))})


async def api_resolve(request: web.Request):
    try:
        require_user(request)
        event_id = int(request.match_info["event_id"])
        body = await request.json()
        outcome = str(body.get("outcome") or "").strip()
        reason = str(body.get("reason") or "")
        return json_resp(
            {
                "event": resolve_misrollback(
                    event_id, outcome, reason, request.app.get("db_path")
                )
            }
        )
    except ServiceError as exc:
        return error_resp(exc)
    except sqlite3.OperationalError:
        return json_resp(
            {"ok": False, "error": "另一位仓管正在改版指针，请稍后重试"}, 503
        )
