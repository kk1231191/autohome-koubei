# -*- coding: utf-8 -*-
"""列表页渲染层：Playwright + 浏览器。

为什么不继续用 Selenium:
    Selenium 需要手工维护 chromedriver 与 Chrome 的大版本对齐, 差一个版本就报
    SessionNotCreatedException(本项目踩过: PATH 里 148 / 浏览器 155)。
    Playwright 通过 CDP 直接驱动浏览器, **不需要 chromedriver**,
    也就没有"驱动与浏览器版本必须匹配"这个问题。

浏览器从哪来(按优先级):
    1. config 的 browser.executable_path (显式指定)
    2. <项目>/browsers/ 下的 Chrome for Testing (由 setup_browser.py 下载,
       随项目分发 —— 这是"换机即用"的关键)
    3. 系统安装的 Chrome (Windows/macOS 基本都有)
    4. Playwright 自带的 chromium (需要 cdn.playwright.dev, 国内常被墙)

只有列表页走这里:
    评论列表由前端 XHR 拉取, 页面 HTML 里 __NEXT_DATA__.list 是空数组,
    必须执行 JS 才能拿到 10 张卡片。详情页是 SSR, 走 fetcher.HttpFetcher 即可。
"""
from __future__ import annotations

import glob
import logging
import os
import random
import time
from typing import Optional, Tuple

log = logging.getLogger(__name__)

_SYSTEM_CHROME = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]

# Chrome 的启动参数。--no-proxy-server 是必须的:
# 本机设了 HTTP_PROXY, 若不显式禁用, chromium 会把请求送进代理并报
# ERR_PROXY_CONNECTION_FAILED(已踩过 —— new_context(proxy={"server":"direct://"})
# 并不能覆盖这个行为)。
_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--no-proxy-server",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=Translate,BackForwardCache",
    # 图片/字体对抓文本无用, 关掉能明显加快列表页渲染
    "--blink-settings=imagesEnabled=false",
]


def find_project_browser(browsers_path: str) -> Optional[str]:
    """在项目目录里找 Chrome for Testing 的可执行文件。

    注意 Chrome for Testing 各平台解压后的目录名并不统一:
        win64   -> chrome-win64/chrome.exe
        linux64 -> chrome-linux64/chrome     (注意是 linux64, 不是 linux)
        mac     -> chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/...
    setup_browser.py 解压时会去掉一层 chrome-<plat>/ 再展开,
    所以两种层级都要能认出来。
    """
    if not browsers_path or not os.path.isdir(browsers_path):
        return None
    patterns = [
        # 解压后再展开一层的结构: browsers/chrome-win64/chrome.exe
        os.path.join(browsers_path, "chrome-win64", "chrome.exe"),
        os.path.join(browsers_path, "chrome-win", "chrome.exe"),
        os.path.join(browsers_path, "chrome-linux64", "chrome"),
        os.path.join(browsers_path, "chrome-linux", "chrome"),
        # 原始压缩包结构: browsers/chrome-<plat>/chrome-<plat>/...
        os.path.join(browsers_path, "chrome-*", "chrome.exe"),
        os.path.join(browsers_path, "chrome-*", "chrome-win64", "chrome.exe"),
        os.path.join(browsers_path, "chrome-*", "chrome-linux64", "chrome"),
        os.path.join(browsers_path, "chrome-*", "chrome-linux", "chrome"),
        os.path.join(browsers_path, "chromium-*", "chrome-win", "chrome.exe"),
        # Playwright 自带 chromium 的目录结构
        os.path.join(browsers_path, "chromium-*", "chrome-linux", "chrome"),
        os.path.join(browsers_path, "chrome-mac", "Chromium.app", "Contents", "MacOS", "Chromium"),
        os.path.join(browsers_path, "chrome-*", "Google Chrome for Testing.app",
                     "Contents", "MacOS", "Google Chrome for Testing"),
    ]
    hits = []
    for pat in patterns:
        hits.extend(glob.glob(pat))
    hits = [h for h in hits if os.path.isfile(h)]
    if not hits:
        return None
    # 版本目录形如 chrome-148.0.7778.96, 逆序取最新
    return sorted(hits, reverse=True)[0]


def find_system_chrome() -> Optional[str]:
    for p in _SYSTEM_CHROME:
        if p and os.path.isfile(p):
            return p
    return None


def resolve_browser(browsers_path: str, explicit: str = "") -> Tuple[Optional[str], str]:
    """返回 (可执行文件路径, 来源说明)。路径为 None 表示交给 Playwright 自带的 chromium。"""
    if explicit and os.path.isfile(explicit):
        return explicit, "配置指定"
    p = find_project_browser(browsers_path)
    if p:
        return p, "项目内置"
    p = find_system_chrome()
    if p:
        return p, "系统安装"
    return None, "Playwright 内置(未找到本地浏览器)"


class PlaywrightRenderer:
    def __init__(self, browsers_path: str, headless: bool = True,
                 user_agent: str = "", timeout_ms: int = 25000,
                 min_delay: float = 1.0, max_delay: float = 3.0,
                 executable_path: str = ""):
        self.browsers_path = browsers_path
        self.headless = headless
        self.user_agent = user_agent
        self.timeout_ms = timeout_ms
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.executable_path = executable_path
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None
        self.browser_source = ""

    # -- 生命周期 --------------------------------------------------------- #
    def start(self) -> None:
        # Playwright 自带 chromium 的安装目录; 若用 executable_path 则不影响
        if self.browsers_path:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = self.browsers_path

        from playwright.sync_api import sync_playwright
        exe, source = resolve_browser(self.browsers_path, self.executable_path)
        self.browser_source = source

        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                headless=self.headless,
                executable_path=exe or None,
                args=list(_ARGS),
            )
        except Exception as e:
            msg = str(e)
            if "Executable doesn't exist" in msg or "playwright install" in msg:
                raise RuntimeError(
                    "找不到可用浏览器。请任选其一:\n"
                    "  1) 双击运行「准备浏览器.bat」把 Chrome 下载到项目内(推荐, 换机即用)\n"
                    "  2) 安装 Google Chrome\n"
                    "  3) 在 config.yaml 的 browser.executable_path 写明浏览器路径\n"
                    f"原始错误: {msg[:300]}"
                ) from e
            raise

        self._ctx = self._browser.new_context(
            user_agent=self.user_agent or None,
            locale="zh-CN",
            viewport={"width": 1440, "height": 2400},
            ignore_https_errors=True,
        )
        # 抹掉 navigator.webdriver, 降低被判定为自动化的概率
        self._ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        self._page = self._ctx.new_page()
        self._page.set_default_timeout(self.timeout_ms)
        log.info("浏览器就绪: %s (%s)", self._browser.version, source)

    def close(self) -> None:
        for obj, name in ((self._ctx, "ctx"), (self._browser, "browser")):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._ctx = self._page = None

    # -- 抓取 ------------------------------------------------------------- #
    def get_html(self, url: str, ready_selector: str, label: str = "") -> Optional[str]:
        """导航并等待目标元素出现，返回渲染后的 HTML；失败返回 None。

        ready_selector 必须指向"数据已填进去"的元素, 不能只等容器:
        Next.js 先渲染骨架再填列表, 只等容器会拿到空列表(已踩过)。
        """
        assert self._page is not None, "renderer 未 start()"
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            self._page.wait_for_selector(ready_selector, state="attached",
                                         timeout=self.timeout_ms)
            return self._page.content()
        except Exception as e:
            msg = str(e).split("\n")[0][:180]
            log.warning("%s 渲染失败: %s", label or url, msg)
            return None

    def count(self, selector: str) -> int:
        try:
            assert self._page is not None
            return self._page.locator(selector).count()
        except Exception:
            return 0

    def content(self) -> str:
        """取当前页面 HTML。列表翻到末页时, 等待卡片必然超时,
        但页面本身是有效的空列表页, 用它来判空。"""
        try:
            assert self._page is not None
            return self._page.content()
        except Exception:
            return ""

    def sleep(self) -> None:
        time.sleep(random.uniform(self.min_delay, self.max_delay))
