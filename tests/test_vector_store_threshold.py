"""检索相似度阈值（`min_score`）的离线用例。

覆盖三件事：

1. **等价性**：`min_score=0.0`（默认）时，`InMemoryVectorStore` 与 `ChromaVectorStore`
   的返回条数、顺序、分数必须与"没有阈值参数"的 V1 行为一致 —— 一个都不能少；
2. **过滤生效**：阈值调高后低分结果被剔除，且**过滤发生在截断 top_k 之前**
   （否则"前 k 个都低于阈值"会错误地返回空集）；
3. **边界**：阈值高于所有分数 → 返回空；阈值**恰等于**某条分数 → 该条保留（`>=` 语义）。

内存实现与 Chroma 实现用同一组向量做对照，两者结果必须逐条一致。
Chroma 用 `tmp_path` 做持久化目录，不碰仓库里的 `data/chroma_db/`。
"""
from __future__ import annotations

import pytest

from app.core.vector_store import ChromaVectorStore, InMemoryVectorStore, SearchResult

# 一组固定的正交/近正交向量，分数可预期且不依赖任何模型
_DIM = 4
_ITEMS = [
    # (id, embedding, document)
    ("a", [1.0, 0.0, 0.0, 0.0], "完全对齐"),
    ("b", [0.8, 0.6, 0.0, 0.0], "高度相似"),
    ("c", [0.6, 0.8, 0.0, 0.0], "中等相似"),
    ("d", [0.0, 1.0, 0.0, 0.0], "正交"),
    ("e", [0.0, 0.0, 1.0, 0.0], "另一个正交方向"),
]
_QUERY = [1.0, 0.0, 0.0, 0.0]


def _fill(store) -> None:
    for item_id, embedding, document in _ITEMS:
        store.add(item_id, embedding, {"document_id": item_id, "document_name": f"{item_id}.txt"}, document)


@pytest.fixture
def memory_store():
    store = InMemoryVectorStore()
    _fill(store)
    return store


@pytest.fixture
def chroma_store(tmp_path):
    """Chroma 实现，持久化到 tmp_path（用完即弃，不污染仓库 data/）。"""
    store = ChromaVectorStore(str(tmp_path / "chroma_threshold"))
    store.reset()
    _fill(store)
    return store


@pytest.fixture(params=["memory", "chroma"])
def store(request, memory_store, chroma_store):
    """同一组参数化用例分别跑内存实现与 Chroma 实现。"""
    return memory_store if request.param == "memory" else chroma_store


# ---------------------------------------------------------------------------
# 1. min_score=0.0 与 V1 等价
# ---------------------------------------------------------------------------

def test_default_matches_v1_result_count(memory_store):
    """默认 min_score=0.0：显式传 0.0 与不传参数必须完全等价。"""
    v1 = memory_store.query(_QUERY, 5)
    with_default = memory_store.query(_QUERY, 5, 0.0)

    assert [r.id for r in v1] == [r.id for r in with_default]
    assert [r.score for r in v1] == [r.score for r in with_default]
    assert len(v1) == len(_ITEMS), "默认阈值不能丢掉任何结果"


def test_default_keeps_everything_even_for_negative_scores(memory_store):
    """即使分数为 0（正交向量），默认阈值也必须保留 —— V1 语义是"不过滤"。"""
    results = memory_store.query([0.0, 0.0, 0.0, 1.0], 5, 0.0)
    assert len(results) == len(_ITEMS)
    assert all(r.score == pytest.approx(0.0) for r in results)


def test_zero_threshold_equals_no_threshold_param(store):
    """参数化跑两种实现：不传参 vs 传 0.0，条数一致。"""
    assert len(store.query(_QUERY, 3)) == len(store.query(_QUERY, 3, 0.0))


# ---------------------------------------------------------------------------
# 2. 阈值过滤生效
# ---------------------------------------------------------------------------

def test_threshold_filters_low_scores(memory_store):
    """调高阈值后，低分结果被过滤掉，且剩余结果保持有序。"""
    all_results = memory_store.query(_QUERY, 5, 0.0)
    threshold = 0.7

    filtered = memory_store.query(_QUERY, 5, threshold)

    assert 0 < len(filtered) < len(all_results), "阈值应过滤掉一部分结果"
    assert all(r.score >= threshold for r in filtered)
    assert [r.id for r in filtered] == [r.id for r in all_results if r.score >= threshold]
    # 过滤不改变剩余结果的相对顺序
    assert [r.score for r in filtered] == sorted((r.score for r in filtered), reverse=True)


def test_filter_happens_before_top_k_truncation(memory_store):
    """过滤发生在截断之前：前 k 条低于阈值时，要继续往下取够分的条目。

    这是关键回归点 —— 若先取 top_k 再过滤，`top_k=1, min_score=0.5` 会漏掉 `b`。
    """
    results = memory_store.query(_QUERY, 1, 0.5)
    assert len(results) == 1
    assert results[0].id == "a"

    # 阈值刚好排除最高分那条时，应返回次高的那条（而不是空）
    results = memory_store.query(_QUERY, 1, 1.0)
    assert [r.id for r in results] == ["a"]


def test_top_k_still_applies_after_threshold(store):
    """阈值不放大结果集：过滤后仍然最多返回 top_k 条。"""
    results = store.query(_QUERY, 2, 0.0)
    assert len(results) == 2


# ---------------------------------------------------------------------------
# 3. 边界：高于所有分数 / 恰等于某分数
# ---------------------------------------------------------------------------

def test_threshold_above_all_scores_returns_empty(store):
    """阈值高于所有相似度 → 返回空列表。"""
    assert store.query(_QUERY, 5, 1.5) == []


def test_threshold_exactly_equal_keeps_result(memory_store):
    """阈值**恰等于**某条分数 → 该条保留（`>=` 而非 `>`）。"""
    all_results = memory_store.query(_QUERY, 5, 0.0)
    exact = all_results[1].score  # 取"高度相似"那条的精确分数

    kept = memory_store.query(_QUERY, 5, exact)

    ids = [r.id for r in kept]
    assert all_results[1].id in ids, "分数恰好等于阈值的条目必须保留"
    assert all(r.score + 1e-12 >= exact for r in kept)


def test_threshold_just_above_result_excludes_it(memory_store):
    """阈值仅比某条分数大一点点 → 该条被排除（验证是 `>=` 而不是浮点误判）。"""
    all_results = memory_store.query(_QUERY, 5, 0.0)
    target = all_results[1]

    kept = memory_store.query(_QUERY, 5, target.score + 1e-6)

    assert target.id not in [r.id for r in kept]


def test_empty_store_returns_empty_for_any_threshold():
    """空库对任何阈值都返回空，不抛异常。"""
    store = InMemoryVectorStore()
    assert store.query(_QUERY, 5, 0.0) == []
    assert store.query(_QUERY, 5, 0.9) == []


# ---------------------------------------------------------------------------
# 4. 两种实现行为一致
# ---------------------------------------------------------------------------

def test_memory_and_chroma_agree_at_several_thresholds(memory_store, chroma_store):
    """同一组向量、多个阈值下，内存实现与 Chroma 实现的 id 序列完全一致。"""
    for threshold in (0.0, 0.3, 0.6, 0.75, 1.0, 1.2):
        mem = memory_store.query(_QUERY, 5, threshold)
        chroma = chroma_store.query(_QUERY, 5, threshold)

        assert [r.id for r in mem] == [r.id for r in chroma], f"阈值 {threshold} 下两实现不一致"
        for m, c in zip(mem, chroma):
            # Chroma 的距离转分有浮点误差，允许 1e-6 级差异
            assert m.score == pytest.approx(c.score, abs=1e-6)


def test_chroma_score_is_one_minus_distance(chroma_store):
    """Chroma 的分数是 `1 - distance` 且被 `max(0.0, ...)` 截断到非负。"""
    results = chroma_store.query(_QUERY, 5, 0.0)
    assert all(0.0 <= r.score <= 1.0 + 1e-6 for r in results)
    top = results[0]
    assert top.id == "a"
    assert top.score == pytest.approx(1.0, abs=1e-6)


def test_chroma_reset_and_count(chroma_store):
    """补测 `reset()` / `count()`：清空后计数归零，重新灌入后计数正确。"""
    assert chroma_store.count() == len(_ITEMS)
    chroma_store.reset()
    assert chroma_store.count() == 0
    assert chroma_store.query(_QUERY, 5, 0.0) == []


def test_memory_reset_and_count():
    """内存实现的 `reset()` / `count()` 同样归零并清空检索结果。"""
    store = InMemoryVectorStore()
    _fill(store)
    assert store.count() == len(_ITEMS)
    store.reset()
    assert store.count() == 0
    assert store.query(_QUERY, 5) == []


def test_search_result_is_ordered_descending(store):
    """无论是否设阈值，返回结果都按分数降序。"""
    scores = [r.score for r in store.query(_QUERY, 5, 0.0)]
    assert scores == sorted(scores, reverse=True)


def test_delete_document_respects_threshold(store):
    """删除文档后再检索，阈值行为不受影响。"""
    store.delete_document("a")
    assert "a" not in [r.id for r in store.query(_QUERY, 5, 0.0)]
    assert all(r.score >= 0.5 for r in store.query(_QUERY, 5, 0.5))


def test_search_result_fields_preserved(memory_store):
    """过滤不应破坏 SearchResult 的字段。"""
    results = memory_store.query(_QUERY, 5, 0.5)
    assert results
    for r in results:
        assert isinstance(r, SearchResult)
        assert r.document
        assert r.metadata["document_name"].endswith(".txt")
