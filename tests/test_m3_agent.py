import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from app.main import create_app

print("==> 创建应用 ...")
app = create_app()
client = TestClient(app)

# 1) Chat 分支
print("\n==> [1/3] Chat: 你好")
r = client.post("/ask", json={"question": "你好"})
d = r.json()
print("mode:", d.get("mode"))
print("answer:", d.get("answer"))
assert d.get("mode") == "chat", d.get("mode")
assert d.get("answer"), "chat 应返回回答"

# 2) Agent calculator（纯计算表达式 → agent）
print("\n==> [2/3] Agent calculator: 100 * 1.08")
r2 = client.post("/ask", json={"question": "100 * 1.08"})
d2 = r2.json()
print("mode:", d2.get("mode"))
print("answer:", d2.get("answer"))
assert d2.get("mode") == "agent", d2.get("mode")
assert "108" in d2.get("answer", ""), "计算结果应含 108"

# 3) Agent 知识检索（grounding + 引用硬约束）
print("\n==> [3/3] Agent knowledge_search: 年假有多少天？")
agent = app.state.container.agent
answer3, sources3 = agent.run("年假有多少天？")
print("answer:", answer3)
print("sources:", [s.document_name for s in sources3])
assert answer3, "agent 应返回回答"
assert sources3, "agent 应通过检索获得来源"

print("\n==> M3 验证通过 ==")