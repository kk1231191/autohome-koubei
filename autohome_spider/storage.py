# -*- coding: utf-8 -*-
"""数据落地: CSV 增量写入 + 去重索引 + 断点状态。

关于"断点续抓"的一个事实, 必须写清楚:

    口碑列表按发表时间倒序, 新口碑会插到第 1 页, 导致 `index_5.html` 的内容
    整体后移。所以"上次抓到第 5 页, 这次从第 6 页开始"这种页级断点是不可靠的 ——
    会漏掉被挤下去的记录。

    本实现因此以 **口碑ID 去重** 作为断点机制: 每次从指定起始页重新扫描,
    命中已抓 ID 就跳过详情页请求。代价是列表页会被重复渲染(每页 1 次请求,
    很便宜), 收益是任何中断/重跑都不会漏数据、不会重复。
    state 文件只用于记录进度统计和失败清单, 不承担正确性责任。
"""
from __future__ import annotations

import csv
import json
import logging
import os
import shutil
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set

log = logging.getLogger(__name__)


class CsvStore:
    def __init__(self, path: str, columns: List[str], encoding: str = "utf-8-sig"):
        self.path = path
        self.columns = list(columns)
        self.encoding = encoding
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._header = self._ensure_schema()

    # -- 表头 ------------------------------------------------------------- #
    def _read_header(self) -> List[str]:
        if not os.path.exists(self.path) or os.path.getsize(self.path) == 0:
            return []
        with open(self.path, "r", encoding=self.encoding, newline="") as f:
            row = next(csv.reader(f), [])
        return [c.strip().lstrip("\ufeff") for c in row]

    def _ensure_schema(self) -> List[str]:
        """对齐磁盘上的表头与本代码的列定义。

        - 文件不存在 / 空文件  -> 用代码里的列, 首次写入时出表头
        - 表头完全一致          -> 直接用
        - 老表头是新列的子集    -> 备份后按新列重写(旧数据补空), 避免列错位
        - 出现代码里没有的列    -> 拒绝写入, 避免把用户手工加的列冲掉
        """
        old = self._read_header()
        if not old:
            return self.columns
        if old == self.columns:
            return old

        unknown = [c for c in old if c not in self.columns]
        if unknown:
            raise RuntimeError(
                f"CSV 表头包含代码未定义的列 {unknown}。\n"
                f"为避免覆盖你的数据, 已中止写入。请先把这些列补充到 "
                f"autohome_spider/config.py 的 CSV_COLUMNS, 或换一个输出文件。"
            )

        backup = f"{self.path}.bak-{datetime.now():%Y%m%d_%H%M%S}"
        shutil.copy2(self.path, backup)
        log.warning("检测到表头扩展, 已备份原文件到 %s", backup)

        with open(self.path, "r", encoding=self.encoding, newline="") as f:
            rows = list(csv.DictReader(f))
        with open(self.path, "w", encoding=self.encoding, newline="") as f:
            w = csv.DictWriter(f, fieldnames=self.columns, restval="", extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
        log.info("表头已迁移: 新增列 %s", [c for c in self.columns if c not in old])
        return self.columns

    # -- 读写 ------------------------------------------------------------- #
    def existing_ids(self) -> Set[str]:
        if not os.path.exists(self.path) or os.path.getsize(self.path) == 0:
            return set()
        ids: Set[str] = set()
        with open(self.path, "r", encoding=self.encoding, newline="") as f:
            for r in csv.DictReader(f):
                v = (r.get("口碑ID") or "").strip()
                if v:
                    ids.add(v)
        log.info("已存在 %d 条记录, 本次将跳过这些口碑ID", len(ids))
        return ids

    def append(self, rows: Iterable[Dict[str, Any]]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        is_new = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
        with open(self.path, "a", encoding=self.encoding, newline="") as f:
            w = csv.DictWriter(f, fieldnames=self._header, restval="", extrasaction="ignore")
            if is_new:
                w.writeheader()
            for r in rows:
                w.writerow({k: _cell(v) for k, v in r.items()})
        return len(rows)


def _cell(v: Any) -> Any:
    """把 None/list 之类统一成 CSV 可接受的标量。"""
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return " | ".join(str(x) for x in v)
    return v


class StateStore:
    """每个车系一个 json, 记录进度与失败清单。不参与正确性判断。"""

    def __init__(self, state_dir: str, series_id: int):
        os.makedirs(state_dir, exist_ok=True)
        self.path = os.path.join(state_dir, f"state_{series_id}.json")
        self.data: Dict[str, Any] = {"series_id": series_id}
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception as e:
                log.warning("读取断点状态失败(将重新开始): %s", e)

    def update(self, **kw) -> None:
        self.data.update(kw)
        self.data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)   # 原子替换, 避免中断时留下半个 json

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def add_failures(self, failures: List[Dict[str, str]]) -> None:
        prev = self.data.get("failures") or []
        self.update(failures=prev + failures)
