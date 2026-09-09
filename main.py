"""
信用卡停車優惠地圖 - 公開展示版
獨立於 Open WebUI 之外的小型 FastAPI 服務，重用同一份停車場資料與搜尋邏輯，
但改成一般網頁（非 sandboxed iframe），所以「使用我的位置」可以直接呼叫瀏覽器
原生的 navigator.geolocation，不需要繞路。
"""

import json
import os
from math import radians, cos, sin, asin, sqrt

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(APP_DIR, "data", "parking_lots.json")

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


@app.get("/api/search")
def api_search(location: str = Query(..., min_length=1), limit: int = 20):
    loc = location.strip()

    def hay(l):
        return "".join(
            [l.get("city", ""), l.get("district", ""), l.get("address", ""), l.get("name", "")]
        )

    matches = [l for l in _eligible_lots() if loc in hay(l)]
    total_found = len(matches)
    matches = matches[:limit]
    return JSONResponse(
        {
            "mode": "by_location_text",
            "query": loc,
            "total_found": total_found,
            "shown": len(matches),
            "lots": matches,
        }
    )


@app.get("/api/nearby")
def api_nearby(lat: float, lng: float, radius_km: float = 3.0, limit: int = 20):
    lots = _eligible_lots()
    with_coords = [
        l for l in lots
        if l.get("latitude") not in (None, "", 0) and l.get("longitude") not in (None, "", 0)
    ]
    missing_coords = len(lots) - len(with_coords)

    for l in with_coords:
        l = dict(l)
    scored = []
    for l in with_coords:
        d = _haversine_km(lat, lng, float(l["latitude"]), float(l["longitude"]))
        item = dict(l)
        item["distance_km"] = round(d, 3)
        scored.append(item)

    within = [l for l in scored if l["distance_km"] <= radius_km]
    within.sort(key=lambda l: l["distance_km"])

    total_found = len(within)
    matches = within[:limit]
    return JSONResponse(
        {
            "mode": "by_geolocation",
            "user_lat": lat,
            "user_lng": lng,
            "radius_km": radius_km,
            "total_found": total_found,
            "shown": len(matches),
            "lots": matches,
            "missing_coords_count": missing_coords,
        }
    )


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
    }


INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>信用卡停車優惠地圖 - Demo</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang TC", "Microsoft JhengHei", sans-serif;
    background: #fafaf9; color: #1c1917;
  }
  .wrap { max-width: 880px; margin: 0 auto; padding: 24px 16px 60px; }
  header { margin-bottom: 20px; }
  header h1 { font-size: 1.4em; margin: 0 0 4px; }
  header p { color: #78716c; font-size: 0.9em; margin: 0; line-height: 1.5; }
  .search-row { display: flex; gap: 8px; margin: 20px 0 8px; flex-wrap: wrap; }
  input[type=text] {
    flex: 1; min-width: 200px; padding: 10px 14px; border: 1px solid #d6d3d1; border-radius: 10px;
    font-size: 0.95em;
  }
  button {
    padding: 10px 16px; border: none; border-radius: 10px; font-size: 0.9em; font-weight: 600;
    cursor: pointer; background: #1c1917; color: #fff;
  }
  button.secondary { background: #f5f5f4; color: #1c1917; border: 1px solid #d6d3d1; }
  button:disabled { opacity: 0.6; cursor: default; }
  .status { font-size: 0.85em; color: #78716c; min-height: 1.4em; margin: 6px 0 14px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 12px; }
  .lot {
    background: #fff; border: 1px solid #e7e5e4; border-radius: 12px; padding: 14px 16px;
  }
  .lot-top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
  .badge {
    font-size: 0.72em; font-weight: 600; color: #15803d; background: #dcfce7;
    padding: 2px 8px; border-radius: 999px;
  }
  .op { font-size: 0.72em; color: #a8a29e; }
  .lot h3 { font-size: 0.98em; margin: 4px 0 2px; line-height: 1.35; }
  .addr { font-size: 0.85em; color: #57534e; margin: 0 0 4px; line-height: 1.45; }
  .dist { font-size: 0.8em; color: #2563eb; margin: 0 0 6px; }
  .maps { font-size: 0.85em; color: #2563eb; text-decoration: none; }
  .maps:hover { text-decoration: underline; }
  footer { margin-top: 32px; font-size: 0.78em; color: #a8a29e; line-height: 1.6; }
  footer a { color: #78716c; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>🅿️ 信用卡停車優惠地圖</h1>
    <p>輸入地點文字查詢，或直接用瀏覽器定位找附近符合中信信用卡停車優惠資格的合作停車場。這是一份作品集 demo，展示地點式優惠查詢的互動設計。</p>
  </header>

  <div class="search-row">
    <input type="text" id="locInput" placeholder="輸入地點，例如「信義區」「台北市」「基隆」">
    <button id="searchBtn">搜尋</button>
    <button id="geoBtn" class="secondary">📍 使用我的位置</button>
  </div>
  <div class="status" id="status"></div>
  <div class="grid" id="results"></div>

  <footer id="footerStats">
    資料來源：<a href="https://help.carmochi.com/cityparking/available" target="_blank" rel="noopener">help.carmochi.com</a>
  </footer>
</div>

<script>
const resultsEl = document.getElementById('results');
const statusEl = document.getElementById('status');
const locInput = document.getElementById('locInput');

function escapeHtml(s) {
  const d = document.createElement('div');
  d.innerText = s ?? '';
  return d.innerHTML;
}

function renderLots(lots, extraLabel) {
  if (!lots.length) {
    resultsEl.innerHTML = '';
    statusEl.textContent = '沒有找到符合資格的停車場。' + (extraLabel || '');
    return;
  }
  resultsEl.innerHTML = lots.map(l => `
    <article class="lot">
      <div class="lot-top">
        <span class="badge">✓ 符合優惠資格</span>
        <span class="op">${escapeHtml(l.operator || '')}</span>
      </div>
      <h3>${escapeHtml(l.name || '(未命名場站)')}</h3>
      <p class="addr">${escapeHtml(l.address || '')}</p>
      ${l.distance_km != null ? `<p class="dist">📍 距離約 ${l.distance_km < 1 ? Math.round(l.distance_km*1000)+' 公尺' : l.distance_km.toFixed(1)+' 公里'}</p>` : ''}
      ${l.google_maps_url ? `<a class="maps" href="${l.google_maps_url}" target="_blank" rel="noopener">導航前往 ↗</a>` : ''}
    </article>
  `).join('');
}

async function doSearch() {
  const loc = locInput.value.trim();
  if (!loc) { statusEl.textContent = '請輸入地點。'; return; }
  statusEl.textContent = '搜尋中…';
  try {
    const res = await fetch(`/api/search?location=${encodeURIComponent(loc)}`);
    const data = await res.json();
    statusEl.textContent = `共 ${data.total_found} 筆符合資格，顯示前 ${data.shown} 筆`;
    renderLots(data.lots);
  } catch (e) {
    statusEl.textContent = '搜尋失敗，請稍後再試。';
  }
}

async function doGeoSearch() {
  if (!navigator.geolocation) {
    statusEl.textContent = '此瀏覽器不支援定位功能，請改用文字搜尋。';
    return;
  }
  statusEl.textContent = '正在取得您的位置（請允許瀏覽器的定位授權提示）…';
  navigator.geolocation.getCurrentPosition(async (pos) => {
    const { latitude, longitude } = pos.coords;
    statusEl.textContent = '搜尋中…';
    try {
      const res = await fetch(`/api/nearby?lat=${latitude}&lng=${longitude}`);
      const data = await res.json();
      let extra = '';
      if (data.missing_coords_count) {
        extra = `（另有 ${data.missing_coords_count} 筆符合資格但缺少座標的場站，無法出現在定位搜尋中，可改用文字搜尋找到。）`;
      }
      statusEl.textContent = `以您目前位置為中心，${data.radius_km} 公里內共 ${data.total_found} 筆符合資格，顯示前 ${data.shown} 筆${extra}`;
      renderLots(data.lots);
    } catch (e) {
      statusEl.textContent = '搜尋失敗，請稍後再試。';
    }
  }, (err) => {
    statusEl.textContent = '沒有取得您的位置：' + (err.message || '使用者拒絕或無法取得定位') + '。請改用文字搜尋。';
  }, { enableHighAccuracy: true, timeout: 10000, maximumAge: 0 });
}

document.getElementById('searchBtn').addEventListener('click', doSearch);
document.getElementById('geoBtn').addEventListener('click', doGeoSearch);
locInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(); });

fetch('/api/stats').then(r => r.json()).then(s => {
  document.getElementById('footerStats').innerHTML =
    `資料庫共 ${s.total_lots} 筆場站，${s.eligible_lots} 筆符合信用卡優惠資格` +
    (s.source_updated_at ? `，資料更新於 ${s.source_updated_at}` : '') +
    `。資料來源：<a href="https://help.carmochi.com/cityparking/available" target="_blank" rel="noopener">help.carmochi.com</a>`;
}).catch(() => {});
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML
