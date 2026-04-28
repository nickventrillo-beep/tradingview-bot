from flask import Flask, request, jsonify
from datetime import datetime
from zoneinfo import ZoneInfo
import os
import json
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# =========================
# SETTINGS
# =========================

SECRET = os.getenv("WEBHOOK_SECRET", "chidrew1")

STOP_LOSS_PIPS = float(os.getenv("BOT_STOP_LOSS_PIPS", 5))
TAKE_PROFIT_PIPS = float(os.getenv("BOT_TAKE_PROFIT_PIPS", 7))

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

BANGKOK_TZ = ZoneInfo("Asia/Bangkok")

current_trades = {}


# =========================
# HELPERS
# =========================

def now_bangkok():
    return datetime.now(BANGKOK_TZ).strftime("%Y-%m-%d %H:%M:%S")


def get_pip_size(symbol):
    symbol = symbol.upper()
    if "JPY" in symbol:
        return 0.01
    return 0.0001


def pips_between(entry_price, exit_price, side, symbol):
    pip = get_pip_size(symbol)

    if side == "buy":
        return round((exit_price - entry_price) / pip, 1)

    if side == "sell":
        return round((entry_price - exit_price) / pip, 1)

    return 0


def get_sheet():
    if not GOOGLE_SHEET_ID or not GOOGLE_CREDENTIALS_JSON:
        print("Google Sheets not configured.")
        return None

    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)

    return client.open_by_key(GOOGLE_SHEET_ID).sheet1


def write_trade_to_sheet(row):
    sheet = get_sheet()
    if sheet:
        sheet.append_row(row)
        print("Sent to Google Sheets:", row)
    else:
        print("Trade row:", row)


def calculate_sl_tp(entry_price, side, symbol):
    pip = get_pip_size(symbol)

    if side == "buy":
        stop_loss_price = entry_price - (STOP_LOSS_PIPS * pip)
        take_profit_price = entry_price + (TAKE_PROFIT_PIPS * pip)

    else:
        stop_loss_price = entry_price + (STOP_LOSS_PIPS * pip)
        take_profit_price = entry_price - (TAKE_PROFIT_PIPS * pip)

    return round(stop_loss_price, 5), round(take_profit_price, 5)


def check_hard_exit(trade, current_price):
    """
    This is the key fix.

    Stop loss and take profit are checked FIRST.
    Indicator exits only happen later.
    """

    side = trade["side"]
    symbol = trade["symbol"]
    entry_price = trade["entry_price"]
    stop_loss_price = trade["stop_loss_price"]
    take_profit_price = trade["take_profit_price"]

    if side == "buy":
        if current_price <= stop_loss_price:
            return "stop_loss_buy", stop_loss_price

        if current_price >= take_profit_price:
            return "take_profit_buy", take_profit_price

    if side == "sell":
        if current_price >= stop_loss_price:
            return "stop_loss_sell", stop_loss_price

        if current_price <= take_profit_price:
            return "take_profit_sell", take_profit_price

    return None, None


def close_trade(symbol, exit_price, exit_reason):
    trade = current_trades.get(symbol)

    if not trade:
        return {
            "status": "ignored",
            "reason": "no open trade"
        }

    entry_price = trade["entry_price"]
    side = trade["side"]

    profit_pips = pips_between(entry_price, exit_price, side, symbol)

    row = [
        now_bangkok(),
        symbol,
        side,
        trade["signal"],
        entry_price,
        exit_price,
        profit_pips,
        exit_reason,
        trade["entry_time"],
        trade["stop_loss_price"],
        trade["take_profit_price"]
    ]

    write_trade_to_sheet(row)

    print(f"CLOSED {symbol} {side} | {exit_reason} | {profit_pips} pips")

    del current_trades[symbol]

    return {
        "status": "closed",
        "symbol": symbol,
        "side": side,
        "exit_reason": exit_reason,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "profit_pips": profit_pips
    }


def open_trade(symbol, side, signal, price):
    stop_loss_price, take_profit_price = calculate_sl_tp(price, side, symbol)

    current_trades[symbol] = {
        "symbol": symbol,
        "side": side,
        "signal": signal,
        "entry_price": price,
        "entry_time": now_bangkok(),
        "stop_loss_price": stop_loss_price,
        "take_profit_price": take_profit_price
    }

    print(
        f"OPENED {symbol} {side} | entry={price} | "
        f"SL={stop_loss_price} | TP={take_profit_price}"
    )

    return {
        "status": "opened",
        "symbol": symbol,
        "side": side,
        "signal": signal,
        "entry_price": price,
        "stop_loss_price": stop_loss_price,
        "take_profit_price": take_profit_price
    }


# =========================
# WEBHOOK
# =========================

@app.route("/", methods=["GET"])
def home():
    return "Trading bot is running."


@app.route("/", methods=["POST"])
def webhook():
    data = request.json or {}

    if data.get("secret") != SECRET:
        return jsonify({"status": "unauthorized"}), 401

    symbol = data.get("symbol", "").upper()
    action = data.get("action", "").lower()
    signal = data.get("signal", "").lower()

    try:
        price = float(data.get("price"))
    except:
        return jsonify({
            "status": "error",
            "reason": "missing or invalid price",
            "received": data
        }), 400

    if not symbol:
        return jsonify({
            "status": "error",
            "reason": "missing symbol"
        }), 400

    print("ALERT RECEIVED:", data)

    # =========================
    # 1. HARD SL / TP CHECK FIRST
    # =========================

    if symbol in current_trades:
        trade = current_trades[symbol]

        hard_exit_reason, hard_exit_price = check_hard_exit(trade, price)

        if hard_exit_reason:
            return jsonify(
                close_trade(symbol, hard_exit_price, hard_exit_reason)
            )

    # =========================
    # 2. INDICATOR EXIT SECOND
    # =========================

    if symbol in current_trades:
        trade = current_trades[symbol]

        if trade["side"] == "buy" and signal in ["exit_buy", "bot_exit_buy"]:
            return jsonify(
                close_trade(symbol, price, "indicator_exit_buy")
            )

        if trade["side"] == "sell" and signal in ["exit_sell", "bot_exit_sell"]:
            return jsonify(
                close_trade(symbol, price, "indicator_exit_sell")
            )

    # =========================
    # 3. ENTRY SIGNALS LAST
    # =========================

    allowed_entry_signals = [
        "buy_pullback",
        "buy_continuation",
        "sell_pullback",
        "sell_continuation"
    ]

    if signal not in allowed_entry_signals:
        return jsonify({
            "status": "ignored",
            "reason": "not an entry signal and no exit triggered",
            "signal": signal
        })

    if symbol in current_trades:
        return jsonify({
            "status": "ignored",
            "reason": "trade already open",
            "open_trade": current_trades[symbol]
        })

    if action == "buy" or signal.startswith("buy_"):
        return jsonify(open_trade(symbol, "buy", signal, price))

    if action == "sell" or signal.startswith("sell_"):
        return jsonify(open_trade(symbol, "sell", signal, price))

    return jsonify({
        "status": "ignored",
        "reason": "unknown action",
        "data": data
    })


# =========================
# START
# =========================

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)