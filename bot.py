from flask import Flask, request
import json
import datetime
from zoneinfo import ZoneInfo


app = Flask(__name__)

current_trades = {}

TRADES_FILE = "trades.xlsx"
SETTINGS_FILE = "settings.json"
BANGKOK_TZ = ZoneInfo("Asia/Bangkok")


def load_settings():
    with open(SETTINGS_FILE, "r") as f:
        return json.load(f)


def now_bangkok():
    return datetime.datetime.now(BANGKOK_TZ)


def to_bangkok_time(tv_time):
    """
    Converts TradingView UTC/Zulu time to readable Bangkok time.
    Example:
    2026-04-24T15:44:00Z -> 2026-04-24 22:44:00
    """
    try:
        dt = datetime.datetime.fromisoformat(tv_time.replace("Z", "+00:00"))
        return dt.astimezone(BANGKOK_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return tv_time


def get_pip_size(symbol, settings):
    if "JPY" in symbol.upper():
        return float(settings.get("jpy_pip_size", 0.01))
    return float(settings.get("pip_size", settings.get("bot_pip_size", 0.0001)))


def get_stop_loss_pips(settings):
    return float(settings.get("bot_stop_loss_pips", settings.get("stop_loss_pips", 5)))


def get_take_profit_pips(settings):
    return float(settings.get("bot_take_profit_pips", settings.get("take_profit_pips", 10)))


def calculate_lot_size(settings):
    lot_mode = settings.get("lot_mode", "fixed")

    if lot_mode == "fixed":
        return float(settings.get("fixed_lot_size", 1000))

    balance = float(settings.get("account_balance", 10000))
    risk_pct = float(settings.get("risk_per_trade_percent", 1)) / 100
    stop_loss = get_stop_loss_pips(settings)
    pip_value = float(settings.get("pip_value_per_1000", 0.10))

    risk_amount = balance * risk_pct
    lot_size = risk_amount / (stop_loss * pip_value)

    return round(lot_size, 2)


def calc_pips(symbol, side, entry, exit_price):
    pip_multiplier = 100 if "JPY" in symbol.upper() else 10000

    if side == "buy":
        return (exit_price - entry) * pip_multiplier
    else:
        return (entry - exit_price) * pip_multiplier


def format_duration(start_dt, end_dt):
    delta = end_dt - start_dt
    total_seconds = int(delta.total_seconds())

    if total_seconds < 0:
        return ""

    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def official_exit_price(symbol, side, entry, alert_price, reason, signal, settings):
    reason_text = f"{reason} {signal}".lower()

    pip_size = get_pip_size(symbol, settings)
    stop_pips = get_stop_loss_pips(settings)
    take_pips = get_take_profit_pips(settings)

    if "stop_loss" in reason_text or "stop loss" in reason_text:
        if side == "buy":
            return entry - (stop_pips * pip_size)
        else:
            return entry + (stop_pips * pip_size)

    if "take_profit" in reason_text or "take profit" in reason_text:
        if side == "buy":
            return entry + (take_pips * pip_size)
        else:
            return entry - (take_pips * pip_size)

    return alert_price


def log_trade(data):
    try:
        df = pd.read_excel(TRADES_FILE)
    except Exception:
        df = pd.DataFrame()

    df = pd.concat([df, pd.DataFrame([data])], ignore_index=True)
    df.to_excel(TRADES_FILE, index=False)

    try:
        wb = load_workbook(TRADES_FILE)
        ws = wb.active

        headers = [cell.value for cell in ws[1]]

        if "profit" in headers:
            profit_col = headers.index("profit") + 1
            for row in range(2, ws.max_row + 1):
                ws.cell(row=row, column=profit_col).number_format = '$#,##0.00;-$#,##0.00'

        wb.save(TRADES_FILE)

    except Exception as e:
        print(f"WARNING: could not format Excel file: {e}")


@app.route("/", methods=["POST"])
def webhook():
    global current_trades

    data = request.json
    settings = load_settings()

    if data.get("secret") != "chidrew1":
        return {"status": "unauthorized"}

    action = data.get("action")
    symbol = data.get("symbol", "").upper()
    signal = data.get("signal", "")
    reason = data.get("reason", signal)
    alert_time_raw = data.get("time", "")
    alert_time_bangkok = to_bangkok_time(alert_time_raw)

    allowed_entry_signals = [
        "buy_pullback",
        "buy_continuation",
        "sell_pullback",
        "sell_continuation"
    ]

    if action in ["buy", "sell"] and signal not in allowed_entry_signals:
        print(f"IGNORED {signal}: not an allowed entry signal")
        return {"status": "ignored", "reason": "entry signal not allowed"}

    try:
        price = float(data.get("price"))
    except Exception:
        return {"status": "bad price"}

    lot_size = calculate_lot_size(settings)

    print("Incoming:", data)

    # ======================
    # ENTRY LOGIC
    # ======================
    if action in ["buy", "sell"]:

        if symbol in current_trades:
            print(f"IGNORED {action.upper()} {symbol}: already in trade")
            return {"status": "ignored", "reason": "already in trade for symbol"}

        opened_at_dt = now_bangkok()

        current_trades[symbol] = {
            "symbol": symbol,
            "side": action,
            "entry_price": price,
            "lot_size": lot_size,
            "entry_signal": signal,
            "entry_time": alert_time_bangkok,
            "opened_at": opened_at_dt.strftime("%Y-%m-%d %H:%M:%S")
        }

        print(f"ENTER {action.upper()} {symbol} @ {price} | Lot: {lot_size} | Bangkok: {alert_time_bangkok}")

        return {
            "status": "entered",
            "symbol": symbol,
            "side": action,
            "lot_size": lot_size,
            "entry_price": price,
            "stop_loss_pips": get_stop_loss_pips(settings),
            "take_profit_pips": get_take_profit_pips(settings),
            "entry_time_bangkok": alert_time_bangkok
        }

    # ======================
    # EXIT LOGIC
    # ======================
    if action == "exit":

        if symbol not in current_trades:
            print(f"IGNORED EXIT {symbol}: no open trade")
            return {"status": "ignored", "reason": "no open trade for symbol"}

        trade = current_trades[symbol]

        entry = float(trade["entry_price"])
        side = trade["side"]
        lot = float(trade["lot_size"])

        exit_price = official_exit_price(symbol, side, entry, price, reason, signal, settings)

        pips = calc_pips(symbol, side, entry, exit_price)
        profit = pips * (lot / 1000) * float(settings.get("pip_value_per_1000", 0.10))

        closed_at_dt = now_bangkok()

        try:
            opened_at_dt = datetime.datetime.strptime(trade.get("opened_at", ""), "%Y-%m-%d %H:%M:%S")
            opened_at_dt = opened_at_dt.replace(tzinfo=BANGKOK_TZ)
            duration = format_duration(opened_at_dt, closed_at_dt)
        except Exception:
            duration = ""

        result = {
            "symbol": symbol,
            "side": side,
            "entry": entry,
            "exit": round(exit_price, 5),
            "pips": round(pips, 2),
            "profit": round(profit, 2),
            "lot_size": lot,
            "entry_signal": trade.get("entry_signal", ""),
            "exit_signal": signal,
            "entry_time_bangkok": trade.get("entry_time", ""),
            "exit_time_bangkok": alert_time_bangkok,
            "opened_at_bangkok": trade.get("opened_at", ""),
            "closed_at_bangkok": closed_at_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "trade_duration": duration,
            "exit_reason": reason,
            "alert_exit_price": price
        }

        print("TRADE LOG:", result)

        print(f"EXIT {side.upper()} {symbol} @ {exit_price}")
        print(f"PIPS: {round(pips, 2)} | PROFIT: ${round(profit, 2)} | DURATION: {duration}")

        del current_trades[symbol]

        return {
            "status": "exited",
            "symbol": symbol,
            "pips": round(pips, 2),
            "profit": round(profit, 2),
            "exit_price": round(exit_price, 5),
            "duration": duration,
            "exit_reason": reason,
            "exit_time_bangkok": alert_time_bangkok
        }

    return {"status": "ignored", "reason": "unknown action"}


@app.route("/status", methods=["GET"])
def status():
    return {
        "status": "running",
        "open_trades": current_trades
    }


import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)