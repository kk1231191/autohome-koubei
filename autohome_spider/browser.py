# -*- coding: utf-8 -*-
"""浏览器与驱动管理。

这个模块存在的唯一理由是抹平两个会让"看起来能跑"的脚本直接崩掉的环境坑:

坑 1 —— chromedriver 与 Chrome 大版本不一致
    Chrome 是自动更新的, chromedriver 不会。差一个大版本就报
    `SessionNotCreatedException: This version of ChromeDriver only supports Chrome version N`。
    本机实测: PATH 里躺着 148 的驱动, 浏览器已经是 155。

坑 2 —— 系统级 HTTP_PROXY 把"连本地驱动"的请求也送进代理
    Selenium 用 urllib3 连 127.0.0.1:<驱动端口>, 而 urllib3 会读 *_proxy 环境变量,
    于是请求被转发到外部代理, 代理回一句 `unhandled request`, Selenium 把它
    包装成 WebDriverException。报错信息完全看不出和代理有关, 极难排查。
    修复方式是创建 driver 前把 proxy 环境变量清掉 —— 见 sanitize_proxy_env()。
    注意顺序: 下载驱动时仍需要代理(访问 googleapis), 所以清理动作必须
    放在下载之后、创建 driver 之前。
"""
from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import subprocess
import sys
import zipfile
from typing import Optional, Tuple

log = logging.getLogger(__name__)

_CFT_BASE = "https://storage.googleapis.com/chrome-for-testing-public"
_CFT_VERSIONS = "https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json"

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


def sanitize_proxy_env() -> list:
    """清空进程内的 proxy 环境变量, 返回被清掉的键名(便于日志审计)。

    只影响当前进程, 不写注册表、不改系统设置, 退出即恢复。
    """
    removed = []
    for k in list(os.environ.keys()):
        if k.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
            removed.append(f"{k}={os.environ.pop(k)}")
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    return removed


def detect_chrome_binary(explicit: str = "") -> Optional[str]:
    if explicit and os.path.exists(explicit):
        return explicit
    for p in CHROME_CANDIDATES:
        if p and os.path.exists(p):
            return p
    for pat in (
        r"C:\Program Files\Google\Chrome\Application\*\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\*\chrome.exe",
    ):
        hits = sorted(glob.glob(pat), reverse=True)
        if hits:
            return hits[0]
    return None


def chrome_version(chrome_binary: str) -> Tuple[int, str]:
    """返回 (主版本, 完整版本)。

    优先取安装目录下的版本号子目录名(最快、无需启动进程);
    兜底用 chrome.exe -dump-dom 之外的方式不可靠, 因此最后才用文件版本。
    """
    if chrome_binary:
        parts = os.path.normpath(chrome_binary).split(os.sep)
        for seg in reversed(parts):
            m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)\.(\d+)", seg)
            if m:
                return int(m.group(1)), seg
    # 兜底: 用 PowerShell 读文件版本
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Item '{chrome_binary}').VersionInfo.ProductVersion"],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        m = re.match(r"(\d+)\.(\d+)\.(\d+)\.(\d+)", out)
        if m:
            return int(m.group(1)), m.group(0)
    except Exception as e:
        log.debug("读取 Chrome 版本失败: %s", e)
    return 0, ""


def _driver_major(path: str) -> int:
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20).stdout
        m = re.search(r"ChromeDriver\s+(\d+)\.", out)
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


def _download(url: str, dst: str, timeout: int = 180) -> bool:
    import requests
    try:
        with requests.get(url, stream=True, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"}) as r:
            if r.status_code != 200:
                log.warning("下载失败 %s -> HTTP %s", url, r.status_code)
                return False
            with open(dst, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
        return os.path.getsize(dst) > 1_000_000
    except Exception as e:
        log.warning("下载异常 %s: %s", url, e)
        return False


def _resolve_cft_version(full_version: str, major: int) -> str:
    """给定完整版本, 返回 chrome-for-testing 上确实存在的版本号。

    先用完整版本直试; 不存在则查 known-good-versions 找同主版本最新。
    """
    import requests
    try:
        r = requests.get(_CFT_VERSIONS, timeout=40)
        if r.status_code == 200:
            data = r.json().get("versions", [])
            same = [v["version"] for v in data if v.get("version", "").startswith(f"{major}.")]
            if same:
                # 优先完全一致
                if full_version in same:
                    return full_version
                return same[-1]
    except Exception as e:
        log.debug("查询 chrome-for-testing 版本列表失败: %s", e)
    return full_version


def ensure_driver(explicit: str, chrome_binary: str, cache_dir: str) -> Optional[str]:
    """返回一个与本机 Chrome 主版本匹配的 chromedriver 绝对路径。"""
    major, full = chrome_version(chrome_binary)
    if not major:
        log.error("无法识别 Chrome 版本, 请检查浏览器是否安装")
        return None
    log.info("本机 Chrome: %s (主版本 %s)", full or "未知", major)

    # 1) 配置里显式指定的驱动
    if explicit and os.path.exists(explicit):
        dm = _driver_major(explicit)
        if dm == major:
            log.info("使用配置指定的驱动: %s (v%s)", explicit, dm)
            return explicit
        log.warning("配置指定的驱动主版本 %s 与 Chrome %s 不匹配, 尝试自动匹配", dm, major)

    # 2) 缓存目录
    os.makedirs(cache_dir, exist_ok=True)
    cached = os.path.join(cache_dir, f"chromedriver-{major}", "chromedriver-win64", "chromedriver.exe")
    if os.path.exists(cached) and _driver_major(cached) == major:
        log.info("使用缓存驱动: %s", cached)
        return cached

    # 3) 下载
    real_ver = _resolve_cft_version(full, major)
    dst_dir = os.path.join(cache_dir, f"chromedriver-{major}")
    os.makedirs(dst_dir, exist_ok=True)
    zip_path = os.path.join(dst_dir, "chromedriver.zip")
    platform_dir = "win64" if sys.maxsize > 2**32 else "win32"
    url = f"{_CFT_BASE}/{real_ver}/{platform_dir}/chromedriver-{platform_dir}.zip"
    log.info("下载 chromedriver %s: %s", real_ver, url)
    if _download(url, zip_path):
        try:
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(dst_dir)
            if os.path.exists(cached) and _driver_major(cached) == major:
                log.info("驱动就绪: %s", cached)
                return cached
        except Exception as e:
            log.warning("解压驱动失败: %s", e)

    # 4) 全局兜底: PATH / Selenium Manager 自带的驱动
    found = shutil.which("chromedriver")
    if found and _driver_major(found) == major:
        log.info("回退到 PATH 中的驱动: %s", found)
        return found

    log.error(
        "无法获得匹配 Chrome %s 的 chromedriver。请手动下载:\n"
        "  https://googlechromelabs.github.io/chrome-for-testing/\n"
        "然后写入 config.yaml 的 browser.driver_path", major,
    )
    return None


def create_driver(cfg, chrome_binary: str, driver_path: str):
    """创建 WebDriver。调用前 proxy 必须已清理(见 sanitize_proxy_env)。"""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    opts = Options()
    if cfg.headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument(f"--window-size={cfg.window_size}")
    opts.add_argument(f"--user-agent={cfg.user_agent}")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--lang=zh-CN")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_experimental_option("prefs", {"profile.managed_default_content_settings.images": 2})
    for a in cfg.extra_args:
        opts.add_argument(a)
    if chrome_binary:
        opts.binary_location = chrome_binary

    driver = webdriver.Chrome(service=Service(executable_path=driver_path), options=opts)
    driver.set_page_load_timeout(cfg.page_load_timeout)
    # 抹掉 navigator.webdriver, 降低被判定为自动化的概率
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"},
        )
    except Exception as e:
        log.debug("注入反检测脚本失败(不影响抓取): %s", e)
    return driver
