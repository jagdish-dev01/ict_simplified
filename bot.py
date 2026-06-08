import os, logging, re, json
from datetime import datetime, timezone, timedelta
from urllib.parse import unquote_plus
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse
from telegram import Bot
from telegram.constants import ParseMode
from dotenv import load_dotenv

load_dotenv()

# Config
BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID_ENV    = os.getenv("TELEGRAM_CHAT_ID",   "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET",     "pipstone2024")
PORT           = int(os.getenv("PORT", 8000))
IST            = timezone(timedelta(hours=5, minutes=30))

# Parse chat IDs (supports JSON array string or comma-separated string)
try:
    chat_ids = json.loads(CHAT_ID_ENV)
    if not isinstance(chat_ids, list):
        chat_ids = [CHAT_ID_ENV]
except Exception:
    chat_ids = [c.strip() for c in CHAT_ID_ENV.split(",")] if "," in CHAT_ID_ENV else [CHAT_ID_ENV]

chat_ids = [str(c) for c in chat_ids if c]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pipstone")

app = FastAPI(title="PIPSTONE ICT Signal Bot")
bot = Bot(token=BOT_TOKEN)

# Decoder Maps
DIRECTION_MAP = {1: "LONG", -1: "SHORT"}
TIER_MAP = {2: "A/B", 1: "C", 0: "X"}
CONF_MAP = {
    0: "None",
    1: "Engulfing",
    2: "Sweep Reclaim",
    3: "Pin Bar",
    4: "Displacement",
    5: "Micro FVG"
}
PAIR_TIER = {
    "EURJPY": "S", "CADJPY": "S", "NZDUSD": "S", "CHFJPY": "S",
    "EURUSD": "A", "XAUUSD": "A", "GBPUSD": "A", "GBPJPY": "A",
    "AUDUSD": "B", "USDCAD": "B", "AUDJPY": "B", "EURCAD": "B",
    "US30": "B", "USTEC": "B", "EURAUD": "B",
    "US500": "SKIP"
}

active_obs = {}
recent_alerts = [] # To store last 10 alerts for /status

def escape_md(text: str) -> str:
    """Escape markdown variables for MarkdownV2."""
    chars = r"[_*\[\]()~`>#+\-=|{}.!]"
    return re.sub(f"({chars})", r"\\\1", str(text))

def unix_ms_to_ist(unix_ms: float) -> datetime:
    return datetime.fromtimestamp(unix_ms / 1000, tz=timezone.utc).astimezone(IST)

def get_session_info(ist_time: datetime, pair: str) -> tuple[str, int]:
    h, m = ist_time.hour, ist_time.minute
    total_min = h * 60 + m

    london_start = 13 * 60 + 30
    london_end   = 17 * 60 + 30
    ny_start     = 18 * 60 + 30
    ny_end       = 22 * 60 + 30
    asian_start  =  5 * 60 + 30
    asian_end    = 11 * 60 + 30

    asian_pairs = {"EURJPY","GBPJPY","AUDJPY","CADJPY","CHFJPY","AUDUSD","NZDUSD"}

    if london_start <= total_min < london_end:
        return "London 🇬🇧", total_min - london_start
    elif ny_start <= total_min < ny_end:
        return "New York 🇺🇸", total_min - ny_start
    elif asian_start <= total_min < asian_end and pair in asian_pairs:
        return "Asian 🌏", total_min - asian_start
    else:
        return "Off-Session", -1

def format_price(val: float, pair: str) -> str:
    if "XAUUSD" in pair or "US500" in pair: return f"{val:.2f}"
    if "US30" in pair or "USTEC" in pair: return f"{val:.1f}"
    if "JPY" in pair: return f"{val:.3f}"
    return f"{val:.5f}"

def build_message(payload_parts: list, raw_body: str) -> str:
    try:
        stage = int(payload_parts[0])
        pair = payload_parts[1].upper()
        direction_code = int(float(payload_parts[2]))
        direction = DIRECTION_MAP.get(direction_code, "UNKNOWN")
        
        ob_high = float(payload_parts[3])
        ob_low = float(payload_parts[4])
        ob_score = int(float(payload_parts[5]))
        ob_tier_code = int(float(payload_parts[6]))
        ob_age_h = float(payload_parts[7])
        fvg = int(float(payload_parts[8])) == 1
        sweep = int(float(payload_parts[9])) == 1
        atr_disp = int(float(payload_parts[10])) == 1
        atr = float(payload_parts[11])
        d_ema50 = float(payload_parts[12])
        h4_ema8 = float(payload_parts[13])
        h4_ema21 = float(payload_parts[14])
        
        pair_tier = PAIR_TIER.get(pair, "B")
        if pair_tier == "SKIP":
            log.info(f"Suppressed: US500 pair {pair}")
            return None

        ob_tier_str = TIER_MAP.get(ob_tier_code, "X")
        bias_str = "Bullish" if direction_code == 1 else "Bearish"
        
        # State & Suppression Rule 2
        now = datetime.now()
        if pair in active_obs:
            if stage <= active_obs[pair]["stage"]:
                if (now - active_obs[pair]["last_alert"]) < timedelta(hours=2):
                    log.info(f"Suppressed: Duplicate stage {stage} for {pair} within 2h")
                    return None

        # Suppression Rule 5: OB invalidated
        if pair in active_obs:
            if direction_code == 1 and active_obs[pair]["current_price"] and active_obs[pair]["current_price"] < ob_low:
                del active_obs[pair]
                log.info(f"OB Invalidated for {pair} (price broke low)")
                return "INVALIDATED"
            elif direction_code == -1 and active_obs[pair]["current_price"] and active_obs[pair]["current_price"] > ob_high:
                del active_obs[pair]
                log.info(f"OB Invalidated for {pair} (price broke high)")
                return "INVALIDATED"

        # Initialize or update state
        if pair not in active_obs:
            active_obs[pair] = {"current_price": None}
            
        active_obs[pair].update({
            "direction": direction,
            "ob_high": ob_high,
            "ob_low": ob_low,
            "ob_score": ob_score,
            "ob_tier": ob_tier_str,
            "stage": stage,
            "last_alert": now
        })

        msg = ""

        # Formatting values
        e_ob_high = escape_md(format_price(ob_high, pair))
        e_ob_low  = escape_md(format_price(ob_low, pair))
        e_pair    = escape_md(pair)
        e_score   = escape_md(ob_score)
        e_tier    = escape_md(ob_tier_str)
        e_age     = escape_md(f"{ob_age_h:.1f}")
        e_pair_t  = escape_md(pair_tier)

        if stage == 1:
            unix_ms = float(payload_parts[15])
            ist_time = unix_ms_to_ist(unix_ms)
            ist_str = escape_md(ist_time.strftime("%d %b %I:%M %p IST"))
            
            msg = (
                f"🔍 *OB DETECTED* — {e_pair}  \\[{e_pair_t}\\-tier\\]\n\n"
                f"Direction : {direction} {'🟢' if direction_code == 1 else '🔴'}\n"
                f"Bias      : {bias_str} ✅\n"
                f"OB Zone   : {e_ob_low} – {e_ob_high}\n"
                f"OB Tier   : {e_tier} \\(Score: {e_score}/4\\)\n"
                f"OB Age    : Fresh \\({e_age}h old\\)\n"
                f"FVG       : {'✅' if fvg else '❌'}\n"
                f"Liq Sweep : {'✅' if sweep else '❌'}\n"
                f"ATR Disp  : {'✅' if atr_disp else '❌'}\n\n"
                f"⏳ Watching for retest of zone\\.\\.\\.\n"
                f"🕐 {ist_str}"
            )

        elif stage == 2:
            current_price = float(payload_parts[15])
            unix_ms = float(payload_parts[16])
            
            active_obs[pair]["current_price"] = current_price
            ist_time = unix_ms_to_ist(unix_ms)
            session_name, session_min = get_session_info(ist_time, pair)
            
            ist_str = escape_md(ist_time.strftime("%d %b %I:%M %p IST"))
            e_price = escape_md(format_price(current_price, pair))
            e_sess  = escape_md(session_name)
            e_sess_m = escape_md(session_min)
            e_sl = escape_md(format_price(ob_low if direction_code == 1 else ob_high, pair)) # Approximation for display
            
            msg = (
                f"⚡ *PRICE IN ZONE* — {e_pair} {direction}\n\n"
                f"OB Zone       : {e_ob_low} – {e_ob_high}\n"
                f"Current Price : {e_price}\n"
                f"Session       : {e_sess} \\({e_sess_m} min in\\)\n\n"
                f"OB Tier  : {e_tier} \\| Score {e_score}/4\n"
                f"{'FVG ✅' if fvg else 'FVG ❌'} \\| {'Sweep ✅' if sweep else 'Sweep ❌'} \\| {'ATR ✅' if atr_disp else 'ATR ❌'}\n\n"
                f"👁 Watch 15M for confirmation candle\n"
                f"SL will be: \\~{e_sl}\n"
                f"🕐 {ist_str}"
            )

        elif stage == 3:
            entry_price = float(payload_parts[15])
            sl_price = float(payload_parts[16])
            tp1_price = float(payload_parts[17])
            tp2_price = float(payload_parts[18])
            # For pips, we'll use our own calculation since we have the prices and the pip multipliers
            pip_val = 0.01 if "JPY" in pair else (1.0 if pair in {"XAUUSD","US30","USTEC"} else 0.0001)
            risk_pips = round(abs(entry_price - sl_price) / pip_val, 1)
            tp1_pips = round(abs(tp1_price - entry_price) / pip_val, 1)
            tp2_pips = round(abs(tp2_price - entry_price) / pip_val, 1)
            
            rr = float(payload_parts[22])
            conf_type = int(float(payload_parts[23]))
            current_price = float(payload_parts[24])
            unix_ms = float(payload_parts[25])
            
            active_obs[pair]["current_price"] = current_price
            ist_time = unix_ms_to_ist(unix_ms)
            session_name, _ = get_session_info(ist_time, pair)
            
            if session_name == "Off-Session":
                log.info(f"Suppressed: Stage 3 Off-Session for {pair}")
                return None
            if rr < 2.0:
                log.info(f"Suppressed: RR {rr} < 2.0 for {pair}")
                return None

            ist_str = escape_md(ist_time.strftime("%d %b %I:%M %p IST"))
            e_sess = escape_md(session_name)
            e_conf = escape_md(CONF_MAP.get(conf_type, "Unknown"))
            
            e_entry = escape_md(format_price(entry_price, pair))
            e_sl_p = escape_md(format_price(sl_price, pair))
            e_tp1 = escape_md(format_price(tp1_price, pair))
            e_tp2 = escape_md(format_price(tp2_price, pair))
            
            e_rp = escape_md(risk_pips)
            e_tp1p = escape_md(tp1_pips)
            e_tp2p = escape_md(tp2_pips)
            e_rr = escape_md(f"{rr:.1f}")
            
            e_atr = escape_md(f"{atr:.5f}")
            e_d50 = escape_md(format_price(d_ema50, pair))
            e_h4_8 = escape_md(format_price(h4_ema8, pair))
            e_h4_21 = escape_md(format_price(h4_ema21, pair))

            msg = (
                f"{'🔴' if direction_code == -1 else '🟢'} *ENTRY SIGNAL* — {e_pair} {direction}\n\n"
                f"Bias         : {bias_str}\n"
                f"Session      : {e_sess}\n"
                f"Confirmation : {e_conf}\n\n"
                f"OB Zone  : {e_ob_low} – {e_ob_high}\n"
                f"Tier     : {e_tier} \\| Score {e_score}/4\n"
                f"{'FVG ✅' if fvg else 'FVG ❌'} \\| {'Sweep ✅' if sweep else 'Sweep ❌'} \\| {'ATR ✅' if atr_disp else 'ATR ❌'}\n\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"ENTRY     : {e_entry}\n"
                f"SL        : {e_sl_p}  \\(\\+{e_rp} pips\\)\n"
                f"TP1 \\(50%\\): {e_tp1}  \\(\\+{e_tp1p} pips\\)\n"
                f"TP2 \\(50%\\): {e_tp2}  \\(\\+{e_tp2p} pips\\)\n"
                f"R:R       : {e_rr}R ✅\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"ATR       : {e_atr}\n"
                f"D EMA50   : {e_d50}\n"
                f"4H EMA8   : {e_h4_8}\n"
                f"4H EMA21  : {e_h4_21}\n\n"
                f"🕐 {ist_str}"
            )

        if ob_tier_str == "C":
            msg += "\n\n⚠️ *C\\-Tier — low conviction*"
            
        recent_alerts.insert(0, f"{pair} Stage {stage} at {datetime.now().strftime('%H:%M:%S')}")
        if len(recent_alerts) > 10:
            recent_alerts.pop()
            
        return msg
    except Exception as e:
        log.error(f"Error parsing payload or building msg: {e}")
        return None

@app.post("/webhook")
async def webhook(request: Request, x_webhook_secret: str = Header(default="")):
    if x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    body = await request.body()
    try:
        raw_text = unquote_plus(body.decode("utf-8")).strip()
        if raw_text.startswith("payload="):
            raw_text = raw_text[8:]
    except Exception:
        raw_text = body.decode("utf-8").strip()

    log.info(f"Received payload: {raw_text}")
    parts = raw_text.split("|")
    if len(parts) < 16:
        raise HTTPException(status_code=400, detail="Invalid payload length")

    msg = build_message(parts, raw_text)
    
    if msg == "INVALIDATED":
        return JSONResponse({"status": "invalidated"})
    if msg:
        try:
            for c_id in chat_ids:
                await bot.send_message(chat_id=c_id, text=msg, parse_mode=ParseMode.MARKDOWN_V2)
            log.info(f"Sent to {len(chat_ids)} Telegram chat(s)")
            return JSONResponse({"status": "ok", "stage": parts[0]})
        except Exception as e:
            log.error(f"Telegram error: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    else:
        return JSONResponse({"status": "suppressed"})

@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.now(IST).strftime("%I:%M %p IST")}

@app.get("/status")
async def status():
    return {"active_obs": active_obs, "recent_alerts": recent_alerts, "chat_ids": chat_ids}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bot:app", host="0.0.0.0", port=PORT)
