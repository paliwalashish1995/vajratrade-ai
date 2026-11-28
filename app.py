import os
import json
import time
import random
import hashlib
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, request, jsonify, session
from broker_engine import UniversalBrokerEngine

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:
    firebase_admin = None

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "vajratrade_ai_universal_secret_2026")

IST = timezone(timedelta(hours=5, minutes=30))
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "vajratrade_universal.db"))

# --- Firebase Firestore Setup ---
fs_db = None
_mem_cache = {}
_db_lock = threading.Lock()

_fb_key_raw = os.environ.get("FIREBASE_KEY", "").strip()
if _fb_key_raw and firebase_admin is not None:
    try:
        cred_dict = json.loads(_fb_key_raw)
        if not firebase_admin._apps:
            firebase_admin.initialize_app(credentials.Certificate(cred_dict))
        fs_db = firestore.client()
        print("Firebase Firestore connected successfully!")
    except Exception as e:
        print(f"Firebase init failed, using SQLite fallback: {e}")
        fs_db = None


def now_ist():
    return datetime.now(IST)


def hash_pin(pin: str) -> str:
    return hashlib.sha256(("VT_UNI_SALT_99_" + str(pin)).encode("utf-8")).hexdigest()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS kv_store (k TEXT PRIMARY KEY, v TEXT)")
    conn.commit()
    conn.close()


def db_get(key, default):
    with _db_lock:
        if key in _mem_cache:
            try:
                return json.loads(_mem_cache[key])
            except Exception:
                return default

        if fs_db is not None:
            try:
                doc = fs_db.collection("vajratrade_store").document(key).get()
                if doc.exists:
                    raw = doc.to_dict().get("v")
                    if raw is not None:
                        _mem_cache[key] = raw
                        return json.loads(raw)
                return default
            except Exception as e:
                print(f"Firestore read error ({key}): {e}")

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT v FROM kv_store WHERE k = ?", (key,))
        row = c.fetchone()
        conn.close()
        if row:
            try:
                _mem_cache[key] = row[0]
                return json.loads(row[0])
            except Exception:
                return default
        return default


def db_set(key, value):
    raw = json.dumps(value, ensure_ascii=False)
    with _db_lock:
        _mem_cache[key] = raw
        if fs_db is not None:
            try:
                fs_db.collection("vajratrade_store").document(key).set({"v": raw})
            except Exception as e:
                print(f"Firestore write error ({key}): {e}")

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("REPLACE INTO kv_store (k, v) VALUES (?, ?)", (key, raw))
        conn.commit()
        conn.close()


init_db()

DEFAULT_SETTINGS = {
    "mode": "PAPER",
    "allow_24x7": True,
    "market_open": "09:15",
    "market_close": "15:15",
    "daily_capital": 25000.0,
    "daily_profit_target": 5000.0,
    "daily_max_loss": 2000.0,
    "stop_loss_pct": 8.0,
    "target_pct": 18.0,
    "trailing_sl_pct": 4.0,
    "breakeven_lock": True,
    "strategies": {
        "vwap_supertrend": {"name": "VWAP + Supertrend Momentum", "inst": "NIFTY", "lot": 75, "enabled": True},
        "oi_breakout": {"name": "Option Chain OI Breakout", "inst": "BANKNIFTY", "lot": 30, "enabled": True},
        "rsi_ema": {"name": "RSI + EMA Scalping", "inst": "FINNIFTY", "lot": 65, "enabled": False},
        "auto_hedge": {"name": "Auto-Hedge Capital Protector", "inst": "NIFTY", "lot": 75, "enabled": True}
    }
}

DEFAULT_BROKERS = [
    {"id": "BRK_DEMO_1", "broker_type": "ANGEL_ONE", "broker_name": "Angel One (SmartAPI)", "account_label": "Angel Demo", "client_id": "A90124", "trade_permission": True, "verified": False}
]

ENV_USER = os.environ.get("VAJRA_LOGIN_ID", "").strip()
ENV_PIN = os.environ.get("VAJRA_LOGIN_PIN", "").strip()
if ENV_USER and ENV_PIN:
    if not db_get("auth", {}).get("registered"):
        db_set("auth", {
            "registered": True, "name": os.environ.get("VAJRA_USER_NAME", "Trader"),
            "loginId": ENV_USER, "pinHash": hash_pin(ENV_PIN), "failedAttempts": 0, "lockUntil": 0
        })

if db_get("settings", None) is None:
    db_set("settings", DEFAULT_SETTINGS)
if db_get("brokers", None) is None:
    db_set("brokers", DEFAULT_BROKERS)

state_lock = threading.Lock()
runtime = {
    "auto_armed": False,
    "running": False,
    "status_text": "STOPPED",
    "realized_pnl": 0.0,
    "unrealized_pnl": 0.0,
    "open_positions": [],
    "logs": [{"time": now_ist().strftime("%H:%M:%S"), "level": "INFO", "msg": f"VajraTrade AI Cloud Ready ({'Firebase DB' if fs_db else 'Local DB'})."}],
    "ticks": {"NIFTY": 24350.0, "BANKNIFTY": 52180.0, "FINNIFTY": 23490.0}
}


def add_log(msg, level="INFO"):
    runtime["logs"].insert(0, {"time": now_ist().strftime("%H:%M:%S"), "level": level, "msg": msg})
    runtime["logs"] = runtime["logs"][:80]


def dispatch_order(mode, brokers, symbol, side, qty, price):
    permitted = [b for b in brokers if b.get("trade_permission")]
    if not permitted:
        return None
    if mode == "PAPER":
        return ", ".join(b.get("broker_name", "Demat").split()[0] for b in permitted)
    routed = []
    for b in permitted:
        res = UniversalBrokerEngine.place_live_order(b, symbol, side, qty, price)
        routed.append(f"{b.get('broker_name', 'Broker').split()[0]}:{res.get('status')}")
    return ", ".join(routed)


def close_all_locked(mode, brokers, reason):
    closed = db_get("closed_trades", [])
    for p in runtime["open_positions"]:
        dispatch_order(mode, brokers, p["symbol"], "SELL", p["qty"], p["ltp"])
        p["exit_price"] = p["ltp"]
        p["exit_time"] = now_ist().strftime("%H:%M:%S")
        p["exit_reason"] = reason
        runtime["realized_pnl"] = round(runtime["realized_pnl"] + p["pnl"], 2)
        closed.insert(0, p)
    db_set("closed_trades", closed[:100])
    runtime["open_positions"] = []
    runtime["unrealized_pnl"] = 0.0


def cloud_trading_loop():
    tick_counter = 0
    while True:
        time.sleep(1.0)
        with state_lock:
            for k in runtime["ticks"]:
                runtime["ticks"][k] = round(runtime["ticks"][k] + random.uniform(-14.0, 14.5), 2)

            if not runtime["auto_armed"]:
                runtime["running"] = False
                if runtime["status_text"] not in ("TARGET_HIT", "KILL_SWITCH", "MARKET_CLOSED"):
                    runtime["status_text"] = "STOPPED"
                continue

            s = db_get("settings", DEFAULT_SETTINGS)
            brokers = db_get("brokers", DEFAULT_BROKERS)
            mode = s.get("mode", "PAPER")
            hm = now_ist().strftime("%H:%M")
            bypass = (mode == "PAPER" and s.get("allow_24x7", True))

            if not bypass and hm < s.get("market_open", "09:15"):
                if runtime["status_text"] != "WAITING_FOR_MARKET_OPEN":
                    add_log(f"ARMED: Waiting for IST Market Open ({s.get('market_open')})...", "INFO")
                runtime["running"] = False
                runtime["status_text"] = "WAITING_FOR_MARKET_OPEN"
                continue

            if not bypass and hm >= s.get("market_close", "15:15"):
                if runtime["open_positions"]:
                    close_all_locked(mode, brokers, "MARKET CLOSE SQUAREOFF")
                    add_log(f"Market Closed ({s.get('market_close')} IST). All positions squared-off.", "WARN")
                runtime["running"] = False
                runtime["auto_armed"] = False
                runtime["status_text"] = "MARKET_CLOSED"
                continue

            if not runtime["running"]:
                runtime["running"] = True
                runtime["status_text"] = "AUTO_RUNNING"
                add_log(f"AUTO-ENGINE STARTED ({mode} MODE).", "SUCCESS")

            # Monitor Open Positions
            unreal = 0.0
            remaining = []
            closed = db_get("closed_trades", [])
            closed_changed = False

            for p in runtime["open_positions"]:
                pct = random.uniform(-0.025, 0.032)
                p["ltp"] = max(1.0, round(p["ltp"] * (1 + pct), 2))
                p["high_ltp"] = max(p["high_ltp"], p["ltp"])
                p["pnl"] = round((p["ltp"] - p["entry_price"]) * p["qty"], 2)

                profit_pct = ((p["ltp"] - p["entry_price"]) / p["entry_price"]) * 100.0
                if s.get("breakeven_lock", True) and profit_pct >= 5.0 and p["sl_price"] < p["entry_price"]:
                    p["sl_price"] = round(p["entry_price"] * 1.005, 2)
                    add_log(f"BREAKEVEN LOCKED: {p['symbol']} SL -> Rs.{p['sl_price']}", "SUCCESS")

                tsl = round(p["high_ltp"] * (1.0 - s.get("trailing_sl_pct", 4.0) / 100.0), 2)
                if tsl > p["sl_price"]:
                    p["sl_price"] = tsl

                reason = None
                if p["ltp"] <= p["sl_price"]:
                    reason = "TRAILING SL HIT" if p["sl_price"] >= p["entry_price"] else "STOP-LOSS HIT"
                elif p["ltp"] >= p["target_price"]:
                    reason = "TARGET ACHIEVED"

                if reason:
                    dispatch_order(mode, brokers, p["symbol"], "SELL", p["qty"], p["ltp"])
                    p["exit_price"] = p["ltp"]
                    p["exit_time"] = now_ist().strftime("%H:%M:%S")
                    p["exit_reason"] = reason
                    runtime["realized_pnl"] = round(runtime["realized_pnl"] + p["pnl"], 2)
                    closed.insert(0, p)
                    closed_changed = True
                    add_log(f"AUTO SELL {p['symbol']} @ Rs.{p['ltp']} ({reason}) | P&L: Rs.{p['pnl']}", "SUCCESS" if p["pnl"] >= 0 else "WARN")
                else:
                    unreal += p["pnl"]
                    remaining.append(p)

            if closed_changed:
                db_set("closed_trades", closed[:100])

            runtime["open_positions"] = remaining
            runtime["unrealized_pnl"] = round(unreal, 2)
            total_pnl = round(runtime["realized_pnl"] + runtime["unrealized_pnl"], 2)

            if total_pnl >= s.get("daily_profit_target", 5000.0):
                close_all_locked(mode, brokers, "DAILY TARGET HIT")
                runtime["running"] = False
                runtime["auto_armed"] = False
                runtime["status_text"] = "TARGET_HIT"
                add_log(f"TARGET ACHIEVED (Rs.{total_pnl})! Engine stopped.", "SUCCESS")
                continue

            if total_pnl <= -abs(s.get("daily_max_loss", 2000.0)):
                close_all_locked(mode, brokers, "MAX LOSS KILL-SWITCH")
                runtime["running"] = False
                runtime["auto_armed"] = False
                runtime["status_text"] = "KILL_SWITCH"
                add_log(f"KILL-SWITCH HIT (Rs.{total_pnl})! All positions closed.", "ERROR")
                continue

            tick_counter += 1
            if tick_counter % 4 == 0 and len(runtime["open_positions"]) < 3:
                permitted = [b for b in brokers if b.get("trade_permission")]
                if not permitted:
                    if tick_counter % 16 == 0:
                        add_log("Allow at least 1 Demat account permission to trade.", "WARN")
                    continue
                enabled_strats = [v for v in s.get("strategies", {}).values() if v.get("enabled")]
                if enabled_strats and random.random() < 0.5:
                    st = random.choice(enabled_strats)
                    spot = runtime["ticks"].get(st["inst"], 24350.0)
                    strike = int(round(spot / 50.0) * 50)
                    symbol = f"{st['inst']} {strike} {random.choice(['CE', 'PE'])}"
                    if not any(x["symbol"] == symbol for x in runtime["open_positions"]):
                        entry = round(random.uniform(100.0, 210.0), 2)
                        if entry * st["lot"] <= s.get("daily_capital", 25000.0):
                            sl = round(entry * (1.0 - s.get("stop_loss_pct", 8.0) / 100.0), 2)
                            tgt = round(entry * (1.0 + s.get("target_pct", 18.0) / 100.0), 2)
                            acc_tag = dispatch_order(mode, brokers, symbol, "BUY", st["lot"], entry)
                            if acc_tag:
                                runtime["open_positions"].append({
                                    "id": f"POS{int(time.time()*1000)}",
                                    "strategy": st["name"], "broker": acc_tag, "symbol": symbol,
                                    "qty": st["lot"], "entry_price": entry, "ltp": entry,
                                    "high_ltp": entry, "sl_price": sl, "target_price": tgt, "pnl": 0.0
                                })
                                add_log(f"AUTO BUY [{acc_tag}] {symbol} x{st['lot']} @ Rs.{entry} | SL: Rs.{sl}", "INFO")


threading.Thread(target=cloud_trading_loop, daemon=True).start()


def require_auth():
    return bool(session.get("authenticated"))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/auth/state", methods=["GET"])
def api_auth_state():
    auth = db_get("auth", {"registered": False})
    return jsonify({"registered": bool(auth.get("registered")), "authenticated": require_auth(), "name": auth.get("name", "")})


@app.route("/api/auth/register", methods=["POST"])
def api_register():
    d = request.json or {}
    lid, pin = str(d.get("loginId", "")).strip(), str(d.get("pin", "")).strip()
    if not lid or len(pin) < 4:
        return jsonify({"error": "Enter Mobile/User ID & min 4-digit PIN."}), 400
    db_set("auth", {"registered": True, "name": d.get("name", "Trader") or "Trader", "loginId": lid, "pinHash": hash_pin(pin), "failedAttempts": 0, "lockUntil": 0})
    session["authenticated"] = True
    return jsonify({"status": "OK"})


@app.route("/api/auth/login", methods=["POST"])
def api_login():
    auth = db_get("auth", {"registered": False})
    if not auth.get("registered"):
        return jsonify({"error": "No account registered yet. Tap Register."}), 400
    now = time.time()
    if auth.get("lockUntil", 0) > now:
        return jsonify({"error": f"Locked for {int(auth['lockUntil'] - now)}s."}), 403
    d = request.json or {}
    if str(d.get("loginId", "")).strip().lower() == str(auth.get("loginId", "")).lower() and hash_pin(d.get("pin", "")) == auth.get("pinHash"):
        auth["failedAttempts"], auth["lockUntil"] = 0, 0
        db_set("auth", auth)
        session["authenticated"] = True
        return jsonify({"status": "OK"})
    auth["failedAttempts"] = auth.get("failedAttempts", 0) + 1
    if auth["failedAttempts"] >= 5:
        auth["lockUntil"] = now + 60
    db_set("auth", auth)
    return jsonify({"error": f"Invalid ID or PIN ({auth['failedAttempts']}/5)."}), 401


@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"status": "OK"})


@app.route("/api/status", methods=["GET"])
def api_status():
    if not require_auth():
        return jsonify({"error": "Unauthorized"}), 401
    with state_lock:
        s = db_get("settings", DEFAULT_SETTINGS)
        brokers = db_get("brokers", DEFAULT_BROKERS)
        safe_brokers = [
            {
                "id": b["id"], "broker_type": b.get("broker_type", "CUSTOM"),
                "broker_name": b.get("broker_name", "Broker"), "account_label": b.get("account_label", ""),
                "client_id": b.get("client_id", ""), "trade_permission": b.get("trade_permission", False),
                "verified": b.get("verified", False)
            }
            for b in brokers
        ]
        return jsonify({
            "auto_armed": runtime["auto_armed"], "running": runtime["running"], "status_text": runtime["status_text"],
            "server_time": now_ist().strftime("%H:%M:%S"), "realized_pnl": runtime["realized_pnl"],
            "unrealized_pnl": runtime["unrealized_pnl"], "total_pnl": round(runtime["realized_pnl"] + runtime["unrealized_pnl"], 2),
            "open_positions": runtime["open_positions"], "closed_trades": db_get("closed_trades", [])[:20],
            "logs": runtime["logs"][:40], "ticks": runtime["ticks"], "settings": s, "brokers": safe_brokers
        })


@app.route("/api/engine/auto-mode", methods=["POST"])
def api_auto_mode():
    if not require_auth():
        return jsonify({"error": "Unauthorized"}), 401
    enable = bool((request.json or {}).get("enable"))
    with state_lock:
        runtime["auto_armed"] = enable
        if not enable:
            runtime["running"] = False
            runtime["status_text"] = "STOPPED"
        add_log("AUTO-MODE ARMED (ON)." if enable else "AUTO-MODE STOPPED.", "SUCCESS" if enable else "WARN")
        return jsonify({"auto_armed": runtime["auto_armed"]})


@app.route("/api/engine/panic-exit", methods=["POST"])
def api_panic_exit():
    if not require_auth():
        return jsonify({"error": "Unauthorized"}), 401
    with state_lock:
        s = db_get("settings", DEFAULT_SETTINGS)
        brokers = db_get("brokers", DEFAULT_BROKERS)
        close_all_locked(s.get("mode", "PAPER"), brokers, "EMERGENCY EXIT ALL")
        runtime["auto_armed"], runtime["running"], runtime["status_text"] = False, False, "STOPPED"
        add_log("EMERGENCY EXIT ALL executed!", "ERROR")
        return jsonify({"status": "OK"})


@app.route("/api/engine/exit-pos", methods=["POST"])
def api_exit_pos():
    if not require_auth():
        return jsonify({"error": "Unauthorized"}), 401
    pid = (request.json or {}).get("id")
    with state_lock:
        s = db_get("settings", DEFAULT_SETTINGS)
        brokers = db_get("brokers", DEFAULT_BROKERS)
        closed = db_get("closed_trades", [])
        rem = []
        for p in runtime["open_positions"]:
            if p["id"] == pid:
                dispatch_order(s.get("mode", "PAPER"), brokers, p["symbol"], "SELL", p["qty"], p["ltp"])
                p["exit_price"], p["exit_time"], p["exit_reason"] = p["ltp"], now_ist().strftime("%H:%M:%S"), "MANUAL EXIT"
                runtime["realized_pnl"] = round(runtime["realized_pnl"] + p["pnl"], 2)
                closed.insert(0, p)
            else:
                rem.append(p)
        runtime["open_positions"] = rem
        db_set("closed_trades", closed[:100])
        return jsonify({"status": "OK"})


@app.route("/api/engine/reset", methods=["POST"])
def api_reset():
    if not require_auth():
        return jsonify({"error": "Unauthorized"}), 401
    with state_lock:
        runtime["realized_pnl"], runtime["unrealized_pnl"] = 0.0, 0.0
        if not runtime["auto_armed"]:
            runtime["status_text"] = "STOPPED"
        return jsonify({"status": "OK"})


@app.route("/api/settings", methods=["POST"])
def api_settings():
    if not require_auth():
        return jsonify({"error": "Unauthorized"}), 401
    s = db_get("settings", DEFAULT_SETTINGS)
    s.update(request.json or {})
    db_set("settings", s)
    return jsonify({"status": "OK"})


@app.route("/api/brokers", methods=["POST", "DELETE"])
def api_brokers():
    if not require_auth():
        return jsonify({"error": "Unauthorized"}), 401
    brokers = db_get("brokers", DEFAULT_BROKERS)
    if request.method == "POST":
        d = request.json or {}
        if d.get("action") == "TOGGLE":
            for b in brokers:
                if b["id"] == d.get("id"):
                    b["trade_permission"] = not b.get("trade_permission", False)
            db_set("brokers", brokers)
            return jsonify({"status": "OK"})
        brokers.append({
            "id": f"BRK{int(time.time()*1000)}",
            "broker_type": d.get("broker_type", "ANGEL_ONE"),
            "broker_name": d.get("broker_name", "Angel One (SmartAPI)"),
            "account_label": d.get("account_label", "My Demat"),
            "client_id": d.get("client_id", ""),
            "api_key": d.get("api_key", ""),
            "api_secret": d.get("api_secret", ""),
            "access_token": d.get("access_token", ""),
            "mpin": d.get("mpin", ""),
            "totp_secret": d.get("totp_secret", ""),
            "webhook_url": d.get("webhook_url", ""),
            "trade_permission": True,
            "verified": False
        })
        db_set("brokers", brokers)
        return jsonify({"status": "ADDED"})
    bid = request.args.get("id")
    brokers = [b for b in brokers if b["id"] != bid]
    db_set("brokers", brokers)
    return jsonify({"status": "DELETED"})


@app.route("/api/brokers/test", methods=["POST"])
def api_test_broker():
    if not require_auth():
        return jsonify({"error": "Unauthorized"}), 401
    bid = (request.json or {}).get("id")
    brokers = db_get("brokers", DEFAULT_BROKERS)
    for b in brokers:
        if b["id"] == bid:
            res = UniversalBrokerEngine.test_connection(b)
            b["verified"] = bool(res.get("ok"))
            if res.get("session_token"):
                b["session_token"] = res["session_token"]
            db_set("brokers", brokers)
            with state_lock:
                add_log(f"Demat Login Test [{b['broker_name']}]: {res.get('msg')}", "SUCCESS" if res.get("ok") else "ERROR")
            return jsonify(res)
    return jsonify({"ok": False, "msg": "Broker not found."}), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
