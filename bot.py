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

# =====================================================
# BASIC SETTINGS
# =====================================================

SECRET = os.getenv("WEBHOOK_SECRET", "chidrew1")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
WORKSHEET_NAME = os.getenv("GOOGLE_WORKSHEET_NAME", "Sheet1").strip()

LOT_SIZE = float(os.getenv("LOT_SIZE", "1000"))
REENTRY_BLOCK_MINUTES = int(os.getenv("REENTRY_BLOCK_MINUTES", "15"))
BANGKOK_TZ = ZoneInfo("Asia/Bangkok")

# =====================================================
# RAILWAY CONTROL VARIABLES
# =====================================================

MIN_ADX = float(os.getenv("MIN_ADX", "28"))
MIN_TRADE_BIAS = float(os.getenv("MIN_TRADE_BIAS", "65"))
MIN_EMA_SPREAD = float(os.getenv("MIN_EMA_SPREAD", "0.05"))
MIN_SCORE_GAP = float(os.getenv("MIN_SCORE_GAP", "0"))

REQUIRE_TRENDING = os.getenv("REQUIRE_TRENDING", "true").lower() == "true"
REQUIRE_HTF = os.getenv("REQUIRE_HTF", "true").lower() == "true"
REQUIRE_DI_ALIGNMENT = os.getenv("REQUIRE_DI_ALIGNMENT", "true").lower() == "true"
REQUIRE_SCORE_DOMINANCE = os.getenv("REQUIRE_SCORE_DOMINANCE", "true").lower() == "true"

ALLOW_PULLBACKS = os.getenv("ALLOW_PULLBACKS", "true").lower() == "true"
ALLOW_CONTINUATIONS = os.getenv("ALLOW_CONTINUATIONS", "false").lower() == "true"

# =====================================================
# GOOGLE SHEET HEADERS - MATCHES YOUR SHEET1 FORMAT
# =====================================================

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

PULLBACK_SIGNALS = {"buy_pullback", "sell_pullback"}
CONTINUATION_SIGNALS = {"buy_continuation", "sell_continuation"}
ALLOWED_EXIT_SIGNALS = {
    "exit_buy",
    "exit_sell",
    "stop_loss_buy",
    "take_profit_buy",
    "stop_loss_sell",
    "take_profit_sell",
}

# =====================================================
# STATE
# =====================================================

current_trades = {}
last_exit_time_by_symbol = {}

# =====================================================
# HELPERS
# =====================================================

def safe_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def parse_event_time(value):
    if value is None or str(value).strip() == "":
        return datetime.now(timezone.utc)

    text = str(value).strip()

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


def pip_size_for_symbol(symbol):
    return 0.01 if "JPY" in symbol.upper() else 0.0001


def calc_pips(symbol, side, entry, exit_price):
    pip_size = pip_size_for_symbol(symbol)
    if side == "buy":
        return round((exit_price - entry) / pip_size, 1)
    return round((entry - exit_price) / pip_size, 1)


def calc_profit(pips, lot_size):
    return round(pips * (lot_size / 10000), 2)


def log_reject(symbol, signal, reason):
    print("====================================")
    print(f"IGNORED TRADE: {symbol}")
    print(f"SIGNAL: {signal}")
    print(f"REASON: {reason}")
    print("====================================")


def log_accept(symbol, side, signal, price, metrics):
    print("====================================")
    print(f"ACCEPTED TRADE: {symbol}")
    print(f"SIDE: {side.upper()}")
    print(f"SIGNAL: {signal}")
    print(f"PRICE: {price}")
    print(f"ADX: {metrics['adx']}")
    print(f"TRADE BIAS: {metrics['trade_bias']}")
    print(f"EMA SPREAD %: {metrics['ema_spread_pct']}")
    print(f"MARKET STATE: {metrics['market_state']}")
    print(f"HTF BIAS: {metrics['htf_bias']}")
    print(f"BUY SCORE: {metrics['buy_score']}")
    print(f"SELL SCORE: {metrics['sell_score']}")
    print("====================================")


def log_close(symbol, side, entry, exit_price, pips, profit, exit_reason):
    print("====================================")
    print(f"CLOSED TRADE: {symbol}")
    print(f"SIDE: {side.upper()}")
    print(f"ENTRY: {entry}")
    print(f"EXIT: {exit_price}")
    print(f"PIPS: {pips}")
    print(f"PROFIT: {profit}")
    print(f"EXIT REASON: {exit_reason}")
    print("====================================")

# =====================================================
# GOOGLE SHEETS
# =====================================================

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

# =====================================================
# FILTERING LOGIC
# =====================================================

def extract_metrics(data):
    return {
        "adx": safe_float(data.get("adx"), 0),
        "trade_bias": safe_float(data.get("trade_bias"), 0),
        "buy_score": safe_float(data.get("buy_score"), 0),
        "sell_score": safe_float(data.get("sell_score"), 0),
        "ema_spread_pct": safe_float(data.get("ema_spread_pct"), 0),
        "htf_dist_pct": safe_float(data.get("htf_dist_pct"), 0),
        "plus_di": safe_float(data.get("plus_di"), 0),
        "minus_di": safe_float(data.get("minus_di"), 0),
        # Pine hidden plots: market_state 1=TRENDING, 0=SIDEWAYS; htf_bias 1=BULLISH, -1=BEARISH, 0=NEUTRAL
        "market_state": safe_float(data.get("market_state"), 0),
        "htf_bias": safe_float(data.get("htf_bias"), 0),
    }


def should_accept_entry(symbol, side, signal, event_dt, metrics):
    if signal in PULLBACK_SIGNALS and not ALLOW_PULLBACKS:
        return False, "pullbacks disabled"

    if signal in CONTINUATION_SIGNALS and not ALLOW_CONTINUATIONS:
        return False, "continuations disabled"

    if signal not in PULLBACK_SIGNALS and signal not in CONTINUATION_SIGNALS:
        return False, f"entry signal not allowed: {signal}"

    if symbol in current_trades:
        return False, f"already in trade: {current_trades[symbol]['side']}"

    last_exit_dt = last_exit_time_by_symbol.get(symbol)
    if last_exit_dt:
        minutes_since_exit = (event_dt - last_exit_dt).total_seconds() / 60
        if minutes_since_exit < REENTRY_BLOCK_MINUTES:
            return False, f"re-entry blocked after recent exit ({minutes_since_exit:.1f} minutes ago)"

    if metrics["adx"] < MIN_ADX:
        return False, f"ADX {metrics['adx']:.1f} below minimum {MIN_ADX}"

    if metrics["trade_bias"] < MIN_TRADE_BIAS:
        return False, f"trade_bias {metrics['trade_bias']:.1f} below minimum {MIN_TRADE_BIAS}"

    if metrics["ema_spread_pct"] < MIN_EMA_SPREAD:
        return False, f"EMA spread {metrics['ema_spread_pct']:.3f}% below minimum {MIN_EMA_SPREAD}%"

    if REQUIRE_TRENDING and metrics["market_state"] != 1:
        return False, "market state is not TRENDING"

    if REQUIRE_HTF:
        if side == "buy" and metrics["htf_bias"] != 1:
            return False, "HTF bias is not BULLISH"
        if side == "sell" and metrics["htf_bias"] != -1:
            return False, "HTF bias is not BEARISH"

    if REQUIRE_DI_ALIGNMENT:
        if side == "buy" and metrics["plus_di"] <= metrics["minus_di"]:
            return False, f"DI not aligned for buy: plus_di {metrics['plus_di']:.1f} <= minus_di {metrics['minus_di']:.1f}"
        if side == "sell" and metrics["minus_di"] <= metrics["plus_di"]:
            return False, f"DI not aligned for sell: minus_di {metrics['minus_di']:.1f} <= plus_di {metrics['plus_di']:.1f}"

    if REQUIRE_SCORE_DOMINANCE:
        if side == "buy":
            gap = metrics["buy_score"] - metrics["sell_score"]
            if gap < MIN_SCORE_GAP:
                return False, f"buy score gap {gap:.1f} below minimum {MIN_SCORE_GAP}"
            if metrics["buy_score"] <= metrics["sell_score"]:
                return False, "buy score is not dominant"

        if side == "sell":
            gap = metrics["sell_score"] - metrics["buy_score"]
            if gap < MIN_SCORE_GAP:
                return False, f"sell score gap {gap:.1f} below minimum {MIN_SCORE_GAP}"
            if metrics["sell_score"] <= metrics["buy_score"]:
                return False, "sell score is not dominant"

    return True, "accepted"

# =====================================================
# ROUTES
# =====================================================

@app.route("/", methods=["GET"])
def health_check():
    return jsonify({
        "status": "ok",
        "service": "tradingview-bot",
        "filters": {
            "MIN_ADX": MIN_ADX,
            "MIN_TRADE_BIAS": MIN_TRADE_BIAS,
            "MIN_EMA_SPREAD": MIN_EMA_SPREAD,
            "REQUIRE_TRENDING": REQUIRE_TRENDING,
            "REQUIRE_HTF": REQUIRE_HTF,
            "REQUIRE_DI_ALIGNMENT": REQUIRE_DI_ALIGNMENT,
            "REQUIRE_SCORE_DOMINANCE": REQUIRE_SCORE_DOMINANCE,
            "ALLOW_PULLBACKS": ALLOW_PULLBACKS,
            "ALLOW_CONTINUATIONS": ALLOW_CONTINUATIONS,
            "MIN_SCORE_GAP": MIN_SCORE_GAP,
            "REENTRY_BLOCK_MINUTES": REENTRY_BLOCK_MINUTES,
        }
    })


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

    if action in {"buy", "sell"}:
        side = action
        metrics = extract_metrics(data)

        accepted, reject_reason = should_accept_entry(symbol, side, signal, event_dt, metrics)

        if not accepted:
            log_reject(symbol, signal, reject_reason)
            return jsonify({"status": "ignored", "symbol": symbol, "signal": signal, "reason": reject_reason})

        current_trades[symbol] = {
            "symbol": symbol,
            "side": side,
            "entry": price,
            "entry_signal": signal,
            "entry_dt": event_dt,
            "lot_size": LOT_SIZE,
            "metrics": metrics,
        }

        log_accept(symbol, side, signal, price, metrics)
        return jsonify({"status": "accepted", "symbol": symbol, "side": side, "signal": signal})

    if action == "exit":
        if signal not in ALLOWED_EXIT_SIGNALS:
            log_reject(symbol, signal, f"exit signal not allowed: {signal}")
            return jsonify({"status": "ignored", "symbol": symbol, "signal": signal, "reason": "exit signal not allowed"})

        trade = current_trades.get(symbol)
        if not trade:
            log_reject(symbol, signal, "no open trade")
            return jsonify({"status": "ignored", "symbol": symbol, "signal": signal, "reason": "no open trade"})

        if side and side != trade["side"]:
            log_reject(symbol, signal, f"side mismatch: alert={side}, open={trade['side']}")
            return jsonify({"status": "ignored", "symbol": symbol, "signal": signal, "reason": "side mismatch"})

        pips = calc_pips(symbol, trade["side"], trade["entry"], price)
        profit = calc_profit(pips, trade["lot_size"])

        wrote = append_closed_trade(trade, signal, reason, price, event_dt)

        del current_trades[symbol]
        last_exit_time_by_symbol[symbol] = event_dt

        log_close(symbol, trade["side"], trade["entry"], price, pips, profit, reason)

        return jsonify({"status": "closed", "symbol": symbol, "side": trade["side"], "sheet_written": wrote, "pips": pips, "profit": profit})

    log_reject(symbol, signal, f"unknown action: {action}")
    return jsonify({"status": "ignored", "symbol": symbol, "signal": signal, "reason": "unknown action"}), 400

# =====================================================
# START SERVER - REQUIRED FOR RAILWAY
# =====================================================

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    print("Starting tradingview-bot...")
    print(f"Listening on port {port}")
    app.run(host="0.0.0.0", port=port)
