# -*- coding: utf-8 -*-
"""抓取编排层。

数据流:
    列表页(index_N.html)  --[chromium 渲染]-->  HTML --parse_list_page--> 10 张卡片
        ├─ 卡片元数据(综合评分/维度评分/标题/昵称/车况/链接)
        └─ 口碑ID ──去重──> 详情页(view_xxx.html) --[纯 HTTP]--> HTML --parse_detail_page--> 全文/交互数
    合并 ──> CSV 追加

两个引擎的分工(2026-10 实测依据):
    - **列表页必须渲染**: 评论列表由前端 XHR 拉取, 页面 HTML 的
      __NEXT_DATA__.list 是空数组。数据源 koubeiipv6.app.autohome.com.cn
      的 robots 为 Disallow: /, 按合规要求不直连, 所以只能执行 JS 拿 DOM。
    - **详情页不必渲染**: requests 直取返回的 HTML 里 kb-item-msg=10 / star-num=7 /
      item-info=5, 与浏览器渲染结果一致 —— 详情页是服务端渲染。
      单篇耗时从约 3s 降到约 0.4s。

    结果是浏览器只承担"每页一次"的列表渲染, 2000 条口碑的详情抓取从约 1.7 小时
    降到约 13 分钟, chromium 进程的负载也下降一个数量级。
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from .config import CSV_COLUMNS, AppConfig
from .fetcher import HttpFetcher
from .parser import parse_detail_page, parse_list_page, parse_series_meta
from .pw_browser import PlaywrightRenderer
from .storage import CsvStore, StateStore

log = logging.getLogger(__name__)

LIST_URL = "https://k.autohome.com.cn/{sid}/"
PAGE_URL = "https://k.autohome.com.cn/{sid}/index_{page}.html"
DETAIL_URL = "https://k.autohome.com.cn/detail/view_{rid}.html"

# 必须指向"数据已填进去"的元素, 不能只等容器:
# Next.js 先渲染骨架再填列表, 只等容器会拿到空列表(已踩过)。
_LIST_READY = 'ul[class*="list_list_con"] li'
_DETAIL_READY = 'div[class*="kb-item"] p[class*="kb-item-msg"]'


class Crawler:
    def __init__(self, cfg: AppConfig, renderer: PlaywrightRenderer,
                 fetcher: HttpFetcher, store: CsvStore):
        self.cfg = cfg
        self.renderer = renderer
        self.fetcher = fetcher
        self.store = store
        self.stats = {"pages": 0, "cards": 0, "skipped": 0, "detail_ok": 0, "detail_fail": 0}
        self.failures: List[Dict[str, str]] = []
        self._series_id: Any = ""

    # -- 基础设施 ---------------------------------------------------------- #
    def _sleep(self) -> None:
        time.sleep(random.uniform(self.cfg.rate_limit.min_delay, self.cfg.rate_limit.max_delay))

    def _load_list(self, url: str, label: str, soft_empty: bool = False) -> Optional[str]:
        """渲染列表页。

        soft_empty=True 用于翻页: 到末页时站点不报错, 只是不渲染卡片, 于是
        "等卡片出现"必然超时。此时若按普通逻辑重试 3 次会白等近 1 分钟,
        所以第二次仍无卡片就直接取当前 HTML, 交给 parse_list_page 判空。
        """
        last_err = ""
        for attempt in range(1, self.cfg.retry.times + 1):
            html = self.renderer.get_html(url, _LIST_READY, label)
            if html is not None:
                return html

            last_err = "列表未渲染出卡片"
            if soft_empty and attempt >= 2:
                html = self.renderer.content()
                if html and self.renderer.count(_LIST_READY) == 0:
                    log.info("%s 连续两次无卡片, 判定到达末页", label)
                return html
            if attempt < self.cfg.retry.times:
                backoff = self.cfg.retry.backoff_base ** attempt
                log.warning("%s 第 %d 次失败(%s), %.1fs 后重试", label, attempt, last_err, backoff)
                time.sleep(backoff)
        log.error("%s 放弃: %s", label, last_err)
        return None

    # -- 主流程 ------------------------------------------------------------ #
    def run(self, series_id: int) -> Dict[str, int]:
        self._series_id = int(series_id)
        store_state = StateStore(self.cfg.output.state_dir, series_id)
        done_ids = self.store.existing_ids()

        # 先探一次列表页, 拿总口碑数, 用于日志与后续校验
        first = self._load_list(LIST_URL.format(sid=series_id), f"[{series_id}] 列表第1页")
        if first is None:
            log.error("车系 %s 列表页无法访问, 跳过", series_id)
            return self.stats
        meta = parse_series_meta(first)
        log.info("车系 %s: 总口碑 %s 条 / 在售 %s 篇",
                 series_id, meta["总口碑数量"], meta["在售口碑数量"])
        if not meta["总口碑数量"] and not meta["在售口碑数量"]:
            # 页面能打开但顶部统计为 0, 通常是该车系已下架或无任何口碑。
            # 明确说清楚, 不然日志只会显示"解析到 0 条", 容易误判成选择器失效。
            log.warning("车系 %s 无口碑数据(总0/在售0), 跳过。请确认车型名是否正确", series_id)
            return self.stats

        start = max(1, self.cfg.crawl.start_page)
        end = start + self.cfg.crawl.max_pages - 1
        buffer: List[Dict[str, Any]] = []

        for page in range(start, end + 1):
            if page == 1:
                html, label = first, f"[{series_id}] 第1页"
            else:
                label = f"[{series_id}] 第{page}页"
                html = self._load_list(PAGE_URL.format(sid=series_id, page=page),
                                       label, soft_empty=True)
            if html is None:
                break

            cards = parse_list_page(html)
            if not cards:
                log.info("%s 无卡片, 视为已到末页", label)
                break
            self.stats["pages"] += 1
            self.stats["cards"] += len(cards)
            log.info("%s 解析到 %d 条", label, len(cards))

            for c in cards:
                rid = c["口碑ID"]
                if self.cfg.crawl.skip_existing and rid in done_ids:
                    self.stats["skipped"] += 1
                    continue

                row = self._build_row(c, meta)
                if self.cfg.crawl.full_text:
                    detail = self._fetch_detail(rid)
                    if detail:
                        row = self._merge(c, detail, meta)
                        self.stats["detail_ok"] += 1
                    else:
                        # 详情页失败不能丢这条 —— 用列表页摘要兜底, 并记入失败清单
                        self.stats["detail_fail"] += 1
                        row["最满意"] = c.get("_满意摘要", "")
                        row["最不满意"] = c.get("_不满摘要", "")
                        self.failures.append({"口碑ID": rid, "链接": c.get("评论链接", "")})

                done_ids.add(rid)
                buffer.append(row)

                if len(buffer) >= 10:
                    self.store.append(buffer)
                    log.info("已落盘 %d 条(累计)", len(buffer))
                    buffer.clear()
                self._sleep()

            if buffer:
                self.store.append(buffer)
                buffer.clear()

            store_state.update(last_page=page, stats=self.stats)
            if page < end:
                self._sleep()

        if buffer:
            self.store.append(buffer)
        if self.failures:
            store_state.add_failures(self.failures)
        store_state.update(stats=self.stats, finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        return self.stats

    # -- 单条 -------------------------------------------------------------- #
    def _fetch_detail(self, rid: str) -> Optional[Dict[str, Any]]:
        url = DETAIL_URL.format(rid=rid)
        label = f"详情 {rid}"
        if self.cfg.crawl.detail_via == "browser":
            html = self.renderer.get_html(url, _DETAIL_READY, label)
        else:
            html = self.fetcher.get_html(url, label)      # HttpFetcher 内部已含重试
        if html is None:
            return None
        data = parse_detail_page(html)
        if not data.get("最满意") and not data.get("最不满意") and not data.get("车型版本"):
            log.warning("详情 %s 关键字段全空, 判为解析失败", rid)
            return None
        return data

    def _build_row(self, card: Dict[str, Any], meta: Dict[str, int]) -> Dict[str, Any]:
        row = {col: "" for col in CSV_COLUMNS}
        for k, v in card.items():
            if not k.startswith("_") and k in row:
                row[k] = v
        row["车系ID"] = self._series_id
        row["购车目的"] = ""          # 站点已移除该字段, 保留空列
        row["总口碑数量"] = meta["总口碑数量"]
        row["在售口碑数量"] = meta["在售口碑数量"]
        row["爬取时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return row

    def _merge(self, card: Dict[str, Any], detail: Dict[str, Any],
               meta: Dict[str, int]) -> Dict[str, Any]:
        """详情页为底, 列表页补空。

        唯一例外是 车型版本: 详情页会带车系前缀("RAV4荣放 2026款..."),
        列表页是纯版本名("2026款..."), 后者更适合做维度字段。
        """
        row = self._build_row(card, meta)
        for k, v in detail.items():
            if k not in row:
                continue
            if v not in ("", None):
                row[k] = v
        if card.get("车型版本"):
            row["车型版本"] = card["车型版本"]
        return row
