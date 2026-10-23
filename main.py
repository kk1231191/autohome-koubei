# -*- coding: utf-8 -*-
"""命令行入口。

用法:
    python main.py                          # 用 config.yaml 的配置
    python main.py --series 770 --pages 2   # 覆盖车系与页数
    python main.py --no-detail              # 只抓列表页(快 10 倍, 无全文)
    python main.py --show-browser           # 关掉 headless, 便于人工观察/过验证码
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import List

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from autohome_spider import browser as B                 # noqa: E402  (仅用 sanitize_proxy_env)
from autohome_spider import search as search_mod          # noqa: E402
from autohome_spider.config import CSV_COLUMNS, load_config   # noqa: E402
from autohome_spider.crawler import Crawler              # noqa: E402
from autohome_spider.fetcher import HttpFetcher          # noqa: E402
from autohome_spider.pw_browser import PlaywrightRenderer  # noqa: E402
from autohome_spider.search import search_series          # noqa: E402
from autohome_spider.storage import CsvStore             # noqa: E402


def setup_logging(cfg) -> None:
    os.makedirs(cfg.logging.dir, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-7s %(message)s"
    logging.basicConfig(
        level=getattr(logging, cfg.logging.level.upper(), logging.INFO),
        format=fmt,
        handlers=[
            logging.FileHandler(os.path.join(cfg.logging.dir, cfg.logging.file_name), encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    # selenium/urllib3 的 DEBUG 噪音太大
    for noisy in ("selenium", "urllib3", "WDM"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="汽车之家口碑抓取器")
    p.add_argument("-c", "--config", default=os.path.join(BASE_DIR, "config.yaml"))
    p.add_argument("-s", "--series", nargs="*",
                   help="车型名或车系ID, 可多个。如 RAV4 / 零跑C10 / 770")
    p.add_argument("--search", metavar="关键词",
                   help="只查询车型名对应的车系ID, 不抓取")
    p.add_argument("-p", "--pages", type=int, help="每车系抓取页数(每页10条)")
    p.add_argument("--start-page", type=int, help="起始页(从1开始)")
    p.add_argument("--no-detail", action="store_true", help="不进详情页, 只用列表页摘要")
    p.add_argument("--show-browser", action="store_true", help="显示浏览器窗口")
    p.add_argument("--csv", help="输出 CSV 路径")
    return p.parse_args(argv)


def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = load_config(args.config, BASE_DIR)

    # 只查 ID 不抓取, 方便用户先确认车型名对应哪个车系
    if args.search:
        cands = search_series(args.search)
        if not cands:
            print(f"未找到与「{args.search}」匹配的车系")
            return 3
        print(f"{'车系ID':>8}  {'车系名称':<22} 价格区间")
        print("-" * 56)
        for c in cands:
            print(f"{c['id']:>8}  {c['name']:<22} {c['price'] or '-'}")
        return 0

    if args.pages is not None:
        cfg.crawl.max_pages = args.pages
    if args.start_page:
        cfg.crawl.start_page = args.start_page
    if args.no_detail:
        cfg.crawl.full_text = False
    if args.show_browser:
        cfg.browser.headless = False
    if args.csv:
        cfg.output.csv_name = args.csv

    setup_logging(cfg)
    log = logging.getLogger("main")

    # 把用户输入(车型名或ID)统一解析成车系ID。放在开浏览器之前做:
    # 无效ID在这里就能被拦下, 不必等 40 秒列表页超时。
    tokens = ([str(x) for x in args.series] if args.series
              else [str(x) for x in cfg.crawl.series_ids])
    series_ids, msgs = search_mod.resolve_input(tokens)
    log.info("=" * 62)
    for m in msgs:
        log.info("车系解析: %s", m)
    if not series_ids:
        log.error("没有可用的车系, 请检查车型名或车系ID")
        return 3
    log.info("汽车之家口碑抓取 | 车系 %s | 页数 %s | 详情页 %s",
             series_ids, cfg.crawl.max_pages,
             "开启" if cfg.crawl.full_text else "关闭")

    # 1) 清掉 proxy 环境变量。chromium 已用 proxy=direct:// 强制直连,
    #    这里再清一次是为了让 requests(HttpFetcher) 也不受系统代理影响。
    if cfg.browser.sanitize_proxy_env:
        removed = B.sanitize_proxy_env()
        if removed:
            log.info("已临时清除代理环境变量(仅本进程): %s", removed)

    store = CsvStore(os.path.join(cfg.output.dir, cfg.output.csv_name), CSV_COLUMNS,
                     encoding=cfg.output.encoding)
    log.info("输出文件: %s", store.path)

    renderer = None
    try:
        # 2) 启动项目内置的 chromium(只在列表页用)
        renderer = PlaywrightRenderer(
            browsers_path=cfg.browser.browsers_path,
            headless=cfg.browser.headless,
            user_agent=cfg.browser.user_agent,
            timeout_ms=cfg.browser.element_timeout * 1000,
            executable_path=cfg.browser.executable_path,
        )
        renderer.start()

        fetcher = HttpFetcher(
            user_agent=cfg.browser.user_agent,
            timeout=cfg.browser.element_timeout,
            retry_times=cfg.retry.times,
            backoff_base=cfg.retry.backoff_base,
            min_delay=cfg.rate_limit.min_delay,
            max_delay=cfg.rate_limit.max_delay,
        )

        crawler = Crawler(cfg, renderer, fetcher, store)
        total = {"pages": 0, "cards": 0, "skipped": 0, "detail_ok": 0, "detail_fail": 0}
        for sid in series_ids:
            st = crawler.run(int(sid))
            for k in total:
                total[k] += st.get(k, 0)

        log.info("-" * 62)
        log.info("完成: 页=%d 卡片=%d 跳过(已存在)=%d 详情成功=%d 详情失败=%d",
                 total["pages"], total["cards"], total["skipped"],
                 total["detail_ok"], total["detail_fail"])
        log.info("HTTP 详情: 成功=%d 失败=%d 重试=%d",
                 fetcher.stats["ok"], fetcher.stats["fail"], fetcher.stats["retry"])
        if total["detail_fail"]:
            log.warning("有 %d 条详情页失败, 已用列表页摘要兜底并记入 state 的 failures",
                        total["detail_fail"])
        log.info("输出: %s", store.path)
        return 0
    except KeyboardInterrupt:
        log.warning("用户中断。已落盘的记录不会丢失, 重跑会自动跳过已抓口碑ID")
        return 130
    except Exception as e:
        log.exception("执行失败: %s", e)
        return 1
    finally:
        if renderer is not None:
            renderer.close()


if __name__ == "__main__":
    raise SystemExit(main())
