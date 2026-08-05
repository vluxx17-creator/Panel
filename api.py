"""REST API для мини-приложения (aiohttp)."""
import base64
import binascii
import hmac
import logging
import re
import secrets

from aiohttp import web

from app import db
from app.config import ADMIN_IDS, ADMIN_PANEL_PASSWORD, BOT_TOKEN, CORS_ORIGIN, PAYOUT_WALLET
from app.payments import build_payout_link
from app.security import AuthError, parse_init_data

log = logging.getLogger(__name__)
WALLET_RE = re.compile(r"^[EU]Q[A-Za-z0-9_-]{46}$")
_admin_tokens: dict[str, int] = {}


def _cors(response: web.StreamResponse) -> web.StreamResponse:
    response.headers["Access-Control-Allow-Origin"] = CORS_ORIGIN
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        return _cors(web.Response(status=204))
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        _cors(exc)
        raise
    return _cors(response)


async def _payload(request: web.Request) -> dict:
    if request.method == "GET":
        return dict(request.query)
    try:
        return await request.json()
    except Exception:  # noqa: BLE001 - тело может быть пустым/битым
        return {}


async def _auth(request: web.Request) -> tuple[dict, dict]:
    data = await _payload(request)
    try:
        user = parse_init_data(data.get("init_data", ""), BOT_TOKEN)
    except AuthError as exc:
        raise web.HTTPUnauthorized(text=str(exc))
    conn = request.app["db"]
    await db.upsert_user(conn, user)
    row = await db.get_user(conn, user["id"])
    if row and row["is_banned"]:
        raise web.HTTPForbidden(text="пользователь заблокирован")
    return user, data


def _require_admin(data: dict) -> int:
    admin_id = _admin_tokens.get(data.get("token", ""))
    if not admin_id:
        raise web.HTTPUnauthorized(text="нужен вход в админ-панель")
    return admin_id


async def me(request: web.Request) -> web.Response:
    user, _ = await _auth(request)
    conn = request.app["db"]
    row = await db.get_user(conn, user["id"])
    history = await db.list_payouts(conn, user_id=user["id"])
    accepted = [p for p in history if p["status"] == "accepted"]
    return web.json_response({
        "user": {
            "id": row["user_id"],
            "name": row["nickname"] or row["first_name"] or "Модератор",
            "username": row["username"] or "",
            "photo": row["photo_url"] or user.get("photo_url", ""),
            "role": row["role"],
            "wallet": row["wallet"] or "",
            "balance": round(row["balance"] or 0, 2),
            "earned": round(sum(p["amount"] or 0 for p in accepted), 2),
            "payouts": len(accepted),
            "place": await db.place_of(conn, user["id"]),
            "streak": row["streak"] or 0,
            "referrals": row["referrals"] or 0,
            "join_status": row["join_status"],
            "level": 1 + int(sum(p["amount"] or 0 for p in accepted) // 10),
            "limit_used": 0,
            "limit_total": 250,
            "chart": await db.monthly_chart(conn, user["id"]),
            "is_admin": user["id"] in ADMIN_IDS,
        },
        "tasks": [dict(t) for t in await db.user_tasks(conn, user["id"])],
        "history": [{
            "id": p["id"], "code": p["code"], "amount": p["amount"], "status": p["status"],
            "requested_at": (p["requested_at"] or "")[:16].replace("T", " "),
            "requested_at_iso": p["requested_at"],
        } for p in history],
        "leaders": [dict(row) | {"name": row["name"] or "Модератор", "photo": row["photo_url"] or ""}
                    for row in await db.leaderboard(conn)],
    })


async def set_wallet(request: web.Request) -> web.Response:
    user, data = await _auth(request)
    wallet = (data.get("wallet") or "").strip()
    if not WALLET_RE.match(wallet):
        raise web.HTTPBadRequest(text="неверный формат адреса")
    await db.set_fields(request.app["db"], user["id"], wallet=wallet)
    return web.json_response({"ok": True})


async def set_nick(request: web.Request) -> web.Response:
    user, data = await _auth(request)
    name = (data.get("name") or "").strip()[:24]
    if len(name) < 2:
        raise web.HTTPBadRequest(text="слишком короткое имя")
    await db.set_fields(request.app["db"], user["id"], nickname=name)
    return web.json_response({"ok": True})


def _decode_shot(shot: str) -> bytes | None:
    """Разбирает data-URL со скриншотом (не более ~5 МБ)."""
    if not shot or "," not in shot:
        return None
    header, encoded = shot.split(",", 1)
    if "image/" not in header or len(encoded) > 7_000_000:
        return None
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None


async def create_payout(request: web.Request) -> web.Response:
    user, data = await _auth(request)
    conn, bot = request.app["db"], request.app["bot"]
    code = (data.get("code") or "").strip()[:64]
    link = (data.get("link") or "").strip()[:512]
    wallet = (data.get("wallet") or "").strip()
    if not code:
        raise web.HTTPBadRequest(text="не указан тег смены")
    if not link.startswith(("http://", "https://")):
        raise web.HTTPBadRequest(text="нужна ссылка на отчёт")
    if not WALLET_RE.match(wallet):
        raise web.HTTPBadRequest(text="неверный формат адреса")

    payout_id = await db.create_payout(conn, user["id"], code, link, wallet, None)
    await db.set_fields(conn, user["id"], wallet=wallet)
    shot = _decode_shot(data.get("shot", ""))
    await request.app["notify_admins"](bot, payout_id, user, code, link, wallet, shot)
    return web.json_response({"ok": True, "id": payout_id})


async def chat(request: web.Request) -> web.Response:
    user, data = await _auth(request)
    conn = request.app["db"]
    if request.method == "POST" and data.get("text"):
        await db.add_chat_message(conn, user["id"], str(data["text"])[:500])
    return web.json_response({"messages": [{
        "name": row["name"] or "Модератор", "photo": row["photo_url"] or "",
        "text": row["text"], "at": (row["sent_at"] or "")[11:16],
    } for row in await db.chat_messages(conn)]})


async def promo(request: web.Request) -> web.Response:
    user, data = await _auth(request)
    conn = request.app["db"]
    bonus = await db.use_promo(conn, (data.get("code") or "").strip().upper())
    if bonus is None:
        return web.json_response({"ok": False})
    await db.add_balance(conn, user["id"], bonus)
    return web.json_response({"ok": True, "bonus": bonus})


async def admin_login(request: web.Request) -> web.Response:
    user, data = await _auth(request)
    if user["id"] not in ADMIN_IDS:
        raise web.HTTPForbidden(text="нет прав администратора")
    if not ADMIN_PANEL_PASSWORD:
        raise web.HTTPServiceUnavailable(text="ADMIN_PANEL_PASSWORD не задан")
    if not hmac.compare_digest(str(data.get("password", "")), ADMIN_PANEL_PASSWORD):
        raise web.HTTPUnauthorized(text="неверный пароль")
    token = secrets.token_urlsafe(24)
    _admin_tokens[token] = user["id"]
    return web.json_response({"ok": True, "token": token})


async def admin_payouts(request: web.Request) -> web.Response:
    _, data = await _auth(request)
    _require_admin(data)
    rows = await db.list_payouts(request.app["db"], status=data.get("status", "pending"))
    return web.json_response({"payouts": [{
        "id": p["id"], "user_id": p["user_id"], "code": p["code"], "link": p["link"],
        "wallet": p["wallet"], "status": p["status"],
        "requested_at": (p["requested_at"] or "")[:16].replace("T", " "),
    } for p in rows]})


async def admin_accept(request: web.Request) -> web.Response:
    _, data = await _auth(request)
    admin_id = _require_admin(data)
    conn, bot = request.app["db"], request.app["bot"]
    try:
        amount = float(data.get("amount"))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="неверная сумма")
    payout = await db.get_payout(conn, int(data.get("id", 0)))
    if not payout or payout["status"] != "pending":
        raise web.HTTPBadRequest(text="заявка не найдена или уже обработана")
    result = await request.app["approve_payout"](bot, conn, payout, amount, admin_id)
    return web.json_response({"ok": True, **result})


async def admin_reject(request: web.Request) -> web.Response:
    _, data = await _auth(request)
    admin_id = _require_admin(data)
    conn, bot = request.app["db"], request.app["bot"]
    reason = (data.get("reason") or "").strip()[:200]
    if not reason:
        raise web.HTTPBadRequest(text="нужна причина")
    payout = await db.get_payout(conn, int(data.get("id", 0)))
    if not payout or payout["status"] != "pending":
        raise web.HTTPBadRequest(text="заявка не найдена или уже обработана")
    await request.app["reject_payout"](bot, conn, payout, reason, admin_id)
    return web.json_response({"ok": True})


async def admin_broadcast(request: web.Request) -> web.Response:
    _, data = await _auth(request)
    _require_admin(data)
    text = (data.get("text") or "").strip()
    if not text:
        raise web.HTTPBadRequest(text="пустой текст")
    sent = await request.app["broadcast"](request.app["bot"], request.app["db"], text)
    return web.json_response({"ok": True, "sent": sent})


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "payout_wallet_configured": bool(PAYOUT_WALLET)})


def setup_routes(app: web.Application) -> None:
    app.add_routes([
        web.get("/api/health", health),
        web.get("/api/me", me),
        web.post("/api/me", me),
        web.post("/api/wallet", set_wallet),
        web.post("/api/nick", set_nick),
        web.post("/api/payout", create_payout),
        web.get("/api/chat", chat),
        web.post("/api/chat", chat),
        web.post("/api/promo", promo),
        web.post("/api/admin/login", admin_login),
        web.post("/api/admin/payouts", admin_payouts),
        web.post("/api/admin/accept", admin_accept),
        web.post("/api/admin/reject", admin_reject),
        web.post("/api/admin/broadcast", admin_broadcast),
    ])


__all__ = ["build_payout_link", "cors_middleware", "setup_routes"]
