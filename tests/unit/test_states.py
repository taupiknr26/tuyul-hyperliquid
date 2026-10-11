"""State machines (PRD §12.2 - §12.4, §13.3; NFR-017: every transition covered)."""

import pytest

from hlbot.domain import states
from hlbot.domain.states import IllegalTransition, can_transition, check_transition, map_exchange_status

# The transitions drawn in the PRD diagrams, written out independently of the implementation.
PRD_SIGNAL = {
    ("RECEIVED", "PARSED"), ("PARSED", "INVALID"), ("PARSED", "NOT_A_SIGNAL"), ("PARSED", "PLANNED"),
    ("PLANNED", "WAITING_PRICE"), ("WAITING_PRICE", "EXPIRED"), ("WAITING_PRICE", "DUPLICATE"),
    ("WAITING_PRICE", "REJECTED_RISK"), ("WAITING_PRICE", "ENTERING"), ("ENTERING", "TRADED"),
    ("ENTERING", "WAITING_PRICE"), ("ENTERING", "FAILED"),
}
PRD_ORDER = {
    ("INTENT", "SUBMITTING"), ("SUBMITTING", "RESTING"), ("SUBMITTING", "FILLED"), ("SUBMITTING", "PARTIALLY_FILLED"),
    ("SUBMITTING", "REJECTED"), ("SUBMITTING", "UNKNOWN"), ("UNKNOWN", "RESTING"), ("UNKNOWN", "FILLED"),
    ("UNKNOWN", "PARTIALLY_FILLED"), ("UNKNOWN", "REJECTED"), ("UNKNOWN", "NOT_ACCEPTED"), ("UNKNOWN", "UNRECONCILED"),
    ("RESTING", "TRIGGERED"), ("TRIGGERED", "FILLED"), ("TRIGGERED", "PARTIALLY_FILLED"), ("RESTING", "PARTIALLY_FILLED"),
    ("PARTIALLY_FILLED", "FILLED"), ("PARTIALLY_FILLED", "CANCELED"), ("RESTING", "CANCEL_REQUESTED"),
    ("CANCEL_REQUESTED", "CANCELED"), ("CANCEL_REQUESTED", "FILLED"), ("RESTING", "CANCELED"),
    ("RESTING", "MODIFY_REQUESTED"), ("MODIFY_REQUESTED", "RESTING"), ("UNRECONCILED", "RESTING"),
    ("UNRECONCILED", "CANCELED"),
}
# Documented extensions (see the module docstring of hlbot.domain.states).
ORDER_EXTENSIONS = {
    ("RESTING", "FILLED"), ("UNKNOWN", "CANCELED"), ("MODIFY_REQUESTED", "FILLED"), ("MODIFY_REQUESTED", "PARTIALLY_FILLED"),
    ("MODIFY_REQUESTED", "CANCELED"), ("MODIFY_REQUESTED", "UNKNOWN"), ("CANCEL_REQUESTED", "PARTIALLY_FILLED"),
    ("CANCEL_REQUESTED", "RESTING"), ("INTENT", "NOT_ACCEPTED"),
}
PRD_TRADE = {
    ("ENTERING", "ABORTED"), ("ENTERING", "OPEN_UNPROTECTED"), ("OPEN_UNPROTECTED", "PROTECTED"),
    ("OPEN_UNPROTECTED", "FLATTENING"), ("PROTECTED", "REDUCING"), ("REDUCING", "REDUCING"),
    ("PROTECTED", "OPEN_UNPROTECTED"), ("REDUCING", "OPEN_UNPROTECTED"), ("PROTECTED", "CLOSING"),
    ("REDUCING", "CLOSING"), ("FLATTENING", "CLOSING"), ("FLATTENING", "HALTED_MANUAL"), ("CLOSING", "CLOSED"),
    ("PROTECTED", "UNRECONCILED"), ("REDUCING", "UNRECONCILED"), ("UNRECONCILED", "PROTECTED"),
    ("UNRECONCILED", "CLOSING"), ("HALTED_MANUAL", "CLOSED"),
}


def pairs(table):
    return {(a, b) for a, targets in table.items() for b in targets}


def test_signal_machine_is_exactly_the_prd_diagram():
    assert pairs(states.SIGNAL_TRANSITIONS) == PRD_SIGNAL


def test_order_machine_is_the_prd_diagram_plus_documented_extensions():
    assert pairs(states.ORDER_TRANSITIONS) == PRD_ORDER | ORDER_EXTENSIONS


def test_trade_machine_is_exactly_the_prd_diagram():
    assert pairs(states.TRADE_TRANSITIONS) == PRD_TRADE


@pytest.mark.parametrize("machine, table", [("signal", PRD_SIGNAL), ("order", PRD_ORDER | ORDER_EXTENSIONS), ("trade", PRD_TRADE)])
def test_every_listed_transition_is_accepted(machine, table):
    for current, new in table:
        check_transition(machine, current, new)
        assert can_transition(machine, current, new)


def test_terminal_states_have_no_exit():
    assert states.SIGNAL_TERMINAL == {"INVALID", "NOT_A_SIGNAL", "EXPIRED", "DUPLICATE", "REJECTED_RISK", "TRADED", "FAILED"}
    assert states.ORDER_TERMINAL == {"FILLED", "CANCELED", "REJECTED", "NOT_ACCEPTED"}
    assert states.TRADE_TERMINAL == {"ABORTED", "CLOSED"}
    for machine, terminal in (("signal", states.SIGNAL_TERMINAL), ("order", states.ORDER_TERMINAL), ("trade", states.TRADE_TERMINAL)):
        for state in terminal:
            for other in ("RESTING", "FILLED", "PROTECTED", "WAITING_PRICE"):
                assert not can_transition(machine, state, other)


def test_illegal_and_unknown_transitions_raise():
    with pytest.raises(IllegalTransition):
        check_transition("order", "INTENT", "FILLED")  # nothing fills before it is sent
    with pytest.raises(IllegalTransition):
        check_transition("trade", "OPEN_UNPROTECTED", "CLOSED")
    with pytest.raises(IllegalTransition):
        check_transition("trade", "NOPE", "CLOSED")
    assert not can_transition("order", "FILLED", "CANCELED")


def test_an_ioc_never_rests_in_practice():
    # §12.3 rule: an IOC's terminal states are FILLED, CANCELED (after a partial fill), REJECTED, NOT_ACCEPTED.
    assert {"FILLED", "PARTIALLY_FILLED", "REJECTED", "UNKNOWN"} <= states.ORDER_TRANSITIONS["SUBMITTING"]
    assert "CANCELED" in states.ORDER_TRANSITIONS["PARTIALLY_FILLED"]


@pytest.mark.parametrize(
    "status, local",
    [
        ("open", "RESTING"), ("filled", "FILLED"), ("triggered", "TRIGGERED"), ("canceled", "CANCELED"),
        ("siblingFilledCanceled", "CANCELED"), ("reduceOnlyCanceled", "CANCELED"), ("marginCanceled", "CANCELED"),
        ("liquidatedCanceled", "CANCELED"), ("delistedCanceled", "CANCELED"), ("scheduledCancel", "CANCELED"),
        ("selfTradeCanceled", "CANCELED"), ("openInterestCapCanceled", "CANCELED"), ("vaultWithdrawalCanceled", "CANCELED"),
        ("rejected", "REJECTED"), ("tickRejected", "REJECTED"), ("minTradeNtlRejected", "REJECTED"),
        ("perpMarginRejected", "REJECTED"), ("reduceOnlyRejected", "REJECTED"), ("badAloPxRejected", "REJECTED"),
        ("iocCancelRejected", "REJECTED"), ("badTriggerPxRejected", "REJECTED"), ("marketOrderNoLiquidityRejected", "REJECTED"),
        ("positionIncreaseAtOpenInterestCapRejected", "REJECTED"), ("oracleRejected", "REJECTED"),
        ("perpMaxPositionRejected", "REJECTED"), ("insufficientSpotBalanceRejected", "REJECTED"),
    ],
)
def test_exchange_status_mapping_13_3(status, local):
    assert map_exchange_status(status) == local


def test_unmapped_status_is_an_error_not_a_guess():
    with pytest.raises(ValueError):
        map_exchange_status("somethingNew")
    with pytest.raises(ValueError):
        map_exchange_status("unknownOid")


def test_cancel_reasons_that_need_an_alert():
    assert {"marginCanceled", "liquidatedCanceled", "delistedCanceled", "scheduledCancel"} <= states.CANCEL_REASON_ALERT
    assert {"canceled", "siblingFilledCanceled", "reduceOnlyCanceled"} == states.CANCEL_REASON_EXPECTED
