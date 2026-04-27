import os
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify

try:
    import gspread
    from google.oauth2.service_account import Credentials
except Exception:
    gspread = None
    Credentials = None


app = Flask(__name__)

# =========================
# SETTINGS
# =========================

SECRET = os.getenv("WEBHOOK_SECRET", "chidrew1")

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()

# IMPORTANT:
# This writes to your original Sheet1 tab using your original column order.
WORKSHEET_NAME = os.getenv("GOOGLE_WORKSHEET_NAME", "Sheet1").strip()

LOT_SIZE = float(os.getenv("LOT_SIZE", "1000"))
REENTRY_BLOCK_MINUTES = int(os.getenv("REENTRY_BLOCK_MINUTES", "15"))

BANGKOK_TZ = ZoneInfo("Asia/Bangkok")

HEADERS = [
    "symbol",
    "side",
    "entry",
    "exit",
    "pips",
    "profit",
    "lot_size",
    "entry_signal",
    "exit_signal",
    "entry_time_bangkok",
    "exit_time_bangkok",
    "trade_duration",
    "exit_reason",
]

ALLOWED_ENTRY_SIGNALS = {
    "buy_pullback",
    "sell_pullback",
}

ALLOWED_EXIT_SIGNALS = {
    "exit_buy",
    "exit_sell",
    "stop_loss_buy",
    "take_profit_buy",
    "stop_loss_sell",
    "take_profit_sell",
}


# =========================
# STATE
# =========================

current_trades = {}
last_exit_time_by_symbol = {}


# =========================
# TIME HELPERS
# =========================

def parse_event_time(value):
    """
    Accepts TradingView epoch milliseconds, epoch seconds, ISO strings, or blank.
    Returns timezone-aware UTC datetime.
    """
    if value is None or str(value).strip() == "":
        return datetime.now(timezone.utc)

    text = str(value).strip()

    # TradingView {{time}} is commonly epoch milliseconds.
    if text.isdigit():
        n = int(text)
        if n > 10_000_000_000:
            return datetime.fromtimestamp(n / 1000, tz=timezone.utc)
        return datetime.fromtimestamp(n, tz=timezone.utc)

    try:
        text = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def fmt_bangkok(dt):
    return dt.astimezone(BANGKOK_TZ).strftime("%Y-%m-%d %H:%M:%S")


def fmt_duration(start_dt, end_dt):
    seconds = max(0, int((end_dt - start_dt).total_seconds()))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:02d}"


# =========================
# PRICE / PIP HELPERS
# =========================

def pip_size_for_symbol(symbol):
    symbol = symbol.upper()
    if "JPY" in symbol:
        return 0.01
    return 0.0001


def calc_pips(symbol, side, entry, exit_price):
    pip_size = pip_size_for_symbol(symbol)
    if side == "buy":
        return round((exit_price - entry) / pip_size, 1)
    return round((entry - exit_price) / pip_size, 1)


def calc_profit(pips, lot_size):
    # Keeps same behavior as your old sheet examples:
    # 10 pips with 1000 lot_size = 1.0
    return round(pips * (lot_size / 10000), 2)


# =========================
# GOOGLE SHEETS
# =========================

def get_sheet():
    if not GOOGLE_SHEET_ID:
        print("Google Sheets disabled: GOOGLE_SHEET_ID not set")
        return None

    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        print("Google Sheets disabled: GOOGLE_SERVICE_ACCOUNT_JSON not set")
        return None

    if gspread is None or Credentials is None:
        print("Google Sheets disabled: gspread/google-auth not installed")
        return None

    try:
        service_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(service_info, scopes=scopes)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)

        try:
            sheet = spreadsheet.worksheet(WORKSHEET_NAME)
        except gspread.WorksheetNotFound:
            print(f"Worksheet {WORKSHEET_NAME} not found; creating it")
            sheet = spreadsheet.add_worksheet(title=WORKSHEET_NAME, rows=1000, cols=len(HEADERS))

        first_row = sheet.row_values(1)
        if first_row[:len(HEADERS)] != HEADERS:
            sheet.update("A1:M1", [HEADERS])
            print(f"Updated {WORKSHEET_NAME} headers")

        return sheet

    except Exception as e:
        print(f"Google Sheets setup failed: {e}")
        return None


def append_closed_trade(trade, exit_signal, exit_reason, exit_price, exit_dt):
    sheet = get_sheet()
    if sheet is None:
        print("Closed trade not written to sheet because sheet is unavailable")
        return False

    symbol = trade["symbol"]
    side = trade["side"]
    entry = trade["entry"]
    entry_dt = trade["entry_dt"]
    lot_size = trade["lot_size"]

    pips = calc_pips(symbol, side, entry, exit_price)
    profit = calc_profit(pips, lot_size)

    row = [
        symbol,
        side,
        round(entry, 5),
        round(exit_price, 5),
        pips,
        profit,
        lot_size,
        trade["entry_signal"],
        exit_signal,
        fmt_bangkok(entry_dt),
        fmt_bangkok(exit_dt),
        fmt_duration(entry_dt, exit_dt),
        exit_reason,
    ]

    try:
        sheet.append_row(row, value_input_option="USER_ENTERED")
        print(f"Wrote closed trade to {WORKSHEET_NAME}")
        return True
    except Exception as e:
        print(f"Failed writing trade to Google Sheets: {e}")
        return False


# =========================
# ROUTES
# =========================

@app.route("/", methods=["GET"])
def health_check():
    return jsonify({"status": "ok", "service": "tradingview-bot"})


@app.route("/", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}

    if data.get("secret") != SECRET:
        print("IGNORED: unauthorized request")
        return jsonify({"status": "unauthorized"}), 401

    action = str(data.get("action", "")).lower().strip()
    symbol = str(data.get("symbol", "")).upper().strip()
    signal = str(data.get("signal", "")).lower().strip()
    side = str(data.get("side", "")).lower().strip()
    reason = str(data.get("reason", signal)).lower().strip()

    if not symbol:
        return jsonify({"status": "ignored", "reason": "missing symbol"}), 400

    try:
        price = float(data.get("price"))
    except Exception:
        return jsonify({"status": "ignored", "reason": "invalid price"}), 400

    event_dt = parse_event_time(data.get("time"))

    # =========================
    # ENTRY
    # =========================
    if action in {"buy", "sell"}:
        if signal not in ALLOWED_ENTRY_SIGNALS:
            print(f"IGNORED {symbol} {action}: signal not allowed: {signal}")
            return jsonify({"status": "ignored", "reason": "entry signal not allowed"})

        last_exit_dt = last_exit_time_by_symbol.get(symbol)
        if last_exit_dt:
            minutes_since_exit = (event_dt - last_exit_dt).total_seconds() / 60
            if minutes_since_exit < REENTRY_BLOCK_MINUTES:
                print(f"IGNORED {symbol} {action}: re-entry blocked after recent exit")
                return jsonify({"status": "ignored", "reason": "reentry blocked after recent exit"})

        if symbol in current_trades:
            print(f"IGNORED {symbol} {action}: already in {current_trades[symbol]['side']}")
            return jsonify({"status": "ignored", "reason": "already in trade"})

        current_trades[symbol] = {
            "symbol": symbol,
            "side": action,
            "entry": price,
            "entry_signal": signal,
            "entry_dt": event_dt,
            "lot_size": LOT_SIZE,
        }

        print(f"OPENED {symbol} {action.upper()} at {price} Bangkok {fmt_bangkok(event_dt)}")
        return jsonify({"status": "opened", "symbol": symbol, "side": action})

    # =========================
    # EXIT
    # =========================
    if action == "exit":
        if signal not in ALLOWED_EXIT_SIGNALS:
            print(f"IGNORED {symbol} exit: signal not allowed: {signal}")
            return jsonify({"status": "ignored", "reason": "exit signal not allowed"})

        trade = current_trades.get(symbol)
        if not trade:
            print(f"IGNORED {symbol} exit: no open trade")
            return jsonify({"status": "ignored", "reason": "no open trade"})

        if side and side != trade["side"]:
            print(f"IGNORED {symbol} exit: side mismatch. alert={side}, open={trade['side']}")
            return jsonify({"status": "ignored", "reason": "side mismatch"})

        wrote = append_closed_trade(trade, signal, reason, price, event_dt)

        del current_trades[symbol]
        last_exit_time_by_symbol[symbol] = event_dt

        print(f"CLOSED {symbol} {trade['side'].upper()} at {price} Bangkok {fmt_bangkok(event_dt)}")
        return jsonify({"status": "closed", "symbol": symbol, "sheet_written": wrote})

    print(f"IGNORED {symbol}: unknown action {action}")
    return jsonify({"status": "ignored", "reason": "unknown action"}), 400


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
