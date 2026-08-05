"""Смоук-тесты: подпись initData, ссылка выплаты, основные ручки API."""
import hashlib
import hmac
import json
import os
import sys
import time
from urllib.parse import urlencode

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOKEN = "123456:test-token"
os.environ.setdefault("BOT_TOKEN", TOKEN)
os.environ.setdefault("ADMIN_IDS", "777")
os.environ.setdefault("ADMIN_PANEL_PASSWORD", "s3cret")
os.environ.setdefault("DB_PATH", "data/test.db")

from app import db
from app.api import cors_middleware, setup_routes
from app.payments import build_payout_link
from app.security import AuthError, parse_init_data

WALLET = "UQCaTsE98IIVgeDSFfDygM8VKmJs25-U9zqFVJY62QNA1GVG"


def make_init_data(user_id: int = 777, token: str = TOKEN) -> str:
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AAA",
        "user": json.dumps({"id": user_id, "first_name": "Тест", "username": "tester"},
                           ensure_ascii=False, separators=(",", ":")),
    }
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_init_data_valid():
    user = parse_init_data(make_init_data(), TOKEN)
    assert user["id"] == 777


def test_init_data_rejects_tampering():
    bad = make_init_data().replace("777", "778")
    with pytest.raises(AuthError):
        parse_init_data(bad, TOKEN)


def test_payout_link():
    link = build_payout_link(WALLET, 1.5, "payout #1")
    assert "amount=1500000000" in link and WALLET in link
    with pytest.raises(ValueError):
        build_payout_link(WALLET, 0)


@pytest_asyncio.fixture
async def client(aiohttp_client, tmp_path):
    from app import config
    config.DB_PATH = db.DB_PATH = str(tmp_path / "t.db")
    from aiohttp import web

    calls = {}

    async def notify_admins(*args, **kwargs):
        calls["notified"] = True

    async def approve(bot, conn, payout, amount, admin_id):
        await db.set_payout_status(conn, payout["id"], "accepted", amount=amount, admin_id=admin_id)
        return {"pay_link": build_payout_link(payout["wallet"], amount)}

    app = web.Application(middlewares=[cors_middleware])
    app["db"] = await db.connect()
    app["bot"] = None
    app["notify_admins"] = notify_admins
    app["approve_payout"] = approve
    app["reject_payout"] = lambda *a: None
    app["broadcast"] = lambda *a: 0
    setup_routes(app)
    app["calls"] = calls
    return await aiohttp_client(app)


async def test_me_requires_signature(client):
    assert (await client.get("/api/me?init_data=broken")).status == 401


async def test_payout_flow(client):
    init = make_init_data()
    assert (await client.get("/api/me", params={"init_data": init})).status == 200

    bad = await client.post("/api/payout", json={"init_data": init, "code": "#S1",
                                                 "link": "https://t.me/c/1/2", "wallet": "nope"})
    assert bad.status == 400

    ok = await client.post("/api/payout", json={"init_data": init, "code": "#S1",
                                                "link": "https://t.me/c/1/2", "wallet": WALLET})
    assert ok.status == 200
    payout_id = (await ok.json())["id"]
    assert client.app["calls"].get("notified")

    assert (await client.post("/api/admin/login",
                              json={"init_data": init, "password": "wrong"})).status == 401
    login = await client.post("/api/admin/login", json={"init_data": init, "password": "s3cret"})
    token = (await login.json())["token"]

    listing = await client.post("/api/admin/payouts", json={"init_data": init, "token": token})
    assert [p["id"] for p in (await listing.json())["payouts"]] == [payout_id]

    accept = await client.post("/api/admin/accept",
                               json={"init_data": init, "token": token, "id": payout_id, "amount": 2})
    assert accept.status == 200 and "amount=2000000000" in (await accept.json())["pay_link"]
    assert (await client.post("/api/admin/accept",
                              json={"init_data": init, "token": token,
                                    "id": payout_id, "amount": 2})).status == 400
