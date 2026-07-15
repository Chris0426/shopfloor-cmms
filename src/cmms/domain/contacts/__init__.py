"""Contacts 切片(#6)— 組織(Organization)與人員(Person)。讀取掃描最後一個。

migration + domain service + 讀取 API/MCP + 載入器 + tests。本切片**讀取為主**。

範圍與決議(2026-06-22,見 06-contacts §5/§6;Jordan 拍板):
- **編碼**:contacts.csv 為 **latin-1**(只有 ß/ü,無 cp1252 專屬位元)+ 少量 HTML-entity
  (`&rsquo;`)→ `clean_text` = latin-1 解碼(loader)+ `html.unescape`。全 237 列欄位數一致
  (無 RFC-4180 畸形行,異於 inventory)。
- **拆兩實體**:扁平 contacts → `organization`(由 `company` 萃取)+ `person`(其餘欄)。
- **org_id**:company 名 slug(新代理鍵);`CMA`→`CMA`、`SF`→`SF` 自然落位;實證 211 家零碰撞。
- **org_type 推導**:category→type(Supplier→Supplier、Employee→Contractor、Customer→Customer)
  + 名稱 override(`SF`→Internal;SF 的 contacts category=Customer 但 SF=Shopfloor 自有)。
  `CMB` 為 loader 種子的**歷史 Contractor**(`is_active=false`,不在 CSV,供 14,381 張舊工單
  `assignto` 解析,**不建人員**)。實證無公司跨 category 混用 → 推導乾淨。
- **person.category**:存**原始 eMaint 分類**(Supplier/Employee/Customer),**不**沿用 domain
  model 的 `role` enum —— 實證 12 個 Customer 全是 `@example.com`/company=SF 的 Shopfloor 內部人,
  硬套 `CustomerContact` 會誤標(守護欄 #8 不臆測)。
- **保守去重**(C1/§5.4 擴充):profiling 抓到 7 對疑似重複,Jordan 採**保守**:只併「同公司、
  近乎一模一樣」者(`SMWU`→`SAMWU99`、`NOPT`→`NOPTIC`,canonical 取字母序在前),寫入
  `person_alias`(別名不建 person 列、供舊資料解析);跨公司同 email(Group B)與僅同名
  (Group C)**保留為獨立 person**(不併、不遺失)。
- **PII 治理**(§3):批次列舉(REST + MCP)只回非 PII 摘要;單筆查詢回完整(含別名解析)。
- **FK 不 retrofit**(Jordan 拍板:只建本體):`inventory_item.supplier`、`pm_schedule/work_order
  .assigned_person`、`work_order.closed_by` 維持 text 軟參照;既有 `vendor` lookup 不動,
  與 `organization` 並存(CMA/CMB/SF 兩表皆有),日後再對帳。
- **延後**:`app_user`(登入帳號≠聯絡人,延 #4b 寫入切片)、CRUD governed write、站點操作員
  查找表(C1 待 UI 補抽 `opened_by`)、home_address([DROP] 1 筆垃圾值)。
"""
