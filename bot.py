import os
import json
import smtplib
import requests
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from flask import Flask, request

app = Flask(__name__)

BANGKOK_TZ = timezone(timedelta(hours=7))
current_trades = {}

SECRET = os.getenv("WEBHOOK_SECRET", "chidrew1")

LOT_SIZE = float(os.getenv("LOT_SIZE", "1000"))
STOP_LOSS_PIPS = float(os.getenv("STOP_LOSS_PIPS", "5"))
TAKE_PROFIT_PIPS = float(os.getenv("TAKE_PROFIT_PIPS", "7"))

MIN_ADX = float(os.getenv("MIN_ADX", "20"))
MIN_DI_GAP = float(os.getenv("MIN_DI_GAP", "7"))
MIN_SCORE = float(os.getenv("MIN_SCORE", "70"))

ALLOW_PULLBACKS = os.getenv("ALLOW_PULLBACKS", "false").lower() == "true"
IGNORE_PROFITABLE_INDICATOR_EXIT = os.getenv("IGNORE_PROFITABLE_INDICATOR_EXIT", "true").lower() == "true"

GOOGLE_SHEET_WEBAPP_URL = os.getenv("GOOGLE_SHEET_WEBAPP_URL", "")

EMAIL_ON_CLOSE = os.getenv("EMAIL_ON_CLOSE", "true").lower() == "true"
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
EMAIL_TO = os.getenv("EMAIL_TO", "")


def now_bangkok():
    return datetime.now(BANGKOK_TZ).strftime("%Y-%m-%d %H:%M:%S")


def parse_alert_time(value):
    if not value:
        return now_bangkok()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(BANGKOK_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return now_bangkok()


def pip_size(symbol):
    symbol = symbol.upper()
    if "JPY" in symbol:
        return 0.01
    return 0.0001


def pips_to_price(symbol, pips):
    return pips * pip_size(symbol)


def calc_pips(symbol, side, entry, exit_price):
    ps = pip_size(symbol)
    if side == "buy":
        return round((exit_price - entry) / ps, 1)
    return round((entry - exit_price) / ps, 1)


def calc_profit(pips):
    return round((pips * LOT_SIZE) / 10000, 2)


def send_close_email(row):
    if not EMAIL_ON_CLOSE:
        return

    if not SMTP_USER or not SMTP_PASSWORD or not EMAIL_TO:
        print("EMAIL SKIPPED: missing SMTP_USER, SMTP_PASSWORD, or EMAIL_TO")
        return

    symbol, side, entry, exit_price, pips, profit, lot_size, entry_signal, exit_signal, entry_time, exit_time, duration, exit_reason = row

    subject = f"Trade Closed: {symbol} {side.upper()} | {pips} pips"

    body = f"""
Trade closed and written to Google Sheets.

Symbol: {symbol}
Side: {side}
Entry: {entry}
Exit: {exit_price}
Pips: {pips}
Profit: {profit}
Lot size: {lot_size}

Entry signal: {entry_signal}
Exit signal: {exit_signal}
Exit reason: {exit_reason}

Entry time Bangkok: {entry_time}
Exit time Bangkok: {exit_time}
Duration: {duration}
""".strip()

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)

        print(f"EMAIL SENT: {EMAIL_TO}")

    except Exception as e:
        print(f"EMAIL ERROR: {e}")


def send_to_google_sheet(row):
    if not GOOGLE_SHEET_WEBAPP_URL:
        print("GOOGLE SHEET SKIPPED: missing GOOGLE_SHEET_WEBAPP_URL")
        return False

    try:
        response = requests.post(
            GOOGLE_SHEET_WEBAPP_URL,
            json={"row": row},
            timeout=10
        )

        if response.status_code >= 400:
            print(f"GOOGLE SHEET ERROR: {response.status_code} {response.text}")
            return False

        print(f"Sent to Google Sheets: {row}")
        return True

    except Exception as e:
        print(f"GOOGLE SHEET ERROR: {e}")
        return False


def close_trade(symbol, exit_price, exit_signal, exit_reason):
    symbol = symbol.upper()

    if symbol not in current_trades:
        return {"status": "ignored", "reason": "no_open_trade"}

    trade = current_trades[symbol]

    side = trade["side"]
    entry = trade["entry"]
    entry_time_dt = trade["entry_time_dt"]

    pips = calc_pips(symbol, side, entry, exit_price)
    profit = calc_profit(pips)

    exit_time_dt = datetime.now(BANGKOK_TZ)
    duration = str(exit_time_dt - entry_time_dt).split(".")[0]

    row = [
        symbol,
        side,
        entry,
        exit_price,
        pips,
        profit,
        LOT_SIZE,
        trade["entry_signal"],
        exit_signal,
        entry_time_dt.strftime("%Y-%m-%d %H:%M:%S"),
        exit_time_dt.strftime("%Y-%m-%d %H:%M:%S"),
        duration,
        exit_reason
    ]

    written = send_to_google_sheet(row)

    if written:
        send_close_email(row)

    del current_trades[symbol]

    print(f"CLOSED {symbol} {side} | {exit_signal} | {pips} pips")

    return {
        "status": "closed",
        "symbol": symbol,
        "side": side,
        "pips": pips,
        "profit": profit,
        "exit_reason": exit_reason
    }


def passes_entry_filters(data):
    action = data.get("action", "").lower()
    signal = data.get("signal", "").lower()

    allowed_entry_signals = ["buy_continuation", "sell_continuation"]

    if ALLOW_PULLBACKS:
        allowed_entry_signals += ["buy_pullback", "sell_pullback"]

    if signal not in allowed_entry_signals:
        return False, "signal_not_allowed"

    try:
        adx = float(data.get("adx", 0))
        plus_di = float(data.get("plus_di", 0))
        minus_di = float(data.get("minus_di", 0))
        buy_score = float(data.get("buy_score", 0))
        sell_score = float(data.get("sell_score", 0))
    except Exception:
        return False, "bad_filter_data"

    if adx < MIN_ADX:
        return False, "low_adx"

    if abs(plus_di - minus_di) < MIN_DI_GAP:
        return False, "weak_direction"

    if action == "buy" and buy_score < MIN_SCORE:
        return False, "low_buy_score"

    if action == "sell" and sell_score < MIN_SCORE:
        return False, "low_sell_score"

    return True, "passed"


@app.route("/", methods=["GET"])
def health():
    return {
        "status": "running",
        "open_trades": list(current_trades.keys()),
        "bangkok_time": now_bangkok()
    }


@app.route("/", methods=["POST"])
def webhook():
    data = request.json or {}

    print(f"ALERT RECEIVED: {data}")

    if data.get("secret") != SECRET:
        return {"status": "unauthorized"}, 401

    action = data.get("action", "").lower()
    symbol = data.get("symbol", "").upper()
    signal = data.get("signal", "").lower()
    reason = data.get("reason", signal)

    if not symbol:
        return {"status": "ignored", "reason": "missing_symbol"}

    try:
        price = float(data.get("price"))
    except Exception:
        return {"status": "ignored", "reason": "bad_price"}

    if action in ["buy", "sell"]:
        if symbol in current_trades:
            return {"status": "ignored", "reason": "trade_already_open"}

        passed, filter_reason = passes_entry_filters(data)

        if not passed:
            print(f"FILTERED {symbol} {action} {signal}: {filter_reason}")
            return {"status": "filtered", "reason": filter_reason}

        ps = pip_size(symbol)

        if action == "buy":
            stop_loss = round(price - pips_to_price(symbol, STOP_LOSS_PIPS), 5)
            take_profit = round(price + pips_to_price(symbol, TAKE_PROFIT_PIPS), 5)
        else:
            stop_loss = round(price + pips_to_price(symbol, STOP_LOSS_PIPS), 5)
            take_profit = round(price - pips_to_price(symbol, TAKE_PROFIT_PIPS), 5)

        current_trades[symbol] = {
            "symbol": symbol,
            "side": action,
            "entry": price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "entry_signal": signal,
            "entry_time": parse_alert_time(data.get("time")),
            "entry_time_dt": datetime.now(BANGKOK_TZ)
        }

        print(f"OPENED {symbol} {action} | entry={price} | SL={stop_loss} | TP={take_profit}")

        return {
            "status": "opened",
            "symbol": symbol,
            "side": action,
            "entry": price,
            "stop_loss": stop_loss,
            "take_profit": take_profit
        }

    if action == "update":
        if symbol not in current_trades:
            return {"status": "ignored", "reason": "no_open_trade"}

        trade = current_trades[symbol]
        side = trade["side"]

        high = float(data.get("high", price))
        low = float(data.get("low", price))

        if side == "buy":
            if low <= trade["stop_loss"]:
                return close_trade(symbol, trade["stop_loss"], "stop_loss_buy", "stop_loss")
            if high >= trade["take_profit"]:
                return close_trade(symbol, trade["take_profit"], "take_profit_buy", "take_profit")

        if side == "sell":
            if high >= trade["stop_loss"]:
                return close_trade(symbol, trade["stop_loss"], "stop_loss_sell", "stop_loss")
            if low <= trade["take_profit"]:
                return close_trade(symbol, trade["take_profit"], "take_profit_sell", "take_profit")

        return {"status": "updated", "reason": "no_exit"}

    if action == "exit":
        if symbol not in current_trades:
            return {"status": "ignored", "reason": "no_open_trade"}

        trade = current_trades[symbol]
        current_pips = calc_pips(symbol, trade["side"], trade["entry"], price)

        if reason == "indicator_exit" and IGNORE_PROFITABLE_INDICATOR_EXIT and current_pips > 0:
            print(f"IGNORED PROFITABLE INDICATOR EXIT: {symbol} {current_pips} pips")
            return {
                "status": "ignored_exit",
                "reason": "let_winner_run",
                "current_pips": current_pips
            }

        return close_trade(symbol, price, signal, reason)

    return {"status": "ignored", "reason": "unknown_action"}


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    print("Starting tradingview-bot...")
    print(f"Listening on port {port}")
    app.run(host="0.0.0.0", port=port)