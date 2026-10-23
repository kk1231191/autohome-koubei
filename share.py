# -*- coding: utf-8 -*-
"""一键把控制台分享到公网。

做三件事:
  1. 确保本机控制台在跑 (没跑就起一个)
  2. 用 cloudflared 快速隧道把 8760 映射成公网 https 地址
  3. 打印带口令的完整分享链接

为什么不是把程序部署到云服务器:
    抓取需要真实浏览器渲染(列表页数据由前端 XHR 拉取), 且汽车之家对云机房 IP 有风控。
    本项目的浏览器已内置在 browsers/ 下(Chrome for Testing, 由 准备浏览器.bat 下载),
    Playwright 走 CDP 直接驱动, 不需要 chromedriver。
    所以"分享"的正确形态是: 服务在你机器上跑, 只把入口通过隧道暴露出去。
    别人打开链接操作, 任务实际在你这台机器上执行。

    注意: 对方不需要装 Python / Chrome / chromedriver, 但**必须由你这边执行抓取**,
    因此你的机器要开机联网。要彻底脱离本机, 得买一台 Windows 云主机部署。

关于链接稳定性:
    Cloudflare 快速隧道(免账号)官方声明不保证可用性, 且**每次重启域名都会变**。
    之前的链接一旦隧道进程退出就失效, 需要重新运行本脚本并把新链接发出去。
    要长期固定域名, 需要 Cloudflare 账号 + 命名隧道 (cloudflared tunnel create)。

用法:
    python share.py                 # 起服务+隧道, 打印链接
    python share.py --port 9000
    python share.py --no-web        # 控制台已在别处跑, 只起隧道
"""
from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(BASE_DIR, "output", ".share_token")
CF_CANDIDATES = [
    os.environ.get("CLOUDFLARED", ""),
    os.path.join(BASE_DIR, "tools", "cloudflared", "cf.exe"),
    r"D:\WorkBuddy\tools\cloudflared\cf.exe",
    "cloudflared",
]
_URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex((host, port)) == 0


def find_cloudflared() -> str | None:
    for p in CF_CANDIDATES:
        if not p:
            continue
        if os.path.sep in p or "/" in p:
            if os.path.exists(p):
                return p
        else:
            from shutil import which
            hit = which(p)
            if hit:
                return hit
    return None


def read_token(timeout: float = 15.0) -> str:
    """读分享口令。web.py 首次启动时会生成该文件。"""
    end = time.time() + timeout
    while time.time() < end:
        try:
            with open(TOKEN_FILE, "r", encoding="utf-8") as f:
                t = f.read().strip()
            if t:
                return t
        except FileNotFoundError:
            pass
        time.sleep(0.4)
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="把口碑抓取控制台分享到公网")
    ap.add_argument("-p", "--port", type=int, default=8760)
    ap.add_argument("--no-web", action="store_true", help="不启动控制台(假定已在运行)")
    args = ap.parse_args()

    cf = find_cloudflared()
    if not cf:
        print("找不到 cloudflared。请把它放到 D:\\WorkBuddy\\tools\\cloudflared\\cf.exe,\n"
              "或设置环境变量 CLOUDFLARED 指向可执行文件。\n"
              "下载: https://github.com/cloudflare/cloudflared/releases")
        return 2

    web_proc = None
    if args.no_web or port_open(args.port):
        print(f"[1/3] 控制台已在 127.0.0.1:{args.port} 运行, 复用")
    else:
        print(f"[1/3] 启动控制台 127.0.0.1:{args.port} ...")
        web_proc = subprocess.Popen(
            [sys.executable, "-u", os.path.join(BASE_DIR, "web.py"), "--port", str(args.port)],
            cwd=BASE_DIR,
        )
        for _ in range(60):
            if port_open(args.port):
                break
            time.sleep(0.4)
        else:
            print("控制台启动超时")
            return 2

    token = read_token()
    if not token:
        print("未能读取分享口令( output/.share_token )。控制台可能启动失败。")
        return 2

    print(f"[2/3] 建立隧道 ...")
    env = dict(os.environ)
    # cloudflared 自己连 Cloudflare 边缘, 不要被系统代理拦下来
    for k in list(env):
        if k.lower() in ("http_proxy", "https_proxy", "all_proxy"):
            env.pop(k)

    cf_proc = subprocess.Popen(
        [cf, "tunnel", "--url", f"http://127.0.0.1:{args.port}", "--no-autoupdate"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
    )

    found = threading.Event()
    holder: dict = {}

    def _pump():
        try:
            for line in cf_proc.stdout:            # type: ignore[union-attr]
                clean = _ANSI.sub("", line).rstrip()
                if not found.is_set():
                    m = _URL_RE.search(clean)
                    if m:
                        holder["url"] = m.group(0)
                        found.set()
                        continue
                    # 隧道建立前的进度信息保留, 方便排查
                    if clean and ("ERR" in clean or "WRN" in clean):
                        print("   " + clean[:160])
        except Exception:
            pass

    threading.Thread(target=_pump, daemon=True).start()

    if not found.wait(timeout=45):
        print("隧道建立超时。可能的网络原因: 无法出站访问 Cloudflare 边缘节点。")
        cf_proc.terminate()
        if web_proc:
            web_proc.terminate()
        return 2

    url = holder["url"]
    share = f"{url}/?k={token}"

    print("[3/3] 隧道已就绪\n")
    print("=" * 78)
    print("  分享链接（直接发给对方，点开即用，无需单独报口令）")
    print()
    print(f"    {share}")
    print()
    print(f"  仅口令:  {token}")
    print(f"  本机:    http://127.0.0.1:{args.port}")
    print("=" * 78)
    print("\n注意:")
    print("  · 任务在你自己这台电脑上执行，对方只是远程操作界面")
    print("  · 你的电脑关机 / 断网 / 关闭本窗口，链接立即失效")
    print("  · 域名每次重启都会变，重启后要重新发一次链接")
    print("  · 同一时间只能跑一个抓取任务（第二个人点开始会提示占用）")
    print("  · Ctrl+C 停止分享\n")

    try:
        while True:
            if cf_proc.poll() is not None:
                print("\n[!] 隧道进程已退出，链接失效。请重新运行 share.py")
                break
            if web_proc is not None and web_proc.poll() is not None:
                print("\n[!] 控制台进程已退出。")
                break
            time.sleep(1.5)
    except KeyboardInterrupt:
        print("\n正在关闭隧道 ...")
    finally:
        for p in (cf_proc, web_proc):
            if p is not None and p.poll() is None:
                try:
                    p.terminate()
                    p.wait(timeout=6)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
