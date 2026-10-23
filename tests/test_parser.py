# -*- coding: utf-8 -*-
"""解析层离线回归测试。

跑法(不需要浏览器, 不需要联网, 全程 <1 秒):

    python -m unittest discover -s tests -v

夹具说明:
    tests/fixtures/list_page.html   —— k.autohome.com.cn/770/ 渲染后的列表页快照
    tests/fixtures/detail_page.html —— 一篇口碑详情页快照

站点改版时, 把新的页面快照覆盖进 fixtures 再跑本测试,
失败的断言会指出到底哪个字段的 XPath 失效了。
"""
from __future__ import annotations

import json
import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from autohome_spider.parser import parse_detail_page, parse_list_page, parse_series_meta  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _read(name: str) -> str:
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


class TestSeriesMeta(unittest.TestCase):
    def test_counts(self):
        meta = parse_series_meta(_read("list_page.html"))
        self.assertEqual(meta["总口碑数量"], 18168)
        self.assertEqual(meta["在售口碑数量"], 236)

    def test_garbage_input_does_not_raise(self):
        self.assertEqual(parse_series_meta(""), {"总口碑数量": 0, "在售口碑数量": 0})
        self.assertEqual(parse_series_meta("<html><body>nothing</body></html>"),
                         {"总口碑数量": 0, "在售口碑数量": 0})


class TestListPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cards = parse_list_page(_read("list_page.html"))

    def test_card_count(self):
        self.assertEqual(len(self.cards), 10)

    def test_ids_unique_and_complete(self):
        ids = [c["口碑ID"] for c in self.cards]
        self.assertTrue(all(ids), "存在无法提取口碑ID的卡片")
        self.assertEqual(len(ids), len(set(ids)))

    def test_first_card_fields(self):
        # 避免与 fixtures 快照更新后失配。
        c = self.cards[0]
        self.assertEqual(c["口碑ID"], "01m5h0f7766mwk8chr6crg0000")
        self.assertEqual(c["口碑标题"], "丰田荣放双擎精英感受")
        self.assertEqual(c["用户昵称"], "烟台车友9256875")
        self.assertEqual(c["发表时间"], "2026-10-22")
        self.assertAlmostEqual(float(c["综合口碑评分"]), 3.71, places=2)
        self.assertEqual(c["车系名称"], "RAV4荣放")
        self.assertEqual(c["车型版本"], "2026款 双擎 2.0L 两驱精英版")
        self.assertEqual(c["购车经销商"], "烟台中升丰田")

    def test_condition_fields_not_empty(self):
        # 回归点: 列表页类名是 list_item_info(下划线), 详情页是 item-info(连字符),
        # 只写一种会导致整组车况字段静默为空。
        c = self.cards[0]
        self.assertEqual(c["行驶里程"], "1700km")
        self.assertEqual(c["裸车购买价"], "13.60万")
        self.assertEqual(c["购买时间"], "2026-09")
        self.assertEqual(c["购买地点"], "烟台")
        self.assertEqual(c["百公里油耗"], "3.9L")

    def test_dimension_scores(self):
        c = self.cards[0]
        self.assertAlmostEqual(float(c["空间评分"]), 4.0)
        self.assertAlmostEqual(float(c["内饰评分"]), 3.0)
        self.assertAlmostEqual(float(c["配置评分"]), 3.0)

    def test_unsupported_dimension_is_blank_not_zero(self):
        # 燃油车没有"续航"维度, 应为空而不是 0 —— 0 是合法评分, 混淆会污染分析。
        self.assertEqual(self.cards[0]["续航评分"], "")

    def test_link_is_clean(self):
        self.assertTrue(self.cards[0]["评论链接"].endswith(".html"))
        self.assertNotIn("#", self.cards[0]["评论链接"])

    def test_empty_html(self):
        self.assertEqual(parse_list_page(""), [])
        self.assertEqual(parse_list_page("<html></html>"), [])


class TestDetailPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = parse_detail_page(_read("detail_page.html"))

    def test_basic(self):
        self.assertEqual(self.d["口碑标题"], "丰田荣放双擎精英感受")
        self.assertEqual(self.d["发表时间"], "2026-10-22")
        self.assertEqual(self.d["用户昵称"], "烟台车友9256875")
        self.assertIn("RAV4荣放", self.d["车型版本"])

    def test_condition_kv(self):
        self.assertEqual(self.d["行驶里程"], "1700km")
        self.assertEqual(self.d["裸车购买价"], "13.60万")
        self.assertEqual(self.d["购买地点"], "烟台")
        self.assertEqual(self.d["购买时间"], "2026-09")
        self.assertEqual(self.d["百公里油耗"], "3.9L")

    def test_dimension_name_strips_score_digit(self):
        # 回归点: 维度名必须取 h1 的第一个文本节点。
        # 用 text_content() 会把 "空间 4" 一起带出, 导致维度名匹配失败。
        self.assertAlmostEqual(float(self.d["空间评分"]), 4.0)
        self.assertAlmostEqual(float(self.d["内饰评分"]), 3.0)
        self.assertIn("家用空间够用", self.d["空间评论"])

    def test_full_text(self):
        self.assertGreater(len(self.d["最满意"]), 50)
        self.assertGreater(len(self.d["最不满意"]), 30)
        self.assertNotIn("...", self.d["最满意"])

    def test_like_count_is_blank_not_zero(self):
        # 点赞数由前端异步填充, SSR HTML 里是空字符串。
        # 必须输出空串, 不能输出 0 —— 否则把"未知"伪装成"零点赞"。
        self.assertEqual(self.d["点赞数"], "")

    def test_view_count_parsed(self):
        self.assertEqual(self.d["观看数"], 0)   # 这篇是当天发表的新口碑, 真实为 0

    def test_empty_html(self):
        self.assertEqual(parse_detail_page(""), {})


class TestCsvSchema(unittest.TestCase):
    def test_columns_unique(self):
        from autohome_spider.config import CSV_COLUMNS
        self.assertEqual(len(CSV_COLUMNS), len(set(CSV_COLUMNS)), "CSV 列名有重复")

    def test_all_parser_outputs_map_into_columns(self):
        from autohome_spider.config import CSV_COLUMNS
        d = parse_detail_page(_read("detail_page.html"))
        missing = [k for k in d if k not in CSV_COLUMNS]
        self.assertEqual(missing, [], f"以下解析字段没有对应的 CSV 列: {missing}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
