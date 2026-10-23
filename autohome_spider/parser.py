# -*- coding: utf-8 -*-
"""HTML 解析层: 输入"渲染后的 HTML", 输出结构化 dict。

三条设计原则:

1. 纯函数, 不 import selenium。这样可以直接拿离线 HTML 做解析单测,
   验证一次解析正确性不需要开浏览器(单篇详情页解析耗时 <5ms, 开浏览器要约 6s)。
   这是本项目"可验证"的基础。

2. 选择器一律用 `contains(@class, 前缀)`。汽车之家前端用 CSS Modules,
   类名形如 `list_kb_nums__iH75q`, 后半段是构建哈希, 每次发版都变;
   只有前半段语义名(如 `list_kb_nums`)稳定。原脚本写死完整类名
   (`div.list_buy_target__rsfaE`)是它最容易失效的地方 —— 实测该类名已消失。

3. 每个字段独立 try/except。单个字段解析失败只返回空值, 不拖垮整条记录。
   口碑站点结构经常局部调整, 全或无的解析方式会让一次小改版颗粒无收。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from lxml import html as LH

from .config import STANDARD_CONDITION_KEYS, STANDARD_DIMENSIONS

_SPACE_RE = re.compile(r"[\s\u3000\xa0]+")
_DATE_RE = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
_REVIEW_ID_RE = re.compile(r"view_([0-9a-zA-Z]+)\.html")


def _norm(s: Optional[str]) -> str:
    """折叠所有空白字符(含全角空格/不换行空格)。"""
    return _SPACE_RE.sub(" ", s or "").strip()


def _text(el) -> str:
    return _norm(el.text_content()) if el is not None else ""


def _first(root, xpath: str):
    r = root.xpath(xpath)
    return r[0] if r else None


def _num(s: str) -> Optional[float]:
    m = re.search(r"(\d+(?:\.\d+)?)", s or "")
    return float(m.group(1)) if m else None


def _date(s: str) -> str:
    """"2026-10-22 发表口碑" -> "2026-10-22"。统一格式, 便于后续排序。"""
    m = _DATE_RE.search(s or "")
    if not m:
        return ""
    y, mo, d = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def _review_id(url: str) -> str:
    m = _REVIEW_ID_RE.search(url or "")
    return m.group(1) if m else ""


# --------------------------------------------------------------------------- #
# 列表页
# --------------------------------------------------------------------------- #
def parse_series_meta(html: str) -> Dict[str, int]:
    """从列表页顶部取"总口碑数 / 在售口碑数"。

    实测文本形如 "18168条口碑在售236篇"。
    """
    out = {"总口碑数量": 0, "在售口碑数量": 0}
    try:
        doc = LH.fromstring(html)
    except Exception:
        return out
    el = _first(doc, '//*[contains(@class,"list_kb_nums")]')
    text = _text(el)
    if not text:
        return out
    m = re.search(r"(\d+)\s*条口碑", text)
    if m:
        out["总口碑数量"] = int(m.group(1))
    m = re.search(r"在售\s*(\d+)\s*篇", text)
    if m:
        out["在售口碑数量"] = int(m.group(1))
    return out


def _parse_card(li) -> Optional[Dict[str, Any]]:
    """解析一张口碑卡片。卡片里已经有 7 项维度评分和综合评分, 不必再进详情页。"""
    card: Dict[str, Any] = {}

    link_el = _first(li, './/h1[contains(@class,"list_kb_title")]//a')
    if link_el is None:
        # 兜底: 直接找"查看完整口碑"所在卡片内的详情链接
        link_el = _first(li, './/a[contains(@href,"/detail/view_")]')
    href = (link_el.get("href") if link_el is not None else "") or ""
    card["评论链接"] = href.split("#")[0]
    card["口碑ID"] = _review_id(href)
    card["口碑标题"] = _text(link_el)

    card["用户昵称"] = ""
    nick = _first(li, './/*[contains(@class,"list_msg")]//span')
    if nick is not None:
        card["用户昵称"] = _text(nick).rstrip(":：")

    card["发表时间"] = _date(_text(_first(li, './/*[contains(@class,"list_timeline")]')))

    # 综合口碑评分: 容器内直挂的 <span>3.71</span>
    card["综合口碑评分"] = ""
    rank = _first(li, './/*[contains(@class,"list_custom_rank")]')
    if rank is not None:
        span = _first(rank, "./span")
        v = _num(_text(span))
        if v is not None:
            card["综合口碑评分"] = v

    # 7 项维度评分
    for d in STANDARD_DIMENSIONS:
        card[f"{d}评分"] = ""
    for li2 in li.xpath('.//ul[contains(@class,"list_dimension_score")]/li'):
        name = _text(_first(li2, './/p[contains(@class,"d_name")]'))
        score = _num(_text(_first(li2, './/p[contains(@class,"score")]')))
        if name and score is not None:
            card[f"{name}评分"] = score

    card["车系名称"] = _text(_first(li, './/span[contains(@class,"list_series")]'))
    card["车型版本"] = _text(_first(li, './/span[contains(@class,"list_spec")]'))
    card["购车经销商"] = _text(_first(li, './/span[contains(@class,"list_dealer")]'))
    if card["购车经销商"].startswith("购车经销商"):
        card["购车经销商"] = card["购车经销商"].split("：")[-1].split(":")[-1].strip()

    # 车况信息: 列表页只给摘要(里程/油耗/车价/时间/地点), 详情页更全
    # 注意列表页类名是 list_item_info__EoTAO(下划线), 详情页是 item-info(连字符),
    # 两种写法都要覆盖 —— 只写一种会导致整组字段静默为空。
    cond: Dict[str, str] = {}
    for li3 in li.xpath('.//li[contains(@class,"item-info") or contains(@class,"item_info")]'):
        k = _text(_first(li3, './/*[contains(@class,"list_key") or contains(@class,"key")]'))
        n = _text(_first(li3, './/*[contains(@class,"list_name") or contains(@class,"name")]'))
        if k and n:
            cond[n] = k
    for k in STANDARD_CONDITION_KEYS:
        card[k] = cond.get(k, "")

    # 满意/不满意摘要在列表页是被截断的, 仅作 fallback
    feel = li.xpath('.//div[contains(@class,"list_nice_value")]')
    card["_满意摘要"] = _text(feel[0]) if len(feel) > 0 else ""
    card["_不满摘要"] = _text(feel[1]) if len(feel) > 1 else ""

    return card if card["口碑ID"] else None


def parse_list_page(html: str) -> List[Dict[str, Any]]:
    """解析列表页的一整页卡片。"""
    try:
        doc = LH.fromstring(html)
    except Exception:
        return []

    cards: List[Dict[str, Any]] = []
    seen = set()
    for li in doc.xpath('//ul[contains(@class,"list_list_con")]/li'):
        try:
            c = _parse_card(li)
        except Exception:
            continue
        if c and c["口碑ID"] not in seen:
            seen.add(c["口碑ID"])
            cards.append(c)

    # 兜底: 结构再改时, 退化为"按详情链接定位卡片"
    if not cards:
        for a in doc.xpath('//a[contains(text(),"查看完整口碑")]'):
            node = a
            for _ in range(5):
                if node is None:
                    break
                node = node.getparent()
                if node is not None and node.tag == "li":
                    try:
                        c = _parse_card(node)
                    except Exception:
                        c = None
                    if c and c["口碑ID"] not in seen:
                        seen.add(c["口碑ID"])
                        cards.append(c)
                    break
    return cards


# --------------------------------------------------------------------------- #
# 详情页
# --------------------------------------------------------------------------- #
def _parse_star(item) -> Optional[float]:
    """维度评分。优先取 <span class="star-num">4</span>, 兜底用星级条宽度换算。"""
    n = _num(_text(_first(item, './/*[contains(@class,"star-num")]')))
    if n is not None:
        return n
    bar = _first(item, './/*[contains(@class,"athm-star")]//*[contains(@class,"kb-star")]')
    if bar is not None:
        m = re.search(r"width:\s*(\d+(?:\.\d+)?)%", bar.get("style", ""))
        if m:
            return round(float(m.group(1)) / 20.0, 1)
    return None


def parse_detail_page(html: str) -> Dict[str, Any]:
    """解析口碑详情页。

    实测结构(2026-10):
      <div class="space kb-item">
        <h1>空间 <div class="athm-star">..</div><span class="star-num">4</span></h1>
        <p class="kb-item-msg">正文</p>
      </div>
    维度名必须取 h1 的第一个文本节点, 用 text_content() 会把评分数字一起带出来。
    """
    out: Dict[str, Any] = {}
    try:
        doc = LH.fromstring(html)
    except Exception:
        return out

    out["口碑标题"] = _text(_first(doc, "//h1[@class='title']"))
    out["车型版本"] = _text(_first(doc, '//*[contains(@class,"subnav-title-name")]'))
    if not out["车型版本"]:
        out["车型版本"] = _text(_first(doc, '//*[contains(@class,"main-spec")]'))

    # 发表时间: "2026-10-22 首次发表"
    tl = doc.xpath('//*[contains(@class,"timeline")]')
    out["发表时间"] = ""
    for e in tl:
        d = _date(_text(e))
        if d:
            out["发表时间"] = d
            break

    # 用户昵称: 详情页比列表页多一个"作者"区域
    nick = _first(doc, '//*[contains(@class,"user-msg")]//*[contains(@class,"name")]')
    if nick is None:
        nick = _first(doc, '//div[contains(@class,"name-con")]//span[contains(@class,"name")]')
    out["用户昵称"] = _text(nick).rstrip(":：")

    # 车况信息(行驶里程/裸车价/电耗/续航/购买时间/购买地点 ...)
    # 不硬编码字段名 —— 纯电车会多出"夏季电耗/冬季续航"等键, 硬编码会丢。
    cond: Dict[str, str] = {}
    for li in doc.xpath('//li[contains(@class,"item-info") or contains(@class,"item_info")]'):
        k = _text(_first(li, './/*[contains(@class,"key")]'))
        n = _text(_first(li, './/*[contains(@class,"name")]'))
        if n and k:
            cond[n] = k
    for k in STANDARD_CONDITION_KEYS:
        out[k] = cond.pop(k, "")
    out["车况信息_其他"] = json.dumps(cond, ensure_ascii=False) if cond else ""

    # 维度块
    for d in STANDARD_DIMENSIONS:
        out[f"{d}评分"] = ""
        out[f"{d}评论"] = ""
    out["最满意"] = ""
    out["最不满意"] = ""

    for item in doc.xpath('//div[contains(@class,"kb-item") and contains(@class,"space")]'):
        h1 = _first(item, "./h1")
        if h1 is None:
            continue
        # 第一个文本节点才是维度名, 后面跟着星级和分数
        name = _norm(h1.text or "")
        if not name:
            sp = _first(h1, './span[not(contains(@class,"star-num"))]')
            name = _text(sp)
        name = re.sub(r"\d+(\.\d+)?\s*$", "", name).strip()
        body = _text(_first(item, './/p[contains(@class,"kb-item-msg")]'))
        if not name:
            continue
        if name == "最满意":
            out["最满意"] = body
        elif name == "最不满意":
            out["最不满意"] = body
        elif name in STANDARD_DIMENSIONS:
            st = _parse_star(item)
            out[f"{name}评分"] = st if st is not None else ""
            out[f"{name}评论"] = body

    # 交互数据。实测这三个 span 就在 .option-list 里默认可见,
    # 原脚本的滚动/悬停/remove('fn-hide') 全部无效且多余, 已删除。
    #
    # 注意: option-goods(点赞数) 在 SSR HTML 里恒为空字符串 —— 它由前端异步填充,
    # 数据源在 koubeiipv6.app.autohome.com.cn(robots 全站 Disallow), 按合规要求不取。
    # 因此解析不到时输出空串而非 0, 以免把"未知"误报成"零点赞"。
    for key, cls in (("观看数", "option-views"), ("点赞数", "option-goods"), ("评论数", "option-comments")):
        el = _first(doc, f'//span[contains(@class,"{cls}")]')
        raw = _text(el)
        if raw == "":
            out[key] = ""
        else:
            v = _num(raw)
            out[key] = int(v) if v is not None else ""

    return out
