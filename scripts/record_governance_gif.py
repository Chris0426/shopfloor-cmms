#!/usr/bin/env python
r"""錄製 README 的治理迴圈 GIF(docs/media/governance-loop.gif)。

流程(對應 docs/DEMO.md §3–4):
  1. 登入頁(demo banner + 三個一鍵帳號)→ 以 admin 登入
  2. /admin/proposals:兩筆待確認提案,游標指向 agent:assistant 提的 close_work_order(WO 96301)
  3. 按該筆的 Confirm
  4. /admin/audit:最新一筆確認記在 human:admin 名下,不是 agent
  5. /app/work-orders/96301:狀態 Closed,再往下捲到工作日誌與領料

選擇器對的是 v2 介面(templates/v2/;登入頁 v2 仍沿用 v1 的 login.html)。

每一步都對 page.content() 做脫敏檢查(整字、不分大小寫,禁字由環境變數帶入),命中即中止;
並列出各頁看到的產線名稱(預期只有 Zone A–Zone D)。
最後以 ffmpeg 兩段式調色盤(palettegen / paletteuse)把 .webm 轉成 GIF;
mpdecimate 丟掉靜止期間的重複影格(GIF 改用可變影格延遲,節奏不變),
UI 畫面多為平塗色故不 dither——不這樣做 12 fps / 1024 px 約 3 MB。

在 demo 主機上用官方 Playwright 容器執行(主機 OS 不必安裝任何東西):

    docker run --rm --network host -e FORBIDDEN_TERMS -v /home/ubuntu/gifrec:/work -w /work \
      mcr.microsoft.com/playwright/python:v1.49.0-noble \
      bash -c "pip install -q --break-system-packages playwright==1.49.0 \
        && apt-get update -qq && apt-get install -y -qq ffmpeg >/dev/null \
        && python record_governance_gif.py"

(該映像只帶瀏覽器,不帶 playwright Python 套件,版本需與映像 tag 一致;
 內附的 ffmpeg 是錄影用精簡版、沒有 GIF 濾鏡,故另裝 ffmpeg。)

環境變數:
  FORBIDDEN_TERMS / FORBIDDEN_TERMS_FILE
            脫敏禁字(必填;來源是私有的 PUBLISHING.md 敏感詞清單,絕不寫進本 repo)
  BASE_URL  站台位址,預設 http://127.0.0.1:8000
  OUT_DIR   輸出目錄,預設目前目錄(產出 governance-loop.gif 與原始 .webm)
  TEXT_DIR  選填;每個檢查步驟把 document.body.innerText 存成 <step>.txt,供另行核對畫面文字
  GIF_FPS   預設 12
  GIF_WIDTH 預設 1024
  MODE      full(預設)/ convert(只重轉既有 .webm,配 TRIM_START)/ sweep(只跑脫敏補充檢查)

注意:demo 站每逢 :00 / :30 UTC 重置,約停一分鐘;勿在前後兩分鐘內錄。
提案一旦 Confirm 就消失,重錄前需等下一次重置。
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
OUT_DIR = Path(os.environ.get("OUT_DIR", ".")).resolve()
GIF_FPS = int(os.environ.get("GIF_FPS", "12"))
GIF_WIDTH = int(os.environ.get("GIF_WIDTH", "1024"))
TEXT_DIR = Path(os.environ["TEXT_DIR"]).resolve() if os.environ.get("TEXT_DIR") else None
VIEWPORT = {"width": 1280, "height": 800}
WORK_ORDER_NO = "96301"


# 脫敏閘門:禁字清單刻意不寫進公開 repo(清單本身就會洩漏原始名稱),
# 由 FORBIDDEN_TERMS(逗號分隔)或 FORBIDDEN_TERMS_FILE(一行一個)帶入;沒給就拒跑。
def _load_forbidden_terms() -> list[str]:
    terms = [t.strip() for t in os.environ.get("FORBIDDEN_TERMS", "").split(",")]
    path = os.environ.get("FORBIDDEN_TERMS_FILE")
    if path:
        terms += [t.strip() for t in Path(path).read_text(encoding="utf-8").splitlines()]
    terms = [t for t in terms if t]
    if not terms:
        raise SystemExit("sanitization gate needs FORBIDDEN_TERMS or FORBIDDEN_TERMS_FILE")
    return terms


FORBIDDEN_RE = [
    (term, re.compile(r"(?<![\w.])" + re.escape(term) + r"(?![\w.])", re.IGNORECASE))
    for term in _load_forbidden_terms()
]
LINE_NAME_RE = re.compile(r"\b(?:Zone|Line)\s+[A-Za-z0-9-]+\b")

# 無頭錄影不會畫出滑鼠,注入一個假游標讓觀者看得到「指向」與「點擊」
FAKE_CURSOR_JS = """
(() => {
  const install = () => {
    if (document.getElementById('__gif_cursor')) return;
    const c = document.createElement('div');
    c.id = '__gif_cursor';
    c.style.cssText = 'position:fixed;left:-40px;top:-40px;width:18px;height:18px;' +
      'border-radius:50%;background:rgba(220,38,38,.55);border:2px solid #fff;' +
      'box-shadow:0 0 0 1px rgba(0,0,0,.35);pointer-events:none;z-index:2147483647;' +
      'transform:translate(-50%,-50%);transition:transform .12s;';
    document.body.appendChild(c);
    const last = sessionStorage.getItem('__gif_cursor_pos');
    if (last) { const [x, y] = JSON.parse(last); c.style.left = x + 'px'; c.style.top = y + 'px'; }
    document.addEventListener('mousemove', e => {
      c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px';
      sessionStorage.setItem('__gif_cursor_pos', JSON.stringify([e.clientX, e.clientY]));
    }, true);
    const press = s => () => { c.style.transform = 'translate(-50%,-50%) scale(' + s + ')'; };
    document.addEventListener('mousedown', press(.6), true);
    document.addEventListener('mouseup', press(1), true);
  };
  if (document.body) install(); else document.addEventListener('DOMContentLoaded', install);
})();
"""

seen_line_names: set[str] = set()
gate_log: list[str] = []


def check_page(page: Page, step: str) -> None:
    """脫敏閘門:命中任何禁字就丟例外,並收集產線名稱。"""
    html = page.content()
    # 標籤名(HTML 元素名)不是畫面內容,短禁字可能誤中;只抹掉元素名,屬性與文字照掃
    scan = re.sub(r"</?[A-Za-z][\w-]*", "<", html)
    hits = []
    for term, rx in FORBIDDEN_RE:
        for m in rx.finditer(scan):
            ctx = scan[max(0, m.start() - 40) : m.end() + 40].replace("\n", " ")
            hits.append(f"{term!r} at {m.start()}: ...{ctx}...")
    names = sorted(set(LINE_NAME_RE.findall(html)))
    seen_line_names.update(names)
    line = f"[gate] {step:<22} url={page.url} forbidden_hits={len(hits)} line_names={names}"
    print(line, flush=True)
    gate_log.append(line)
    if TEXT_DIR is not None:
        TEXT_DIR.mkdir(parents=True, exist_ok=True)
        (TEXT_DIR / f"{step}.txt").write_text(
            page.evaluate("document.body.innerText"), encoding="utf-8"
        )
    if hits:
        for h in hits:
            print("  HIT " + h, file=sys.stderr)
        raise SystemExit(f"SANITIZATION GATE FAILED at step {step!r}: {len(hits)} hit(s)")


def glide_to(page: Page, locator, steps: int = 25, first_line: bool = False) -> None:
    """游標平滑移到元素中心(給觀者看清楚指的是哪裡)。

    first_line=True:改指第一行文字的高度 —— v2 表格列較高、文字靠上,指儲存格中心會落在空白處。
    """
    box = locator.bounding_box()
    if box is None:
        raise SystemExit(f"element not visible: {locator}")
    dy = min(box["height"] / 2, 16) if first_line else box["height"] / 2
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + dy, steps=steps)


def find_ffmpeg() -> str:
    """優先用系統 ffmpeg(需有 palettegen);Playwright 內附的精簡版通常沒有 GIF 濾鏡。"""
    candidates = [shutil.which("ffmpeg")] + sorted(glob.glob("/ms-playwright/ffmpeg-*/ffmpeg*"))
    for cand in filter(None, candidates):
        try:
            out = subprocess.run(
                [cand, "-hide_banner", "-filters"], capture_output=True, text=True
            ).stdout
        except OSError:
            continue
        if "palettegen" in out and "paletteuse" in out:
            return cand
    raise SystemExit(
        "no ffmpeg with palettegen/paletteuse found; apt-get install -y ffmpeg in the container"
    )


def record() -> tuple[Path, float]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    video_dir = OUT_DIR / "video"
    shutil.rmtree(video_dir, ignore_errors=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(
            viewport=VIEWPORT,
            locale="en-US",
            record_video_dir=str(video_dir),
            record_video_size=VIEWPORT,
        )
        context.add_init_script(FAKE_CURSOR_JS)
        page = context.new_page()
        t0 = time.monotonic()

        # 1. 登入頁(先切英文)
        page.goto(f"{BASE_URL}/app/set-locale?locale=en&next=/app/login", wait_until="load")
        if "/app/login" not in page.url:
            raise SystemExit(f"expected login page, got {page.url}")
        page.mouse.move(640, 300)
        page.wait_for_timeout(300)
        trim_start = time.monotonic() - t0
        check_page(page, "1-login")
        page.wait_for_timeout(2800)

        admin_btn = page.locator(
            "form.login-demo__row:has(input[name=username][value=admin]) button"
        )
        glide_to(page, admin_btn)
        page.wait_for_timeout(900)
        admin_btn.click()
        page.wait_for_load_state("load")
        check_page(page, "1b-after-login")
        page.wait_for_timeout(1500)

        # 2. 待確認提案
        page.goto(f"{BASE_URL}/admin/proposals", wait_until="load")
        check_page(page, "2-proposals")
        rows = page.locator("table.adm-table").first.locator("tbody tr")
        if rows.count() != 2:
            raise SystemExit(
                f"expected 2 pending proposals, found {rows.count()}"
                " (already confirmed? wait for the next reset)"
            )
        agent_row = rows.filter(has=page.locator("td.mono", has_text="agent:assistant"))
        if agent_row.count() != 1 or WORK_ORDER_NO not in agent_row.inner_text():
            raise SystemExit("agent:assistant proposal for WO 96301 not found")
        page.wait_for_timeout(2000)
        glide_to(page, agent_row.locator("td.mono"), first_line=True)
        page.wait_for_timeout(1800)
        # v2:操作欄是「Close work order」標籤 + WO-96301 chip,指標籤
        glide_to(page, agent_row.locator("td").first.locator(".tag"))
        page.wait_for_timeout(1800)

        # 3. Confirm
        confirm_btn = agent_row.locator("form[action$='/confirm'] button[type=submit]")
        glide_to(page, confirm_btn)
        page.wait_for_timeout(1000)
        confirm_btn.click()
        page.wait_for_load_state("load")
        check_page(page, "3-after-confirm")
        page.wait_for_timeout(2200)

        # 4. 稽核軌跡
        page.goto(f"{BASE_URL}/admin/audit", wait_until="load")
        check_page(page, "4-audit")
        first_row = page.locator("table.adm-table tbody tr").first
        first_text = first_row.inner_text()
        print(f"[audit] newest row: {' | '.join(first_text.split())}", flush=True)
        if "human:admin" not in first_text:
            raise SystemExit("newest audit row is not attributed to human:admin")
        page.wait_for_timeout(1500)
        # v2:作者是 span.author(v1 的 .author-chip 已不存在)
        glide_to(page, first_row.locator(".author"))
        page.wait_for_timeout(2800)

        # 5. 工單 96301
        page.goto(f"{BASE_URL}/app/work-orders/{WORK_ORDER_NO}", wait_until="load")
        check_page(page, "5-work-order")
        body = page.inner_text("body")
        if "Closed" not in body:
            raise SystemExit("WO 96301 does not show Closed")
        page.wait_for_timeout(2500)
        # v2 沒有 v1 那種狀態時間軸;往下捲到領料區底端剛好入鏡,
        # 畫面保留結案處置 / 停機時數、工作日誌與領料
        parts = page.locator("section.sec:has(#parts)").bounding_box()
        if parts is None:
            raise SystemExit("parts section not found on the work-order page")
        page.mouse.wheel(0, max(0, parts["y"] + parts["height"] + 24 - VIEWPORT["height"]))
        page.wait_for_timeout(3000)
        check_page(page, "5b-log-parts")

        video_path = Path(page.video.path())
        context.close()
        browser.close()

    raw = OUT_DIR / "governance-loop.webm"
    shutil.move(str(video_path), raw)
    shutil.rmtree(video_dir, ignore_errors=True)
    return raw, trim_start


def to_gif(raw: Path, trim_start: float) -> Path:
    ffmpeg = find_ffmpeg()
    gif = OUT_DIR / "governance-loop.gif"
    palette = OUT_DIR / "palette.png"
    # mpdecimate max=6:最多連丟 6 格,靜止畫面仍至少每 0.5 秒有一格,結尾停留不會被吃掉
    filters = f"fps={GIF_FPS},mpdecimate=max=6,scale={GIF_WIDTH}:-1:flags=lanczos"
    common = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{trim_start:.2f}",
        "-i",
        str(raw),
    ]
    subprocess.run(
        common + ["-vf", f"{filters},palettegen=stats_mode=diff", str(palette)], check=True
    )
    subprocess.run(
        common[:-2]
        + [
            "-i",
            str(raw),
            "-i",
            str(palette),
            "-lavfi",
            f"{filters}[x];[x][1:v]paletteuse=dither=none:diff_mode=rectangle",
            "-fps_mode",
            "vfr",
            "-loop",
            "0",
            str(gif),
        ],
        check=True,
    )
    palette.unlink(missing_ok=True)
    return gif


def sweep() -> None:
    """錄影外的補充檢查:列出全部設備產線的頁面也要過脫敏閘門(不進 GIF)。"""
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_context(viewport=VIEWPORT, locale="en-US").new_page()
        page.goto(f"{BASE_URL}/app/set-locale?locale=en&next=/app/login", wait_until="load")
        page.locator("form.login-demo__row:has(input[name=username][value=admin]) button").click()
        page.wait_for_load_state("load")
        page.goto(f"{BASE_URL}/admin/owners", wait_until="load")
        check_page(page, "sweep-owners")
        page.goto(f"{BASE_URL}/app/work-orders/{WORK_ORDER_NO}", wait_until="load")
        eq_link = page.locator("a[href^='/app/equipment/']").first
        if eq_link.count():
            page.goto(BASE_URL + eq_link.get_attribute("href"), wait_until="load")
            check_page(page, "sweep-equipment")
        browser.close()


def main() -> None:
    # MODE=full(預設:錄影+轉檔+補充檢查)/ convert(只把既有 .webm 重轉 GIF)/ sweep(只跑補充檢查)
    mode = os.environ.get("MODE", "full")
    if mode in ("full", "convert"):
        if mode == "full":
            raw, trim_start = record()
        else:
            raw, trim_start = (
                OUT_DIR / "governance-loop.webm",
                float(os.environ.get("TRIM_START", "0.5")),
            )
        gif = to_gif(raw, trim_start)
        print(f"[done] {gif} {gif.stat().st_size / 1e6:.2f} MB (trimmed first {trim_start:.2f}s)")
    if mode in ("full", "sweep"):
        sweep()
    if gate_log:
        print(f"[done] distinct line names seen: {sorted(seen_line_names)}")
        print(f"[done] forbidden-term hits: 0 across {len(gate_log)} checks")


if __name__ == "__main__":
    main()
