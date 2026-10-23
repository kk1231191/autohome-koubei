# -*- coding: utf-8 -*-
"""把浏览器下载到项目目录，让程序自带浏览器、换机即用。

为什么不用 `playwright install`:
    它从 cdn.playwright.dev 拉 Chrome for Testing, 国内经常连不上(实测卡住 4 分钟无进展)。
    这里改从 npmmirror 的 chrome-for-testing 镜像下载, 实测可用。

下载什么:
    Chrome for Testing 是一个独立的、可随项目分发的 Chrome, 不写注册表、不装系统组件,
    解压到 <项目>/browsers/ 就能用。Playwright 通过 CDP 驱动它, 不需要 chromedriver,
    所以也不存在"驱动版本与浏览器版本必须一致"的问题。

用法:
    python setup_browser.py                 # 下载到 <项目>/browsers/
    python setup_browser.py --version 148.0.7778.96
    python setup_browser.py --check         # 只看当前用的是哪个浏览器, 不下载
"""
from __future__ import annotations

import argparse
import os
import sys
import zipfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from autohome_spider.pw_browser import find_project_browser, find_system_chrome  # noqa: E402

# 与 playwright 1.x 期望的 chromium 版本对齐; 可用 --version 覆盖
DEFAULT_VERSION = "148.0.7778.96"
MIRRORS = [
    "https://cdn.npmmirror.com/binaries/chrome-for-testing/{ver}/{plat}/{name}",
    "https://npmmirror.com/mirrors/chrome-for-testing/{ver}/{plat}/{name}",
    "https://storage.googleapis.com/chrome-for-testing-public/{ver}/{plat}/{name}",
]


def mirror_list() -> list[str]:
    """按当前平台给下载源排序。

    国内 Windows 桌面优先 npmmirror(快); GitHub Actions 这类海外 Linux 机器
    反而直连 storage.googleapis.com 更快, 跨境拉 npmmirror 经常只有几十 KB/s。
    顺序只影响速度, 不影响成功率 —— 失败会自动试下一个。
    """
    override = os.environ.get("CFT_MIRROR", "").strip()
    if override:
        # 允许环境变量指定单一镜像, 模板里同样支持 {ver}/{plat}/{name}
        return [override]
    if sys.platform.startswith("linux"):
        return list(reversed(MIRRORS))
    return list(MIRRORS)


def platform_dir() -> str:
    if sys.platform.startswith("win"):
        return "win64" if sys.maxsize > 2**32 else "win32"
    if sys.platform == "darwin":
        return "mac-arm64" if "arm" in os.uname().machine else "mac-x64"
    return "linux64"


def download(ver: str, browsers_path: str) -> bool:
    import requests

    pd = platform_dir()
    name = f"chrome-{pd}.zip"
    dest_dir = os.path.join(browsers_path, f"chrome-{pd}")
    os.makedirs(dest_dir, exist_ok=True)
    zip_path = os.path.join(browsers_path, f"chrome-{pd}.zip.part")

    # 支持断点续传: 大文件在国内镜像上偶尔会断
    have = os.path.getsize(zip_path) if os.path.exists(zip_path) else 0

    session = requests.Session()
    session.trust_env = False              # 直连, 避免系统代理干扰
    for tpl in mirror_list():
        url = tpl.format(ver=ver, plat=pd, name=name)
        headers = {"User-Agent": "Mozilla/5.0"}
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            print(f"  尝试: {url}")
            with session.get(url, headers=headers, stream=True, timeout=120) as r:
                if r.status_code not in (200, 206):
                    print(f"    HTTP {r.status_code}, 换下一个源")
                    continue
                mode = "ab" if (r.status_code == 206 and have) else "wb"
                if mode == "wb":
                    have = 0
                total = int(r.headers.get("Content-Length") or 0) + have
                done = have
                with open(zip_path, mode) as f:
                    for chunk in r.iter_content(1 << 16):
                        f.write(chunk)
                        done += len(chunk)
                        if total:
                            pct = done * 100 // total
                            mb = done / 1024 / 1024
                            tm = total / 1024 / 1024
                            sys.stdout.write(f"\r    下载中 {pct:3d}%  {mb:6.1f}/{tm:6.1f} MB")
                            sys.stdout.flush()
                sys.stdout.write("\n")
                print(f"    完成: {done / 1024 / 1024:.1f} MB")
                break
        except Exception as e:
            print(f"    失败: {str(e)[:120]}")
            continue
    else:
        print("  所有下载源都失败。")
        return False

    print(f"  解压到 {dest_dir} ...")
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(browsers_path)
    except Exception as e:
        print(f"  解压失败: {e}")
        return False

    # zipfile.extractall 不恢复 Unix 权限位: Linux 上 chrome 会变成 0644,
    # 启动时直接 PermissionError: [Errno 13]。必须显式补 0755。
    if not sys.platform.startswith("win"):
        _EXECUTABLES = {"chrome", "chrome_crashpad_handler", "chrome_sandbox", "chromedriver"}
        fixed = 0
        for root, _dirs, files in os.walk(browsers_path):
            for fn in files:
                if fn in _EXECUTABLES:
                    try:
                        os.chmod(os.path.join(root, fn), 0o755)
                        fixed += 1
                    except OSError:
                        pass
        if fixed:
            print(f"  已修正 {fixed} 个可执行文件的权限位")

    try:
        os.remove(zip_path)
    except OSError:
        pass

    exe = find_project_browser(browsers_path)
    if not exe:
        print("  解压后未找到 chrome 可执行文件, 目录结构可能不符合预期。")
        return False
    print(f"  浏览器就绪: {exe}")
    return True


def verify(exe: str) -> None:
    """启动一次, 确认能跑并打印版本。"""
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.join(BASE_DIR, "browsers"))
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, executable_path=exe,
                                  args=["--no-sandbox", "--no-proxy-server"])
            print(f"  自检通过: 浏览器版本 {b.version}")
            b.close()
    except Exception as e:
        print(f"  自检未通过(不一定影响使用): {str(e)[:200]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="下载 Chrome 到项目目录")
    ap.add_argument("--version", default=DEFAULT_VERSION)
    ap.add_argument("--browsers-path", default=os.path.join(BASE_DIR, "browsers"))
    ap.add_argument("--check", action="store_true", help="只检查当前使用的浏览器")
    ap.add_argument("--force", action="store_true", help="已有也重新下载")
    args = ap.parse_args()

    browsers_path = os.path.abspath(args.browsers_path)
    print("=" * 64)
    print("  浏览器准备")

    proj = find_project_browser(browsers_path)
    if proj and not args.force:
        print(f"  项目内已有浏览器: {proj}")
        if args.check:
            verify(proj)
        else:
            verify(proj)
        print("  (如需重新下载, 加 --force)")
        print("=" * 64)
        return 0

    if args.check:
        sysc = find_system_chrome()
        print(f"  项目内: 无")
        print(f"  系统 Chrome: {sysc or '未找到'}")
        print("=" * 64)
        return 0

    print(f"  版本: {args.version}   平台: {platform_dir()}")
    print(f"  目标: {browsers_path}")
    ok = download(args.version, browsers_path)
    if ok:
        exe = find_project_browser(browsers_path)
        if exe:
            verify(exe)
    print("=" * 64)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
