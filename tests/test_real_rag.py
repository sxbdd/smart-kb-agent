import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from app.main import create_app

print("==> 创建应用（加载真实 bge 模型 + Chroma + MySQL）...")
app = create_app()
client = TestClient(app)
print("==> 应用就绪")

# 准备制度文档
doc = Path("D:/Projects/smart-kb-agent/data/_real_test_hr.txt")
doc.parent.mkdir(parents=True, exist_ok=True)
doc.write_text(
    "员工考勤与休假制度\n"
    "1. 上班时间：周一至周五 9:00-18:00，午休 12:00-13:00。\n"
    "2. 年假：入职满 1 年享 5 天年假，满 5 年享 10 天年假。\n"
    "3. 出差住宿标准：一线城市每晚不超过 600 元，其他城市不超过 450 元。\n"
    "4. 市内交通费：凭票实报实销，单日上限 100 元。\n"
    "5. 加班餐补：工作日加班超过 20:00 可报销 30 元餐补。\n",
    encoding="utf-8",
)

print("==> 上传文档 ...")
with open(doc, "rb") as f:
    r = client.post("/upload", files={"file": ("员工考勤制度.txt", f, "text/plain")})
print("upload status:", r.status_code)
assert r.status_code == 200, r.text

print("==> 提问：出差住宿标准是多少？ ...")
r2 = client.post("/ask", json={"question": "出差住宿标准是多少？", "top_k": 3})
print("ask status:", r2.status_code)
d = r2.json()
print("mode:", d.get("mode"))
print("answer:", d.get("answer"))
print("source docs:", [s["document_name"] for s in d.get("sources", [])])
assert d.get("answer"), "answer 不能为空"
assert d.get("sources"), "命中题应有引用来源"

print("==> 拒答验证：问知识库没有的内容 ...")
r3 = client.post("/ask", json={"question": "公司年会抽奖的一等奖奖品是什么？", "top_k": 3})
d3 = r3.json()
print("refusal answer:", d3.get("answer"))
print("refusal source docs:", [s["document_name"] for s in d3.get("sources", [])])

print("==> 真实 RAG + 拒答验证结束 ==")