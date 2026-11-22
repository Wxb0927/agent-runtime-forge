# -*- coding: utf-8 -*-
"""门禁**自己能失败**的证明。

⚠️ 这些用例故意把"坏数据"喂进门禁函数（用注入参数，⛔ 改磁盘源码），
因为一条从没红过的门禁和没写门禁等价——这是 D1 那轮"假能失败"教训的延续。
真实的源码级变异（改歪→红）见 ``tools/mutation_check.py`` 的 D3 组。
"""
from __future__ import annotations

import json

import pytest

from forge.contract import cards as C
from forge.contract import gates as G
from forge.contract import metrics as M


def test_g1_passes_on_current_code():
    assert G.assert_describe_matches_args()["params"] == ["metric", "project", "topK"]


def test_g1_fails_when_card_declares_an_extra_param():
    cards = json.loads(json.dumps(C.describe()))
    cards["stats"]["params"]["limit"] = {"type": "integer"}       # 说明书多写一个参数
    with pytest.raises(G.GateFailure, match="只在说明书里有"):
        G.assert_describe_matches_args(cards=cards)


def test_g1_fails_when_cap_differs_from_code():
    cards = json.loads(json.dumps(C.describe()))
    cards["stats"]["params"]["topK"]["max"] = 9999                # 说明书写 9999，代码钳 50
    with pytest.raises(G.GateFailure, match="上限分叉"):
        G.assert_describe_matches_args(cards=cards)


def test_g2_fails_when_registry_loses_a_metric():
    orig = M.declared_metrics
    try:
        M.declared_metrics = lambda: [m for m in orig() if m != "cpi"]   # 少登记一个
        with pytest.raises(M.RegistryDrift, match="跑出来但"):
            M.assert_registry_matches_run()
    finally:
        M.declared_metrics = orig


def test_g2_fails_when_registry_invents_a_metric():
    orig = M.declared_metrics
    try:
        M.declared_metrics = lambda: orig() + ["made_up_rate"]
        with pytest.raises(M.RegistryDrift, match="只登记未跑出来"):
            M.assert_registry_matches_run()
    finally:
        M.declared_metrics = orig


def test_g3_fails_on_a_stale_menu():
    live = json.loads(json.dumps(C.describe()))
    live["stats"]["enums"]["metric"].append("new_metric")          # 加了指标，菜单忘了重生成
    with pytest.raises(G.GateFailure, match="菜单已过期"):
        G.assert_menu_not_stale(live=live)


def test_g3_fails_when_menu_file_is_missing(tmp_path):
    with pytest.raises(G.GateFailure, match="缺菜单文件"):
        G.assert_menu_not_stale(cards_path=tmp_path / "nope.json")


def test_run_all_reports_three_gates():
    res = G.run_all()
    assert set(res) == {"G1", "G2", "G3"}
    assert len(res["G2"]["declared"]) == len(res["G2"]["produced"]) >= 10


def test_cli_exits_nonzero_when_a_gate_breaks(monkeypatch, capsys):
    def boom():
        raise G.GateFailure("G1 自描述与真参数面分叉：测试注入")
    monkeypatch.setattr(G, "assert_describe_matches_args", boom)
    assert G.main([]) == 1
    assert "[GATE FAIL]" in capsys.readouterr().out
