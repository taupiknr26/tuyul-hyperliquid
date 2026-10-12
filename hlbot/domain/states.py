"""Signal, order and trade state machines (PRD §12.2 - §12.4).

The transition tables are the single place that says which state change
is legal. The repository refuses anything else, so a bug cannot rewrite
a terminal state.

Extensions to the PRD diagrams (each is a transition the diagrams imply
but do not draw):

* order ``RESTING -> FILLED``: a resting order filled in one match.
* order ``UNKNOWN -> CANCELED``: lookup by cloid found it cancelled.
* order ``MODIFY_REQUESTED -> FILLED / PARTIALLY_FILLED / CANCELED /
  UNKNOWN``: the order was hit, cancelled or lost while a modify was in
  flight.
* order ``CANCEL_REQUESTED -> PARTIALLY_FILLED / RESTING``: partial fill
  raced the cancel / the cancel was rejected and the order still rests.
* order ``INTENT -> NOT_ACCEPTED``: the intent was abandoned before any
  request left the process (for example the action queue refused it).
"""

from __future__ import annotations


class IllegalTransition(Exception):
    def __init__(self, machine: str, current: str, new: str):
        super().__init__(f"illegal {machine} transition {current} -> {new}")
        self.machine = machine
        self.current = current
        self.new = new


def _table(spec: dict[str, tuple]) -> dict[str, frozenset[str]]:
    return {state: frozenset(targets) for state, targets in spec.items()}


SIGNAL_TRANSITIONS = _table(
    {
        "RECEIVED": ("PARSED",),
        "PARSED": ("INVALID", "NOT_A_SIGNAL", "PLANNED"),
        "PLANNED": ("WAITING_PRICE",),
        "WAITING_PRICE": ("EXPIRED", "DUPLICATE", "REJECTED_RISK", "ENTERING"),
        "ENTERING": ("TRADED", "WAITING_PRICE", "FAILED"),
        "INVALID": (),
        "NOT_A_SIGNAL": (),
        "EXPIRED": (),
        "DUPLICATE": (),
        "REJECTED_RISK": (),
        "TRADED": (),
        "FAILED": (),
    }
)
SIGNAL_TERMINAL = frozenset(s for s, t in SIGNAL_TRANSITIONS.items() if not t)
SIGNAL_ACTED = frozenset({"ENTERING", "TRADED"})

ORDER_TRANSITIONS = _table(
    {
        "INTENT": ("SUBMITTING", "NOT_ACCEPTED"),
        "SUBMITTING": ("RESTING", "FILLED", "PARTIALLY_FILLED", "REJECTED", "UNKNOWN"),
        "UNKNOWN": (
            "RESTING",
            "FILLED",
            "PARTIALLY_FILLED",
            "REJECTED",
            "NOT_ACCEPTED",
            "UNRECONCILED",
            "CANCELED",
        ),
        "RESTING": (
            "TRIGGERED",
            "PARTIALLY_FILLED",
            "FILLED",
            "CANCEL_REQUESTED",
            "CANCELED",
            "MODIFY_REQUESTED",
        ),
        "TRIGGERED": ("FILLED", "PARTIALLY_FILLED"),
        "PARTIALLY_FILLED": ("FILLED", "CANCELED"),
        "CANCEL_REQUESTED": ("CANCELED", "FILLED", "PARTIALLY_FILLED", "RESTING"),
        "MODIFY_REQUESTED": ("RESTING", "FILLED", "PARTIALLY_FILLED", "CANCELED", "UNKNOWN"),
        "UNRECONCILED": ("RESTING", "CANCELED"),
        "FILLED": (),
        "CANCELED": (),
        "REJECTED": (),
        "NOT_ACCEPTED": (),
    }
)
ORDER_TERMINAL = frozenset(s for s, t in ORDER_TRANSITIONS.items() if not t)
ORDER_UNRESOLVED = frozenset({"INTENT", "SUBMITTING", "UNKNOWN"})
ORDER_LIVE = frozenset(
    {"RESTING", "TRIGGERED", "PARTIALLY_FILLED", "CANCEL_REQUESTED", "MODIFY_REQUESTED", "UNRECONCILED"}
)

TRADE_TRANSITIONS = _table(
    {
        "ENTERING": ("ABORTED", "OPEN_UNPROTECTED"),
        "OPEN_UNPROTECTED": ("PROTECTED", "FLATTENING"),
        "PROTECTED": ("REDUCING", "OPEN_UNPROTECTED", "CLOSING", "UNRECONCILED"),
        "REDUCING": ("REDUCING", "OPEN_UNPROTECTED", "CLOSING", "UNRECONCILED"),
        "FLATTENING": ("CLOSING", "HALTED_MANUAL"),
        "CLOSING": ("CLOSED",),
        "UNRECONCILED": ("PROTECTED", "CLOSING"),
        "HALTED_MANUAL": ("CLOSED",),
        "ABORTED": (),
        "CLOSED": (),
    }
)
TRADE_TERMINAL = frozenset(s for s, t in TRADE_TRANSITIONS.items() if not t)
TRADE_OPEN = frozenset(TRADE_TRANSITIONS) - TRADE_TERMINAL
TRADE_HOLDING = TRADE_OPEN - {"ENTERING"}

OUTCOMES = ("WIN_ALL", "WIN_PARTIAL", "LOSS_ALL", "MANUAL", "FLATTENED", "LIQUIDATED")

_MACHINES = {
    "signal": SIGNAL_TRANSITIONS,
    "order": ORDER_TRANSITIONS,
    "trade": TRADE_TRANSITIONS,
}


def check_transition(machine: str, current: str, new: str) -> None:
    table = _MACHINES[machine]
    if current not in table:
        raise IllegalTransition(machine, current, new)
    if new not in table[current]:
        raise IllegalTransition(machine, current, new)


def can_transition(machine: str, current: str, new: str) -> bool:
    table = _MACHINES[machine]
    return current in table and new in table[current]


# Exchange order status -> local order state (PRD §13.3).
_EXCHANGE_CANCELED = frozenset(
    {
        "canceled",
        "siblingFilledCanceled",
        "reduceOnlyCanceled",
        "marginCanceled",
        "liquidatedCanceled",
        "delistedCanceled",
        "scheduledCancel",
        "selfTradeCanceled",
        "openInterestCapCanceled",
        "vaultWithdrawalCanceled",
    }
)


def map_exchange_status(status: str) -> str:
    """Local order state for an exchange order status. ``unknownOid`` is
    not a status of an order and is handled by the resolution procedure."""
    if status == "open":
        return "RESTING"
    if status == "filled":
        return "FILLED"
    if status == "triggered":
        return "TRIGGERED"
    if status in _EXCHANGE_CANCELED:
        return "CANCELED"
    if status == "rejected" or status.endswith("Rejected"):
        return "REJECTED"
    raise ValueError(f"unmapped exchange order status: {status!r}")


# Cancel reasons that need more than bookkeeping (PRD §13.3 "Trade effect").
CANCEL_REASON_EXPECTED = frozenset({"canceled", "siblingFilledCanceled", "reduceOnlyCanceled"})
CANCEL_REASON_ALERT = _EXCHANGE_CANCELED - CANCEL_REASON_EXPECTED
