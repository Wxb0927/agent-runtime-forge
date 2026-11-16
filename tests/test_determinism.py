"""逐字节可复现性 —— D1 验收第一条。"""
from __future__ import annotations

import hashlib

from forge.synth.generate import build_dataset, main, to_jsonl


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def test_same_seed_is_byte_identical():
    a = to_jsonl(build_dataset(42)).encode("utf-8")
    b = to_jsonl(build_dataset(42)).encode("utf-8")
    assert sha(a) == sha(b)


def test_repeated_calls_stable_across_ten_runs():
    digests = {sha(to_jsonl(build_dataset(7)).encode("utf-8")) for _ in range(10)}
    assert len(digests) == 1, "多次运行出现不同哈希 ⇒ 有未受控的随机源（dict 序／时钟／内建 hash 盐）"


def test_different_seed_changes_content_but_keeps_shape():
    a, b = build_dataset(1), build_dataset(2)
    assert sha(to_jsonl(a).encode()) != sha(to_jsonl(b).encode()), "seed 没起作用 ⇒ --seed 是装饰"
    assert len(a.projects) == len(b.projects) == 300
    assert len(a.tasks) == len(b.tasks) == 12_000
    # 形状（稀疏性）由同余保证，必须与 seed 无关
    assert sum(1 for n in a.payment_nodes if n.plan_amount_cents is not None) == \
           sum(1 for n in b.payment_nodes if n.plan_amount_cents is not None) == 184


def test_cli_output_is_reproducible(tmp_path):
    """验收脚本走的同一条路径：CLI 两次运行产物字节相同。"""
    main(["--seed", "42", "--out", str(tmp_path / "a")])
    main(["--seed", "42", "--out", str(tmp_path / "b")])
    fa = (tmp_path / "a" / "synth-seed42.jsonl").read_bytes()
    fb = (tmp_path / "b" / "synth-seed42.jsonl").read_bytes()
    assert sha(fa) == sha(fb)
    assert len(fa) > 1_000_000, "输出体量异常小 ⇒ 生成器可能只写了子集"
