# 信用卡停車優惠地圖 - Demo

獨立於 Open WebUI 之外的小型公開展示服務，重用同一份 1297 筆合作停車場資料，
提供文字地點搜尋與瀏覽器定位搜尋兩種查詢方式。

## 技術重點

- FastAPI 後端，`data/parking_lots.json` 為資料來源
- 前端是一般網頁（非沙盒 iframe），瀏覽器定位可直接呼叫 `navigator.geolocation`
- haversine 公式計算距離、依距離排序

## 資料來源

https://help.carmochi.com/cityparking/available

## 已知限制

資料庫中約 23% 符合信用卡優惠資格的場站缺少經緯度座標，無法出現在「使用我的位置」
這種依距離的搜尋結果中，僅能透過文字地點搜尋找到。

## 開發測試認證

目前版本可啟用開發測試用的身分驗證閘門。這不是正式的身分證或銀行驗證服務；正式上線前必須改接合規的第三方身分／卡片資格服務。

認證資料只接受雜湊值，不要把真實身分證字號或驗證碼寫入程式碼、資料檔或 log。設定以下 Zeabur 環境變數後再部署：

- `AUTH_REQUIRED=true`
- `DEV_AUTH_ID_SHA256`：測試身分證字號正規化（去除空白與連字號、轉大寫）後的 SHA-256；開發測試接受 1 個英文字母加 8–9 個數字
- `DEV_AUTH_CODE_SHA256`：測試驗證碼的 SHA-256
- `DEV_AUTH_SESSION_SECRET`：至少 32 字元的隨機 session 簽章密鑰
- `DEV_AUTH_CARD_IDS`：測試帳號可使用的卡別，以 `|` 分隔，名稱必須與 `benefit_rules.eligible_cards` 完全一致
- `DEV_AUTH_SESSION_TTL_SEC`：短期 session 秒數，預設 1800
- `DEV_AUTH_COOKIE_SECURE=true`：正式 HTTPS 環境保持預設值；本機用 HTTP 測試時才暫設為 `false`

驗證成功後只發送簽名的短期 HttpOnly cookie；服務不保存明文身分證字號或驗證碼。所有查詢 API 也會在後端再次檢查 session，不能只靠前端畫面繞過。
