"""Inventory 切片(#5)— 零件 / 耗材庫存(關聯最多的模組)。

migration + domain service + 讀取 API/MCP + 載入器 + tests。本切片**讀取為主**。

範圍與決議(2026-06-21,見 P1-A3):
- **編碼**:inventory.csv 為 **cp1252**(Windows export;`\x99`=™ 等需 cp1252 才正確)+
  HTML-entity(`&#956;`=μ)→ `clean_text` = cp1252 解碼 + `html.unescape`。
- **畸形行**(資料品質 #1):10 筆 descrip 內嵌逗號 → 欄位數≠20。**跳過並記錄**被跳過的
  item code(載乾淨 ~1321;migration 可重跑,修原始檔再 backfill)。
- **ES/EC**(I1):原意 ES=零件/EC=耗材,但**早已混用** → `item_category` 存前綴做溯源,
  標註為 legacy 前綴、**非可靠分類**。
- **cost 幣別**(I2):單一 **USD**。
- **A3 子類型**:建 canonical `asset_subtype` lookup(自動整併明顯變體,如
  `STA1 CALIBRATOR`→`CALIBRATOR STA1`);inventory 經 junction FK 進去;**asset.asset_subtype
  維持 text 軟參照(不retrofit FK,免破壞 Asset 切片)**。**無法機械判斷的 40 個子類型
  (21 asset-only + 19 inv-only)列清單待 Jordan 逐個釐清**。
- **多值欄**:asset_sub / alt_item / parnt_item+child_item(BOM)拆 junction;孤兒邊跳過。
- **延後**:uom(無資料)、supplier→company FK(Contacts 切片)、stock_transaction(寫入)、
  Related Parts qty(I6)、BOM 雙向不對稱清理。
"""
