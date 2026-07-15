"""failure_vocab 切片(C2)— 共用失效受控詞彙的兩軸 lookup(詞彙來源方→cmms 種子)。

授權來源:內部設計評審 W4/消費端需求 裁決 —— C2 共用失效詞彙 committed,**cmms 持受控詞彙的
單一權威 lookup**,分析平台供種子。本切片 = migration + domain service + loader +
CLI + /admin 唯讀顯示 + tests。

兩軸(詞彙來源方鐵則:**永不合併成一張表**):
- **mfc**(`mes_failmode`)= 產品/良率軸「料為何被判退」;種子 = `data/raw/mes_failmode_seed.csv`
  (113 失效旗標 + 3 三分流大類)。
  ★ 自然鍵 = **(station, label)** 複合鍵 —— `signal_id` 跨站碰撞(如 sensorfail/shortfail/
  overcurrentfail 同時見於 prober/sta3/sta4,不同 seg/station),**絕不可**單獨當唯一鍵;
  三分流(playbook 級)列根本無 signal_id。`signal_id` 由 `label` 小寫衍生(diagnose.py 的
  `mes.failmode.{label.lower()}`),此處僅面值保存,不重算。
- **efc**(`equipment_failure_code`)= 設備軸「機台為何故障」;種子 = `data/raw/
  efc_equipment_codes.csv`(107 碼)。自然鍵 = `code`。**僅詞彙層** —— 不建任何引用 詞彙來源方
  emit schema 的物件(`failure_seed.v0` 仍 DRAFT)。

政策:
- **additive-only**:loader 冪等 upsert,永不刪除;退役 = `is_active` 旗標(loader **永不**翻它,
  由 admin 治理,延 D6 切片)。
- **不臆測語意**(護欄 #8):未經校準的語意在種子中標 `TODO(校準)`,面值保存;
  station_hint 為前綴推斷(非權威),四個 SA 家族(efcSA1/SA2/SA3/SA4)站別未解。
"""
