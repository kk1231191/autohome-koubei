# -*- coding: utf-8 -*-
"""详情页抓取层：纯 HTTP。

为什么详情页不需要浏览器（2026-10 实测）:
    requests 直接 GET `k.autohome.com.cn/detail/view_xxx.html`, 返回的 HTML 里
    kb-item=25 / kb-item-msg=10 / star-num=7 / item-info=5 / timeline=3,
    与 Selenium 渲染后的 DOM 完全一致 —— 详情页是服务端渲染的。

    所以原来"每篇口碑开一次浏览器"纯属浪费: 单篇耗时从约 3s 降到约 0.4s,
    2000 条口碑的详情抓取从约 1.7 小时降到约 13 分钟。

只有列表页必须渲染:
    评论列表由前端 XHR 拉取(koubeiipv6.app.autohome.com.cn/pc/series/list),
    该子域 robots 为 Disallow: /, 按合规要求不直连; 页面 HTML 里的
    __NEXT_DATA__ 中 list 是空数组。所以列表页仍需浏览器。
"""
from __future__ import annotations

import logging
import random
import time
from typing import Dict, Optional

import requests

log = logging.getLogger(__name__)


class HttpFetcher:
    """带重试与限速的 HTTP 抓取器。

    trust_env=False: 忽略系统代理直连。本机存在 HTTP_PROXY, 走代理访问汽车之家
    既慢又不稳; 直连已验证可用。这样也不会因为"有没有代理"产生环境差异。
    """

    def __init__(self, user_agent: str, timeout: int = 20,
                 retry_times: int = 3, backoff_base: float = 2.0,
                 min_delay: float = 1.0, max_delay: float = 3.0):
        self.timeout = timeout
        self.retry_times = max(1, retry_times)
        self.backoff_base = backoff_base
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://k.autohome.com.cn/",
        })
        self.stats: Dict[str, int] = {"ok": 0, "fail": 0, "retry": 0}

    def _sleep(self) -> None:
        time.sleep(random.uniform(self.min_delay, self.max_delay))

    def get_html(self, url: str, label: str = "") -> Optional[str]:
        """返回 HTML；重试耗尽后返回 None（调用方负责兜底，不抛异常）。"""
        last = ""
        for attempt in range(1, self.retry_times + 1):
            try:
                r = self.session.get(url, timeout=self.timeout)
                if r.status_code == 200:
                    # 汽车之家是 UTF-8, 但个别页面会缺 charset 头, 显式指定避免乱码
                    r.encoding = "utf-8"
                    self.stats["ok"] += 1
                    return r.text
                last = f"HTTP {r.status_code}"
            except Exception as e:
                last = str(e).split("\n")[0][:140]

            self.stats["retry"] += 1
            if attempt < self.retry_times:
                backoff = self.backoff_base ** attempt
                log.warning("%s 第 %d 次失败(%s), %.1fs 后重试", label or url, attempt, last, backoff)
                time.sleep(backoff)

        self.stats["fail"] += 1
        log.error("%s 放弃: %s", label or url, last)
        return None
