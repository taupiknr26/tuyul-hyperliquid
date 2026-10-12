"""Exchange error text -> error class (PRD §13.4).

Matching is on stable fragments of the official error strings, lower
cased. Anything unrecognised is ``OTHER`` and is treated by callers as a
terminal rejection of that one request, never as success.
"""

from __future__ import annotations

from hlbot.exchange.base import ErrorClass, ExchangeError

_RULES: tuple[tuple[ErrorClass, tuple[str, ...]], ...] = (
    (ErrorClass.NO_MATCH, ("could not immediately match", "ioccancel")),
    (
        ErrorClass.LOCAL_BUG,
        (
            "divisible by tick size",
            "minimum value of $10",
            "minimum value of 10",
            "mintradentl",
            "tickrejected",
            "invalid size",
            "has too many decimals",
        ),
    ),
    (ErrorClass.INSUFFICIENT_MARGIN, ("insufficient margin", "perpmargin")),
    (ErrorClass.REDUCE_ONLY, ("reduce only order would increase", "reduceonlyrejected")),
    (ErrorClass.INVALID_TPSL, ("invalid tp/sl price", "badtriggerpx")),
    (ErrorClass.NO_LIQUIDITY, ("no liquidity available", "marketordernoliquidity")),
    (ErrorClass.OI_CAP, ("open interest", "openinterest")),
    (ErrorClass.ORACLE_FAR, ("too far from oracle", "oraclerejected")),
    (ErrorClass.MARGIN_TIER, ("margin tier limit", "perpmaxposition")),
    (ErrorClass.NOT_FOUND, ("never placed, already canceled, or filled",)),
    (
        ErrorClass.AUTH,
        (
            "does not exist",
            "must deposit before performing actions",
            "api wallet",
            "invalid signature",
            "not authorized",
        ),
    ),
    (ErrorClass.RATE_LIMIT, ("rate limit", "too many", "429")),
    (ErrorClass.EXPIRED, ("expired", "expiresafter")),
)


def classify_error(text: str) -> ErrorClass:
    lowered = (text or "").lower()
    for kind, fragments in _RULES:
        if any(fragment in lowered for fragment in fragments):
            return kind
    return ErrorClass.OTHER


def to_error(text: str) -> ExchangeError:
    return ExchangeError(classify_error(text), text)


# What each class means when it comes back for an ENTRY order.
ENTRY_WAIT = frozenset(
    {ErrorClass.NO_MATCH, ErrorClass.NO_LIQUIDITY, ErrorClass.ORACLE_FAR, ErrorClass.EXPIRED}
)
ENTRY_REJECT_RISK = frozenset({ErrorClass.INSUFFICIENT_MARGIN, ErrorClass.MARGIN_TIER})
ENTRY_BLOCK_ASSET = frozenset({ErrorClass.OI_CAP})
