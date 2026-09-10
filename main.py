"""
信用卡停車優惠地圖 - 對話式公開展示版
在原本的 /api/search、/api/nearby 之上，加一層 /api/chat：
用便宜模型做「意圖解析」（不是開放式聊天），把使用者的自然語句轉成
{intent: search_location|use_geolocation|unclear, location: str|None}，
再交給原本已經測過的搜尋邏輯處理。含每 IP 流量限制與每日總額上限，
避免公開demo被拿去打爆 LLM API 額度。
"""

import json
import os
import time
import re
import base64
import hashlib
import hmac
from collections import defaultdict, deque
from math import radians, cos, sin, asin, sqrt

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(APP_DIR, "data", "parking_lots.json")

# ---- LLM 意圖解析 ----
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

INTENT_SYSTEM_PROMPT = """你是一個信用卡停車優惠查詢助理的意圖解析器。使用者會用自然語言詢問停車優惠相關問題。
你的唯一任務：判斷使用者的意圖，並用純 JSON（不要任何其他文字、不要 markdown code fence）回覆，格式為：
{"intent": "search_location", "location": "地點文字"}
或
{"intent": "use_geolocation"}
或
{"intent": "unclear", "clarify": "一句簡短的中文澄清問句"}

規則：
- 如果使用者提到具體地名/城市/行政區（例如「信義區」「台北車站附近」「基隆」），回傳 search_location，location 只填地點關鍵字本身。
- 如果使用者說「附近」「我這裡」「目前位置」「我的位置」等但沒給具體地名，回傳 use_geolocation。
- 如果訊息含糊、無法判斷地點（例如打招呼、問其他不相關問題），回傳 unclear，並給一句簡短澄清問句。
- 只回傳 JSON，不要任何額外說明文字。"""

# ---- 流量限制（記憶體內，服務重啟會重置，demo 用途足夠）----
RATE_LIMIT_PER_IP = 8          # 每個 IP 每個時間窗口的請求數上限
RATE_LIMIT_WINDOW_SEC = 600    # 時間窗口（秒）
DAILY_GLOBAL_LIMIT = 300       # 整個服務每天的 LLM 呼叫總上限（保護 API 額度）
MAX_MESSAGE_LEN = 200          # 單則訊息最大字數，避免塞入超長文字拉高成本

# ---- 開發測試認證 ----
# 只接受雜湊後的測試資料；不在程式碼、cookie、log 或資料檔保存明文身分證字號/驗證碼。

def _env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


AUTH_REQUIRED = _env_bool("AUTH_REQUIRED", True)
AUTH_SESSION_SECRET = os.environ.get("DEV_AUTH_SESSION_SECRET", "")
AUTH_ID_SHA256 = os.environ.get("DEV_AUTH_ID_SHA256", "").strip().lower()
AUTH_CODE_SHA256 = os.environ.get("DEV_AUTH_CODE_SHA256", "").strip().lower()
AUTH_CARD_IDS = tuple(
    card.strip() for card in os.environ.get("DEV_AUTH_CARD_IDS", "").split("|") if card.strip()
)
AUTH_PROFILE = os.environ.get("DEV_AUTH_PROFILE", "development-test")
AUTH_COOKIE_NAME = "parking_auth"
AUTH_SESSION_TTL_SEC = max(300, int(os.environ.get("DEV_AUTH_SESSION_TTL_SEC", "1800")))
AUTH_COOKIE_SECURE = _env_bool("DEV_AUTH_COOKIE_SECURE", True)
AUTH_RATE_LIMIT_PER_IP = 5
AUTH_RATE_LIMIT_WINDOW_SEC = 900

_ip_requests = defaultdict(deque)
_auth_requests = defaultdict(deque)
_daily_count = {"date": None, "count": 0}

app = FastAPI(title="信用卡停車優惠地圖")

_cache = None


def _load():
    global _cache
    if _cache is None:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            _cache = json.load(f)
    return _cache


def _haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * 6371 * asin(sqrt(a))


def _eligible_lots(card_ids=None):
    data = _load()
    allowed_cards = set(data.get("benefit_rules", {}).get("eligible_cards", []))
    if card_ids is not None and not (allowed_cards & set(card_ids)):
        return []
    lots = data.get("parking_lots", [])
    return [l for l in lots if l.get("ctbc_eligible")]


def _search_by_location(loc: str, limit: int = 12, card_ids=None):
    def hay(l):
        return "".join(
            [l.get("city", ""), l.get("district", ""), l.get("address", ""), l.get("name", "")]
        )

    matches = [l for l in _eligible_lots(card_ids) if loc in hay(l)]
    total_found = len(matches)
    return total_found, matches[:limit]


def _search_nearby(lat: float, lng: float, radius_km: float = 3.0, limit: int = 12, card_ids=None):
    lots = _eligible_lots(card_ids)
    with_coords = [
        l for l in lots
        if l.get("latitude") not in (None, "", 0) and l.get("longitude") not in (None, "", 0)
    ]
    missing_coords = len(lots) - len(with_coords)

    scored = []
    for l in with_coords:
        d = _haversine_km(lat, lng, float(l["latitude"]), float(l["longitude"]))
        item = dict(l)
        item["distance_km"] = round(d, 3)
        scored.append(item)

    within = [l for l in scored if l["distance_km"] <= radius_km]
    within.sort(key=lambda l: l["distance_km"])
    return len(within), within[:limit], missing_coords


# ---------------- 流量限制 ----------------

def _check_rate_limit(ip: str):
    now = time.time()
    today = time.strftime("%Y-%m-%d", time.gmtime(now))

    if _daily_count["date"] != today:
        _daily_count["date"] = today
        _daily_count["count"] = 0

    if _daily_count["count"] >= DAILY_GLOBAL_LIMIT:
        return False, "這個 demo 今天的查詢額度已經用完了，請明天再來，或改用文字搜尋。"

    q = _ip_requests[ip]
    while q and now - q[0] > RATE_LIMIT_WINDOW_SEC:
        q.popleft()

    if len(q) >= RATE_LIMIT_PER_IP:
        return False, "請求太頻繁了，請稍等幾分鐘再試。"

    q.append(now)
    _daily_count["count"] += 1
    return True, None


# ---------------- LLM 意圖解析 ----------------

async def _parse_intent(message: str) -> dict:
    if not DEEPSEEK_API_KEY:
        # 沒設定金鑰時退回規則式判斷，服務仍可運作
        return _rule_based_intent(message)

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                f"{DEEPSEEK_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
                json={
                    "model": DEEPSEEK_MODEL,
                    "messages": [
                        {"role": "system", "content": INTENT_SYSTEM_PROMPT},
                        {"role": "user", "content": message},
                    ],
                    "max_tokens": 150,
                    "temperature": 0,
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
            content = re.sub(r"^```json\s*|\s*```$", "", content).strip()
            parsed = json.loads(content)
            if parsed.get("intent") not in ("search_location", "use_geolocation", "unclear"):
                raise ValueError("unexpected intent value")
            return parsed
    except Exception:
        return _rule_based_intent(message)


def _rule_based_intent(message: str) -> dict:
    msg = message.strip()
    if any(kw in msg for kw in ["附近", "我這裡", "我的位置", "目前位置", "現在位置"]):
        return {"intent": "use_geolocation"}
    if not msg:
        return {"intent": "unclear", "clarify": "請問您想查詢哪個地點的停車優惠呢？"}
    return {"intent": "search_location", "location": msg}


def _lots_summary_text(total_found, shown_count, extra=""):
    if total_found == 0:
        return "沒有找到符合信用卡優惠資格的停車場。" + extra
    return f"找到 {total_found} 筆符合資格的停車場，以下是前 {shown_count} 筆：" + extra


# ---------------- API ----------------

def _auth_configured():
    return bool(
        re.fullmatch(r"[0-9a-f]{64}", AUTH_ID_SHA256)
        and re.fullmatch(r"[0-9a-f]{64}", AUTH_CODE_SHA256)
        and len(AUTH_SESSION_SECRET) >= 32
        and AUTH_CARD_IDS
    )


def _normalize_id_number(value):
    return re.sub(r"[\s-]", "", str(value or "")).upper()


def _hash_secret(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cookie_secure(request: Request):
    return AUTH_COOKIE_SECURE


def _make_session():
    payload = {
        "profile": AUTH_PROFILE,
        "exp": int(time.time()) + AUTH_SESSION_TTL_SEC,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    signature = hmac.new(
        AUTH_SESSION_SECRET.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{encoded}.{signature}"


def _read_session(request: Request):
    if not AUTH_SESSION_SECRET:
        return None
    raw = request.cookies.get(AUTH_COOKIE_NAME, "")
    try:
        encoded, signature = raw.split(".", 1)
        expected = hmac.new(
            AUTH_SESSION_SECRET.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if int(payload.get("exp", 0)) <= int(time.time()):
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _auth_guard(request: Request):
    if not AUTH_REQUIRED:
        return None
    if not _auth_configured():
        return JSONResponse(
            {"authenticated": False, "auth_required": True,
             "reply": "開發認證尚未完成環境設定，請聯絡服務管理者。"},
            status_code=503,
        )
    if _read_session(request) is None:
        return JSONResponse(
            {"authenticated": False, "auth_required": True,
             "reply": "請先完成身分驗證，再查詢停車優惠。"},
            status_code=401,
        )
    return None


def _check_auth_rate_limit(ip: str):
    now = time.time()
    q = _auth_requests[ip]
    while q and now - q[0] > AUTH_RATE_LIMIT_WINDOW_SEC:
        q.popleft()
    if len(q) >= AUTH_RATE_LIMIT_PER_IP:
        return False
    q.append(now)
    return True


def _request_card_ids():
    return AUTH_CARD_IDS if AUTH_CARD_IDS else None


@app.get("/api/auth/status")
def api_auth_status(request: Request):
    session = _read_session(request)
    authenticated = not AUTH_REQUIRED or session is not None
    return {
        "authenticated": authenticated,
        "auth_required": AUTH_REQUIRED,
        "configured": (not AUTH_REQUIRED) or _auth_configured(),
        "expires_at": session.get("exp") if session else None,
    }


@app.post("/api/auth/login")
async def api_auth_login(request: Request):
    if not AUTH_REQUIRED:
        return {"authenticated": True, "auth_required": False}
    if not _auth_configured():
        return JSONResponse({"reply": "開發認證尚未完成環境設定，請聯絡服務管理者。"}, status_code=503)

    ip = request.client.host if request.client else "unknown"
    if not _check_auth_rate_limit(ip):
        return JSONResponse({"reply": "驗證嘗試太頻繁，請稍後再試。"}, status_code=429)

    body = await request.json()
    id_number = _normalize_id_number(body.get("id_number"))
    verification_code = str(body.get("verification_code") or "").strip()
    # 這是開發測試閘門，不做正式身分證檢核碼驗證；正式上線時應改接合規驗證服務。
    if not re.fullmatch(r"[A-Z][0-9]{8,9}", id_number) or not re.fullmatch(r"[A-Za-z0-9-]{4,64}", verification_code):
        return JSONResponse({"reply": "身分證字號或驗證碼格式不正確。"}, status_code=400)

    valid_id = hmac.compare_digest(_hash_secret(id_number), AUTH_ID_SHA256)
    valid_code = hmac.compare_digest(_hash_secret(verification_code), AUTH_CODE_SHA256)
    if not (valid_id and valid_code):
        return JSONResponse({"reply": "身分驗證失敗，請確認輸入內容。"}, status_code=401)

    response = JSONResponse({"authenticated": True, "auth_required": True,
                             "reply": "驗證成功，現在可以查詢符合您測試卡別的停車優惠。"})
    response.set_cookie(
        AUTH_COOKIE_NAME,
        _make_session(),
        max_age=AUTH_SESSION_TTL_SEC,
        httponly=True,
        secure=_cookie_secure(request),
        samesite="lax",
        path="/",
    )
    return response


@app.post("/api/auth/logout")
def api_auth_logout():
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return response

@app.get("/api/search")
def api_search(request: Request, location: str = Query(..., min_length=1), limit: int = 20):
    guard = _auth_guard(request)
    if guard:
        return guard
    total_found, matches = _search_by_location(location.strip(), limit, _request_card_ids())
    return JSONResponse(
        {"mode": "by_location_text", "query": location, "total_found": total_found,
         "shown": len(matches), "lots": matches}
    )


@app.get("/api/nearby")
def api_nearby(request: Request, lat: float, lng: float, radius_km: float = 3.0, limit: int = 20):
    guard = _auth_guard(request)
    if guard:
        return guard
    total_found, matches, missing_coords = _search_nearby(lat, lng, radius_km, limit, _request_card_ids())
    return JSONResponse(
        {"mode": "by_geolocation", "user_lat": lat, "user_lng": lng, "radius_km": radius_km,
         "total_found": total_found, "shown": len(matches), "lots": matches,
         "missing_coords_count": missing_coords}
    )


@app.post("/api/chat")
async def api_chat(request: Request):
    guard = _auth_guard(request)
    if guard:
        return guard
    body = await request.json()
    message = (body.get("message") or "")[:MAX_MESSAGE_LEN]
    ip = request.client.host if request.client else "unknown"

    ok, err = _check_rate_limit(ip)
    if not ok:
        return JSONResponse({"reply": err, "lots": None, "need_geolocation": False})

    if not message.strip():
        return JSONResponse({"reply": "請輸入您想查詢的地點，例如「信義區」。", "lots": None, "need_geolocation": False})

    intent = await _parse_intent(message)

    if intent["intent"] == "use_geolocation":
        return JSONResponse({"reply": None, "lots": None, "need_geolocation": True})

    if intent["intent"] == "unclear":
        return JSONResponse({"reply": intent.get("clarify", "可以請您說得更具體一點嗎？例如想查詢的地點。"),
                              "lots": None, "need_geolocation": False})

    loc = intent.get("location", "").strip()
    if not loc:
        return JSONResponse({"reply": "請問您想查詢哪個地點呢？", "lots": None, "need_geolocation": False})

    total_found, matches = _search_by_location(loc, card_ids=_request_card_ids())
    return JSONResponse({
        "reply": _lots_summary_text(total_found, len(matches)),
        "lots": matches,
        "query": loc,
        "need_geolocation": False,
    })


@app.post("/api/chat/geolocation")
async def api_chat_geolocation(request: Request):
    guard = _auth_guard(request)
    if guard:
        return guard
    body = await request.json()
    lat = body.get("lat")
    lng = body.get("lng")
    ip = request.client.host if request.client else "unknown"

    ok, err = _check_rate_limit(ip)
    if not ok:
        return JSONResponse({"reply": err, "lots": None})

    if lat is None or lng is None:
        return JSONResponse({"reply": "沒有取得有效的定位座標，請改用文字告訴我地點。", "lots": None})

    total_found, matches, missing_coords = _search_nearby(
        float(lat), float(lng), card_ids=_request_card_ids()
    )
    extra = f"（另有 {missing_coords} 筆符合資格但缺少座標的場站，只能用文字地點查詢找到。）" if missing_coords else ""
    return JSONResponse({
        "reply": _lots_summary_text(total_found, len(matches), extra),
        "lots": matches,
    })


@app.get("/api/stats")
def api_stats(request: Request):
    guard = _auth_guard(request)
    if guard:
        return guard
    data = _load()
    lots = data.get("parking_lots", [])
    eligible = _eligible_lots(_request_card_ids())
    with_coords = [
        l for l in eligible
        if l.get("latitude") not in (None, "", 0) and l.get("longitude") not in (None, "", 0)
    ]
    return {
        "total_lots": len(lots),
        "eligible_lots": len(eligible),
        "eligible_with_coords": len(with_coords),
        "source_updated_at": data.get("source_updated_at"),
        "llm_enabled": bool(DEEPSEEK_API_KEY),
    }


INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>信用卡停車優惠地圖 - 對話式 Demo</title>
<style>
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0; height: 100%;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang TC", "Microsoft JhengHei", sans-serif;
    background: #fafaf9; color: #1c1917;
  }
  .app { max-width: 720px; margin: 0 auto; height: 100dvh; display: flex; flex-direction: column; }
  header { padding: 16px; border-bottom: 1px solid #e7e5e4; background: #fff; }
  header h1 { font-size: 1.1em; margin: 0 0 2px; }
  header p { color: #78716c; font-size: 0.8em; margin: 0; line-height: 1.4; }
  .auth-panel { margin: 14px 16px 0; padding: 14px; border: 1px solid #d6d3d1; border-radius: 14px; background: #fff; }
  .auth-panel h2 { margin: 0 0 5px; font-size: 0.95em; }
  .auth-panel p { margin: 0 0 10px; color: #78716c; font-size: 0.78em; line-height: 1.45; }
  .auth-form { display: grid; grid-template-columns: 1fr 1fr auto; gap: 7px; }
  .auth-form input { min-width: 0; padding: 9px 10px; border: 1px solid #d6d3d1; border-radius: 8px; font-size: 0.86em; }
  .auth-form button, .auth-logout { padding: 9px 12px; border: none; border-radius: 8px; background: #176d5f; color: #fff; cursor: pointer; font-weight: 600; }
  .auth-logout { display: none; background: #78716c; font-size: 0.78em; }
  .auth-status { margin-top: 7px; color: #78716c; font-size: 0.78em; }
  .auth-status.error { color: #b91c1c; }
  @media (max-width: 560px) { .auth-form { grid-template-columns: 1fr 1fr; } .auth-form button { grid-column: 1 / -1; } }
  .messages { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 14px; }
  .msg { display: flex; }
  .msg.user { justify-content: flex-end; }
  .msg.assistant { justify-content: flex-start; }
  .bubble {
    max-width: 82%; padding: 10px 14px; border-radius: 16px; font-size: 0.92em; line-height: 1.55;
    white-space: pre-wrap;
  }
  .msg.user .bubble { background: #1c1917; color: #fff; border-bottom-right-radius: 4px; }
  .msg.assistant .bubble { background: #fff; border: 1px solid #e7e5e4; border-bottom-left-radius: 4px; }
  .msg.system .bubble { background: transparent; color: #a8a29e; font-size: 0.8em; border: none; padding: 2px 6px; }
  .typing { display: inline-flex; gap: 3px; align-items: center; padding: 4px 2px; }
  .typing span {
    width: 6px; height: 6px; border-radius: 50%; background: #a8a29e;
    animation: blink 1.2s infinite ease-in-out;
  }
  .typing span:nth-child(2) { animation-delay: 0.2s; }
  .typing span:nth-child(3) { animation-delay: 0.4s; }
  @keyframes blink { 0%, 80%, 100% { opacity: 0.25; } 40% { opacity: 1; } }
  .pk-carousel { display: grid; grid-template-columns: auto 1fr auto; gap: 6px; align-items: center; margin-top: 10px; }
  .pk-arrow {
    width: 28px; height: 28px; border-radius: 50%; border: 1px solid #e7e5e4; background: #fff;
    font-size: 15px; cursor: pointer; flex-shrink: 0;
  }
  .pk-carousel article {
    display: grid; grid-template-columns: 92px 1fr; border: 1px solid #e7e5e4; border-radius: 12px;
    overflow: hidden; min-height: 116px; background: #fafaf9;
  }
  .pk-visual {
    display: grid; place-content: center; justify-items: center; gap: 4px; color: #fff;
    background: linear-gradient(145deg, #176d5f, #24a69a);
  }
  .pk-visual span {
    display: grid; width: 40px; height: 40px; place-items: center; border: 3px solid #fff;
    border-radius: 10px; font-size: 22px; font-weight: 900; line-height: 1;
  }
  .pk-visual small { font-size: 9px; font-weight: 700; padding: 0 4px; text-align: center; }
  .pk-copy { padding: 9px 11px; display: grid; gap: 2px; align-content: center; min-width: 0; }
  .pk-badge { font-size: 0.68em; font-weight: 700; color: #15803d; }
  .pk-copy h3 { margin: 0; font-size: 0.92em; line-height: 1.3; }
  .pk-copy p { margin: 0; color: #78716c; font-size: 0.78em; line-height: 1.4; }
  .pk-dist { color: #2563eb !important; }
  .pk-copy a { color: #2563eb; font-size: 0.8em; font-weight: 600; text-decoration: none; }
  .pk-copy a:hover { text-decoration: underline; }
  .pk-page {
    display: flex; gap: 6px; align-items: center; justify-content: center; margin-top: 6px;
    font-size: 0.72em; color: #a8a29e;
  }
  .pk-page i { display: inline-block; width: 5px; height: 5px; border-radius: 50%; background: #d6d3d1; margin: 0 1px; }
  .pk-page i.on { background: #176d5f; }
  .pk-evidence { margin: 2px 0 0; }
  .pk-evidence summary { color: #176d5f; font-size: 0.72em; font-weight: 700; cursor: pointer; list-style: none; }
  .pk-evidence summary::-webkit-details-marker { display: none; }
  .pk-evidence summary::before { content: '▸ '; }
  .pk-evidence[open] summary::before { content: '▾ '; }
  .pk-evidence div { margin-top: 4px; font-size: 0.72em; color: #78716c; line-height: 1.5; }
  .pk-evidence a { color: #2563eb; }
  .composer {
    display: flex; gap: 8px; padding: 12px 16px; border-top: 1px solid #e7e5e4; background: #fff;
  }
  .composer input {
    flex: 1; padding: 10px 14px; border: 1px solid #d6d3d1; border-radius: 999px; font-size: 0.92em;
  }
  .composer button {
    padding: 10px 18px; border: none; border-radius: 999px; font-weight: 600; font-size: 0.88em;
    cursor: pointer; background: #1c1917; color: #fff;
  }
  .composer button:disabled { opacity: 0.5; cursor: default; }
  .quick-actions { display: flex; gap: 6px; padding: 0 16px 10px; flex-wrap: wrap; }
  .quick-actions button {
    font-size: 0.78em; padding: 6px 12px; border-radius: 999px; border: 1px solid #d6d3d1;
    background: #fff; color: #44403c; cursor: pointer;
  }
  footer { text-align: center; font-size: 0.72em; color: #a8a29e; padding: 6px 0 10px; }
  footer a { color: #78716c; }
</style>
</head>
<body>
<div class="app">
  <header>
    <h1>🅿️ 信用卡停車優惠小助理</h1>
    <p>完成開發測試認證後，依測試帳號對應的卡別查詢附近停車優惠。</p>
  </header>

  <section class="auth-panel" id="authPanel">
    <h2>開發測試認證</h2>
    <p>身分證字號與驗證碼只用於本次測試驗證，不會顯示在聊天內容中。</p>
    <form class="auth-form" id="authForm">
      <input type="text" id="idNumber" inputmode="text" autocomplete="off" placeholder="身分證字號" maxlength="10" required>
      <input type="password" id="verificationCode" autocomplete="one-time-code" placeholder="驗證碼" maxlength="64" required>
      <button type="submit" id="authBtn">開始驗證</button>
    </form>
    <button type="button" class="auth-logout" id="logoutBtn">登出測試帳號</button>
    <div class="auth-status" id="authStatus"></div>
  </section>

  <div class="messages" id="messages"></div>

  <div class="quick-actions">
    <button data-msg="信義區有停車優惠嗎">信義區有停車優惠嗎</button>
    <button data-msg="附近有停車場優惠嗎">附近有停車場優惠嗎</button>
  </div>

  <div class="composer">
    <input type="text" id="input" placeholder="輸入訊息，例如「信義區有停車優惠嗎」" autocomplete="off">
    <button id="sendBtn">送出</button>
  </div>
  <footer id="footerStats">資料來源：<a href="https://help.carmochi.com/cityparking/available" target="_blank" rel="noopener">help.carmochi.com</a></footer>
</div>

<script>
const messagesEl = document.getElementById('messages');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('sendBtn');
const authPanel = document.getElementById('authPanel');
const authForm = document.getElementById('authForm');
const idNumberEl = document.getElementById('idNumber');
const verificationCodeEl = document.getElementById('verificationCode');
const authBtn = document.getElementById('authBtn');
const logoutBtn = document.getElementById('logoutBtn');
const authStatusEl = document.getElementById('authStatus');
let authenticated = false;
let authRequired = true;

function escapeHtml(s) {
  const d = document.createElement('div');
  d.innerText = s ?? '';
  return d.innerHTML;
}

function addMessage(role, text) {
  const wrap = document.createElement('div');
  wrap.className = 'msg ' + role;
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = text;
  wrap.appendChild(bubble);
  messagesEl.appendChild(wrap);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return bubble;
}

function addTyping() {
  const wrap = document.createElement('div');
  wrap.className = 'msg assistant';
  wrap.id = 'typing-indicator';
  wrap.innerHTML = '<div class="bubble"><div class="typing"><span></span><span></span><span></span></div></div>';
  messagesEl.appendChild(wrap);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function removeTyping() {
  const el = document.getElementById('typing-indicator');
  if (el) el.remove();
}

function setQueryEnabled(enabled) {
  inputEl.disabled = !enabled;
  sendBtn.disabled = !enabled;
  document.querySelectorAll('.quick-actions button').forEach(btn => { btn.disabled = !enabled; });
}

function setAuthStatus(text, isError = false) {
  authStatusEl.textContent = text || '';
  authStatusEl.className = 'auth-status' + (isError ? ' error' : '');
}

function showAuthenticated() {
  authForm.style.display = 'none';
  logoutBtn.style.display = 'inline-block';
  setAuthStatus('已完成開發測試認證，可以查詢符合測試卡別的優惠。');
  setQueryEnabled(true);
}

function showLogin(message) {
  authForm.style.display = '';
  logoutBtn.style.display = 'none';
  setAuthStatus(message || '請先完成開發測試認證。', !!message);
  setQueryEnabled(false);
}

async function loadStats() {
  try {
    const res = await fetch('/api/stats');
    if (!res.ok) return;
    const s = await res.json();
    document.getElementById('footerStats').innerHTML =
      `資料庫共 ${s.total_lots} 筆場站，${s.eligible_lots} 筆符合您測試卡別的優惠資格。資料來源：` +
      `<a href="https://help.carmochi.com/cityparking/available" target="_blank" rel="noopener">help.carmochi.com</a>`;
  } catch (e) {}
}

async function refreshAuth() {
  try {
    const res = await fetch('/api/auth/status');
    const data = await res.json();
    authRequired = !!data.auth_required;
    if (!authRequired) {
      authPanel.style.display = 'none';
      authenticated = true;
      setQueryEnabled(true);
      addMessage('assistant', '嗨！我可以幫您查詢符合信用卡停車優惠資格的合作停車場。可以直接告訴我地點，或說「附近」。');
      loadStats();
      return;
    }
    if (!data.configured) {
      showLogin('服務尚未完成開發認證設定，請聯絡管理者。');
      authBtn.disabled = true;
      return;
    }
    if (data.authenticated) {
      authenticated = true;
      showAuthenticated();
      addMessage('assistant', '驗證已生效！可以告訴我地點，或說「附近」查詢符合測試卡別的停車優惠。');
      loadStats();
    } else {
      showLogin('請先完成開發測試認證。');
    }
  } catch (e) {
    showLogin('無法確認認證狀態，請稍後再試。');
  }
}

authForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  authBtn.disabled = true;
  setAuthStatus('正在驗證…');
  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id_number: idNumberEl.value, verification_code: verificationCodeEl.value }),
    });
    const data = await res.json();
    if (!res.ok) {
      showLogin(data.reply || '驗證失敗，請稍後再試。');
      return;
    }
    authenticated = true;
    idNumberEl.value = '';
    verificationCodeEl.value = '';
    showAuthenticated();
    addMessage('assistant', data.reply || '驗證成功，現在可以查詢停車優惠。');
    loadStats();
  } catch (e) {
    showLogin('驗證服務暫時無法使用，請稍後再試。');
  } finally {
    authBtn.disabled = false;
  }
});

logoutBtn.addEventListener('click', async () => {
  await fetch('/api/auth/logout', { method: 'POST' }).catch(() => {});
  authenticated = false;
  showLogin('已登出，請重新完成開發測試認證。');
});

function renderCardsInto(bubble, lots) {
  if (!lots || !lots.length) return;
  const cid = 'pk' + Math.random().toString(36).slice(2, 7);
  let idx = 0;

  const wrap = document.createElement('div');
  wrap.id = cid;
  wrap.innerHTML = `
    <div class="pk-carousel">
      <button type="button" class="pk-arrow" data-prev aria-label="上一個">‹</button>
      <article data-card></article>
      <button type="button" class="pk-arrow" data-next aria-label="下一個">›</button>
    </div>
    <div class="pk-page"><strong data-idx></strong><span data-dots></span></div>
    <details class="pk-evidence" data-evidence></details>
  `;
  bubble.appendChild(wrap);

  const distLabel = (l) => {
    if (l.distance_km == null) return '';
    const km = l.distance_km;
    return km < 1 ? Math.round(km * 1000) + ' 公尺' : km.toFixed(1) + ' 公里';
  };

  const draw = () => {
    const l = lots[idx % lots.length];
    wrap.querySelector('[data-card]').innerHTML = `
      <div class="pk-visual"><span>P</span><small>${escapeHtml(l.operator || '合作停車場')}</small></div>
      <div class="pk-copy">
        <span class="pk-badge">✓ 符合信用卡優惠資格</span>
        <h3>${escapeHtml(l.name || '(未命名場站)')}</h3>
        <p>${escapeHtml(l.address || '')}</p>
        ${l.distance_km != null ? `<p class="pk-dist">📍 距離約 ${distLabel(l)}</p>` : ''}
        ${l.google_maps_url ? `<a href="${l.google_maps_url}" target="_blank" rel="noopener">導航前往 ↗</a>` : ''}
      </div>
    `;
    wrap.querySelector('[data-idx]').textContent = `${(idx % lots.length) + 1} / ${lots.length}`;
    const dotCount = Math.min(lots.length, 8);
    wrap.querySelector('[data-dots]').innerHTML = Array.from({ length: dotCount }, (_, i) =>
      `<i class="${i === idx % dotCount ? 'on' : ''}"></i>`
    ).join('');
    const evEl = wrap.querySelector('[data-evidence]');
    if (l.eligibility_evidence) {
      evEl.innerHTML = `<summary>查看資格依據</summary><div>${escapeHtml(l.eligibility_evidence)}${l.source_url ? ` — <a href="${l.source_url}" target="_blank" rel="noopener">來源 ↗</a>` : ''}</div>`;
      evEl.style.display = '';
    } else {
      evEl.style.display = 'none';
    }
  };

  wrap.querySelector('[data-prev]').onclick = () => { idx = (idx - 1 + lots.length) % lots.length; draw(); };
  wrap.querySelector('[data-next]').onclick = () => { idx = (idx + 1) % lots.length; draw(); };
  draw();
}

function requestGeolocation() {
  addMessage('system', '正在取得您的位置（請允許瀏覽器的定位授權提示）…');
  if (!navigator.geolocation) {
    addMessage('assistant', '這個瀏覽器不支援定位功能，請改用文字告訴我地點。');
    return;
  }
  navigator.geolocation.getCurrentPosition(async (pos) => {
    addTyping();
    try {
      const res = await fetch('/api/chat/geolocation', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lat: pos.coords.latitude, lng: pos.coords.longitude }),
      });
      const data = await res.json();
      removeTyping();
      if (res.status === 401 || res.status === 503) {
        authenticated = false;
        showLogin(data.reply || '請先完成開發測試認證。');
        return;
      }
      const bubble = addMessage('assistant', data.reply || '');
      renderCardsInto(bubble, data.lots);
    } catch (e) {
      removeTyping();
      addMessage('assistant', '搜尋失敗，請稍後再試。');
    }
  }, (err) => {
    addMessage('assistant', '沒有取得您的位置：' + (err.message || '使用者拒絕或無法取得定位') + '。請改用文字告訴我地點。');
  }, { enableHighAccuracy: true, timeout: 10000, maximumAge: 0 });
}

async function sendMessage(text) {
  if (!text.trim()) return;
  if (authRequired && !authenticated) {
    showLogin('請先完成開發測試認證。');
    return;
  }
  addMessage('user', text);
  inputEl.value = '';
  sendBtn.disabled = true;
  addTyping();
  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text }),
    });
    const data = await res.json();
    removeTyping();
    if (res.status === 401 || res.status === 503) {
      authenticated = false;
      showLogin(data.reply || '請先完成開發測試認證。');
      return;
    }
    if (data.need_geolocation) {
      requestGeolocation();
    } else {
      const bubble = addMessage('assistant', data.reply || '');
      renderCardsInto(bubble, data.lots);
    }
  } catch (e) {
    removeTyping();
    addMessage('assistant', '發生錯誤，請稍後再試。');
  } finally {
    sendBtn.disabled = false;
  }
}

sendBtn.addEventListener('click', () => sendMessage(inputEl.value));
inputEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') sendMessage(inputEl.value); });
document.querySelectorAll('.quick-actions button').forEach(btn => {
  btn.addEventListener('click', () => sendMessage(btn.dataset.msg));
});

setQueryEnabled(false);
refreshAuth();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML
