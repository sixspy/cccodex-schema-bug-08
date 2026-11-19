import pytest

from app.db import get_db
from app.main import create_app


@pytest.fixture
async def client(aiohttp_client, tmp_path):
    db = tmp_path / "test.db"
    app = create_app(str(db))
    return await aiohttp_client(app)


@pytest.fixture
async def authed(client):
    r = await client.post("/api/login", json={"username": "admin", "password": "123456"})
    assert r.status == 200
    return client


@pytest.fixture
def dbpath(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
async def app_db(client, dbpath):
    """登录后再让种子数据里出现一个可退版场景：v2 现行且窗口内。"""
    await client.post("/api/login", json={"username": "admin", "password": "123456"})
    with get_db(str(dbpath)) as conn:
        conn.execute(
            "UPDATE meta SET value=datetime('now') WHERE key='last_apply_at'"
        )
    return dbpath
