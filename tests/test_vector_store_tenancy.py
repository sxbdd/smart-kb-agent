"""向量库租户隔离（V2）的离线用例。

这是多租户 RAG 最容易漏的一环（见 docs/v2-plan.md §6.3 / §9.3）：漏了租户过滤
就会跨租户召回别人的文档。本文件只测**向量库自身的过滤**，不依赖 DAO / DB /
MySQL，因此可以在任何机器上离线跑。

覆盖点（与角色分工里的验收清单一一对应）：

1. 两个租户各灌 chunk，A 租户 query **检索不到** B 租户的片段；
2. 缺 `tenant_id` 字段的老数据在 `tenant_id="default"` 查询下**可见**（内存实现）；
3. `tenant_id=None` 时**不过滤** —— V1 既有行为不变；
4. `min_score` 与 `tenant_id` 组合：只减不增、顺序不变；
5. `delete_document(id, tenant_id)` 删不掉别的租户的同名 `document_id`。

内存实现与 Chroma 实现在带租户元数据的数据上必须**逐条一致**，所以大部分用例
都做了 `params=["memory", "chroma"]` 参数化；只有"老数据缺失字段"这条是**已知偏差**，
单独用内存实现断言并标注原因（见文件末尾的 deviation 用例与交付说明）。

Chroma 用 `tmp_path` 持久化，不碰仓库里的 `data/chroma_db/`。
"""
from __future__ import annotations

import pytest

from app.core.vector_store import ChromaVectorStore, InMemoryVectorStore

# ---------------------------------------------------------------------------
# 固定向量：分数可预期、不依赖任何模型（与 test_vector_store_threshold.py 同一套手法）
# ---------------------------------------------------------------------------

_QUERY = [1.0, 0.0, 0.0, 0.0]

#: 带租户元数据的条目：(id, 向量, 租户, document_id)
#: 分数依次为 1.0 / 0.9 / 0.8（租户 A）与 0.0 / 0.0（租户 B），便于验证阈值与租户正交。
_ITEM_A_TOP = ("a_top", [1.0, 0.0, 0.0, 0.0], "a", "doc_a_top")
_ITEM_A_MID = ("a_mid", [0.9, 0.1, 0.0, 0.0], "a", "doc_a_mid")
_ITEM_A_LOW = ("a_low", [0.8, 0.2, 0.0, 0.0], "a", "doc_a_low")
_ITEM_B_TOP = ("b_top", [0.0, 1.0, 0.0, 0.0], "b", "doc_b_top")
_ITEM_B_MID = ("b_mid", [0.0, 0.0, 1.0, 0.0], "b", "doc_b_mid")

_TENANTED_ITEMS = [_ITEM_A_TOP, _ITEM_A_MID, _ITEM_A_LOW, _ITEM_B_TOP, _ITEM_B_MID]

#: 默认租户的条目：它的分数最高，用来证明 `"default"` 被当成真实租户而不是 None
_ITEM_DEFAULT_TOP = ("d_top", [1.0, 0.0, 0.0, 0.0], "default", "doc_d_top")

#: 升级前的老数据：没有 tenant_id 字段（模拟 V1 灌进来的 chunk）
_LEGACY_ITEM = ("legacy", [0.95, 0.05, 0.0, 0.0], "doc_legacy")


def _meta(document_id: str, tenant_id: str | None = None) -> dict:
    """构造 chunk metadata。`tenant_id=None` 表示**不写这个字段**（老数据形态）。"""
    meta = {"document_id": document_id, "document_name": f"{document_id}.txt", "chunk_index": 0}
    if tenant_id is not None:
        meta["tenant_id"] = tenant_id
    return meta


def _fill(store, items=_TENANTED_ITEMS) -> None:
    for item_id, embedding, tenant_id, document_id in items:
        store.add(item_id, embedding, _meta(document_id, tenant_id), f"{item_id} 的内容")


def _fill_with_legacy(store) -> None:
    _fill(store)
    item_id, embedding, document_id = _LEGACY_ITEM
    store.add(item_id, embedding, _meta(document_id, None), f"{item_id} 的内容")


@pytest.fixture
def memory_store():
    store = InMemoryVectorStore()
    _fill(store)
    return store


@pytest.fixture
def chroma_store(tmp_path):
    store = ChromaVectorStore(str(tmp_path / "chroma_tenancy"))
    store.reset()
    _fill(store)
    return store


@pytest.fixture(params=["memory", "chroma"])
def store(request, memory_store, chroma_store):
    """同一组数据分别跑内存实现与 Chroma 实现，保证两者行为一致。"""
    return memory_store if request.param == "memory" else chroma_store


def _ids(results) -> list[str]:
    return [r.id for r in results]


# ---------------------------------------------------------------------------
# 1. 跨租户不可见
# ---------------------------------------------------------------------------

def test_tenant_a_cannot_see_tenant_b(store):
    """A 租户检索结果里不能出现任何 B 租户的片段。"""
    results = store.query(_QUERY, top_k=10, tenant_id="a")

    assert _ids(results) == ["a_top", "a_mid", "a_low"], "A 租户只应看到自己的三片段"
    assert all(r.metadata["tenant_id"] == "a" for r in results)


def test_tenant_b_cannot_see_tenant_a(store):
    """反向同样成立：B 租户看不到 A 租户的片段（隔离是双向的）。"""
    results = store.query(_QUERY, top_k=10, tenant_id="b")

    assert _ids(results) == ["b_top", "b_mid"]
    assert all(r.metadata["tenant_id"] == "b" for r in results)


def test_empty_tenant_returns_empty(store):
    """不存在的租户 → 空结果，且不抛异常（不是"退化成不过滤"）。"""
    assert store.query(_QUERY, top_k=10, tenant_id="c") == []


def test_default_tenant_is_a_real_tenant_not_none(store):
    """`"default"` 是**真实租户**：既不能退化成"查到全部"，也要能查到本租户数据。

    实现里 `tenant_id=None` 才表示不过滤；把 `"default"` 误当成 None 会让默认租户
    的用户直接看到所有租户的片段 —— 这是本文件最该防住的回归。
    """
    _fill(store, [_ITEM_DEFAULT_TOP])

    only_default = _ids(store.query(_QUERY, top_k=10, tenant_id="default"))
    assert only_default == ["d_top"], "默认租户只能看到自己那条，不能看到 a/b 租户的片段"
    assert "a_top" not in only_default
    assert _ids(store.query(_QUERY, top_k=10, tenant_id="a")) == ["a_top", "a_mid", "a_low"]


def test_top_k_applies_within_tenant(store):
    """top_k 只在本租户内截断：A 租户 top_k=2 不会把 B 的片段补进来。"""
    assert _ids(store.query(_QUERY, top_k=2, tenant_id="a")) == ["a_top", "a_mid"]


# ---------------------------------------------------------------------------
# 2. 老数据（缺 tenant_id 字段）视为 default —— 仅内存实现
# ---------------------------------------------------------------------------

def test_legacy_data_without_tenant_field_is_visible_to_default():
    """缺 `tenant_id` 字段的老数据在 `tenant_id="default"` 下**可见**（内存实现）。

    为什么必须这样：升级前灌进库里的 chunk 没有这个字段，若判成"不匹配任何租户"，
    历史数据会凭空消失。归到默认租户既保住老数据，又不会泄漏到别的租户。
    """
    store = InMemoryVectorStore()
    _fill_with_legacy(store)

    ids = _ids(store.query(_QUERY, top_k=10, tenant_id="default"))
    # legacy 是唯一的 default 数据；a/b 租户的片段一条都不能混进来
    assert ids == ["legacy"], "缺字段的老数据必须归到默认租户，且不能顺带放进别的租户的片段"


def test_legacy_data_not_visible_to_other_tenants():
    """老数据不能因为"缺字段"而被别的租户看到 —— 缺失不等于通配。"""
    store = InMemoryVectorStore()
    _fill_with_legacy(store)

    assert "legacy" not in _ids(store.query(_QUERY, top_k=10, tenant_id="a"))
    assert "legacy" not in _ids(store.query(_QUERY, top_k=10, tenant_id="b"))


def test_legacy_data_known_divergence_in_chroma(tmp_path):
    """已知偏差：Chroma 的 `where` **匹配不到缺字段的老数据**，内存实现则归入 default。

    Chroma 的 query 不支持 `$exists`（本地 `chromadb 1.5.9` 实测报
    "Expected where operator to be one of $gt, $gte, …, got $exists"），
    无法表达"字段缺失也算 default"。取舍是 fail-closed：宁可查不到老数据，
    也不能跨租户泄漏。本用例把这个偏差**固化**下来，避免以后被误认为回归；
    老库上线前需要重新灌一次（或在迁移脚本里补齐 metadata）。
    """
    store = ChromaVectorStore(str(tmp_path / "chroma_legacy"))
    store.reset()
    _fill_with_legacy(store)

    tenant_ids = _ids(store.query(_QUERY, top_k=10, tenant_id="default"))
    assert "legacy" not in tenant_ids, "Chroma 下缺字段的老数据当前不可见（已知偏差）"
    assert tenant_ids == [], "默认租户在 Chroma 里没有任何 chunk（老数据是唯一的 default 数据）"

    # 不带租户过滤时它照旧可见/可查（V1 路径不受影响）
    assert "legacy" in _ids(store.query(_QUERY, top_k=10, tenant_id=None))

    # 内存实现对同一份数据的结论不同：legacy 属于 default —— 偏差就在这里，写清楚
    memory = InMemoryVectorStore()
    _fill_with_legacy(memory)
    assert "legacy" in _ids(memory.query(_QUERY, top_k=10, tenant_id="default"))


# ---------------------------------------------------------------------------
# 3. tenant_id=None 不过滤 —— V1 行为回归
# ---------------------------------------------------------------------------

def test_none_tenant_does_not_filter(store):
    """`tenant_id=None` 必须返回全部租户的片段（既有行为、既有单测的依赖）。"""
    ids = _ids(store.query(_QUERY, top_k=10, tenant_id=None))

    assert set(ids) == {"a_top", "a_mid", "a_low", "b_top", "b_mid"}


def test_none_tenant_matches_omitted_argument(store):
    """显式传 `None` 与完全不传该参数必须逐条一致（向后兼容的硬要求）。"""
    omitted = store.query(_QUERY, top_k=10)
    explicit = store.query(_QUERY, top_k=10, tenant_id=None)

    assert _ids(omitted) == _ids(explicit)
    assert [r.score for r in omitted] == [r.score for r in explicit]


def test_tenant_filter_only_removes_and_keeps_order(store):
    """租户过滤只做"减少"，不改变剩余结果的相对顺序。"""
    all_ids = _ids(store.query(_QUERY, top_k=10, tenant_id=None))
    a_ids = _ids(store.query(_QUERY, top_k=10, tenant_id="a"))

    assert a_ids == [i for i in all_ids if i in set(a_ids)]


# ---------------------------------------------------------------------------
# 4. min_score 与 tenant_id 组合
# ---------------------------------------------------------------------------

def test_tenant_and_threshold_are_orthogonal(store):
    """`min_score=0.995` 在 A 租户内只留 `a_top`：阈值不会把 B 的片段补回来。

    三条 A 租户片段的实际分数是 1.0 / 0.9939 / 0.9701（近正交向量在高维余弦下
    仍然接近 1），所以阈值取 0.995 才能恰好只留下最高分那条。
    """
    results = store.query(_QUERY, top_k=10, min_score=0.995, tenant_id="a")

    assert _ids(results) == ["a_top"]
    assert all(r.score >= 0.995 for r in results)


def test_threshold_only_shrinks_within_tenant(store):
    """加阈值后的结果是有租户结果的**子序列**：只减不增、顺序不变。"""
    base = _ids(store.query(_QUERY, top_k=10, min_score=0.0, tenant_id="a"))
    raised = _ids(store.query(_QUERY, top_k=10, min_score=0.995, tenant_id="a"))

    assert base == ["a_top", "a_mid", "a_low"]
    assert set(raised).issubset(set(base))
    assert raised == [i for i in base if i in set(raised)]


def test_tenant_filter_beats_threshold_on_other_tenants(store):
    """即使 `min_score=0.0`（分数不过滤），别的租户的片段也进不来。"""
    ids = _ids(store.query(_QUERY, top_k=10, min_score=0.0, tenant_id="a"))

    assert "b_top" not in ids and "b_mid" not in ids


def test_threshold_above_all_scores_in_tenant_returns_empty(store):
    """本租户内所有分数都低于阈值 → 空（不会回退到别的租户）。"""
    assert store.query(_QUERY, top_k=10, min_score=1.5, tenant_id="a") == []


# ---------------------------------------------------------------------------
# 5. 删除也带租户
# ---------------------------------------------------------------------------

def test_delete_document_does_not_touch_other_tenant(store):
    """同名的 `document_id` 分属两个租户时，只删得掉自己那一个。"""
    store.add("a_shared", [1.0, 0.0, 0.0, 0.0], _meta("shared", "a"), "A 的共享文档")
    store.add("b_shared", [1.0, 0.0, 0.0, 0.0], _meta("shared", "b"), "B 的共享文档")

    store.delete_document("shared", "a")

    assert "a_shared" not in _ids(store.query(_QUERY, top_k=10, tenant_id="a"))
    assert "b_shared" in _ids(store.query(_QUERY, top_k=10, tenant_id="b")), "不该删掉别的租户的同名文档"


def test_delete_document_without_tenant_still_works(store):
    """`tenant_id=None`（既有调用方式）保持 V1 语义：按 document_id 删除。"""
    store.delete_document("doc_b_top")

    assert "b_top" not in _ids(store.query(_QUERY, top_k=10, tenant_id=None))


def test_delete_document_with_wrong_tenant_is_noop(store):
    """租户不匹配 → 一条都不删（幂等，不抛异常）。"""
    store.delete_document("doc_a_top", "b")

    assert "a_top" in _ids(store.query(_QUERY, top_k=10, tenant_id="a"))
    assert store.count() == len(_TENANTED_ITEMS)


# ---------------------------------------------------------------------------
# 6. 两种实现逐条一致（带租户元数据的数据上）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("min_score", [0.0, 0.5, 0.85, 0.95, 1.0, 1.5])
def test_memory_and_chroma_agree_per_tenant(memory_store, chroma_store, min_score):
    """同一份数据、同一阈值下，内存与 Chroma 在每个租户上的 id 序列完全一致。"""
    for tenant in ("a", "b", "default", "c", None):
        mem = memory_store.query(_QUERY, top_k=10, min_score=min_score, tenant_id=tenant)
        chroma = chroma_store.query(_QUERY, top_k=10, min_score=min_score, tenant_id=tenant)

        assert _ids(mem) == _ids(chroma), f"租户 {tenant} / 阈值 {min_score} 下两实现不一致"
        for m, c in zip(mem, chroma):
            assert m.score == pytest.approx(c.score, abs=1e-6)
