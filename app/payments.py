"""Выплаты в TON.

В проекте намеренно нет приватных ключей и сид-фраз: сервер формирует ссылку
на перевод, которую администратор подписывает в своём кошельке (Tonkeeper и др.).
Так подтверждение выплаты остаётся ручным действием владельца средств.
"""
from urllib.parse import quote

from app.config import PAYOUT_LINK_TEMPLATE

NANO = 1_000_000_000


def build_payout_link(wallet: str, amount_ton: float, comment: str = "") -> str:
    """Ссылка на подпись перевода `amount_ton` TON на адрес `wallet`."""
    if not wallet:
        raise ValueError("не указан адрес получателя")
    if amount_ton <= 0:
        raise ValueError("сумма должна быть больше нуля")
    return PAYOUT_LINK_TEMPLATE.format(
        wallet=quote(wallet, safe=""),
        nano=round(amount_ton * NANO),
        comment=quote(comment, safe=""),
    )
