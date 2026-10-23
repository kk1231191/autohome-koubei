# -*- coding: utf-8 -*-
"""本地 Web 控制台: 打开浏览器就能配置并启动抓取。

部署形态:
    服务监听 127.0.0.1, 不直接对外。要分享给他人时用 share.py 起一条
    cloudflared 隧道, 把端口映射成公网 https 地址。
    为什么不能像普通网站那样部署到云服务器: 抓取依赖本机安装的 Chrome 与
    chromedriver, 且汽车之家对云机房 IP 有风控。所以"分享"只能是
    "服务在你机器上跑, 只把入口暴露出去"。

访问控制:
    只要通过隧道暴露, 就必须有口令 —— 否则任何拿到链接的人都能触发抓取
    (消耗你的出口IP, 可能招致封禁) 并下载全量 CSV。
    本机直连 (Host 为 127.0.0.1/localhost) 免口令, 公网访问需 Cookie 或 ?k=。

为什么用标准库而不是 Flask:
    整个服务只有几个 API + 2 个页面, 引入 Flask 的唯一收益是省 30 行路由代码,
    代价是多一个依赖、多一种"装了没装"的故障。这里用 ThreadingHTTPServer 足够。

任务隔离:
    抓取以子进程方式运行(而不是线程), 好处是:
      - 停止时能整棵进程树杀掉, 不会留下孤儿 Chrome
      - 爬虫崩溃不会拖垮 Web 服务
      - 内存/日志天然隔离
    代价是进度只能靠 stdout 与 CSV 行数间接推断, 见 _monitor()。

启动:
    python web.py            # 仅本机, http://127.0.0.1:8760
    python share.py          # 同时开公网隧道并打印分享链接
"""
from __future__ import annotations

import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, urlencode

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from autohome_spider.search import resolve_input, search_series   # noqa: E402
UI_FILE = os.path.join(BASE_DIR, "web_ui.html")
LOGIN_FILE = os.path.join(BASE_DIR, "web_login.html")
TOKEN_FILE = os.path.join(BASE_DIR, "output", ".share_token")
TOKEN_COOKIE = "au_share"

DEFAULT_CSV = "autohome_koubei.csv"
ROWS_PER_PAGE = 10          # 站点固定每页 10 条

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def load_share_token() -> str:
    """取分享口令: 环境变量 > 持久化文件 > 新生成。

    持久化是为了"隧道重启后链接还能用", 否则每次重启都换口令,
    之前发出去的链接就全废了。
    """
    env = (os.environ.get("AUTOHOME_SHARE_TOKEN") or "").strip()
    if env:
        return env
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            t = f.read().strip()
        if t:
            return t
    except FileNotFoundError:
        pass
    t = secrets.token_urlsafe(9)          # 12 字符, 抗暴力猜测足够
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(t)
    return t


# 一旦通过隧道把端口暴露到公网, 任何拿到链接的人都能触发抓取(消耗你的出口IP,
# 可能招致汽车之家封禁你的机器)并下载 CSV 全量数据。所以除本机直连外一律要求口令。
SHARE_TOKEN = load_share_token()


def count_rows(path: str) -> int:
    """统计 CSV 数据行数(不含表头)。文件不存在或只有表头时返回 0。"""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return 0
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            return max(0, sum(1 for _ in f) - 1)
    except Exception:
        return 0


def read_recent(path: str, n: int = 5) -> list:
    """取 CSV 最后 n 条的关键字段, 给前端做实时预览。"""
    if not os.path.exists(path):
        return []
    try:
        import csv
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
            rows = list(csv.DictReader(f))
        out = []
        for r in rows[-n:][::-1]:
            out.append({
                "time": r.get("发表时间", ""),
                "spec": r.get("车型版本", "")[:28],
                "score": r.get("综合口碑评分", ""),
                "title": r.get("口碑标题", "")[:40],
                "place": r.get("购买地点", ""),
            })
        return out
    except Exception:
        return []


class CrawlJob:
    """单例任务。同一时刻只允许一个抓取进程, 避免两个 Chrome 抢同一份 CSV。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.log: deque = deque(maxlen=600)
        self.started_at: float | None = None
        self.ended_at: float | None = None
        self.exit_code: int | None = None
        self.error = ""
        self.target = 0
        self.rows_at_start = 0
        self.params: dict = {}
        self.csv_path = os.path.join(BASE_DIR, "output", DEFAULT_CSV)

    # -- 状态 ------------------------------------------------------------- #
    def is_running(self) -> bool:
        with self._lock:
            return self.proc is not None and self.proc.poll() is None

    def status(self) -> dict:
        with self._lock:
            running = self.proc is not None and self.proc.poll() is None
            if self.proc is not None and not running and self.exit_code is None:
                self.exit_code = self.proc.returncode
                if self.ended_at is None:
                    self.ended_at = time.time()

        rows_now = count_rows(self.csv_path)
        gained = max(0, rows_now - self.rows_at_start)
        # 从未启动过任务时, rows_at_start 还是 0, gained 会等于文件总行数 ——
        # 那会把"历史累计"误显示成"本次完成"。所以用 started_at 做闸门。
        if self.started_at is None:
            gained = 0
        pct = 0
        if self.target:
            pct = min(100, round(gained / self.target * 100))

        end = self.ended_at or time.time()
        elapsed = int(end - self.started_at) if self.started_at else 0

        return {
            "running": running,
            "percent": pct,
            "done": gained,
            "target": self.target,
            "rows_total": rows_now,
            "elapsed": elapsed,
            "exit_code": self.exit_code,
            "error": self.error,
            "csv": os.path.basename(self.csv_path),
            "csv_size": _human_size(self.csv_path),
            "params": self.params,
            "log": list(self.log)[-160:],
            "recent": read_recent(self.csv_path, 5),
        }

    # -- 控制 ------------------------------------------------------------- #
    def start(self, series: list, pages: int, full_text: bool, csv_name: str,
              resolve_msgs: list | None = None) -> tuple:
        """series 必须是已校验过的车系ID列表(int)。"""
        if self.is_running():
            return False, "已有任务在运行, 请先停止"

        if not series:
            return False, "没有可用的车系"
        if not re.fullmatch(r"[A-Za-z0-9_\-.]+\.csv", csv_name or ""):
            return False, "输出文件名只允许字母/数字/下划线/中划线, 且必须以 .csv 结尾"
        csv_name = csv_name or DEFAULT_CSV

        out_dir = os.path.join(BASE_DIR, "output")
        os.makedirs(out_dir, exist_ok=True)

        self.csv_path = os.path.join(out_dir, csv_name)
        self.rows_at_start = count_rows(self.csv_path)
        self.target = max(1, pages) * ROWS_PER_PAGE * len(series)
        self.started_at = time.time()
        self.ended_at = None
        self.exit_code = None
        self.error = ""
        self.params = {"series": series, "pages": pages, "full_text": full_text, "csv": csv_name}
        with self._lock:
            self.log.clear()
            for m in resolve_msgs or []:
                self.log.append(f"[控制台] 车系解析: {m}")

        cmd = [
            sys.executable, "-u", os.path.join(BASE_DIR, "main.py"),
            "--series", *[str(s) for s in series],
            "--pages", str(pages),
            "--csv", csv_name,
        ]
        if not full_text:
            cmd.append("--no-detail")

        # 关键: 子进程必须用 UTF-8 输出。Windows 下 Python 默认按 GBK 写 stdout,
        # 中文日志到这里会变乱码, 前端显示一片问号。
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        flags = 0
        if os.name == "nt":
            # 独立进程组, 才能用 CTRL_BREAK 让子进程走 KeyboardInterrupt 分支,
            # 从而执行 main.py finally 里的 driver.quit(), 不留下孤儿 Chrome。
            flags = subprocess.CREATE_NEW_PROCESS_GROUP

        try:
            proc = subprocess.Popen(
                cmd, cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=env, creationflags=flags,
            )
        except Exception as e:
            self.error = f"启动失败: {e}"
            return False, self.error

        with self._lock:
            self.proc = proc
        threading.Thread(target=self._monitor, args=(proc,), daemon=True).start()
        return True, f"已启动: 车系 {series} / {pages} 页 / 目标约 {self.target} 条"

    def _monitor(self, proc: subprocess.Popen) -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = _ANSI.sub("", line).rstrip()
                if line:
                    with self._lock:
                        self.log.append(line)
        except Exception as e:
            with self._lock:
                self.log.append(f"[控制台] 读取子进程输出失败: {e}")
        code = proc.wait()
        with self._lock:
            self.exit_code = code
            self.ended_at = time.time()
            if code not in (0, 130) and not self.error:
                self.error = f"进程退出码 {code}, 详见运行日志"

    def stop(self) -> tuple:
        with self._lock:
            proc = self.proc
        if proc is None or proc.poll() is not None:
            return False, "当前没有运行中的任务"

        try:
            if os.name == "nt":
                os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
            else:
                proc.send_signal(signal.SIGINT)
        except Exception as e:
            self.log.append(f"[控制台] 发送中断信号失败({e}), 改用强制结束")

        for _ in range(30):          # 最多等 6s 让爬虫自己收拾残局
            if proc.poll() is not None:
                return True, "已停止"
            time.sleep(0.2)
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        return True, "已强制结束(可能残留 Chrome 进程, 可手动结束)"


def _human_size(path: str) -> str:
    try:
        n = os.path.getsize(path)
    except Exception:
        return "-"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


JOB = CrawlJob()


class Handler(BaseHTTPRequestHandler):
    server_version = "AutohomeKoubeiConsole/1.0"

    def log_message(self, fmt, *args):        # 静音默认的访问日志, 避免刷屏
        pass

    # -- 工具 ------------------------------------------------------------- #
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            return {}

    # -- 访问控制 --------------------------------------------------------- #
    @staticmethod
    def _cookie_value(header: str | None, name: str) -> str:
        for part in (header or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""

    def _host_is_local(self) -> bool:
        """是否从本机浏览器直连。

        必须用 Host 判断, 不能看来源IP: 经 cloudflared 隧道进来的请求,
        来源IP 同样是 127.0.0.1, 用IP判断会把所有公网访问都放行。
        """
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost")

    def _guard(self) -> bool:
        """鉴权闸门。True=放行; False=已自行发送响应。"""
        if self._host_is_local():
            return True

        if self._cookie_value(self.headers.get("Cookie"), TOKEN_COOKIE) == SHARE_TOKEN:
            return True

        qs = parse_qs(urlparse(self.path).query)
        if (qs.get("k") or [""])[0] == SHARE_TOKEN:
            # 带口令的分享链接: 种 Cookie 后跳到干净 URL,
            # 避免口令长期留在地址栏、历史记录和 Referer 里。
            rest = {k: v[0] for k, v in qs.items() if k != "k"}
            loc = urlparse(self.path).path + (("?" + urlencode(rest)) if rest else "")
            self._send(302, b"", "text/plain; charset=utf-8", {
                "Location": loc,
                "Set-Cookie": (f"{TOKEN_COOKIE}={SHARE_TOKEN}; Path=/; HttpOnly; "
                               f"SameSite=Lax; Max-Age=2592000"),
            })
            return False

        if urlparse(self.path).path in ("/", "/index.html"):
            self._login_page()
        else:
            self._json({"ok": False, "msg": "需要访问口令", "need_token": True}, 401)
        return False

    def _login_page(self, bad: bool = False) -> None:
        try:
            with open(LOGIN_FILE, "r", encoding="utf-8") as f:
                html = f.read()
        except FileNotFoundError:
            html = "<h1>缺少 web_login.html</h1>"
        html = html.replace(
            "<!--ERR-->",
            '<div class="err">口令不正确，请重试</div>' if bad else "",
        )
        # 统一 401: 未授权就该是 401, 不要用 200 伪装成正常页面。
        # 没有 WWW-Authenticate 头, 所以浏览器不会弹原生登录框。
        self._send(401, html.encode("utf-8"), "text/html; charset=utf-8",
                   {"X-Robots-Tag": "noindex, nofollow"})

    def _do_login(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace") if n > 0 else ""
        ctype = (self.headers.get("Content-Type") or "").lower()
        token = ""
        if "application/json" in ctype:
            try:
                token = str((json.loads(raw or "{}") or {}).get("token") or "").strip()
            except Exception:
                token = ""
        else:
            token = (parse_qs(raw).get("token") or [""])[0].strip()

        if token and secrets.compare_digest(token, SHARE_TOKEN):
            self._send(302, b"", "text/plain; charset=utf-8", {
                "Location": "/",
                "Set-Cookie": (f"{TOKEN_COOKIE}={SHARE_TOKEN}; Path=/; HttpOnly; "
                               f"SameSite=Lax; Max-Age=2592000"),
            })
        else:
            self._login_page(bad=True)

    # -- 路由 ------------------------------------------------------------- #
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/favicon.ico":          # 图标无需鉴权, 否则浏览器控制台报错
            self._send(204, b"", "image/x-icon")
            return
        if not self._guard():
            return
        if path in ("/", "/index.html"):
            try:
                with open(UI_FILE, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(500, b"web_ui.html not found", "text/plain; charset=utf-8")
            return
        if path == "/api/status":
            self._json(JOB.status())
            return
        if path == "/api/search":
            q = (parse_qs(urlparse(self.path).query).get("q") or [""])[0].strip()
            try:
                cands = search_series(q) if q else []
                self._json({"ok": True, "q": q, "candidates": cands})
            except Exception as e:
                self._json({"ok": False, "q": q, "candidates": [], "msg": str(e)}, 500)
            return
        if path == "/api/download":
            self._download()
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/login":                # 登录本身必须免鉴权, 否则永远进不来
            self._do_login()
            return
        if not self._guard():
            return
        if path == "/api/start":
            data = self._read_body()
            # 前端选中候选项时传 ids(精确); 否则传自由文本, 由后端解析车型名。
            ids_in = data.get("ids") or []
            raw = str(data.get("series") or "").strip()
            if isinstance(ids_in, list) and ids_in:
                tokens = [str(x) for x in ids_in]
            else:
                tokens = [p for p in re.split(r"[,，\s]+", raw) if p]
            if not tokens:
                self._json({"ok": False, "msg": "请填写车型名称（或车系ID）"}, 400)
                return

            try:
                series_ids, resolve_msgs = resolve_input(tokens)
            except Exception as e:
                self._json({"ok": False, "msg": f"车型名解析失败: {e}"}, 500)
                return
            if not series_ids:
                self._json({"ok": False,
                            "msg": "没有匹配到车系。" + "；".join(resolve_msgs)}, 400)
                return

            try:
                pages = int(data.get("pages") or 1)
            except ValueError:
                self._json({"ok": False, "msg": "页数必须是整数"}, 400)
                return
            if not (1 <= pages <= 200):
                self._json({"ok": False, "msg": "页数需在 1~200 之间(站点上限 200 页)"}, 400)
                return

            ok, msg = JOB.start(
                series=series_ids,
                pages=pages,
                full_text=bool(data.get("full_text", True)),
                csv_name=str(data.get("csv") or DEFAULT_CSV).strip(),
                resolve_msgs=resolve_msgs,
            )
            self._json({"ok": ok, "msg": msg, "resolved": resolve_msgs},
                       200 if ok else 409)
            return
        if path == "/api/stop":
            ok, msg = JOB.stop()
            self._json({"ok": ok, "msg": msg}, 200 if ok else 409)
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def _download(self) -> None:
        path = JOB.csv_path
        if not os.path.exists(path):
            self._send(404, "还没有生成数据文件".encode("utf-8"), "text/plain; charset=utf-8")
            return
        with open(path, "rb") as f:
            body = f.read()
        # 中文文件名需要 RFC 5987 编码, 否则 Chrome 会存成乱码
        from urllib.parse import quote
        name = os.path.basename(path)
        self._send(200, body, "text/csv; charset=utf-8", {
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
        })


def pick_port(preferred: int) -> int:
    for p in [preferred] + list(range(preferred + 1, preferred + 20)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise SystemExit(f"{preferred}~{preferred + 19} 端口全部占用, 请用 --port 指定其它端口")


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="汽车之家口碑抓取控制台")
    ap.add_argument("-p", "--port", type=int, default=8760)
    args = ap.parse_args()

    port = pick_port(args.port)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print("=" * 62)
    print("  汽车之家口碑抓取控制台")
    print(f"  本机访问: {url}")
    print(f"  分享口令: {SHARE_TOKEN}")
    print("  (本机直连免口令; 从公网链接访问需要口令)")
    print("  分享给他人: 运行 share.py, 会自动开隧道并打印带口令的链接")
    print("  Ctrl+C 退出")
    print("=" * 62, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出...", flush=True)
        JOB.stop()
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
