import os
import smtplib
import requests
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from flask import Flask, request

app = Flask(__name__)

BANGKOK_TZ = timezone(timedelta(hours=7))

# Open trades are stored in memory by symbol.
current_trades = {}

# Prevent duplicate close/write when multiple TradingView alerts hit at the same time.
closing_trades = set()

SECRET = os.getenv("WEBHOOK_SECRET", "chidrew1")

LOT_SIZE = float(os.getenv("LOT_SIZE", "1000"))
STOP_LOSS_PIPS = float(os.getenv("STOP_LOSS_PIPS", "5"))
TAKE_PROFIT_PIPS = float(os.getenv("TAKE_PROFIT_PIPS", "12"))

# Entry filters
MIN_ADX = float(os.getenv("MIN_ADX", "15"))
MIN_DI_GAP = float(os.getenv("MIN_DI_GAP", "3"))
MIN_SCORE = float(os.getenv("MIN_SCORE", "50"))
ALLOW_PULLBACKS = os.getenv("ALLOW_PULLBACKS", "true").lower() == "true"

# Profit protection / early-exit settings
USE_PROFIT_PROTECTION = os.getenv("USE_PROFIT_PROTECTION", "true").lower() == "true"
BREAKEVEN_TRIGGER_PIPS = float(os.getenv("BREAKEVEN_TRIGGER_PIPS", "4"))
LOCK_PROFIT_TRIGGER_PIPS = float(os.getenv("LOCK_PROFIT_TRIGGER_PIPS", "8"))
LOCK_PROFIT_PIPS = float(os.getenv("LOCK_PROFIT_PIPS", "3"))

# Hard early-loss protection: cuts losers before full SL, without relying on indicator exits.
# This version is CLOSE-based: it uses the alert close/price, not the candle wick.
USE_EARLY_LOSS_EXIT = os.getenv("USE_EARLY_LOSS_EXIT", "true").lower() == "true"
EARLY_LOSS_EXIT_PIPS = float(os.getenv("EARLY_LOSS_EXIT_PIPS", "3"))

# Trailing stop settings. The hard TP should be raised in Railway so winners can run.
USE_TRAILING_STOP = os.getenv("USE_TRAILING_STOP", "false").lower() == "true"
TRAIL_START_PIPS = float(os.getenv("TRAIL_START_PIPS", "8"))
TRAIL_DISTANCE_PIPS = float(os.getenv("TRAIL_DISTANCE_PIPS", "5"))
TRAIL_STEP_PIPS = float(os.getenv("TRAIL_STEP_PIPS", "1"))

# Indicator exits act as early exits, but only when useful.
ALLOW_INDICATOR_EXITS = os.getenv("ALLOW_INDICATOR_EXITS", "false").lower() == "true"
INDICATOR_EXIT_MIN_PROFIT_PIPS = float(os.getenv("INDICATOR_EXIT_MIN_PROFIT_PIPS", "1"))
INDICATOR_EXIT_MAX_LOSS_PIPS = float(os.getenv("INDICATOR_EXIT_MAX_LOSS_PIPS", "2"))

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


def round_price(symbol, price):
    return round(price, 3 if "JPY" in symbol.upper() else 5)


def calc_pips(symbol, side, entry, exit_price):
    ps = pip_size(symbol)
    if side == "buy":
        return round((exit_price - entry) / ps, 1)
    return round((entry - exit_price) / ps, 1)


def calc_profit(pips):
    return round((pips * LOT_SIZE) / 10000, 2)


def correct_exit_signal(side, exit_reason):
    if exit_reason == "stop_loss":
        return "stop_loss_buy" if side == "buy" else "stop_loss_sell"
    if exit_reason == "take_profit":
        return "take_profit_buy" if side == "buy" else "take_profit_sell"
    if exit_reason == "breakeven_exit":
        return "breakeven_exit_buy" if side == "buy" else "breakeven_exit_sell"
    if exit_reason == "locked_profit_exit":
        return "locked_profit_exit_buy" if side == "buy" else "locked_profit_exit_sell"
    if exit_reason == "early_profit_exit":
        return "early_profit_exit_buy" if side == "buy" else "early_profit_exit_sell"
    if exit_reason == "early_loss_exit":
        return "early_loss_exit_buy" if side == "buy" else "early_loss_exit_sell"
    if exit_reason == "trailing_stop":
        return "trailing_stop_buy" if side == "buy" else "trailing_stop_sell"
    return "exit_buy" if side == "buy" else "exit_sell"


def price_hit_stop_or_target(trade, price=None, high=None, low=None):
    side = trade["side"]
    stop_loss = trade["stop_loss"]
    take_profit = trade["take_profit"]

    if price is not None:
        high = price if high is None else high
        low = price if low is None else low

    if side == "buy":
        if low <= stop_loss:
            reason = trade.get("stop_reason", "stop_loss")
            return reason, stop_loss
        if high >= take_profit:
            return "take_profit", take_profit

    if side == "sell":
        if high >= stop_loss:
            reason = trade.get("stop_reason", "stop_loss")
            return reason, stop_loss
        if low <= take_profit:
            return "take_profit", take_profit

    return None, None


def early_loss_exit_hit(symbol, trade, price=None, high=None, low=None):
    """Close-based early-loss exit.

    IMPORTANT: This intentionally uses the alert close/price, not the candle high/low.
    That keeps the normal 5-pip SL for breathing room, but cuts trades that actually
    CLOSE around the early-loss threshold.
    """
    if not USE_EARLY_LOSS_EXIT or price is None:
        return None, None

    current_pips = calc_pips(symbol, trade["side"], trade["entry"], price)

    if current_pips <= -EARLY_LOSS_EXIT_PIPS:
        return "early_loss_exit", round_price(symbol, price)

    return None, None

def best_open_profit_pips(symbol, trade, high, low):
    if trade["side"] == "buy":
        return calc_pips(symbol, "buy", trade["entry"], high)
    return calc_pips(symbol, "sell", trade["entry"], low)


def update_profit_protection(symbol, trade, high, low):
    if not USE_PROFIT_PROTECTION:
        return

    side = trade["side"]
    entry = trade["entry"]
    best_pips = best_open_profit_pips(symbol, trade, high, low)

    # Stage 1: move stop to breakeven once the trade has moved in your favor.
    if best_pips >= BREAKEVEN_TRIGGER_PIPS and not trade.get("breakeven_done", False):
        trade["stop_loss"] = round_price(symbol, entry)
        trade["stop_reason"] = "breakeven_exit"
        trade["breakeven_done"] = True
        print(f"PROTECT {symbol}: moved stop to breakeven at {trade['stop_loss']}")

    # Stage 2: lock profit once the trade moves further in your favor.
    if best_pips >= LOCK_PROFIT_TRIGGER_PIPS and not trade.get("lock_done", False):
        if side == "buy":
            new_stop = entry + pips_to_price(symbol, LOCK_PROFIT_PIPS)
        else:
            new_stop = entry - pips_to_price(symbol, LOCK_PROFIT_PIPS)

        trade["stop_loss"] = round_price(symbol, new_stop)
        trade["stop_reason"] = "locked_profit_exit"
        trade["lock_done"] = True
        print(f"PROTECT {symbol}: locked +{LOCK_PROFIT_PIPS} pip at stop {trade['stop_loss']}")


def update_trailing_stop(symbol, trade, high, low):
    """Trail behind the best price once the trade reaches TRAIL_START_PIPS.

    Buy:  stop follows highest high minus TRAIL_DISTANCE_PIPS.
    Sell: stop follows lowest low plus TRAIL_DISTANCE_PIPS.
    The stop only moves in the trade's favor by at least TRAIL_STEP_PIPS.
    """
    if not USE_TRAILING_STOP:
        return

    side = trade["side"]
    entry = trade["entry"]
    current_stop = trade["stop_loss"]
    step_price = pips_to_price(symbol, TRAIL_STEP_PIPS)

    best_pips = best_open_profit_pips(symbol, trade, high, low)
    trade["best_pips"] = max(float(trade.get("best_pips", 0)), best_pips)

    if trade["best_pips"] < TRAIL_START_PIPS:
        return

    if side == "buy":
        new_stop = round_price(symbol, high - pips_to_price(symbol, TRAIL_DISTANCE_PIPS))
        if new_stop > current_stop + step_price:
            trade["stop_loss"] = new_stop
            trade["stop_reason"] = "trailing_stop"
            print(f"TRAIL {symbol}: buy stop moved to {new_stop} after best +{trade['best_pips']} pips")

    if side == "sell":
        new_stop = round_price(symbol, low + pips_to_price(symbol, TRAIL_DISTANCE_PIPS))
        if new_stop < current_stop - step_price:
            trade["stop_loss"] = new_stop
            trade["stop_reason"] = "trailing_stop"
            print(f"TRAIL {symbol}: sell stop moved to {new_stop} after best +{trade['best_pips']} pips")


def price_hit_active_stop(trade, price=None, high=None, low=None):
    side = trade["side"]
    stop_loss = trade["stop_loss"]

    if price is not None:
        high = price if high is None else high
        low = price if low is None else low

    if side == "buy" and low <= stop_loss:
        return trade.get("stop_reason", "stop_loss"), stop_loss

    if side == "sell" and high >= stop_loss:
        return trade.get("stop_reason", "stop_loss"), stop_loss

    return None, None


def price_hit_emergency_take_profit(trade, price=None, high=None, low=None):
    side = trade["side"]
    take_profit = trade["take_profit"]

    if price is not None:
        high = price if high is None else high
        low = price if low is None else low

    if side == "buy" and high >= take_profit:
        return "take_profit", take_profit

    if side == "sell" and low <= take_profit:
        return "take_profit", take_profit

    return None, None


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


def close_trade(symbol, exit_price, exit_reason):
    symbol = symbol.upper()

    # Prevent duplicate closes/writes when multiple alerts hit together.
    if symbol in closing_trades:
        print(f"SKIPPED DUPLICATE CLOSE: {symbol}")
        return {"status": "ignored", "reason": "already_closing"}

    if symbol not in current_trades:
        return {"status": "ignored", "reason": "no_open_trade"}

    closing_trades.add(symbol)

    try:
        trade = current_trades[symbol]

        side = trade["side"]
        entry = trade["entry"]
        entry_time_dt = trade["entry_time_dt"]

        exit_price = round_price(symbol, exit_price)
        exit_signal = correct_exit_signal(side, exit_reason)
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

        current_trades.pop(symbol, None)

        print(f"CLOSED {symbol} {side} | {exit_signal} | {pips} pips | reason={exit_reason}")

        return {
            "status": "closed",
            "symbol": symbol,
            "side": side,
            "pips": pips,
            "profit": profit,
            "exit_reason": exit_reason
        }

    finally:
        closing_trades.discard(symbol)


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
        "closing_trades": list(closing_trades),
        "bangkok_time": now_bangkok(),
        "settings": {
            "lot_size": LOT_SIZE,
            "stop_loss_pips": STOP_LOSS_PIPS,
            "take_profit_pips": TAKE_PROFIT_PIPS,
            "min_adx": MIN_ADX,
            "min_di_gap": MIN_DI_GAP,
            "min_score": MIN_SCORE,
            "allow_pullbacks": ALLOW_PULLBACKS,
            "use_profit_protection": USE_PROFIT_PROTECTION,
            "breakeven_trigger_pips": BREAKEVEN_TRIGGER_PIPS,
            "lock_profit_trigger_pips": LOCK_PROFIT_TRIGGER_PIPS,
            "lock_profit_pips": LOCK_PROFIT_PIPS,
            "use_early_loss_exit": USE_EARLY_LOSS_EXIT,
            "early_loss_exit_pips": EARLY_LOSS_EXIT_PIPS,
            "use_trailing_stop": USE_TRAILING_STOP,
            "trail_start_pips": TRAIL_START_PIPS,
            "trail_distance_pips": TRAIL_DISTANCE_PIPS,
            "trail_step_pips": TRAIL_STEP_PIPS,
            "allow_indicator_exits": ALLOW_INDICATOR_EXITS,
            "indicator_exit_min_profit_pips": INDICATOR_EXIT_MIN_PROFIT_PIPS,
            "indicator_exit_max_loss_pips": INDICATOR_EXIT_MAX_LOSS_PIPS
        }
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
        if symbol in current_trades or symbol in closing_trades:
            return {"status": "ignored", "reason": "trade_already_open_or_closing"}

        passed, filter_reason = passes_entry_filters(data)

        if not passed:
            print(f"FILTERED {symbol} {action} {signal}: {filter_reason}")
            return {"status": "filtered", "reason": filter_reason}

        if action == "buy":
            stop_loss = round_price(symbol, price - pips_to_price(symbol, STOP_LOSS_PIPS))
            take_profit = round_price(symbol, price + pips_to_price(symbol, TAKE_PROFIT_PIPS))
        else:
            stop_loss = round_price(symbol, price + pips_to_price(symbol, STOP_LOSS_PIPS))
            take_profit = round_price(symbol, price - pips_to_price(symbol, TAKE_PROFIT_PIPS))

        current_trades[symbol] = {
            "symbol": symbol,
            "side": action,
            "entry": price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "stop_reason": "stop_loss",
            "breakeven_done": False,
            "lock_done": False,
            "best_pips": 0,
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

        if symbol in closing_trades:
            return {"status": "ignored", "reason": "already_closing"}

        trade = current_trades[symbol]

        try:
            high = float(data.get("high", price))
            low = float(data.get("low", price))
        except Exception:
            return {"status": "ignored", "reason": "bad_high_low"}

        # 1) Close-based early-loss check FIRST. This uses close/price, not wick high/low.
        exit_reason, exit_price = early_loss_exit_hit(symbol, trade, price=price)
        if exit_reason:
            print(f"EARLY LOSS EXIT FROM PRICE UPDATE: {symbol} {trade['side']} at {exit_price}")
            return close_trade(symbol, exit_price, exit_reason)

        # 2) Move stop to breakeven / locked profit before checking TP.
        update_profit_protection(symbol, trade, high, low)

        # 3) Trail the stop after the trade has moved far enough in your favor.
        update_trailing_stop(symbol, trade, high, low)

        # 4) Now check the active stop. This may be original SL, breakeven, locked, or trailing.
        exit_reason, exit_price = price_hit_active_stop(trade, high=high, low=low)
        if exit_reason:
            return close_trade(symbol, exit_price, exit_reason)

        # 5) Finally check emergency take-profit. With TP raised to 30, this no longer caps normal winners.
        exit_reason, exit_price = price_hit_emergency_take_profit(trade, high=high, low=low)
        if exit_reason:
            return close_trade(symbol, exit_price, exit_reason)

        return {
            "status": "updated",
            "reason": "no_exit",
            "stop_loss": trade["stop_loss"],
            "take_profit": trade["take_profit"]
        }

    if action == "exit":
        if symbol not in current_trades:
            return {"status": "ignored", "reason": "no_open_trade"}

        if symbol in closing_trades:
            print(f"SKIPPED DUPLICATE EXIT ALERT: {symbol}")
            return {"status": "ignored", "reason": "already_closing"}

        trade = current_trades[symbol]

        # Close-based early-loss still gets priority on exit alerts.
        exit_reason, exit_price = early_loss_exit_hit(symbol, trade, price=price)
        if exit_reason:
            print(f"EARLY LOSS EXIT FROM EXIT ALERT: {symbol} {trade['side']} at {exit_price}")
            return close_trade(symbol, exit_price, exit_reason)

        # Then respect the active bot stop if price is already beyond it.
        exit_reason, exit_price = price_hit_active_stop(trade, price=price)
        if exit_reason:
            print(f"FORCED EXIT FROM EXIT ALERT: {symbol} {trade['side']} at {exit_price} reason={exit_reason}")
            return close_trade(symbol, exit_price, exit_reason)

        # Finally respect emergency take-profit if price is already beyond it.
        exit_reason, exit_price = price_hit_emergency_take_profit(trade, price=price)
        if exit_reason:
            print(f"FORCED EXIT FROM EXIT ALERT: {symbol} {trade['side']} at {exit_price} reason={exit_reason}")
            return close_trade(symbol, exit_price, exit_reason)

        if reason == "indicator_exit" or signal in ["exit_buy", "exit_sell"]:
            current_pips = calc_pips(symbol, trade["side"], trade["entry"], price)

            if not ALLOW_INDICATOR_EXITS:
                print(f"IGNORED INDICATOR EXIT: {symbol} {trade['side']} | {current_pips} pips")
                return {
                    "status": "ignored_exit",
                    "reason": "indicator_exits_disabled",
                    "current_pips": current_pips
                }

            # Lock small profits when the indicator says momentum faded.
            if current_pips >= INDICATOR_EXIT_MIN_PROFIT_PIPS:
                print(f"EARLY PROFIT EXIT: {symbol} {trade['side']} | {current_pips} pips")
                return close_trade(symbol, price, "early_profit_exit")

            # Cut bad trades early instead of always waiting for full SL.
            if current_pips <= -INDICATOR_EXIT_MAX_LOSS_PIPS:
                print(f"EARLY LOSS EXIT: {symbol} {trade['side']} | {current_pips} pips")
                return close_trade(symbol, price, "early_loss_exit")

            print(f"IGNORED SMALL INDICATOR EXIT: {symbol} {trade['side']} | {current_pips} pips")
            return {
                "status": "ignored_exit",
                "reason": "indicator_exit_too_small",
                "current_pips": current_pips
            }

        return close_trade(symbol, price, "manual_exit")

    return {"status": "ignored", "reason": "unknown_action"}


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    print("Starting tradingview-bot...")
    print(f"Listening on port {port}")
    app.run(host="0.0.0.0", port=port)
