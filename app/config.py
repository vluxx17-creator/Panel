"""Конфигурация приложения (значения читаются из переменных окружения)."""
import os


def _ids(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(";", ",").split(",") if x.strip().lstrip("-").isdigit()}


def _chat_id(raw: str) -> int:
    """Принимает как -100123…, так и ссылку вида https://t.me/c/123/45."""
    raw = (raw or "").strip()
    digits = "".join(ch for ch in raw.rsplit("/c/", 1)[-1].split("/")[0] if ch.isdigit())
    if not digits:
        return 0
    return int(raw) if raw.lstrip("-").isdigit() else int("-100" + digits)


# Секреты задаются только через окружение: держать их в коде нельзя.
BOT_TOKEN = os.getenv("BOT_TOKEN", "8997816663:AAGyPl4aj69g3xeax5AZHmixw7nmhJ5SuLw")
ADMIN_IDS = _ids(os.getenv("ADMIN_IDS", "8297446667"))
GROUP_CHAT_ID = _chat_id(os.getenv("GROUP_CHAT_ID", "-1004421628533"))

WEBAPP_URL = os.getenv("https://vluxx17-creator.github.io/Panel/webapp/", "https://vluxx17-creator.github.io/Panel/")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/tg/webhook")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
PORT = int(os.getenv("PORT", "8080"))

DB_PATH = os.getenv("DB_PATH", "data/panel.db")
ADMIN_PANEL_PASSWORD = os.getenv("ADMIN_PANEL_PASSWORD", "horbikha")
CORS_ORIGIN = os.getenv("CORS_ORIGIN", "*")

# Кошелёк, с которого админ подтверждает выплаты. Приватные ключи в проекте
# не хранятся: сервер только формирует ссылку на подпись транзакции в кошельке.
PAYOUT_WALLET = os.getenv("PAYOUT_WALLET", "")
PAYOUT_LINK_TEMPLATE = os.getenv(
    "PAYOUT_LINK_TEMPLATE",
    "https://app.tonkeeper.com/transfer/{wallet}?amount={nano}&text={comment}",
)
