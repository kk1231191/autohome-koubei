# -*- coding: utf-8 -*-
"""车型名 -> 车系ID 解析。

为什么需要这一层:
    口碑页的 URL 就是 `k.autohome.com.cn/{车系ID}/`, 没有名字到 ID 的规律可循。
    用户凭印象填 ID 极易填错 —— 实测 ID=276 的页面返回 HTTP 200,
    title 是"【怎么样】_怎么样_..."(车系名位置为空), 整页无任何口碑卡片,
    最终表现成"元素等待超时", 报错完全指不出真正原因。
    所以让用户输入车型名, 由程序解析出 ID。

数据来源:
    `https://sou.api.autohome.com.cn/sug/_suggest?plat=pc&q=<关键词>`
    这是官网搜索框的联想接口, 返回里 `wordtype==3` 且 `wordid>0` 的条目即车系。
    合规性: `sou.api.autohome.com.cn` 无 robots.txt(404);
    `sou.autohome.com.cn` 的 robots 只限制 /zonghe_op、/luntan 等路径, 不含 /sug/。

请求走直连(trust_env=False):
    本机有系统级 HTTP_PROXY, 走代理访问 autohome 既慢又不稳; 直连已验证可用。
    这里不依赖进程环境变量, 避免"网页能跑、命令行跑不了"这类环境差异。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Dict, List, Optional, Tuple

import requests

log = logging.getLogger(__name__)

_SUGGEST = "https://sou.api.autohome.com.cn/sug/_suggest"
_SERIES_PAGE = "https://k.autohome.com.cn/{sid}/"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")
# 车系名不能含 '<' 或 '】': 无效ID 的 title 是「【怎么样】_怎么样_...」,
# 若用 `.+?` 会跨行匹配到 title 之后的 HTML, 把整段页面当成车系名(已踩过)。
_TITLE_RE = re.compile(r"<title>【([^<】]+)怎么样】")
_WORDTYPE_SERIES = 3

_session = requests.Session()
_session.trust_env = False          # 忽略系统代理
_session.headers.update({"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9",
                         "Referer": "https://www.autohome.com.cn/"})

_cache: Dict[str, Tuple[float, List[Dict]]] = {}
_CACHE_TTL = 300


def _fmt_price(lo, hi) -> str:
    try:
        lo, hi = int(lo or 0), int(hi or 0)
    except (TypeError, ValueError):
        return ""
    if not lo and not hi:
        return ""
    f = lambda v: f"{v / 10000:.2f}万"
    if lo and hi and lo != hi:
        return f"{f(lo)}-{f(hi)}"
    return f(lo or hi)


def search_series(keyword: str, limit: int = 8) -> List[Dict]:
    """按车型名搜索车系。返回 [{id, name, price}]，按官网相关度排序。

    只保留 wordtype==3 且 wordid>0 的条目 —— 其余是"未被识别的原始词"
    (wordtype=0, id=0) 或资讯文章(wordtype=300, id=0), 无法用来抓口碑。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return []

    now = time.time()
    hit = _cache.get(keyword)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]

    try:
        r = _session.get(_SUGGEST, params={"plat": "pc", "q": keyword}, timeout=12)
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        log.warning("搜索车型名 '%s' 失败: %s", keyword, e)
        return []

    out: List[Dict] = []
    seen = set()
    for item in (payload.get("result", {}) or {}).get("data") or []:
        try:
            if int(item.get("wordtype") or 0) != _WORDTYPE_SERIES:
                continue
            sid = int(item.get("wordid") or 0)
            name = (item.get("key") or "").strip()
            if sid <= 0 or not name or sid in seen:
                continue
            seen.add(sid)
            out.append({"id": sid, "name": name,
                        "price": _fmt_price(item.get("minprice"), item.get("maxprice"))})
        except Exception:
            continue
        if len(out) >= limit:
            break

    _cache[keyword] = (now, out)
    return out


def validate_series(series_id: int, timeout: int = 12) -> Tuple[bool, str]:
    """校验车系ID是否真实存在，顺带取回官方车系名。

    存在的车系: title 形如「【RAV4荣放怎么样】丰田_RAV4荣放怎么样_...」
    不存在的ID: title 形如「【怎么样】_怎么样_...」, 取不到车系名。
    """
    try:
        r = _session.get(_SERIES_PAGE.format(sid=series_id), timeout=timeout)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        r.encoding = "utf-8"
        m = _TITLE_RE.search(r.text)
        name = (m.group(1) or "").strip() if m else ""
        if name:
            return True, name
        return False, "该ID不是有效车系（页面无车系名，可能是已下架车系或错误ID）"
    except Exception as e:
        return False, f"校验请求失败: {e}"


def resolve(series_id: int) -> Optional[Dict]:
    """按 ID 反查车系名（用于结果表格/日志展示）。失败返回 None。"""
    ok, name = validate_series(series_id)
    return {"id": series_id, "name": name} if ok else None


def resolve_input(tokens: List[str]) -> Tuple[List[int], List[str]]:
    """把用户输入(可能是 ID, 也可能是车型名)统一解析成车系ID列表。

    返回 (ids, messages): messages 是给用户看的解析说明, 例如
        "RAV4 -> RAV4荣放 (ID 770)"
        "宝马3系 -> 命中 3 个车系, 取第一个 宝马3系 (ID 66); 其余: 宝马3系(进口)(317), 宝马3系GT(2963)"
        "XYZ -> 未找到匹配车系"
    """
    ids: List[int] = []
    msgs: List[str] = []
    for t in tokens:
        t = (t or "").strip()
        if not t:
            continue
        if t.isdigit():
            sid = int(t)
            ok, name = validate_series(sid)
            if ok:
                ids.append(sid)
                msgs.append(f"{sid} -> {name}")
            else:
                msgs.append(f"{sid} 无效: {name}")
            continue

        cands = search_series(t)
        if not cands:
            msgs.append(f"{t} -> 未找到匹配车系")
            continue
        best = cands[0]
        ids.append(best["id"])
        extra = ""
        if len(cands) > 1:
            others = ", ".join(f"{c['name']}({c['id']})" for c in cands[1:4])
            extra = f"; 其余候选: {others}"
        msgs.append(f"{t} -> {best['name']} (ID {best['id']}){extra}")
    return ids, msgs
