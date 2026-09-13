import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["EMBEDDING_PROVIDER"] = "hash"
os.environ["VECTOR_STORE"] = "memory"
os.environ["LLM_PROVIDER"] = "fake"

from fastapi.testclient import TestClient
from app.main import create_app

app = create_app()
client = TestClient(app)

r = client.get("/healthz")
print("healthz:", r.status_code, r.json())
assert r.status_code == 200 and r.json() == {"status": "ok"}

router = app.state.container.router
cases = [("你好", "chat"), ("报销标准是什么", "rag"), ("100 * 1.08", "agent"), ("今天天气怎么样", "rag")]
for q, expect in cases:
    got = router.route(q)
    print(f"route('{q}') -> {got} (expect {expect})")
    assert got == expect, f"{q}: {got} != {expect}"

print("M1 冒烟验证通过")