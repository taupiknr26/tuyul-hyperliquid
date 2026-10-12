"""Analytics, filtered history, the Excel export and market volume, checked
against small data sets whose answers are worked out by hand."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta, timezone
from xml.etree import ElementTree

import aiohttp
import pytest

from hlbot.api import analytics, export
from hlbot.api.market import LOOKBACK, volume_summary
from hlbot.api.xlsx import Sheet, excel_datetime, workbook
from hlbot.db.database import Database
from hlbot.exchange.base import Candle, MarketContext
from hlbot.timeutil import ManualClock
from tests.component.conftest import COIN, START
from tests.component.test_infra import free_port

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
T0 = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)  # a Monday


def iso(at: datetime) -> str:
    return at.isoformat(timespec="milliseconds")


def add_trade(db: Database, trade_id: int, *, coin: str = "BTC", side: str = "BUY", source: str = "alpha", opened: datetime, minutes: float = 30, entry: float = 100.0,
              size: float = 1.0, exits: tuple[tuple[float, float], ...] = (), fee_each: float = 0.05, funding: float = 0.0, stop: float = 98.0,
              tps: tuple[tuple[float, float], ...] = ((104.0, 1.0),), state: str = "CLOSED") -> dict:
    """One trade with its fills, written the way the bot records them: the entry fill, then one fill per partial close.
    ``exits`` are (price, size). The trade's totals are the sums of its fills, as the engine computes them."""
    closed = opened + timedelta(minutes=minutes)
    sign = 1 if side == "BUY" else -1
    db.execute("INSERT INTO messages (channel_id, message_id, channel_ref, received_at, text) VALUES (1, ?, ?, ?, 'test signal')", (trade_id, source, iso(opened)))
    db.execute("INSERT INTO signals (id, channel_id, message_id, channel_ref, content_id, type, symbol, side, state, created_at, updated_at) VALUES (?, 1, ?, ?, ?, 'signal', ?, ?, 'TRADED', ?, ?)",
               (trade_id, trade_id, source, f"{coin}-{side}-{trade_id}", coin, side, iso(opened), iso(opened)))
    fills = [(trade_id * 100, side, entry, size, fee_each, 0.0, opened)]
    realized = 0.0
    for n, (price, part) in enumerate(exits, 1):
        pnl = (price - entry) * part * sign
        realized += pnl
        fills.append((trade_id * 100 + n, "SELL" if side == "BUY" else "BUY", price, part, fee_each, pnl, closed - timedelta(seconds=len(exits) - n)))
    fees = fee_each * len(fills)
    is_closed = state == "CLOSED"
    db.execute(
        "INSERT INTO trades (id, signal_id, attempt, env, coin, side, state, planned_size, filled_size, avg_entry, remaining_size, sl_price, tp_plan, opened_at, closed_at, outcome, "
        "realized_pnl, fees, funding, net_pnl, created_at, updated_at) VALUES (?, ?, 1, 'testnet', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (trade_id, trade_id, coin, side, state, size, size, entry, 0.0 if is_closed else size, stop, json.dumps([{"level": i, "price": p, "size": s} for i, (p, s) in enumerate(tps, 1)]),
         iso(opened), iso(closed) if is_closed else None, ("WIN_ALL" if realized - fees + funding > 0 else "LOSS_ALL") if is_closed else None,
         realized if is_closed else None, fees if is_closed else None, funding if is_closed else None, realized - fees + funding if is_closed else None, iso(opened), iso(closed)))
    for tid, fill_side, price, part, fee, pnl, at in fills:
        db.execute("INSERT INTO fills (tid, trade_id, oid, coin, side, price, size, fee, fee_token, closed_pnl, time, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'USDC', ?, ?, ?)",
                   (tid, trade_id, tid + 5000, coin, fill_side, price, part, fee, pnl, int(at.timestamp() * 1000), iso(at)))
    return {"realized": realized, "fees": fees, "net": realized - fees + funding}


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "a.sqlite3", ManualClock(START))
    database.migrate()
    yield database
    database.close()


def known_history(db: Database) -> None:
    """Six trades with hand-checked results (fee 0.05 per fill):
       1  BUY  BTC alpha  Mon 10:00  +4.00 in two partial closes (0.4 @ 105, 0.6 @ 103.5 + ... see below)
       2  SELL BTC alpha  Mon 12:00  loss
       3  BUY  ETH beta   Tue 09:00  win, with funding
       4  SELL ETH beta   next Mon   loss
       5  BUY  BTC beta   next month win
       6  BUY  BTC alpha  open (no result yet)"""
    add_trade(db, 1, opened=T0, exits=((105.0, 0.4), (104.0, 0.6)), tps=((104.0, 0.6), (106.0, 0.4)))           # realised 2.0 + 2.4 = 4.4; 3 fills -> fees 0.15; net 4.25
    add_trade(db, 2, side="SELL", opened=T0 + timedelta(hours=2), exits=((102.0, 1.0),), stop=102.0, tps=((96.0, 1.0),))  # realised -2.0; fees 0.10; net -2.10
    add_trade(db, 3, coin="ETH", source="beta", opened=T0 + timedelta(hours=23), entry=50.0, size=2.0, exits=((51.0, 2.0),), funding=-0.02, stop=49.0, tps=((52.0, 2.0),))  # 2.0 - 0.10 - 0.02 = 1.88
    add_trade(db, 4, coin="ETH", side="SELL", source="beta", opened=T0 + timedelta(days=7), entry=50.0, size=2.0, exits=((50.5, 2.0),), stop=50.5, tps=((49.0, 2.0),))  # -1.0 - 0.10 = -1.10
    add_trade(db, 5, source="beta", opened=T0 + timedelta(days=31), exits=((101.0, 1.0),))                       # 1.0 - 0.10 = 0.90
    add_trade(db, 6, opened=T0 + timedelta(days=31, hours=1), state="PROTECTED")                                 # open


# ================================================================== key figures
def test_key_figures_match_a_hand_checked_history(db):
    known_history(db)
    result = analytics.analytics(db, analytics.TradeFilter(), ManualClock(START))
    k = result["kpis"]
    assert (k["total_trades"], k["wins"], k["losses"], k["breakeven"], k["open_trades"]) == (5, 3, 2, 0, 1)
    assert k["win_rate"] == 0.6
    assert k["net_pnl"] == pytest.approx(4.25 - 2.10 + 1.88 - 1.10 + 0.90)        # 3.83
    assert k["gross_profit"] == pytest.approx(4.25 + 1.88 + 0.90) and k["gross_loss"] == pytest.approx(-3.20)
    assert k["profit_factor"] == pytest.approx(7.03 / 3.20, abs=1e-4)
    assert k["avg_win"] == pytest.approx(7.03 / 3) and k["avg_loss"] == pytest.approx(-1.60)
    assert k["payoff_ratio"] == pytest.approx((7.03 / 3) / 1.60, abs=1e-4) and k["expectancy"] == pytest.approx(3.83 / 5)
    # costs are part of the result, and are reported on their own as well
    assert k["realized_before_costs"] == pytest.approx(4.4 - 2.0 + 2.0 - 1.0 + 1.0) and k["fees"] == pytest.approx(0.55) and k["funding"] == pytest.approx(-0.02)
    assert k["net_pnl"] == pytest.approx(k["realized_before_costs"] - k["fees"] + k["funding"])
    # cumulative: 4.25, 2.15, 4.03, 2.93, 3.83 -> the largest fall from a peak is 4.25 -> 2.15
    assert [p["value"] for p in result["cumulative"]] == pytest.approx([4.25, 2.15, 4.03, 2.93, 3.83])
    assert k["max_drawdown"] == pytest.approx(2.10) and k["max_drawdown_from"] == result["cumulative"][0]["at"] and k["max_drawdown_to"] == result["cumulative"][1]["at"]
    # planned reward to risk: trade 1 (0.6 x 4 + 0.4 x 6) / 2 = 2.4; 2: 4/2 = 2; 3: 2/1 = 2; 4: 1/0.5 = 2; 5: 4/2 = 2
    assert k["avg_planned_rr"] == pytest.approx((2.4 + 2 + 2 + 2 + 2) / 5)
    # traded value: every fill once. Trade 1: 100 + 42 + 62.4; 2: 100 + 102; 3: 100 + 102; 4: 100 + 101; 5: 100 + 101
    assert k["volume_quote"] == pytest.approx(204.4 + 202 + 202 + 201 + 201)
    assert k["account"] is None and result["equity"] == [] and result["balance"] == []  # no snapshots stored: nothing is invented


def test_partial_closes_and_several_executions_are_counted_once(db):
    add_trade(db, 1, opened=T0, exits=((101.0, 0.25), (102.0, 0.25), (103.0, 0.25), (104.0, 0.25)))  # four partial closes: realised 0.25+0.5+0.75+1.0 = 2.5; 5 fills
    result = analytics.analytics(db, analytics.TradeFilter(), ManualClock(START))
    k = result["kpis"]
    assert (k["total_trades"], k["wins"]) == (1, 1) and k["net_pnl"] == pytest.approx(2.5 - 0.25) and k["fees"] == pytest.approx(0.25)
    assert len(result["cumulative"]) == 1 and result["by_symbol"][0]["trades"] == 1
    rows = analytics.enrich(db, analytics.select_trades(db, analytics.TradeFilter()))
    assert rows[0]["fill_count"] == 5 and rows[0]["exit_price"] == pytest.approx(102.5)  # size-weighted over the four closing fills
    assert rows[0]["volume_quote"] == pytest.approx(100 + 25.25 + 25.5 + 25.75 + 26.0) and rows[0]["duration_s"] == 1800


def test_filters_by_date_symbol_source_side_and_status(db):
    known_history(db)

    def ids(**kwargs):
        return sorted(t["id"] for t in analytics.select_trades(db, analytics.TradeFilter(**kwargs)))

    assert ids() == [1, 2, 3, 4, 5, 6]
    assert ids(symbols=("ETH",)) == [3, 4] and ids(symbols=("BTC", "ETH")) == [1, 2, 3, 4, 5, 6] and ids(symbols=("SOL",)) == []
    assert ids(sources=("beta",)) == [3, 4, 5] and ids(side="SELL") == [2, 4]
    assert ids(status="closed") == [1, 2, 3, 4, 5] and ids(status="open") == [6]
    assert ids(date_from="2026-03-02", date_to="2026-03-02") == [1, 2]            # a trade belongs to the day it closed
    assert ids(date_from="2026-03-03", date_to="2026-03-08") == [3] and ids(date_from="2026-04-01") == [5, 6] and ids(date_to="2026-03-01") == []
    assert ids(symbols=("BTC",), sources=("alpha",), side="BUY", status="closed") == [1]
    # every figure follows the filter
    eth = analytics.analytics(db, analytics.TradeFilter(symbols=("ETH",)), ManualClock(START))
    assert eth["kpis"]["total_trades"] == 2 and eth["kpis"]["net_pnl"] == pytest.approx(0.78) and [g["key"] for g in eth["by_symbol"]] == ["ETH"]
    assert eth["matched_trades"] == 2 and [g["key"] for g in eth["by_source"]] == ["beta"]
    nothing = analytics.analytics(db, analytics.TradeFilter(symbols=("SOL",)), ManualClock(START))
    k = nothing["kpis"]
    assert k["total_trades"] == 0 and k["win_rate"] is None and k["profit_factor"] is None and k["avg_win"] is None and k["max_drawdown"] == 0
    assert nothing["cumulative"] == [] and nothing["distribution"] == [] and nothing["periods"] == {"day": [], "week": [], "month": []}
    assert nothing["options"]["symbols"] == ["BTC", "ETH"] and nothing["options"]["sources"] == ["alpha", "beta"]  # the choices stay, whatever is selected


def test_periods_breakdowns_and_time_zones(db):
    known_history(db)
    result = analytics.analytics(db, analytics.TradeFilter(), ManualClock(START))
    days = {g["key"]: (g["trades"], round(g["net"], 2)) for g in result["periods"]["day"]}
    assert days == {"2026-03-02": (2, 2.15), "2026-03-03": (1, 1.88), "2026-03-09": (1, -1.10), "2026-04-02": (1, 0.90)}
    assert [(g["key"], g["trades"]) for g in result["periods"]["week"]] == [("2026-03-02", 3), ("2026-03-09", 1), ("2026-03-30", 1)]  # weeks start on Monday
    assert [(g["key"], g["trades"], round(g["net"], 2)) for g in result["periods"]["month"]] == [("2026-03", 4, 2.93), ("2026-04", 1, 0.90)]
    assert {g["key"]: (g["trades"], g["wins"], g["losses"], g["win_rate"], round(g["net"], 2)) for g in result["by_symbol"]} == {
        "BTC": (3, 2, 1, round(2 / 3, 4), 3.05), "ETH": (2, 1, 1, 0.5, 0.78)}
    assert {g["key"]: g["trades"] for g in result["by_source"]} == {"alpha": 2, "beta": 3} and {g["key"]: g["trades"] for g in result["by_side"]} == {"BUY": 3, "SELL": 2}
    assert sum(b["count"] for b in result["distribution"]) == 5 and any(b["from"] == 0 for b in result["distribution"])  # zero is a bin edge
    assert all(b["to"] <= 0 or b["from"] >= 0 for b in result["distribution"])
    assert {o["key"]: o["trades"] for o in result["outcomes"]} == {"WIN_ALL": 3, "LOSS_ALL": 2}
    # Trade 3 closed Tuesday 09:30 UTC. In UTC-10:00 that is still Monday, in UTC+14:00 it is Tuesday; the filter follows the viewer's day.
    west = analytics.TradeFilter(tz_minutes=-600)
    assert {g["key"]: g["trades"] for g in analytics.analytics(db, west, ManualClock(START))["periods"]["day"]}["2026-03-02"] == 3
    assert sorted(t["id"] for t in analytics.select_trades(db, analytics.TradeFilter(date_from="2026-03-02", date_to="2026-03-02", tz_minutes=-600))) == [1, 2, 3]
    assert analytics.TradeFilter(date_from="2026-03-02", date_to="2026-03-02", tz_minutes=420).bounds_utc() == ("2026-03-01T17:00:00.000+00:00", "2026-03-02T17:00:00.000+00:00")
    assert analytics.TradeFilter(tz_minutes=420).tz_label == "UTC+07:00" and analytics.TradeFilter(tz_minutes=-210).tz_label == "UTC-03:30"


def test_equity_and_derived_balance_come_from_the_stored_snapshots(db):
    known_history(db)
    for minutes, value in ((-60, 1000.0), (60, 1004.0), (60 * 30, 1003.5), (60 * 24 * 40, 1004.9)):
        at = iso(T0 + timedelta(minutes=minutes))
        db.execute("INSERT INTO account_snapshots (env, dex, account_value, margin_used, withdrawable, snapshot_at) VALUES ('testnet', '', ?, 5, ?, ?)", (value, value - 5, at))
        db.execute("INSERT INTO account_snapshots (env, dex, account_value, margin_used, withdrawable, snapshot_at) VALUES ('testnet', 'other', 777, 0, 777, ?)", (at,))
    result = analytics.analytics(db, analytics.TradeFilter(), ManualClock(START), dexes=[""])
    assert [p["value"] for p in result["equity"]] == [1000.0, 1004.0, 1003.5, 1004.9]  # the unmanaged dex is left out
    assert result["kpis"]["account"] == {"at": iso(T0 + timedelta(days=40)), "equity": 1004.9, "withdrawable": 999.9, "margin_used": 5.0}
    assert [round(p["value"], 2) for p in result["balance"]] == [1000.0, 1004.25, 1002.15, 1004.03, 1002.93, 1003.83, 1003.83]  # first equity + each closed trade's net,
    assert result["balance"][-1]["at"] == result["equity"][-1]["at"]                                                              # then level to the latest snapshot
    assert result["cumulative_start"] == {"at": iso(T0), "value": 0.0}  # the cumulative line starts at zero when the first trade opened
    assert result["kpis"]["max_drawdown_pct"] == pytest.approx(2.10 / (1000.0 + 4.25), abs=1e-6)
    # the account lines ignore the symbol filter (an account has one balance); the trade figures do not
    eth = analytics.analytics(db, analytics.TradeFilter(symbols=("ETH",)), ManualClock(START), dexes=[""])
    assert eth["equity"] == result["equity"] and eth["balance"] == result["balance"] and eth["kpis"]["total_trades"] == 2


def test_filter_validation_names_what_is_wrong():
    ok = analytics.parse_filter({"from": "2026-03-02", "to": "2026-03-09", "tz": "420", "symbol": "BTC, ETH,BTC", "source": "alpha", "side": "buy", "status": "Closed"})
    assert ok == analytics.TradeFilter("2026-03-02", "2026-03-09", 420, ("BTC", "ETH"), ("alpha",), "BUY", "closed")
    assert analytics.parse_filter({}) == analytics.TradeFilter()
    for bad, word in (({"from": "02/03/2026"}, "from must be a date"), ({"from": "2026-03-09", "to": "2026-03-02"}, "after the end date"), ({"tz": "x"}, "tz"),
                      ({"tz": "9000"}, "out of range"), ({"side": "LONG"}, "BUY or SELL"), ({"status": "pending"}, "status"), ({"symbol": "X" * 81}, "too many or too long"),
                      ({"from": "2026-02-30"}, "from must be a date")):
        with pytest.raises(ValueError, match=word):
            analytics.parse_filter(bad)
    # values go into the query as parameters, never as text
    assert analytics.parse_filter({"symbol": "BTC'; DROP TABLE trades;--"}).symbols == ("BTC'; DROP TABLE trades;--",)


def test_a_hostile_filter_value_is_data_not_sql(db):
    known_history(db)
    assert analytics.select_trades(db, analytics.parse_filter({"symbol": "BTC' OR '1'='1", "source": "x') OR 1=1 --"})) == []
    assert db.scalar("SELECT COUNT(*) FROM trades") == 6


def test_history_pages_cover_every_trade_exactly_once(db):
    for i in range(1, 46):
        add_trade(db, i, opened=T0 + timedelta(hours=i), exits=((101.0, 1.0),), coin="BTC" if i % 3 else "ETH")
    seen = []
    first = analytics.trades_page(db, analytics.TradeFilter(), 1, 20)
    assert (first["total"], first["pages"], first["page"], len(first["trades"])) == (45, 3, 1, 20) and first["trades"][0]["id"] == 45  # newest first
    for page in (1, 2, 3):
        seen += [t["id"] for t in analytics.trades_page(db, analytics.TradeFilter(), page, 20)["trades"]]
    assert sorted(seen) == list(range(1, 46)) and len(set(seen)) == 45
    assert analytics.trades_page(db, analytics.TradeFilter(), 99, 20)["page"] == 3  # past the end: the last page
    eth = analytics.trades_page(db, analytics.TradeFilter(symbols=("ETH",)), 1, 10)
    assert eth["total"] == 15 and eth["pages"] == 2 and all(t["coin"] == "ETH" for t in eth["trades"]) and "orders" in eth["trades"][0]


# ================================================================== Excel
def read_sheet(book: zipfile.ZipFile, index: int) -> tuple[ElementTree.Element, list[list[tuple[str, str, str]]]]:
    """(root, rows) where each cell is (reference, style index, text or number as text)."""
    root = ElementTree.fromstring(book.read(f"xl/worksheets/sheet{index}.xml"))
    rows = []
    for row in root.find("m:sheetData", NS):
        cells = []
        for cell in row:
            inline = cell.find("m:is/m:t", NS)
            value = inline.text if inline is not None else cell.find("m:v", NS).text
            cells.append((cell.get("r"), cell.get("s", "0"), value or ""))
        rows.append(cells)
    return root, rows


def test_the_workbook_is_a_well_formed_xlsx_with_typed_cells(tmp_path):
    when = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
    rows = [[1, "=SUM(A1:A9)", 12.5, excel_datetime(when), None, 0.5, 1 / 24], [2, 'quote " & <tag> \x07bell', -3.25, None, "", 0.0, None]]
    data = workbook([Sheet("Data", [("ID", "int", 6), ("Text", "text", 20), ("Money", "money", 10), ("When", "datetime", 20), ("Empty", "text", 8), ("Share", "percent", 8),
                                    ("Took", "duration", 10)], rows),
                     Sheet("A name that is far too long for a sheet tab", [("K", "text", 10), ("V", "text", 10)], [["n", (3, "int")], ["p", (0.25, "percent")]], table=False)],
                    title="T & <t>", created=when)
    book = zipfile.ZipFile(io.BytesIO(data))
    assert book.testzip() is None
    assert {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml", "xl/_rels/workbook.xml.rels", "xl/styles.xml", "xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml",
            "docProps/core.xml"} <= set(book.namelist())
    for name in book.namelist():
        ElementTree.fromstring(book.read(name))  # every part is well-formed XML
    names = [s.get("name") for s in ElementTree.fromstring(book.read("xl/workbook.xml")).find("m:sheets", NS)]
    assert names == ["Data", "A name that is far too long for"] and all(len(n) <= 31 for n in names)
    root, cells = read_sheet(book, 1)
    assert [c[2] for c in cells[0]] == ["ID", "Text", "Money", "When", "Empty", "Share", "Took"] and {c[1] for c in cells[0]} == {"1"}  # bold header style
    assert root.find("m:sheetViews/m:sheetView/m:pane", NS).attrib == {"ySplit": "1", "topLeftCell": "A2", "activePane": "bottomLeft", "state": "frozen"}
    assert root.find("m:autoFilter", NS).get("ref") == "A1:G3"
    assert [child.tag.split("}")[1] for child in root] == ["sheetViews", "cols", "sheetData", "autoFilter"]  # the order Excel requires
    first = {c[0]: c for c in cells[1]}
    assert first["B2"][2] == "=SUM(A1:A9)" and "<f>" not in book.read("xl/worksheets/sheet1.xml").decode()  # text, never a formula
    assert float(first["C2"][2]) == 12.5 and float(first["D2"][2]) == pytest.approx(46083 + 10 / 24) and "E2" not in first  # an empty cell is simply absent
    styles = ElementTree.fromstring(book.read("xl/styles.xml"))
    formats = {f.get("numFmtId"): f.get("formatCode") for f in styles.find("m:numFmts", NS)}
    xfs = [x.get("numFmtId") for x in styles.find("m:cellXfs", NS)]
    code = lambda cell: formats.get(xfs[int(cell[1])])  # noqa: E731  the number format a cell is shown with
    assert code(first["A2"]) == "0" and code(first["C2"]) == "#,##0.00;[Red]-#,##0.00" and code(first["D2"]) == "yyyy-mm-dd hh:mm:ss"
    assert code(first["F2"]) == "0.00%" and code(first["G2"]) == "[h]:mm:ss"
    second = {c[0]: c for c in cells[2]}
    assert second["B3"][2] == 'quote " & <tag> bell' and float(second["C3"][2]) == -3.25  # escaped, and the control character is gone
    root2, cells2 = read_sheet(book, 2)
    assert root2.find("m:autoFilter", NS) is None and root2.find("m:sheetViews", NS) is None
    assert code(cells2[1][1]) == "0" and code(cells2[2][1]) == "0.00%"  # a cell may override its column's kind
    # shown in a chosen zone: 10:00 UTC is 17:00 at UTC+07:00
    assert excel_datetime(when, timezone(timedelta(hours=7))) == pytest.approx(46083 + 17 / 24)


def test_export_holds_every_matching_trade_its_executions_and_a_summary(db):
    known_history(db)
    chosen = analytics.parse_filter({"tz": "420"})
    data = analytics.export_data(db, chosen)
    now = datetime(2026, 4, 3, 20, 0, tzinfo=UTC)
    assert export.filename(data, now) == "trade-history-2026-04-04.xlsx"  # the viewer's date: 03:00 the next day at UTC+07:00
    book = zipfile.ZipFile(io.BytesIO(export.build_workbook(data, env_label="TESTNET", generated=now)))
    names = [s.get("name") for s in ElementTree.fromstring(book.read("xl/workbook.xml")).find("m:sheets", NS)]
    assert names == ["Trades", "Executions", "Summary"]
    _, trades = read_sheet(book, 1)
    header = [c[2] for c in trades[0]]
    for column in ("Trade ID", "Signal source", "Symbol", "Direction", "Status", "Entry price", "Exit price", "Entry time (UTC+07:00)", "Exit time (UTC+07:00)", "Duration",
                   "Stop loss", "TP1", "TP5", "Gross P&L", "Fees", "Funding", "Net P&L", "Planned R:R", "Entry order ID"):
        assert column in header, column
    assert len(trades) == 1 + 6 and len({row[0][2] for row in trades[1:]}) == 6  # six trades, none twice
    col = lambda name: chr(65 + header.index(name)) if header.index(name) < 26 else "A" + chr(65 + header.index(name) - 26)  # noqa: E731
    by_id = {row[0][2]: {c[0].rstrip("0123456789"): c[2] for c in row} for row in trades[1:]}
    one = by_id["1"]  # a whole number is written as one
    assert one[col("Symbol")] == "BTC" and one[col("Direction")] == "BUY" and one[col("Signal source")] == "alpha" and one[col("Status")] == "CLOSED"
    assert float(one[col("Entry price")]) == 100.0 and float(one[col("Exit price")]) == pytest.approx(104.4)  # (0.4 x 105 + 0.6 x 104) / 1.0
    assert float(one[col("Gross P&L")]) == pytest.approx(4.4) and float(one[col("Fees")]) == pytest.approx(0.15) and float(one[col("Net P&L")]) == pytest.approx(4.25)
    assert float(one[col("Planned R:R")]) == pytest.approx(2.4) and float(one[col("TP1")]) == 104.0 and float(one[col("TP2")]) == 106.0 and col("TP3") not in one
    assert float(one[col("Entry time (UTC+07:00)")]) == pytest.approx(excel_datetime(T0, timezone(timedelta(hours=7)))) and float(one[col("Duration")]) == pytest.approx(30 / 1440)
    open_trade = by_id["6"]
    assert open_trade[col("Status")] == "PROTECTED" and col("Net P&L") not in open_trade and col("Exit price") not in open_trade  # no result yet: left empty, not zero
    _, fills = read_sheet(book, 2)
    assert len(fills) == 1 + 12 and len({row[0][2] for row in fills[1:]}) == 12  # 3 + 2 + 2 + 2 + 2 + 1 fills, each once
    _, summary = read_sheet(book, 3)
    facts = {row[0][2]: (row[1][2] if len(row) > 1 else "") for row in summary[1:] if row}
    assert facts["Environment"] == "TESTNET" and float(facts["Trades in this file"]) == 6 and float(facts["Closed trades"]) == 5
    assert float(facts["Net P&L"]) == pytest.approx(3.83) and float(facts["Win rate"]) == pytest.approx(0.6) and facts["Symbols"] == "all"
    everything = b"".join(book.read(n) for n in book.namelist()).decode()
    for secret in ("cloid", "0x", "account", "token", "nonce"):
        assert secret not in everything.lower().replace("xmlns", ""), secret

    # a filter narrows the file the same way it narrows the page
    eth = analytics.export_data(db, analytics.parse_filter({"symbol": "ETH", "status": "closed"}))
    _, rows = read_sheet(zipfile.ZipFile(io.BytesIO(export.build_workbook(eth, env_label="TESTNET", generated=now))), 1)
    assert sorted(row[0][2] for row in rows[1:]) == ["3", "4"]
    # nothing matches: a valid workbook with headers only
    empty = analytics.export_data(db, analytics.parse_filter({"symbol": "SOL"}))
    book = zipfile.ZipFile(io.BytesIO(export.build_workbook(empty, env_label="TESTNET", generated=now)))
    root, rows = read_sheet(book, 1)
    assert len(rows) == 1 and root.find("m:autoFilter", NS).get("ref").endswith("1")


def test_the_workbook_opens_in_a_spreadsheet_library(db):
    """Read back with openpyxl where it is installed (it is not a dependency of the bot)."""
    openpyxl = pytest.importorskip("openpyxl")
    known_history(db)
    data = analytics.export_data(db, analytics.parse_filter({}))
    book = openpyxl.load_workbook(io.BytesIO(export.build_workbook(data, env_label="TESTNET", generated=datetime(2026, 4, 3, tzinfo=UTC))))
    assert book.sheetnames == ["Trades", "Executions", "Summary"]
    sheet = book["Trades"]
    assert sheet.max_row == 7 and sheet.freeze_panes == "A2" and sheet.auto_filter.ref.startswith("A1:")
    header = [c.value for c in sheet[1]]
    row = {h: c.value for h, c in zip(header, sheet[2], strict=False)}
    assert row["Trade ID"] in (5, 6) and isinstance(row["Entry time (UTC+00:00)"], datetime) and isinstance(row["Entry price"], (int, float))


# ================================================================== market volume
def candles(volumes: list[float], *, interval_ms: int = 3_600_000, end_ms: int = 1_800_000_000_000, price: float = 100.0, trades: int = 50) -> list[Candle]:
    """Hourly candles ending with one that is still forming at ``end_ms`` (30 minutes into it)."""
    last_open = end_ms - interval_ms // 2
    return [Candle("BTC", "1h", last_open - (len(volumes) - 1 - i) * interval_ms, last_open - (len(volumes) - 1 - i) * interval_ms + interval_ms - 1, price, price + 2, price - 1, price + 1,
                   v, trades + i) for i, v in enumerate(volumes)]


def test_volume_summary_separates_base_quote_and_tick_volume():
    now = 1_800_000_000_000
    s = volume_summary(candles([10.0] * 19 + [30.0, 8.0]), now, "1h")  # 19 quiet hours, one busy closed hour, then the hour still forming
    assert s["current"]["volume_base"] == 8.0 and s["current"]["elapsed_pct"] == 50.0 and s["current"]["trades"] == 70
    assert s["last_closed"]["volume_base"] == 30.0 and s["average"]["candles"] == 20 and s["average"]["volume_base"] == pytest.approx(11.0)
    assert s["current"]["volume_quote_est"] == pytest.approx(8.0 * (102 + 99 + 101) / 3, abs=0.01)  # an estimate: base x typical price
    assert s["vs_average_pct"] == pytest.approx((30 / 11 - 1) * 100, abs=0.1) and s["level"] == "unusually_high"
    assert s["trend"] == "rising" and s["change_pct"] == 200.0
    assert s["pace_vs_average_pct"] == pytest.approx((16 / 11 - 1) * 100, abs=0.1)  # 8 in half an hour is a pace of 16
    quiet = volume_summary(candles([10.0] * 19 + [4.0, 0.1]), now, "1h")
    assert quiet["level"] == "unusually_low" and quiet["trend"] == "falling" and quiet["vs_average_pct"] < -50
    flat = volume_summary(candles([10.0] * 21), now, "1h")
    assert flat["level"] == "normal" and flat["trend"] == "flat" and flat["vs_average_pct"] == 0.0
    # only the latest LOOKBACK closed candles count, and a candle that has just opened is not extrapolated
    assert volume_summary(candles([1000.0] * 5 + [10.0] * LOOKBACK + [1.0]), now, "1h")["average"]["volume_base"] == pytest.approx(10.0)
    just_opened = candles([10.0] * 21)
    assert volume_summary(just_opened, just_opened[-1].open_ms + 60_000, "1h")["pace_vs_average_pct"] is None
    # missing data: nothing is invented
    assert volume_summary([], now, "1h") == {"interval": "1h", "current": None, "last_closed": None, "average": None, "vs_average_pct": None, "pace_vs_average_pct": None,
                                             "trend": None, "level": None}
    only_forming = volume_summary(candles([5.0]), now, "1h")
    assert only_forming["current"]["volume_base"] == 5.0 and only_forming["average"] is None and only_forming["level"] is None
    zero = volume_summary(candles([0.0] * 21), now, "1h")
    assert zero["vs_average_pct"] is None and zero["level"] is None and zero["trend"] is None  # no division by a zero average


async def test_volume_history_export_and_analytics_over_the_api(make):
    """Each symbol gets its own figures, a failed refresh keeps the last good ones and says so,
    and the new endpoints follow the sign-in rules."""
    port = free_port()
    h = await make(api_enabled=True, api_port=port)
    await h.open_protected_trade()
    await h.app.reconciler.run_once("test")
    base = f"http://127.0.0.1:{port}"
    token = {"X-Control-Token": h.settings.control_token_path.read_text()}
    now = h.clock.now_ms()
    h.sim.candles[(COIN, "1h")] = candles([10.0] * 19 + [30.0, 8.0], end_ms=now)
    h.sim.candles[(COIN, "15m")] = candles([2.0] * 21, interval_ms=900_000, end_ms=now)
    h.sim.contexts[COIN] = MarketContext(coin=COIN, mark=4287.0, oracle=4287.0, received_ms=now, day_volume_base=123.5, day_volume_quote=529000.0, prev_day_price=4200.0)
    h.app.engine.contexts[COIN] = h.sim.contexts[COIN]

    async with aiohttp.ClientSession() as http:
        for path in ("/api/market/volume", "/api/analytics", "/api/trades", "/api/export/trades.xlsx"):
            async with http.get(base + path) as response:
                assert response.status == 401, path  # nothing without a sign-in or the token
        async with http.get(f"{base}/api/market/volume", headers=token) as response:
            volume = await response.json()
        one = volume["symbols"][COIN]
        assert volume["interval"] == "1h" and one["last_closed"]["volume_base"] == 30.0 and one["level"] == "unusually_high" and one["base_unit"] == "GOLD"
        assert one["day"] == {"volume_base": 123.5, "volume_quote": 529000.0, "age_ms": 0} and one["stale"] is False and one["error"] is None
        async with http.get(f"{base}/api/market/volume", params={"interval": "15m"}, headers=token) as response:
            assert (await response.json())["symbols"][COIN]["average"]["volume_base"] == 2.0  # the figures belong to the interval that was asked for
        async with http.get(f"{base}/api/market/volume", params={"interval": "7m"}, headers=token) as response:
            assert response.status == 400 and "5m, 15m, 1h, 4h, 1d" in (await response.json())["error"]
        # cached: asking again does not ask the exchange again
        before = h.sim.candle_requests
        for _ in range(5):
            async with http.get(f"{base}/api/market/volume", headers=token) as response:
                assert response.status == 200
        assert h.sim.candle_requests == before
        # the exchange fails after the cache has expired: the last good figures stay, marked stale with the reason
        h.clock.advance(120, monotonic=False)
        h.sim.faults.rest_down = True
        async with http.get(f"{base}/api/market/volume", headers=token) as response:
            failed = (await response.json())["symbols"][COIN]
        assert failed["stale"] is True and "REST outage" in failed["error"] and failed["last_closed"]["volume_base"] == 30.0
        assert h.sim.candle_requests == before  # (the simulated outage refuses before counting; a real read is tried twice)
        h.sim.faults.rest_down = False

        # one lost answer is simply asked again: the viewer sees figures, not an error
        from hlbot.exchange.base import OutcomeUnknown

        real, calls = h.sim.get_candles, []

        async def flaky(coin, interval, count):
            calls.append(interval)
            if len(calls) == 1:
                raise OutcomeUnknown("ReadTimeout")
            return await real(coin, interval, count)

        h.sim.get_candles = flaky
        h.clock.advance(120, monotonic=False)
        async with http.get(f"{base}/api/market/volume", headers=token) as response:
            again = (await response.json())["symbols"][COIN]
        assert calls == ["1h", "1h"] and again["error"] is None and again["stale"] is False

        async def dead(coin, interval, count):
            raise OutcomeUnknown("ReadTimeout")

        h.sim.get_candles = dead
        h.clock.advance(120, monotonic=False)
        async with http.get(f"{base}/api/market/volume", headers=token) as response:
            assert (await response.json())["symbols"][COIN]["error"] == "the exchange did not answer in time"
        h.sim.get_candles = real

        # status carries the exact 24-hour volume with the quotes
        async with http.get(f"{base}/api/status", headers=token) as response:
            quote = (await response.json())["live"]["quotes"][COIN]
        assert quote["day_volume_base"] == 123.5 and quote["day_volume_quote"] == 529000.0 and quote["prev_day_price"] == 4200.0

        # history, analytics and export
        async with http.get(f"{base}/api/trades", params={"page_size": "10"}, headers=token) as response:
            page = await response.json()
        assert page["total"] == 1 and page["trades"][0]["state"] == "PROTECTED" and len(page["trades"][0]["orders"]) == 7 and page["options"]["symbols"] == [COIN]
        async with http.get(f"{base}/api/trades", params={"page_size": "7"}, headers=token) as response:
            assert response.status == 400
        async with http.get(f"{base}/api/analytics", params={"side": "SIDEWAYS"}, headers=token) as response:
            assert response.status == 400 and "BUY or SELL" in (await response.json())["error"]
        async with http.get(f"{base}/api/analytics", params={"tz": "420"}, headers=token) as response:
            result = await response.json()
        assert result["kpis"]["total_trades"] == 0 and result["kpis"]["open_trades"] == 1 and result["filter"]["tz"] == "UTC+07:00" and result["kpis"]["account"]["equity"] > 0
        async with http.get(f"{base}/api/export/trades.xlsx", params={"tz": "420", "status": "open"}, headers=token) as response:
            body = await response.read()
            assert response.status == 200 and response.content_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            assert response.headers["Content-Disposition"].startswith('attachment; filename="trade-history-') and response.headers["X-Export-Rows"] == "1"
        book = zipfile.ZipFile(io.BytesIO(body))
        _, rows = read_sheet(book, 1)
        parts = b"".join(book.read(n) for n in book.namelist()).decode()
        assert len(rows) == 2 and token["X-Control-Token"] not in parts and "cloid" not in parts
        async with http.get(f"{base}/api/export/trades.xlsx", params={"from": "tomorrow"}, headers=token) as response:
            assert response.status == 400
    audit = json.loads(h.audit("export.trades")[-1]["payload"])
    assert audit["by"] == "local" and audit["trades"] == 1 and audit["filter"]["status"] == "open"
