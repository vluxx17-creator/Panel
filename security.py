"""Проверка подписи Telegram WebApp initData."""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

MAX_AGE_SECONDS = 24 * 3600


class AuthError(Exception):
    """initData не прошла проверку подписи."""


def parse_init_data(init_data: str, bot_token: str, *, max_age: int = MAX_AGE_SECONDS) -> dict:
    """Возвращает данные пользователя из initData, проверив HMAC-подпись Telegram."""
    if not init_data:
        raise AuthError("init_data отсутствует")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    if not received_hash:
        raise AuthError("нет hash")

    check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise AuthError("подпись неверна")

    auth_date = int(pairs.get("auth_date", "0") or 0)
    if max_age and auth_date and (time.time() - auth_date) > max_age:
        raise AuthError("initData устарела")

    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError as exc:
        raise AuthError("некорректное поле user") from exc
    if not user.get("id"):
        raise AuthError("нет user.id")
    return user
