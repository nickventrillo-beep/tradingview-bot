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

LOT_SIZE = float(os.getenv("LOT_SIZE", 1000))

BANGKOK_TZ = ZoneInfo("Asia/Bangkok")

current_trades = {}


# =========================
# TIME HELPERS
# =========================

def now_bangkok_dt():
    return datetime.now(BANGKOK_TZ)


def fmt_bangkok(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def trade_duration(start_dt, end_dt):
    seconds = max(0, int((end_dt - start_dt).total_seconds()))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:02d}"


# =========================
# PRICE HELPERS
# =========================

def get_pip_size(symbol):
    return 0.01 if "JPY" in symbol.upper() else 0.0001


def pips_between(entry_price, exit_price, side, symbol):
    pip = get_pip_size(symbol)

    if side == "buy":
        return round((exit_price - entry_price) / pip, 1)

    if side == "sell":
        return round((entry_price - exit_price) / pip, 1)

    return 0


def profit_from_pips(pips):
    return round(pips * (LOT_SIZE / 10000), 2)


def calculate_sl_tp(entry_price, side, symbol):
    pip = get_pip_size(symbol)

    if side == "buy":
        stop_loss_price = entry_price - (STOP_LOSS_PIPS * pip)
        take_profit_price = entry_price + (TAKE_PROFIT_PIPS * pip)
    else:
        stop_loss_price = entry_price + (STOP_LOSS_PIPS * pip)
        take_profit_price = entry_price - (TAKE_PROFIT_PIPS * pip)

    return round(stop_loss_price, 5), round(take_profit_price, 5)


# =========================
# GOOGLE SHEETS
# =========================

def get_sheet():
    if not GOOGLE_SHEET_ID:
        print("Google Sheets not configured: GOOGLE_SHEET_ID missing")
        return None

    if not GOOGLE_CREDENTIALS_JSON:
        print("Google Sheets not configured: GOOGLE_SERVICE_ACCOUNT_JSON missing")
        return None

    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]

        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)

        return client.open_by_key(GOOGLE_SHEET_ID).sheet1

    except Exception as e:
        print(f"Google Sheets setup failed: {e}")
        return None


def write_trade_to_sheet(row):
    sheet = get_sheet()

    if sheet:
        sheet.append_row(row, value_input_option="USER_ENTERED")
        print("Sent to Google Sheets:", row)
        return True

    print("Trade row not written:", row)
    return False


# =========================
# TRADE LOGIC
# =========================

def check_hard_exit(trade, current_price):
    side = trade["side"]
    stop_loss_price = trade["stop_loss_price"]
    take_profit_price = trade["take_profit_price"]

    if side == "buy":
        if current_price <= stop_loss_price:
            return "stop_loss_buy", "stop_loss", stop_loss_price

        if current_price >= take_profit_price:
            return "take_profit_buy", "take_profit", take_profit_price

    if side == "sell":
        if current_price >= stop_loss_price:
            return "stop_loss_sell", "stop_loss", stop_loss_price

        if current_price <= take_profit_price:
            return "take_profit_sell", "take_profit", take_profit_price

    return None, None, None


def open_trade(symbol, side, signal, price):
    entry_dt = now_bangkok_dt()
    stop_loss_price, take_profit_price = calculate_sl_tp(price, side, symbol)

    current_trades[symbol] = {
        "symbol": symbol,
        "side": side,
        "entry_signal": signal,
        "entry_price": price,
        "entry_dt": entry_dt,
        "entry_time": fmt_bangkok(entry_dt),
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
        "entry_signal": signal,
        "entry_price": price,
        "stop_loss_price": stop_loss_price,
        "take_profit_price": take_profit_price
    }


def close_trade(symbol, exit_price, exit_signal, exit_reason):
    trade = current_trades.get(symbol)

    if not trade:
        return {
            "status": "ignored",
            "reason": "no open trade"
        }

    exit_dt = now_bangkok_dt()

    side = trade["side"]
    entry_price = trade["entry_price"]

    pips = pips_between(entry_price, exit_price, side, symbol)
    profit = profit_from_pips(pips)

    # EXACT 13-COLUMN GOOGLE SHEETS FORMAT:
    # symbol | side | entry | exit | pips | profit | lot_size |
    # entry_signal | exit_signal | entry_time_bangkok |
    # exit_time_bangkok | trade_duration | exit_reason

    row = [
        symbol,
        side,
        round(entry_price, 5),
        round(exit_price, 5),
        pips,
        profit,
        LOT_SIZE,
        trade["entry_signal"],
        exit_signal,
        trade["entry_time"],
        fmt_bangkok(exit_dt),
        trade_duration(trade["entry_dt"], exit_dt),
        exit_reason
    ]

    sheet_written = write_trade_to_sheet(row)

    print(f"CLOSED {symbol} {side} | {exit_signal} | {pips} pips")

    del current_trades[symbol]

    return {
        "status": "closed",
        "symbol": symbol,
        "side": side,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "pips": pips,
        "profit": profit,
        "exit_signal": exit_signal,
        "exit_reason": exit_reason,
        "sheet_written": sheet_written
    }


# =========================
# ROUTES
# =========================

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "running",
        "service": "tradingview-bot",
        "stop_loss_pips": STOP_LOSS_PIPS,
        "take_profit_pips": TAKE_PROFIT_PIPS,
        "lot_size": LOT_SIZE,
        "open_trades": current_trades
    })


@app.route("/", methods=["POST"])
def webhook():
    data = request.json or {}

    if data.get("secret") != SECRET:
        print("Unauthorized request")
        return jsonify({"status": "unauthorized"}), 401

    symbol = str(data.get("symbol", "")).upper().strip()
    action = str(data.get("action", "")).lower().strip()
    signal = str(data.get("signal", "")).lower().strip()

    try:
        price = float(data.get("price"))
    except Exception:
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

        hard_exit_signal, hard_exit_reason, hard_exit_price = check_hard_exit(trade, price)

        if hard_exit_signal:
            return jsonify(
                close_trade(
                    symbol=symbol,
                    exit_price=hard_exit_price,
                    exit_signal=hard_exit_signal,
                    exit_reason=hard_exit_reason
                )
            )

    # =========================
    # 2. INDICATOR EXIT SECOND
    # =========================

    if symbol in current_trades:
        trade = current_trades[symbol]

        if trade["side"] == "buy" and signal in ["exit_buy", "bot_exit_buy"]:
            return jsonify(
                close_trade(
                    symbol=symbol,
                    exit_price=price,
                    exit_signal="exit_buy",
                    exit_reason="indicator_exit"
                )
            )

        if trade["side"] == "sell" and signal in ["exit_sell", "bot_exit_sell"]:
            return jsonify(
                close_trade(
                    symbol=symbol,
                    exit_price=price,
                    exit_signal="exit_sell",
                    exit_reason="indicator_exit"
                )
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
    port = int(os.getenv("PORT", 8080))
    print("Starting tradingview-bot...")
    print(f"Listening on port {port}")
    app.run(host="0.0.0.0", port=port)