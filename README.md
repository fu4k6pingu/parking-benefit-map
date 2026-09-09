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