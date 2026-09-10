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
- 如果使用者提到具體地名/城市/行政區（例如「信義區「「台北車站附近」「基隆」），回傳 search_location，location 只填地點關鍵字本身。
- 如果使用者說「附近」「我這裡」「目前位置」「我的位置」等但沒給具體地名，回傳 use_geolocation。
- 如果訊息含糊、無法判斷地點（例如打招呼、問其他不相關問題），回傳 unclear，並給一句簡短澄清問句。
- 只回傳 JSON，不要任何額外說明文字。"""

# ---- 流量限制（記憶體內，服務重啟會重置，demo 用途足夠）----
RATE_LIMIT_PER_IP = 8          # 每個 IP 每個時間窗口的請求數上限
RATE_LIMIT_WINDOW_SEC = 600    # 時間窗口（秒）
DAILY_GLOBAL_LIMIT = 300       # 整個服務每天的 LLM 呼叫總上限（保護 API 額度）
MAX_MESSAGE_LEN = 200          # 單則訊息最大字數，避免塞入超長文字拉高成本

_ip_requests = defaultdict(deque)
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


def _eligible_lots():
    data = _load()
    lots = data.get("parking_lots", [])
    return [l for l in lots if l.get("ctbc_eligible")]


def _search_by_location(loc: str, limit: int = 12):
    def hay(l):
        return "".join(
            [l.get("city", ""), l.get("district", ""), l.get("address", ""), l.get("name", "")]
        )

    matches = [l for l in _eligible_lots() if loc in hay(l)]
    total_found = len(matches)
    return total_found, matches[:limit]


def _search_nearby(lat: float, lng: float, radius_km: float = 3.0, limit: int = 12):
    lots = _eligible_lots()
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

@app.get("/api/search")
def api_search(location: str = Query(..., min_length=1), limit: int = 20):
    total_found, matches = _search_by_location(location.strip(), limit)
    return JSONResponse(
        {"mode": "by_location_text", "query": location, "total_found": total_found,
         "shown": len(matches), "lots": matches}
    )


@app.get("/api/nearby")
def api_nearby(lat: float, lng: float, radius_km: float = 3.0, limit: int = 20):
    total_found, matches, missing_coords = _search_nearby(lat, lng, radius_km, limit)
    return JSONResponse(
        {"mode": "by_geolocation", "user_lat": lat, "user_lng": lng, "radius_km": radius_km,
         "total_found": total_found, "shown": len(matches), "lots": matches,
         "missing_coords_count": missing_coords}
    )


@app.post("/api/chat")
async def api_chat(request: Request):
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

    total_found, matches = _search_by_location(loc)
    return JSONResponse({
        "reply": _lots_summary_text(total_found, len(matches)),
        "lots": matches,
        "query": loc,
        "need_geolocation": False,
    })


@app.post("/api/chat/geolocation")
async def api_chat_geolocation(request: Request):
    body = await request.json()
    lat = body.get("lat")
    lng = body.get("lng")
    ip = request.client.host if request.client else "unknown"

    ok, err = _check_rate_limit(ip)
    if not ok:
        return JSONResponse({"reply": err, "lots": None})

    if lat is None or lng is None:
        return JSONResponse({"reply": "沒有取得有效的定位座標，請改用文字告訴我地點。", "lots": None})

    total_found, matches, missing_coords = _search_nearby(float(lat), float(lng))
    extra = f"（另有 {missing_coords} 筆符合資格但缺少座標的場站，只能用文字地點查詢找到。）" if missing_coords else ""
    return JSONResponse({
        "reply": _lots_summary_text(total_found, len(matches), extra),
        "lots": matches,
    })


@app.get("/api/stats")
def api_stats():
    data = _load()
    lots = data.get("parking_lots", [])
    eligible = [l for l in lots if l.get("ctbc_eligible")]
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
  .cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 8px; margin-top: 10px; }
  .lot {
    background: #fafaf9; border: 1px solid #e7e5e4; border-radius: 10px; padding: 10px 12px;
  }
  .lot-top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; }
  .badge {
    font-size: 0.68em; font-weight: 600; color: #15803d; background: #dcfce7;
    padding: 1px 7px; border-radius: 999px;
  }
  .op { font-size: 0.68em; color: #a8a29e; }
  .lot h3 { font-size: 0.88em; margin: 3px 0 2px; line-height: 1.3; }
  .addr { font-size: 0.78em; color: #57534e; margin: 0 0 3px; line-height: 1.4; }
  .dist { font-size: 0.75em; color: #2563eb; margin: 0 0 4px; }
  .maps { font-size: 0.78em; color: #2563eb; text-decoration: none; }
  .maps:hover { text-decoration: underline; }
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
    <p>用聊天的方式問我地點，或直接用您目前的位置查詢附近符合中信信用卡停車優惠資格的合作停車場。</p>
  </header>

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

function renderCardsInto(bubble, lots) {
  if (!lots || !lots.length) return;
  const grid = document.createElement('div');
  grid.className = 'cards';
  grid.innerHTML = lots.map(l => `
    <article class="lot">
      <div class="lot-top">
        <span class="badge">✓ 符合資格</span>
        <span class="op">${escapeHtml(l.operator || '')}</span>
      </div>
      <h3>${escapeHtml(l.name || '(未命名場站)')}</h3>
      <p class="addr">${escapeHtml(l.address || '')}</p>
      ${l.distance_km != null ? `<p class="dist">📍 距離約 ${l.distance_km < 1 ? Math.round(l.distance_km*1000)+' 公尺' : l.distance_km.toFixed(1)+' 公里'}</p>` : ''}
      ${l.google_maps_url ? `<a class="maps" href="${l.google_maps_url}" target="_blank" rel="noopener">導航前往 ↗</a>` : ''}
    </article>
  `).join('');
  bubble.appendChild(grid);
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

addMessage('assistant', '嗨！我可以幫您查詢符合中信信用卡停車優惠資格的合作停車場。可以直接告訴我地點，或說「附近」讓我用您目前的位置查詢。');

fetch('/api/stats').then(r => r.json()).then(s => {
  document.getElementById('footerStats').innerHTML =
    `資料庫共 ${s.total_lots} 筆場站，${s.eligible_lots} 筆符合信用卡優惠資格。資料來源：` +
    `<a href="https://help.carmochi.com/cityparking/available" target="_blank" rel="noopener">help.carmochi.com</a>`;
}).catch(() => {});
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML
