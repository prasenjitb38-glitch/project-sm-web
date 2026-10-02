"""Virtual paper trades, strategy storage, and condition backtests.

Nothing in this module sends an order to a broker. Backtests read only the
candles up to the decision bar. A stop and a target on the same candle are
booked as a stop.
"""
from datetime import datetime, timezone
from threading import Lock
from uuid import uuid4
from zoneinfo import ZoneInfo
import json
import sqlite3

import pandas as pd

from services.signal_engine import build_signal_from_enriched
from services.technical_analysis import (
    CONDITION_LABELS,
    ZONE_CONDITION_IDS,
    add_indicators,
    build_context,
    condition_report,
    matches_logic,
    prepare_ohlcv,
)
from services.trading_intelligence import position_size, trade_plan

IST = ZoneInfo("Asia/Kolkata")
SAME_BAR_RULE = "A stop and a target on the same bar are booked as a stop."
SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    starting_capital REAL NOT NULL,
    cash REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_trades (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    entry_price REAL NOT NULL,
    entry_time TEXT NOT NULL,
    quantity REAL NOT NULL,
    remaining_quantity REAL NOT NULL,
    stop_loss REAL,
    target_1 REAL,
    target_2 REAL,
    target_3 REAL,
    risk REAL,
    risk_reward TEXT,
    signal_score REAL,
    signal_reasons TEXT,
    demand_zone TEXT,
    supply_zone TEXT,
    market_trend TEXT,
    status TEXT NOT NULL,
    exit_price REAL,
    exit_time TEXT,
    pnl REAL,
    pnl_pct REAL,
    exit_reason TEXT,
    notes TEXT,
    chart_ref TEXT,
    indicators TEXT,
    pattern TEXT,
    source TEXT
);
CREATE TABLE IF NOT EXISTS open_positions (
    trade_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS closed_trades (
    trade_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trade_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT NOT NULL,
    notes TEXT NOT NULL,
    chart_ref TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    logic TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    conditions_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_conditions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    position INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS backtest_runs (
    id TEXT PRIMARY KEY,
    symbol TEXT,
    timeframe TEXT,
    strategy TEXT,
    date_from TEXT,
    date_to TEXT,
    initial_capital REAL,
    risk_pct REAL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backtest_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equity_curve (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    bar_time INTEGER,
    equity REAL,
    drawdown_pct REAL
);
"""


def _num(value, places=2):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return round(number, places)


def _now():
    return datetime.now(timezone.utc).isoformat()


def size_position(capital, risk_pct, entry, stop, maximum_loss=None):
    """Quantity from the Phase 4 sizer, capped by the configured maximum loss."""
    sized = position_size(capital, risk_pct, entry, stop)
    if sized.get("data_unavailable"):
        return sized
    if maximum_loss in (None, ""):
        sized["stop_distance"] = _num(abs(float(entry) - float(stop)))
        sized["broker_order"] = False
        return sized
    try:
        cap = float(maximum_loss)
    except (TypeError, ValueError):
        return {"data_unavailable": True, "message": "Maximum loss must be a number"}
    if cap <= 0:
        return {"data_unavailable": True, "message": "Maximum loss must be positive"}
    if sized["risk_amount"] > cap + 1e-9:
        distance = abs(float(entry) - float(stop))
        sized["quantity"] = _num(cap / distance, 4)
        sized["risk_amount"] = _num(cap)
        sized["maximum_loss"] = _num(cap)
        sized["capped_by"] = "maximum loss"
    sized["stop_distance"] = _num(abs(float(entry) - float(stop)))
    sized["broker_order"] = False
    return sized


def resolve_exit(side, high, low, stop, target_1=None, target_2=None, target_3=None):
    """Book the furthest target reached, unless the stop was also touched."""
    if stop is None or high is None or low is None:
        return None
    side = str(side or "").upper()
    if side == "BUY":
        stopped = float(low) <= float(stop)
        hits = [(name, price) for name, price in (("target 1", target_1), ("target 2", target_2), ("target 3", target_3)) if price is not None and float(high) >= float(price)]
    elif side == "SELL":
        stopped = float(high) >= float(stop)
        hits = [(name, price) for name, price in (("target 1", target_1), ("target 2", target_2), ("target 3", target_3)) if price is not None and float(low) <= float(price)]
    else:
        return None
    if stopped and hits:
        return {"price": _num(stop), "reason": "stop loss (same bar as target; stop is assumed first)", "rule": SAME_BAR_RULE}
    if stopped:
        return {"price": _num(stop), "reason": "stop loss", "rule": SAME_BAR_RULE}
    if hits:
        name, price = hits[-1]
        return {"price": _num(price), "reason": name, "rule": SAME_BAR_RULE}
    return None


def _pnl(side, entry, exit_price, quantity):
    direction = 1 if str(side).upper() == "BUY" else -1
    pnl = float(quantity) * (float(exit_price) - float(entry)) * direction
    pct = (float(exit_price) - float(entry)) / float(entry) * 100 * direction
    return _num(pnl), _num(pct)


class PaperBook:
    def __init__(self, path, starting_capital=100000.0):
        self.path = str(path)
        self.starting_capital = float(starting_capital)
        self.lock = Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            row = conn.execute("SELECT cash FROM paper_account WHERE id = 1").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO paper_account (id, starting_capital, cash) VALUES (1, ?, ?)",
                    (self.starting_capital, self.starting_capital),
                )

    def _connect(self):
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _trade_from_row(self, row):
        item = dict(row)
        item["signal_reasons"] = json.loads(item.pop("signal_reasons") or "[]")
        item["indicators"] = json.loads(item.pop("indicators") or "{}")
        return item

    def _cash(self, conn):
        return float(conn.execute("SELECT cash FROM paper_account WHERE id = 1").fetchone()["cash"])

    def _set_cash(self, conn, cash):
        conn.execute("UPDATE paper_account SET cash = ? WHERE id = 1", (cash,))

    def open_trade(self, fields):
        symbol = str(fields.get("symbol") or "").upper().strip()
        direction = str(fields.get("direction") or "").upper()
        timeframe = str(fields.get("timeframe") or "1d")
        if direction not in {"BUY", "SELL"} or not symbol:
            return {"data_unavailable": True, "message": "Symbol and BUY or SELL are required"}
        try:
            entry = float(fields["entry_price"])
            stop = float(fields["stop_loss"])
            quantity = float(fields["quantity"])
        except (KeyError, TypeError, ValueError):
            return {"data_unavailable": True, "message": "Entry, stop, and quantity are required"}
        if quantity <= 0 or entry <= 0 or entry == stop:
            return {"data_unavailable": True, "message": "Quantity and entry must be positive, and the stop must differ from the entry"}
        if direction == "BUY" and entry <= stop:
            return {"data_unavailable": True, "message": "A long stop must be below the entry"}
        if direction == "SELL" and entry >= stop:
            return {"data_unavailable": True, "message": "A short stop must be above the entry"}
        risk = quantity * abs(entry - stop)
        maximum = fields.get("maximum_loss")
        if maximum not in (None, ""):
            try:
                maximum = float(maximum)
            except (TypeError, ValueError):
                return {"data_unavailable": True, "message": "Maximum loss must be a number"}
            if risk > maximum + 0.05:
                return {"data_unavailable": True, "message": "Quantity exceeds the configured maximum loss", "risk": _num(risk), "maximum_loss": _num(maximum)}
        reserve = entry * quantity
        with self.lock:
            conn = self._connect()
            try:
                existing = conn.execute(
                    "SELECT id FROM paper_trades WHERE symbol = ? AND direction = ? AND timeframe = ? AND status = 'open'",
                    (symbol, direction, timeframe),
                ).fetchone()
                if existing:
                    return {"data_unavailable": True, "message": "An open paper trade already exists for this symbol, direction, and timeframe", "trade_id": existing["id"]}
                cash = self._cash(conn)
                if reserve > cash + 0.05:
                    return {"data_unavailable": True, "message": "Not enough virtual capital", "available_capital": _num(cash), "required": _num(reserve)}
                trade_id = str(uuid4())
                reward = None if fields.get("target_1") in (None, "") else abs(float(fields["target_1"]) - entry)
                ratio = None if not abs(entry - stop) or reward is None else _num(reward / abs(entry - stop))
                payload = {
                    "id": trade_id,
                    "symbol": symbol,
                    "direction": direction,
                    "timeframe": timeframe,
                    "entry_price": _num(entry),
                    "entry_time": fields.get("entry_time") or _now(),
                    "quantity": _num(quantity, 4),
                    "remaining_quantity": _num(quantity, 4),
                    "stop_loss": _num(stop),
                    "target_1": _num(fields.get("target_1")),
                    "target_2": _num(fields.get("target_2")),
                    "target_3": _num(fields.get("target_3")),
                    "risk": _num(risk),
                    "risk_reward": None if ratio is None else f"1:{ratio}",
                    "signal_score": _num(fields.get("signal_score")),
                    "signal_reasons": json.dumps(list(fields.get("signal_reasons") or [])),
                    "demand_zone": fields.get("demand_zone") or "Unavailable",
                    "supply_zone": fields.get("supply_zone") or "Unavailable",
                    "market_trend": fields.get("market_trend") or "Unavailable",
                    "status": "open",
                    "exit_price": None,
                    "exit_time": None,
                    "pnl": 0,
                    "pnl_pct": 0,
                    "exit_reason": None,
                    "notes": "",
                    "chart_ref": fields.get("chart_ref") or f"{symbol} {timeframe}",
                    "indicators": json.dumps(fields.get("indicators") or {}),
                    "pattern": fields.get("pattern"),
                    "source": fields.get("source") or "manual",
                }
                conn.execute(
                    """INSERT INTO paper_trades (
                        id, symbol, direction, timeframe, entry_price, entry_time, quantity, remaining_quantity,
                        stop_loss, target_1, target_2, target_3, risk, risk_reward, signal_score, signal_reasons,
                        demand_zone, supply_zone, market_trend, status, exit_price, exit_time, pnl, pnl_pct,
                        exit_reason, notes, chart_ref, indicators, pattern, source
                    ) VALUES (
                        :id, :symbol, :direction, :timeframe, :entry_price, :entry_time, :quantity, :remaining_quantity,
                        :stop_loss, :target_1, :target_2, :target_3, :risk, :risk_reward, :signal_score, :signal_reasons,
                        :demand_zone, :supply_zone, :market_trend, :status, :exit_price, :exit_time, :pnl, :pnl_pct,
                        :exit_reason, :notes, :chart_ref, :indicators, :pattern, :source
                    )""",
                    payload,
                )
                conn.execute(
                    "INSERT INTO open_positions (trade_id, symbol, payload) VALUES (?, ?, ?)",
                    (trade_id, symbol, json.dumps(payload)),
                )
                self._set_cash(conn, cash - reserve)
                conn.commit()
            finally:
                conn.close()
        stored = self.get_trade(trade_id)
        stored["broker_order"] = False
        stored["message"] = "Paper trade recorded. No broker order was sent."
        return stored

    def get_trade(self, trade_id):
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM paper_trades WHERE id = ?", (trade_id,)).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        return self._trade_from_row(row)

    def list_trades(self, status=None, symbol=None):
        query = "SELECT * FROM paper_trades WHERE 1 = 1"
        args = []
        if status:
            query += " AND status = ?"
            args.append(status)
        if symbol:
            query += " AND symbol = ?"
            args.append(str(symbol).upper())
        query += " ORDER BY entry_time DESC"
        with self.lock:
            conn = self._connect()
            try:
                rows = conn.execute(query, args).fetchall()
            finally:
                conn.close()
        return [self._trade_from_row(row) for row in rows]

    def _close_quantity(self, conn, trade, quantity, exit_price, reason, exit_time):
        remaining = float(trade["remaining_quantity"])
        quantity = float(quantity)
        if quantity <= 0 or quantity > remaining + 1e-9:
            return {"data_unavailable": True, "message": "Exit quantity must be within the open quantity"}
        entry = float(trade["entry_price"])
        pnl, pct = _pnl(trade["direction"], entry, exit_price, quantity)
        reserve = entry * quantity
        cash = self._cash(conn) + reserve + (pnl or 0)
        self._set_cash(conn, cash)
        left = _num(remaining - quantity, 4)
        status = "open" if left and left > 0 else "closed"
        total_pnl = _num((trade.get("pnl") or 0) + (pnl or 0))
        conn.execute(
            """UPDATE paper_trades SET remaining_quantity = ?, status = ?, exit_price = ?, exit_time = ?,
               pnl = ?, pnl_pct = ?, exit_reason = ? WHERE id = ?""",
            (left or 0, status, _num(exit_price), exit_time, total_pnl, pct if status == "closed" else trade.get("pnl_pct"), reason, trade["id"]),
        )
        if status == "closed":
            conn.execute("DELETE FROM open_positions WHERE trade_id = ?", (trade["id"],))
            updated = dict(trade)
            updated.update({"status": "closed", "exit_price": _num(exit_price), "exit_time": exit_time, "pnl": total_pnl, "pnl_pct": pct, "exit_reason": reason, "remaining_quantity": 0})
            conn.execute(
                "INSERT OR REPLACE INTO closed_trades (trade_id, symbol, payload) VALUES (?, ?, ?)",
                (trade["id"], trade["symbol"], json.dumps(updated, default=str)),
            )
        else:
            updated = dict(trade)
            updated.update({"status": "open", "remaining_quantity": left, "pnl": total_pnl, "exit_reason": reason})
            conn.execute(
                "UPDATE open_positions SET payload = ? WHERE trade_id = ?",
                (json.dumps(updated, default=str), trade["id"]),
            )
        return {"trade_id": trade["id"], "status": status, "pnl": pnl, "pnl_pct": pct, "exit_reason": reason, "remaining_quantity": left or 0, "broker_order": False}

    def exit_trade(self, trade_id, exit_price, quantity=None, reason="manual full exit", exit_time=None):
        try:
            exit_price = float(exit_price)
        except (TypeError, ValueError):
            return {"data_unavailable": True, "message": "Exit price is required"}
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM paper_trades WHERE id = ?", (trade_id,)).fetchone()
                if row is None:
                    return {"data_unavailable": True, "message": "Trade not found"}
                trade = self._trade_from_row(row)
                if trade["status"] != "open":
                    return {"data_unavailable": True, "message": "Trade is already closed"}
                amount = trade["remaining_quantity"] if quantity in (None, "") else quantity
                result = self._close_quantity(conn, trade, amount, exit_price, reason, exit_time or _now())
                if not result.get("data_unavailable"):
                    conn.commit()
                return result
            finally:
                conn.close()

    def apply_candle(self, trade_id, candle, data_status="DELAYED"):
        """Check one completed candle. A forming bar must not be passed in."""
        trade = self.get_trade(trade_id)
        if not trade or trade["status"] != "open":
            return {"data_unavailable": True, "message": "No open paper trade"}
        if not candle:
            return {"data_unavailable": True, "message": "DATA UNAVAILABLE", "status": trade["status"]}
        hit = resolve_exit(trade["direction"], candle.get("high"), candle.get("low"), trade["stop_loss"], trade["target_1"], trade["target_2"], trade["target_3"])
        mark = {
            "trade_id": trade_id,
            "data_status": data_status,
            "ltp": candle.get("close"),
            "exit": None,
            "broker_order": False,
        }
        if hit is None:
            pnl, pct = _pnl(trade["direction"], trade["entry_price"], candle.get("close"), trade["remaining_quantity"])
            mark.update({"status": "open", "pnl": pnl, "pnl_pct": pct, "message": "Stop and targets were not touched on this candle."})
            return mark
        result = self.exit_trade(trade_id, hit["price"], reason=hit["reason"], exit_time=candle.get("time"))
        result["data_status"] = data_status
        result["rule"] = hit["rule"]
        return result

    def add_note(self, trade_id, notes, chart_ref=None):
        notes = str(notes or "").strip()
        if not notes:
            return {"data_unavailable": True, "message": "A note is required"}
        trade = self.get_trade(trade_id)
        if trade is None:
            return {"data_unavailable": True, "message": "Trade not found"}
        if trade["status"] != "closed":
            return {"data_unavailable": True, "message": "Notes are stored after the paper trade is closed"}
        with self.lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO trade_journal (trade_id, notes, chart_ref, created_at) VALUES (?, ?, ?, ?)",
                    (trade_id, notes, chart_ref or trade.get("chart_ref"), _now()),
                )
                combined = (trade.get("notes") or "").strip()
                combined = notes if not combined else combined + "\n" + notes
                conn.execute("UPDATE paper_trades SET notes = ? WHERE id = ?", (combined, trade_id))
                conn.commit()
            finally:
                conn.close()
        return {"trade_id": trade_id, "notes": combined, "broker_order": False}

    def journal(self, trade_id=None):
        query = "SELECT * FROM trade_journal"
        args = []
        if trade_id:
            query += " WHERE trade_id = ?"
            args.append(trade_id)
        query += " ORDER BY id DESC"
        with self.lock:
            conn = self._connect()
            try:
                rows = [dict(row) for row in conn.execute(query, args).fetchall()]
            finally:
                conn.close()
        return rows

    def portfolio(self, quotes=None):
        quotes = {str(key).upper(): value for key, value in (quotes or {}).items()}
        trades = self.list_trades()
        closed = [trade for trade in trades if trade["status"] == "closed"]
        open_rows = []
        unrealized = 0.0
        unrealized_known = True
        used = 0.0
        for trade in trades:
            if trade["status"] != "open":
                continue
            used += float(trade["entry_price"]) * float(trade["remaining_quantity"])
            quote = quotes.get(trade["symbol"]) or {}
            ltp = quote.get("ltp")
            row = dict(trade)
            row["ltp"] = ltp
            row["data_status"] = quote.get("data_status") or "UNAVAILABLE"
            if ltp is None:
                unrealized_known = False
                row["pnl"] = None
                row["pnl_pct"] = None
            else:
                pnl, pct = _pnl(trade["direction"], trade["entry_price"], ltp, trade["remaining_quantity"])
                row["pnl"] = pnl
                row["pnl_pct"] = pct
                unrealized += pnl or 0
            open_rows.append(row)
        wins = [trade for trade in closed if (trade.get("pnl") or 0) > 0]
        losses = [trade for trade in closed if (trade.get("pnl") or 0) < 0]
        gross_profit = sum(trade["pnl"] for trade in wins)
        gross_loss = abs(sum(trade["pnl"] for trade in losses))
        with self.lock:
            conn = self._connect()
            try:
                cash = self._cash(conn)
                starting = float(conn.execute("SELECT starting_capital FROM paper_account WHERE id = 1").fetchone()["starting_capital"])
            finally:
                conn.close()
        realized = sum(trade.get("pnl") or 0 for trade in closed)
        equity = cash + used + (unrealized if unrealized_known else 0)
        peak = starting
        drawdown = 0.0
        running = starting
        for trade in sorted(closed, key=lambda item: item.get("exit_time") or ""):
            running += trade.get("pnl") or 0
            peak = max(peak, running)
            if peak:
                drawdown = max(drawdown, (peak - running) / peak * 100)
        today = datetime.now(IST).date().isoformat()
        today_realized = sum(trade.get("pnl") or 0 for trade in closed if str(trade.get("exit_time") or "").startswith(today))
        return {
            "currency": "INR",
            "virtual": True,
            "broker_order": False,
            "starting_capital": _num(starting),
            "available_capital": _num(cash),
            "used_capital": _num(used),
            "available_margin": _num(cash),
            "margin_note": "Available margin is unused virtual cash. It is not broker margin.",
            "open_positions": open_rows,
            "closed_positions": closed,
            "total_pnl": _num(realized + (unrealized if unrealized_known else 0)),
            "realized_pnl": _num(realized),
            "unrealized_pnl": None if not unrealized_known else _num(unrealized),
            "today_pnl": None if not unrealized_known else _num(today_realized + unrealized),
            "today_realized_pnl": _num(today_realized),
            "equity": _num(equity),
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": None if not closed else _num(100 * len(wins) / len(closed)),
            "average_win": None if not wins else _num(gross_profit / len(wins)),
            "average_loss": None if not losses else _num(-gross_loss / len(losses)),
            "profit_factor": None if gross_loss == 0 else _num(gross_profit / gross_loss),
            "maximum_drawdown": _num(drawdown),
            "drawdown_basis": "Realized closed-trade equity. Open-trade ticks are not interpolated.",
            "data_status": "DELAYED" if unrealized_known or not open_rows else "UNAVAILABLE",
        }

    def save_strategy(self, name, condition_ids, logic="AND", timeframe="1d", strategy_id=None):
        name = str(name or "").strip()
        logic = str(logic or "AND").upper()
        if not name or logic not in {"AND", "OR"}:
            return {"data_unavailable": True, "message": "Name and AND or OR are required"}
        unknown = [item for item in condition_ids or [] if item not in CONDITION_LABELS]
        if not condition_ids or unknown:
            return {"data_unavailable": True, "message": "Choose existing scanner conditions", "unknown": unknown}
        strategy_id = strategy_id or str(uuid4())
        with self.lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO strategies (id, name, logic, timeframe, conditions_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (strategy_id, name, logic, timeframe, json.dumps(list(condition_ids)), _now()),
                )
                conn.execute("DELETE FROM strategy_conditions WHERE strategy_id = ?", (strategy_id,))
                for index, condition_id in enumerate(condition_ids):
                    conn.execute(
                        "INSERT INTO strategy_conditions (strategy_id, condition_id, position) VALUES (?, ?, ?)",
                        (strategy_id, condition_id, index),
                    )
                conn.commit()
            finally:
                conn.close()
        return self.get_strategy(strategy_id)

    def get_strategy(self, strategy_id):
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        item = dict(row)
        item["conditions"] = json.loads(item.pop("conditions_json"))
        return item

    def list_strategies(self):
        with self.lock:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT * FROM strategies ORDER BY created_at DESC").fetchall()
            finally:
                conn.close()
        items = []
        for row in rows:
            item = dict(row)
            item["conditions"] = json.loads(item.pop("conditions_json"))
            items.append(item)
        return items

    def delete_strategy(self, strategy_id):
        with self.lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM strategy_conditions WHERE strategy_id = ?", (strategy_id,))
                deleted = conn.execute("DELETE FROM strategies WHERE id = ?", (strategy_id,)).rowcount
                conn.commit()
            finally:
                conn.close()
        return {"deleted": bool(deleted), "id": strategy_id}

    def save_backtest(self, result):
        run_id = str(uuid4())
        with self.lock:
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT INTO backtest_runs (
                        id, symbol, timeframe, strategy, date_from, date_to, initial_capital, risk_pct, result_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id, result.get("symbol"), result.get("timeframe"), result.get("strategy"),
                        result.get("date_from"), result.get("date_to"), result.get("initial_capital"),
                        result.get("risk_pct"), json.dumps(result, default=str), _now(),
                    ),
                )
                for trade in result.get("trades") or []:
                    conn.execute(
                        "INSERT INTO backtest_trades (run_id, payload_json) VALUES (?, ?)",
                        (run_id, json.dumps(trade, default=str)),
                    )
                for point in result.get("equity") or []:
                    conn.execute(
                        "INSERT INTO equity_curve (run_id, bar_time, equity, drawdown_pct) VALUES (?, ?, ?, ?)",
                        (run_id, point.get("time"), point.get("equity"), point.get("drawdown_pct")),
                    )
                conn.commit()
            finally:
                conn.close()
        result["run_id"] = run_id
        return result

    def load_backtest(self, run_id):
        """A stored run with its trades and equity read back from their own tables."""
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT result_json FROM backtest_runs WHERE id = ?", (run_id,)).fetchone()
                if not row:
                    return None
                trades = conn.execute(
                    "SELECT payload_json FROM backtest_trades WHERE run_id = ? ORDER BY id", (run_id,)
                ).fetchall()
                equity = conn.execute(
                    "SELECT bar_time, equity, drawdown_pct FROM equity_curve WHERE run_id = ? ORDER BY id", (run_id,)
                ).fetchall()
            finally:
                conn.close()
        result = json.loads(row["result_json"])
        result["run_id"] = run_id
        result["trades"] = [json.loads(item["payload_json"]) for item in trades]
        result["equity"] = [
            {"time": item["bar_time"], "equity": item["equity"], "drawdown_pct": item["drawdown_pct"]} for item in equity
        ]
        return result


def extend_backtest_report(result):
    """Add drawdown points and extremes. This does not change trade P&L."""
    if not result or result.get("data_unavailable"):
        return result
    trades = result.get("trades") or []
    pnls = [trade.get("pnl") for trade in trades if trade.get("pnl") is not None]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    initial = result.get("initial_capital") or 0
    final = result.get("ending_capital")
    result["final_capital"] = final
    result["pnl_pct"] = None if not initial or final is None else _num((float(final) - float(initial)) / float(initial) * 100)
    result["largest_win"] = None if not wins else _num(max(wins))
    result["largest_loss"] = None if not losses else _num(min(losses))
    result["average_trade"] = None if not pnls else _num(sum(pnls) / len(pnls))
    result["profitable"] = bool(result.get("net_profit") is not None and result["net_profit"] > 0)
    peak = None
    for point in result.get("equity") or []:
        equity = point.get("equity")
        if equity is None:
            point["drawdown_pct"] = None
            continue
        peak = equity if peak is None else max(peak, equity)
        point["drawdown_pct"] = 0 if not peak else _num((peak - equity) / peak * 100)
    result["drawdown"] = list(result.get("equity") or [])
    result["same_bar_rule"] = SAME_BAR_RULE
    return result


def _slice_frame(frame, start, end, limit):
    prepared = prepare_ohlcv(frame)
    if prepared.empty:
        return prepared, False
    index = prepared.index
    if getattr(index, "tz", None) is not None:
        prepared = prepared.copy()
        prepared.index = index.tz_convert("Asia/Kolkata").tz_localize(None)
    if start:
        stamp = pd.Timestamp(start)
        if stamp.tzinfo is not None:
            stamp = stamp.tz_localize(None)
        prepared = prepared[prepared.index >= stamp]
    if end:
        stamp = pd.Timestamp(end)
        if stamp.tzinfo is not None:
            stamp = stamp.tz_localize(None)
        prepared = prepared[prepared.index <= stamp]
    capped = False
    if limit and len(prepared) > limit + 40:
        prepared = prepared.iloc[-(int(limit) + 40):]
        capped = True
    return prepared, capped


def run_condition_backtest(frame, timeframe, condition_ids, logic="AND", capital=100000.0, risk_pct=1.0, maximum_loss=None, start=None, end=None, limit=120, symbol=None):
    """Walk candles in order. Bar N uses indicators only through bar N. Fill is the next open."""
    unknown = [item for item in condition_ids or [] if item not in CONDITION_LABELS]
    if not condition_ids or unknown:
        return {"data_unavailable": True, "message": "Choose existing scanner conditions", "unknown": unknown}
    logic = str(logic or "AND").upper()
    if logic not in {"AND", "OR"}:
        return {"data_unavailable": True, "message": "Logic must be AND or OR"}
    prepared, capped = _slice_frame(frame, start, end, limit)
    if len(prepared) < 40:
        return {"data_unavailable": True, "message": "DATA UNAVAILABLE"}
    include_vwap = False
    if isinstance(prepared.index, pd.DatetimeIndex):
        counts = prepared.index.normalize().value_counts()
        include_vwap = int(counts.max()) >= 2 if len(counts) else False
    enriched = add_indicators(prepared, include_vwap=include_vwap)
    zone_ids = [item for item in condition_ids if item in ZONE_CONDITION_IDS]
    trades = []
    cash = float(capital)
    peak = cash
    max_drawdown = 0.0
    equity = []
    position = None
    first = 35
    last = len(enriched) - 1

    def close_position(index, price, reason):
        nonlocal cash, peak, max_drawdown, position
        pnl, pct = _pnl("BUY" if position["side"] == "buy" else "SELL", position["entry"], price, position["quantity"])
        cash += pnl or 0
        peak = max(peak, cash)
        if peak:
            max_drawdown = max(max_drawdown, (peak - cash) / peak * 100)
        trades.append({
            "trade": len(trades) + 1,
            "time": int(pd.Timestamp(enriched.index[position["signal_index"]]).timestamp()),
            "exit_time": int(pd.Timestamp(enriched.index[index]).timestamp()),
            "symbol": symbol,
            "timeframe": timeframe,
            "side": "BUY" if position["side"] == "buy" else "SELL",
            "entry": _num(position["entry"]),
            "stop_loss": _num(position["stop"]),
            "target_1": _num(position["target_1"]),
            "target_2": _num(position["target_2"]),
            "target_3": _num(position["target_3"]),
            "exit": _num(price),
            "exit_reason": reason,
            "pnl": pnl,
            "pnl_pct": pct,
            "score": position["score"],
            "reasons": position["reasons"],
            "matched": position["matched"],
            "failed": position["failed"],
            "unavailable": position["unavailable"],
            "holding_bars": int(index - position["entry_index"]),
        })
        equity.append({"time": trades[-1]["exit_time"], "equity": _num(cash), "drawdown_pct": _num((peak - cash) / peak * 100 if peak else 0)})
        position = None

    for index in range(first, last + 1):
        high = float(prepared["High"].iloc[index])
        low = float(prepared["Low"].iloc[index])
        if position is not None and index >= position["entry_index"]:
            hit = resolve_exit("BUY" if position["side"] == "buy" else "SELL", high, low, position["stop"], position["target_1"], position["target_2"], position["target_3"])
            if hit:
                close_position(index, hit["price"], hit["reason"])
            elif index == last:
                close_position(index, float(prepared["Close"].iloc[index]), "end of test")
        if position is not None or index >= last:
            continue
        window = enriched.iloc[max(0, index - 260): index + 1]
        context = build_context(window, include_zones=False, include_vwap=include_vwap)
        if context is None or not matches_logic(condition_ids, context, logic):
            continue
        report = condition_report(condition_ids, context)
        signal = build_signal_from_enriched(window, include_vwap=include_vwap, zones_calculated=False)
        if signal.get("data_unavailable") or signal.get("signal") not in {"BUY", "SELL"} or signal.get("stop_loss") is None:
            continue
        plan = trade_plan(signal, None)
        entry_bar = index + 1
        entry = float(prepared["Open"].iloc[entry_bar])
        stop = float(signal["stop_loss"])
        side = "buy" if signal["signal"] == "BUY" else "sell"
        if (side == "buy" and entry <= stop) or (side == "sell" and entry >= stop):
            continue
        budget = cash * (float(risk_pct) / 100.0)
        if maximum_loss not in (None, ""):
            budget = min(budget, float(maximum_loss))
        risk = abs(entry - stop)
        reserve = entry * (budget / risk)
        if risk <= 0 or budget <= 0 or reserve > cash:
            continue
        position = {
            "side": side,
            "signal_index": index,
            "entry_index": entry_bar,
            "entry": entry,
            "stop": stop,
            "target_1": signal.get("target_1"),
            "target_2": signal.get("target_2"),
            "target_3": plan.get("target_3"),
            "quantity": budget / risk,
            "score": signal.get("strength"),
            "reasons": list(signal.get("reasons") or []),
            "matched": report["matched"],
            "failed": report["failed"],
            "unavailable": report["unavailable"],
        }
    wins = [trade for trade in trades if (trade["pnl"] or 0) > 0]
    losses = [trade for trade in trades if (trade["pnl"] or 0) < 0]
    gross_profit = sum(trade["pnl"] for trade in wins)
    gross_loss = abs(sum(trade["pnl"] for trade in losses))
    result = {
        "disclaimer": "These figures describe this historical test only. They are not a guarantee of future performance.",
        "methodology": (
            "At bar N the conditions and signal use candles up to and including N. "
            "The fill is the next bar's open. "
            + SAME_BAR_RULE + " "
            "The furthest target touched is used when the stop is not touched. "
            "Demand and supply conditions are left unavailable on every bar so a zone calculated later cannot leak backward. "
            "An AND rule that includes one of them does not open trades. "
            "One position is open at a time."
        ),
        "strategy": "conditions",
        "symbol": symbol,
        "timeframe": timeframe,
        "logic": logic,
        "conditions": list(condition_ids),
        "zone_conditions": zone_ids,
        "zone_note": "Per-bar demand and supply zones are not calculated in this walk.",
        "capped": capped,
        "bar_limit": limit,
        "date_from": None if prepared.empty else str(prepared.index[0]),
        "date_to": None if prepared.empty else str(prepared.index[-1]),
        "initial_capital": _num(capital),
        "risk_pct": risk_pct,
        "ending_capital": _num(cash),
        "final_capital": _num(cash),
        "net_profit": _num(cash - float(capital)),
        "total_trades": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": None if not trades else _num(100 * len(wins) / len(trades)),
        "average_win": None if not wins else _num(gross_profit / len(wins)),
        "average_loss": None if not losses else _num(-gross_loss / len(losses)),
        "profit_factor": None if gross_loss == 0 else _num(gross_profit / gross_loss),
        "max_drawdown_pct": _num(max_drawdown),
        "largest_win": None if not wins else _num(max(trade["pnl"] for trade in wins)),
        "largest_loss": None if not losses else _num(min(trade["pnl"] for trade in losses)),
        "average_trade": None if not trades else _num(sum(trade["pnl"] for trade in trades) / len(trades)),
        "risk_reward": None,
        "trades": trades,
        "equity": equity,
        "same_bar_rule": SAME_BAR_RULE,
        "profitable": cash > float(capital),
    }
    if initial_ok := result["initial_capital"]:
        result["pnl_pct"] = _num((cash - float(capital)) / float(capital) * 100)
    return extend_backtest_report(result)
