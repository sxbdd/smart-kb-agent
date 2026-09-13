"""评测：默认测试集、指标计算、接口与历史。"""
from __future__ import annotations

import io
import json
from pathlib import Path

from app.evaluation.runner import DEFAULT_TEST_SET, run_evaluation


class StubRag:
    """返回固定答案的假 RAG，用来单独验证指标计算逻辑。"""

    def __init__(self, answer: str, source_name: str = "") -> None:
        self._answer = answer
        self._source_name = source_name

    def answer(self, question, top_k=None, history=None):
        from app.models.schemas import Source

        sources = []
        if self._source_name:
            sources.append(Source(
                document_id="d1", document_name=self._source_name,
                chunk_id="c1", content="片段原文", score=0.9,
            ))
        return self._answer, sources


def _write_set(tmp_path, items):
    path = tmp_path / "set.json"
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return str(path)


# ---------- 默认测试集（回归：曾指向不存在的 test_set.json，必 500） ----------

def _default_items():
    return json.loads(DEFAULT_TEST_SET.read_text(encoding="utf-8"))


def _corpus_filenames():
    """data/kb/ 下真正当语料的文件名（README 之类的说明文件不算）。"""
    kb_dir = DEFAULT_TEST_SET.parent.parent / "kb"
    return {
        p.name for p in kb_dir.iterdir()
        if p.is_file() and not p.name.lower().startswith(("readme", "_", "."))
    }


def test_default_test_set_exists():
    assert DEFAULT_TEST_SET.name == "test_set_smart.json"
    assert DEFAULT_TEST_SET.exists(), f"默认测试集不存在：{DEFAULT_TEST_SET}"


def test_default_test_set_schema():
    data = _default_items()
    assert len(data) >= 5
    for item in data:
        assert {"id", "question", "answerable"} <= set(item)
    assert any(item["answerable"] is False for item in data), "测试集应包含越界题以验证拒答"


# ---------- 测试集自身的一致性（扩充到 47 题后必须守住这些约束） ----------

def test_test_set_is_expanded_enough():
    """样本量要求：5 题的指标没有代表性，方案要求 30~50 题。"""
    n = len(_default_items())
    assert 30 <= n <= 50, f"测试集应为 30~50 题，当前 {n}"


def test_ids_unique_and_categories_known():
    items = _default_items()
    ids = [i["id"] for i in items]
    assert len(ids) == len(set(ids)), "id 必须唯一"
    categories = {i.get("category") for i in items}
    assert not (categories - {"fact", "multi_fact", "paraphrase", "refusal"}), f"未知分类：{categories}"
    assert {"fact", "refusal"} <= categories


def test_questions_are_unique():
    questions = [i["question"].strip() for i in _default_items()]
    assert len(questions) == len(set(questions)), "存在重复问题"


def test_answerable_items_point_to_real_corpus_documents():
    """出处必须是 data/kb/ 里真实存在的文件名，否则来源准确率永远不可能满分。"""
    corpus = _corpus_filenames()
    assert corpus, "data/kb/ 下没有语料"
    bad = [
        (i["id"], i["expected_source_doc"])
        for i in _default_items()
        if i["answerable"] and i["expected_source_doc"] not in corpus
    ]
    assert not bad, f"expected_source_doc 不在语料中：{bad}"


def test_answerable_items_have_keywords_present_in_gold_evidence():
    """关键词必须真的出现在 gold_evidence 里，否则这条期望值不可能被满足。"""
    problems = []
    for item in _default_items():
        if not item["answerable"]:
            continue
        if not item["expected_keywords"]:
            problems.append((item["id"], "缺关键词"))
            continue
        for kw in item["expected_keywords"]:
            if kw not in item.get("gold_evidence", ""):
                problems.append((item["id"], kw))
    assert not problems, f"关键词与 gold_evidence 不一致：{problems}"


def test_refusal_items_declare_no_source_or_keywords():
    problems = [
        i["id"] for i in _default_items()
        if not i["answerable"] and (i["expected_source_doc"] or i["expected_keywords"])
    ]
    assert not problems, f"拒答题不应声明来源/关键词：{problems}"


def test_every_corpus_document_is_cited_by_some_question():
    """每个语料文档都要有题引用，否则它进不了检索路径（等于白灌）。"""
    cited = {i["expected_source_doc"] for i in _default_items() if i["answerable"]}
    missing = _corpus_filenames() - cited
    assert not missing, f"这些语料没有任何题目引用：{missing}"


def test_gold_evidence_really_exists_in_its_corpus_file():
    """出题最怕凭空编造事实：每条 gold_evidence 的句子必须能在对应语料文件里找到。

    按句号拆句后逐句比对（忽略空白差异），这样多事实题（两句话分别来自不同条目）
    也能被正确校验。
    """
    import re

    from app.utils.document_parser import parse_document

    kb_dir = DEFAULT_TEST_SET.parent.parent / "kb"
    norm = lambda s: re.sub(r"\s+", "", s)  # noqa: E731

    problems = []
    for item in _default_items():
        if not item["answerable"]:
            continue
        corpus_text = norm(parse_document(str(kb_dir / item["expected_source_doc"])))
        for sentence in (s for s in item["gold_evidence"].split("。") if s.strip()):
            if norm(sentence) not in corpus_text:
                problems.append((item["id"], sentence[:40]))
    assert not problems, f"gold_evidence 在语料中找不到（可能凭空编造）：{problems}"


# ---------- 指标计算 ----------

def test_keyword_and_source_hit(tmp_path):
    path = _write_set(tmp_path, [{
        "id": "q1", "question": "住宿标准", "answerable": True,
        "expected_keywords": ["600", "450"], "expected_source_doc": "制度.txt",
    }])
    metrics = run_evaluation(StubRag("一线城市 600 元，其他 450 元。", "制度.txt"), path)

    assert metrics["total"] == 1
    assert metrics["keyword_accuracy"] == 1.0
    assert metrics["source_accuracy"] == 1.0
    assert metrics["overall_accuracy"] == 1.0
    assert metrics["details"][0]["correct"] is True


def test_keyword_miss_marks_incorrect(tmp_path):
    path = _write_set(tmp_path, [{
        "id": "q1", "question": "住宿标准", "answerable": True,
        "expected_keywords": ["600", "450"], "expected_source_doc": "",
    }])
    metrics = run_evaluation(StubRag("只说了一线城市 600 元。"), path)
    assert metrics["keyword_accuracy"] == 0.0
    assert metrics["overall_accuracy"] == 0.0


def test_wrong_source_marks_incorrect(tmp_path):
    path = _write_set(tmp_path, [{
        "id": "q1", "question": "住宿标准", "answerable": True,
        "expected_keywords": ["600"], "expected_source_doc": "正确制度.txt",
    }])
    metrics = run_evaluation(StubRag("600 元。", "错误制度.txt"), path)
    assert metrics["keyword_accuracy"] == 1.0
    assert metrics["source_accuracy"] == 0.0
    assert metrics["overall_accuracy"] == 0.0


def test_refusal_metric(tmp_path):
    path = _write_set(tmp_path, [{
        "id": "q9", "question": "年会一等奖是什么", "answerable": False,
        "expected_keywords": [], "expected_source_doc": "",
    }])
    metrics = run_evaluation(StubRag("根据当前知识库，我无法回答这个问题。"), path)
    assert metrics["refusal_accuracy"] == 1.0
    assert metrics["overall_accuracy"] == 1.0


def test_non_refusal_on_unanswerable_fails(tmp_path):
    path = _write_set(tmp_path, [{
        "id": "q9", "question": "年会一等奖是什么", "answerable": False,
        "expected_keywords": [], "expected_source_doc": "",
    }])
    metrics = run_evaluation(StubRag("据我所知是 iPhone。"), path)
    assert metrics["refusal_accuracy"] == 0.0
    assert metrics["overall_accuracy"] == 0.0


def test_details_excluded_from_saved_summary(client, auth, fake_db, tmp_path):
    path = _write_set(tmp_path, [{
        "id": "q1", "question": "你好", "answerable": False,
        "expected_keywords": [], "expected_source_doc": "",
    }])
    resp = client.post("/evaluation/run", json={"test_set_path": path}, headers=auth)
    assert resp.status_code == 200, resp.text
    assert "details" in resp.json(), "接口应返回逐题细节"

    saved = list(fake_db.evaluation_runs.values())[0]["metrics"]
    assert "details" not in saved, "落库只存汇总指标，避免数据膨胀"


# ---------- 接口 ----------

def test_run_evaluation_without_body_uses_default_set(client, auth):
    """回归：不传 test_set_path 时必须能用默认测试集，不能再 500。"""
    resp = client.post("/evaluation/run", json={}, headers=auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == len(_default_items())
    assert {"keyword_accuracy", "source_accuracy", "refusal_accuracy", "overall_accuracy"} <= set(body)
    assert body["run_id"]


def test_evaluation_history_records_runs(client, auth):
    assert client.get("/evaluation/runs", headers=auth).json() == []
    client.post("/evaluation/run", json={}, headers=auth)
    rows = client.get("/evaluation/runs", headers=auth).json()
    assert len(rows) == 1
    assert rows[0]["metrics"]["total"] == len(_default_items())
    assert "details" not in rows[0]["metrics"]


def test_evaluation_end_to_end_hits_keyword_and_source(client, auth, tmp_path):
    policy = "员工考勤制度：特殊代号 ZEBRA-TOKEN 对应标准 600 元。"
    client.post(
        "/upload",
        files={"file": ("制度.txt", io.BytesIO(policy.encode("utf-8")), "text/plain")},
        headers=auth,
    )
    path = _write_set(tmp_path, [{
        "id": "q1", "question": "特殊代号 ZEBRA-TOKEN 是什么", "answerable": True,
        "expected_keywords": ["ZEBRA-TOKEN"], "expected_source_doc": "制度.txt",
    }])
    metrics = client.post("/evaluation/run", json={"test_set_path": path, "top_k": 3}, headers=auth).json()
    assert metrics["keyword_accuracy"] == 1.0
    assert metrics["source_accuracy"] == 1.0
    assert metrics["overall_accuracy"] == 1.0
