"""seed — `tendline.seed.v1.1` 種子帳重播切片(`cmms seed-events`)。

把外部產生器產出的事件帳(JSONL × N 日 + `seed-manifest.json`)經**既有 domain service**
重播進本 CMMS,讓合成 Work instance 與產生器共用同一個 `world_id`。

- `reader`:純函式(無 DB / 無 I/O)— manifest 與日檔 bytes 的解析、驗證、正規化。
- `transform`:純函式 — cut-off 解析、area→line 對照、actor 對照、逐事件預檢(dry-run 與實跑共用)。
- `service`:`SeedReplayService` — 指紋 / 進度(`seed_world`)+ 分派到各切片 domain service。
- `models`:`SeedWorld`(migration 0037)。
"""
