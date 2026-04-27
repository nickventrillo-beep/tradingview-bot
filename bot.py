import os
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import Flask, request

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
TIMEZONE = ZoneInfo("Asia/Bangkok")
REENTRY_BLOCK_MINUTES = int(os.getenv("REENTRY_BLOCK_MINUTES", "15"))

# Google Sheets env vars expected on Railway:
# GOOGLE_SHEET_ID = your spreadsheet ID
# GOOGLE_SERVICE_ACCOUNT_JSON = full service account JSON text
# Optional: GOOGLE_SHEET_TAB = worksheet tab name, defaults to Trades
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_SHEET_TAB = os.getenv("GOOGLE_SHEET_TAB", "Trades")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")

ALLOWED_ENTRY_SIGNALS = {
    "buy_pullback",
    "sell_pullback",
}

ALLOWED_EXIT_SIGNALS = {
    "stop_loss_buy",
    "take_profit_buy",
    "exit_buy",
    "stop_loss_sell",
    "take_profit_sell",
    "exit_sell",
}

# One open trade per symbol.
current_trades = {}
last_exit_time_by_symbol = {}


def now_utc():
    return datetime.now(timezone.utc)


def fmt_bangkok(dt):
    return dt.astimezone(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def parse_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def get_sheet():
    if not GOOGLE_SHEET_ID:
        print("Google Sheets disabled: GOOGLE_SHEET_ID not set")
        return None

    if gspread is None or Credentials is None:
        print("Google Sheets disabled: gspread/google-auth not installed")
        return None

    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        print("Google Sheets disabled: GOOGLE_SERVICE_ACCOUNT_JSON not set")
        return None

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    service_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(service_info, scopes=scopes)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)

    try:
        return spreadsheet.worksheet(GOOGLE_SHEET_TAB)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=GOOGLE_SHEET_TAB, rows=1000, cols=20)


def ensure_headers(sheet):
    headers = [
        "Symbol",
        "Side",
        "Entry Signal",
        "Entry Time Bangkok",
        "Entry Price",
        "Exit Signal",
        "Exit Reason",
        "Exit Time Bangkok",
        "Exit Price",
        "Pips",
        "Status",
    ]

    existing = sheet.row_values(1)
    if existing != headers:
        sheet.update("A1:K1", [headers])


def append_closed_trade(trade, exit_signal, exit_reason, exit_price, exit_time):
    sheet = get_sheet()
    if sheet is None:
        print("Closed trade not written to sheet because sheet is unavailable")
        return

    ensure_headers(sheet)

    symbol = trade["symbol"]
    side = trade["side"]
    entry_price = trade["entry_price"]
    entry_signal = trade["entry_signal"]
    entry_time = trade["entry_time"]

    pip_size = 0.01 if "JPY" in symbol.upper() else 0.0001
    if side == "buy":
        pips = (exit_price - entry_price) / pip_size
    else:
        pips = (entry_price - exit_price) / pip_size

    row = [
        symbol,
        side.upper(),
        entry_signal,
        fmt_bangkok(entry_time),
        round(entry_price, 6),
        exit_signal,
        exit_reason,
        fmt_bangkok(exit_time),
        round(exit_price, 6),
        round(pips, 1),
        "CLOSED",
    ]

    sheet.append_row(row, value_input_option="USER_ENTERED")
    print(f"WROTE GOOGLE SHEET: {symbol} {side.upper()} closed {round(pips, 1)} pips")


@app.route("/", methods=["GET"])
def health_check():
    return {"status": "running", "bot": "pullbacks_only"}


@app.route("/", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    event_time = now_utc()

    if data.get("secret") != SECRET:
        print("UNAUTHORIZED webhook")
        return {"status": "unauthorized"}, 401

    action = str(data.get("action", "")).lower().strip()
    signal = str(data.get("signal", "")).lower().strip()
    symbol = str(data.get("symbol", "")).upper().strip()
    side = str(data.get("side", "")).lower().strip()
    reason = str(data.get("reason", signal)).lower().strip()
    price = parse_float(data.get("price"))

    if not symbol:
        return {"status": "ignored", "reason": "missing symbol"}

    if price is None:
        return {"status": "ignored", "reason": "missing or invalid price"}

    # =========================
    # ENTRY HANDLING
    # =========================
    if action in {"buy", "sell"}:
        if signal not in ALLOWED_ENTRY_SIGNALS:
            print(f"IGNORED {symbol} {signal}: not an allowed pullback entry")
            return {"status": "ignored", "reason": "entry signal not allowed"}

        expected_signal = "buy_pullback" if action == "buy" else "sell_pullback"
        if signal != expected_signal:
            print(f"IGNORED {symbol} {signal}: action/signal mismatch")
            return {"status": "ignored", "reason": "action signal mismatch"}

        last_exit_time = last_exit_time_by_symbol.get(symbol)
        if last_exit_time:
            minutes_since_exit = (event_time - last_exit_time).total_seconds() / 60
            if minutes_since_exit < REENTRY_BLOCK_MINUTES:
                print(f"IGNORED {symbol} {action}: re-entry blocked for {REENTRY_BLOCK_MINUTES} minutes after exit")
                return {"status": "ignored", "reason": "reentry blocked after recent exit"}

        if symbol in current_trades:
            existing = current_trades[symbol]
            print(f"IGNORED {symbol} {action}: already in {existing['side'].upper()} trade")
            return {"status": "ignored", "reason": "already in trade"}

        current_trades[symbol] = {
            "symbol": symbol,
            "side": action,
            "entry_signal": signal,
            "entry_price": price,
            "entry_time": event_time,
        }

        print(f"OPENED {symbol} {action.upper()} at {price} Bangkok {fmt_bangkok(event_time)}")
        return {"status": "opened", "symbol": symbol, "side": action, "price": price}

    # =========================
    # EXIT HANDLING
    # =========================
    if action == "exit":
        if signal not in ALLOWED_EXIT_SIGNALS:
            print(f"IGNORED {symbol} {signal}: not an allowed exit signal")
            return {"status": "ignored", "reason": "exit signal not allowed"}

        trade = current_trades.get(symbol)
        if not trade:
            print(f"IGNORED {symbol} exit: no open trade")
            return {"status": "ignored", "reason": "no open trade"}

        trade_side = trade["side"]
        if side and side != trade_side:
            print(f"IGNORED {symbol} exit: side mismatch. Open={trade_side}, alert={side}")
            return {"status": "ignored", "reason": "exit side mismatch"}

        if signal.endswith("_buy") and trade_side != "buy":
            print(f"IGNORED {symbol} {signal}: open trade is not BUY")
            return {"status": "ignored", "reason": "wrong exit side"}

        if signal.endswith("_sell") and trade_side != "sell":
            print(f"IGNORED {symbol} {signal}: open trade is not SELL")
            return {"status": "ignored", "reason": "wrong exit side"}

        append_closed_trade(trade, signal, reason, price, event_time)
        del current_trades[symbol]
        last_exit_time_by_symbol[symbol] = event_time

        print(f"CLOSED {symbol} {trade_side.upper()} at {price} Bangkok {fmt_bangkok(event_time)}")
        return {"status": "closed", "symbol": symbol, "side": trade_side, "price": price}

    print(f"IGNORED {symbol}: unknown action {action}")
    return {"status": "ignored", "reason": "unknown action"}


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
