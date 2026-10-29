"""Anthropic 後端的成本帳本 + 預算閘 + 每來源每日問題上限(SQLite;零 cmms 耦合)。

- 每次 LLM 請求(/chat 一個問題、/mrq-description 一次摘要)記一列:時刻、端點、模型、
  usage 四欄、估算美元。價格表寫死(`PRICES`),2026-10-29 依 claude-api skill 核對。
- 預算閘(fail-closed):當日(UTC)累計 ≥ `HERMES_DAILY_BUDGET_USD` 或當月(UTC)累計 ≥
  `HERMES_MONTHLY_BUDGET_USD` → 呼叫前就拒絕(gateway 回 503),不假裝有答案。≥80% 為 warning。
- 每來源每日問題上限:`/chat` 每個來源(web = 使用者 IP;其他通道 = cmms 給的穩定鍵)每個
  UTC 日最多 `HERMES_DAILY_QUESTIONS_PER_CLIENT` 題。示範帳號共用 → 以 IP 為主,不以帳號。
  ★ 來源鍵不落原值:以 gateway secret 做 HMAC-SHA256 後只存雜湊前綴(不可反推 IP)。
- 單一 sqlite3 檔 + 行程內鎖;量很小(示範站),同步呼叫即可。時間可注入(測試用)。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import sqlite3
import threading
from dataclasses import dataclass

# ── 價格表(USD / 百萬 token)───────────────────────────────────────────────────
# 快取寫入 = 5 分鐘 TTL(1.25× 輸入價);快取讀取依各模型實價(Opus 5.5 為 0.05×,其餘 0.1×)。
# Haiku 5.5 分兩層:單次請求 prompt(input + 快取讀 + 快取寫)> 100K token 時整筆改用長層價。


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_write: float
    cache_read: float


PRICES: dict[str, Price] = {
    "claude-haiku-5-5": Price(input=0.10, output=0.50, cache_write=0.125, cache_read=0.01),
    "claude-sonnet-5-5": Price(input=2.00, output=10.00, cache_write=2.50, cache_read=0.20),
    "claude-opus-5-5": Price(input=4.00, output=20.00, cache_write=5.00, cache_read=0.20),
}
_HAIKU_LONG = Price(input=0.50, output=2.50, cache_write=0.625, cache_read=0.05)
_HAIKU_LONG_THRESHOLD = 100_000
# 未列在表上的模型(例如以環境變數指定的其他型號)以表中最貴者估算 —— 寧可高估、提早熔斷。
_FALLBACK_PRICE = max(PRICES.values(), key=lambda p: p.output)

WARNING_RATIO = 0.8


def estimate_usd(
    model: str, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int
) -> float:
    """單次 API 呼叫的估算美元(分層以該次 prompt 長度判定)。"""
    price = PRICES.get(model, _FALLBACK_PRICE)
    prompt_tokens = input_tokens + cache_read + cache_write
    if model == "claude-haiku-5-5" and prompt_tokens > _HAIKU_LONG_THRESHOLD:
        price = _HAIKU_LONG
    return (
        input_tokens * price.input
        + output_tokens * price.output
        + cache_write * price.cache_write
        + cache_read * price.cache_read
    ) / 1_000_000


def client_hash(client_id: str, key: str) -> str:
    """來源鍵(IP 等)→ 不可反推的雜湊前綴;金鑰 = gateway secret。"""
    return hmac.new(key.encode(), client_id.encode(), hashlib.sha256).hexdigest()[:24]


class BudgetExhausted(RuntimeError):
    """當日或當月預算用罄;scope = "daily" / "monthly"。"""

    def __init__(self, scope: str):
        super().__init__(f"{scope} budget exhausted")
        self.scope = scope


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Ledger:
    """SQLite 帳本。`now` 可注入(測試固定時間)。"""

    def __init__(
        self,
        path: str,
        *,
        daily_budget_usd: float,
        monthly_budget_usd: float,
        now=_utcnow,
    ):
        self.path = path
        self.daily_budget_usd = daily_budget_usd
        self.monthly_budget_usd = monthly_budget_usd
        self._now = now
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS calls (
                    ts TEXT NOT NULL, day TEXT NOT NULL, month TEXT NOT NULL,
                    endpoint TEXT NOT NULL, model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                    cache_read INTEGER NOT NULL, cache_write INTEGER NOT NULL,
                    usd REAL NOT NULL, outcome TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS calls_day ON calls(day);
                CREATE INDEX IF NOT EXISTS calls_month ON calls(month);
                CREATE TABLE IF NOT EXISTS questions (
                    day TEXT NOT NULL, client TEXT NOT NULL, n INTEGER NOT NULL,
                    PRIMARY KEY (day, client)
                );
                """
            )

    def _keys(self) -> tuple[str, str, str]:
        now = self._now()
        return now.isoformat(timespec="seconds"), now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")

    # ── 記帳 ──
    def record(
        self,
        *,
        endpoint: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read: int,
        cache_write: int,
        usd: float,
        outcome: str = "ok",
    ) -> None:
        ts, day, month = self._keys()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO calls VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ts, day, month, endpoint, model, input_tokens, output_tokens,
                 cache_read, cache_write, usd, outcome),
            )

    def spent(self) -> tuple[float, float]:
        """(當日, 當月) 累計美元。"""
        _, day, month = self._keys()
        with self._lock:
            q = "SELECT COALESCE(SUM(usd),0) FROM calls WHERE {}=?"
            d = self._conn.execute(q.format("day"), (day,)).fetchone()[0]
            m = self._conn.execute(q.format("month"), (month,)).fetchone()[0]
        return float(d), float(m)

    # ── 預算閘 ──
    @staticmethod
    def _state(spent: float, limit: float) -> str:
        if limit <= 0 or spent >= limit:
            return "exhausted"
        return "warning" if spent >= WARNING_RATIO * limit else "ok"

    def budget_state(self) -> dict[str, dict[str, float | str]]:
        d, m = self.spent()
        return {
            "daily": {"spent_usd": round(d, 6), "limit_usd": self.daily_budget_usd,
                      "state": self._state(d, self.daily_budget_usd)},
            "monthly": {"spent_usd": round(m, 6), "limit_usd": self.monthly_budget_usd,
                        "state": self._state(m, self.monthly_budget_usd)},
        }

    def check_budget(self) -> None:
        """呼叫前檢查;用罄 → BudgetExhausted(月度優先回報,因為它要到下個月才恢復)。"""
        state = self.budget_state()
        if state["monthly"]["state"] == "exhausted":
            raise BudgetExhausted("monthly")
        if state["daily"]["state"] == "exhausted":
            raise BudgetExhausted("daily")

    # ── 每來源每日問題上限 ──
    def take_question(self, client: str, limit: int) -> bool:
        """原子地「檢查並計一題」;已達上限 → False(不計)。limit <= 0 = 不限。"""
        if limit <= 0:
            return True
        _, day, _ = self._keys()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT n FROM questions WHERE day=? AND client=?", (day, client)
            ).fetchone()
            n = row[0] if row else 0
            if n >= limit:
                return False
            self._conn.execute(
                "INSERT INTO questions(day, client, n) VALUES (?,?,1) "
                "ON CONFLICT(day, client) DO UPDATE SET n = n + 1",
                (day, client),
            )
        return True

    def seconds_until_utc_midnight(self) -> int:
        now = self._now()
        tomorrow = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return max(1, int((tomorrow - now).total_seconds()))
