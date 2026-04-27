import os
from flask import Flask, request, jsonify

app = Flask(__name__)

# =========================
# CONFIG (Railway controls)
# =========================

MIN_ADX = float(os.getenv("MIN_ADX", "28"))
MIN_TRADE_BIAS = float(os.getenv("MIN_TRADE_BIAS", "65"))
MIN_EMA_SPREAD = float(os.getenv("MIN_EMA_SPREAD", "0.05"))
REQUIRE_TRENDING = os.getenv("REQUIRE_TRENDING", "true") == "true"
REQUIRE_HTF = os.getenv("REQUIRE_HTF", "true") == "true"

# =========================
# STATE
# =========================

current_trade = None

# =========================
# HELPER
# =========================

def reject(reason, symbol):
    print(f"IGNORED {symbol}: {reason}")
    return {"status": "ignored", "reason": reason}

# =========================
# WEBHOOK
# =========================

@app.route("/", methods=["POST"])
def webhook():
    global current_trade

    data = request.json
    symbol = data.get("symbol")
    action = data.get("action")

    # Extract metrics
    adx = float(data.get("adx", 0))
    trade_bias = float(data.get("trade_bias", 0))
    ema_spread = float(data.get("ema_spread_pct", 0))
    market_state = data.get("market_state", "")
    htf_bias = data.get("htf_bias", "")
    buy_score = float(data.get("buy_score", 0))
    sell_score = float(data.get("sell_score", 0))

    # =========================
    # ENTRY FILTERS
    # =========================

    if action in ["buy", "sell"]:

        if current_trade is not None:
            return reject("already in trade", symbol)

        if adx < MIN_ADX:
            return reject(f"ADX {adx} below {MIN_ADX}", symbol)

        if trade_bias < MIN_TRADE_BIAS:
            return reject(f"trade_bias {trade_bias} below {MIN_TRADE_BIAS}", symbol)

        if ema_spread < MIN_EMA_SPREAD:
            return reject(f"ema_spread {ema_spread} too low", symbol)

        if REQUIRE_TRENDING and market_state != "TRENDING":
            return reject("market not trending", symbol)

        if REQUIRE_HTF:
            if action == "buy" and htf_bias != "BULLISH":
                return reject("HTF not bullish", symbol)
            if action == "sell" and htf_bias != "BEARISH":
                return reject("HTF not bearish", symbol)

        if action == "buy" and buy_score <= sell_score:
            return reject("buy_score not dominant", symbol)

        if action == "sell" and sell_score <= buy_score:
            return reject("sell_score not dominant", symbol)

        # ACCEPT TRADE
        current_trade = {
            "symbol": symbol,
            "side": action,
            "entry": float(data.get("price"))
        }

        print(f"ACCEPTED {symbol} {action.upper()} at {current_trade['entry']}")
        return {"status": "accepted"}

    # =========================
    # EXIT
    # =========================

    if action == "exit":
        if current_trade is None:
            return reject("no open trade", symbol)

        print(f"CLOSED {symbol} {current_trade['side'].upper()} at {data.get('price')}")
        current_trade = None
        return {"status": "closed"}

    return {"status": "ignored"}