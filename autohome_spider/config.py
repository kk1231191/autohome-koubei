# -*- coding: utf-8 -*-
"""配置加载层。

设计取舍:
- 用 dataclass 而不是裸 dict, 这样拼错配置项会立刻 AttributeError, 而不是在
  半小时后爬到一半才发现某个开关没生效。
- 所有默认值都写死在代码里, config.yaml 只覆盖需要的项, 少一个键不会崩。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List

import yaml

# 表头顺序即 CSV 列顺序。新增字段请追加到末尾, 避免破坏已有文件的列语义。
STANDARD_DIMENSIONS = ["空间", "驾驶感受", "续航", "外观", "内饰", "性价比", "智能化", "油耗", "配置"]

# 车况信息里"常见且需要独立成列"的键。不在此列的键会进 `车况信息_其他`(JSON)。
STANDARD_CONDITION_KEYS = [
    "行驶里程", "裸车购买价", "购买时间", "购买地点",
    "百公里油耗", "百公里电耗",
    "夏季电耗", "春秋电耗", "冬季电耗",
    "夏季续航", "春秋续航", "冬季续航",
]

CSV_COLUMNS: List[str] = (
    ["口碑ID", "车系ID", "车系名称", "车型版本", "口碑标题", "用户昵称",
     "综合口碑评分", "发表时间", "购买时间", "购买地点", "购车经销商",
     "行驶里程", "裸车购买价", "百公里油耗", "百公里电耗",
     "夏季电耗", "春秋电耗", "冬季电耗", "夏季续航", "春秋续航", "冬季续航"]
    + [x for d in STANDARD_DIMENSIONS for x in (f"{d}评分", f"{d}评论")]
    + ["最满意", "最不满意",
       "观看数", "点赞数", "评论数",
       "购车目的",            # 站点已移除该字段, 保留空列以兼容历史表结构
       "车况信息_其他",
       "总口碑数量", "在售口碑数量",
       "评论链接", "爬取时间"]
)


@dataclass
class OutputConfig:
    dir: str = "output"
    csv_name: str = "autohome_koubei.csv"
    encoding: str = "utf-8-sig"      # Excel 需要 BOM 才能正确识别 UTF-8
    state_dir: str = "output/state"


@dataclass
class BrowserConfig:
    headless: bool = True
    window_size: str = "1440,2400"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
    )
    # 项目内置浏览器目录（Playwright 的 chromium）。留空则用 <项目根>/browsers。
    # 用内置浏览器而不是系统 Chrome: 换机器不用装 Chrome, 也不会因系统 Chrome
    # 自动升级导致驱动版本错配。
    browsers_path: str = ""
    # 显式指定浏览器可执行文件。留空则自动: 项目内 > 系统 Chrome。
    executable_path: str = ""
    # 以下三项仅在需要兼容旧 Selenium 路径时使用(当前 crawler 已不依赖它们)
    driver_path: str = ""
    chrome_binary: str = ""
    driver_cache_dir: str = ""
    sanitize_proxy_env: bool = True
    page_load_timeout: int = 45
    element_timeout: int = 20
    extra_args: List[str] = field(default_factory=list)


@dataclass
class RateLimitConfig:
    min_delay: float = 1.0
    max_delay: float = 3.0


@dataclass
class RetryConfig:
    times: int = 3
    backoff_base: float = 2.0        # 第 n 次重试等待 backoff_base ** n 秒


@dataclass
class CrawlConfig:
    series_ids: List[int] = field(default_factory=lambda: [770])
    max_pages: int = 2               # 每个车系最多翻多少页(第 1 页起算)
    start_page: int = 1              # 断点续抓时可手工指定起始页
    skip_existing: bool = True       # 命中已有 口碑ID 则跳过详情页请求
    full_text: bool = True           # True=进详情页取全文; False=仅用列表页摘要
    # 详情页取法。http=requests 直取(详情页是 SSR, 比开浏览器快约 7 倍); browser=走渲染器
    detail_via: str = "http"


@dataclass
class LoggingConfig:
    level: str = "INFO"
    dir: str = "logs"
    file_name: str = "autohome_spider.log"


@dataclass
class AppConfig:
    output: OutputConfig = field(default_factory=OutputConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    crawl: CrawlConfig = field(default_factory=CrawlConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    def resolve_paths(self, base_dir: str) -> "AppConfig":
        """把配置里的相对路径统一解析成基于项目根目录的绝对路径。

        为什么不在加载时解析: 用户可能从任意 cwd 启动脚本, 相对路径的语义
        必须锚定项目根, 否则 output/ 会散落在不同地方。
        """
        self.output.dir = _abs(base_dir, self.output.dir)
        self.output.state_dir = _abs(base_dir, self.output.state_dir)
        self.logging.dir = _abs(base_dir, self.logging.dir)
        if self.browser.driver_cache_dir:
            self.browser.driver_cache_dir = _abs(base_dir, self.browser.driver_cache_dir)
        else:
            self.browser.driver_cache_dir = os.path.join(base_dir, "drivers")
        if self.browser.browsers_path:
            self.browser.browsers_path = _abs(base_dir, self.browser.browsers_path)
        else:
            self.browser.browsers_path = os.path.join(base_dir, "browsers")
        return self


def _abs(base: str, p: str) -> str:
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))


def _fill(obj: Any, data: Dict[str, Any]) -> Any:
    """只覆盖 dataclass 已声明的键, 忽略未知键(带告警)。"""
    known = {f for f in obj.__dataclass_fields__}
    for k, v in (data or {}).items():
        if k in known:
            setattr(obj, k, v)
    return obj


def load_config(path: str, base_dir: str) -> AppConfig:
    cfg = AppConfig()
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        for section in ("output", "browser", "rate_limit", "retry", "crawl", "logging"):
            if isinstance(raw.get(section), dict):
                _fill(getattr(cfg, section), raw[section])
    return cfg.resolve_paths(base_dir)
