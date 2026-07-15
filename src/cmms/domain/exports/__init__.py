"""匯出中心讀取切片(ExportService)。

唯讀:把五個資料集(工單 / 工單領料 / 設備 / 保養排程 / 保養步驟明細)以扁平列
供 web 層串成 CSV。零寫入、無 migration、不碰對外 JSON 讀 API 與 MCP。
"""
