import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from app.main import create_app

print("==> 创建应用 ...")
app = create_app()
client = TestClient(app)

# 1) 注册 / 登录
print("==> [1] 注册 / 登录")
r = client.post("/auth/register", json={"username": "testuser", "password": "test123456"})
if r.status_code == 409:
    r = client.post("/auth/login", json={"username": "testuser", "password": "test123456"})
print("auth status:", r.status_code)
assert r.status_code == 200, r.text
token = r.json()["token"]
print("token: [已获取]")

# 2) 无 token → 401
print("==> [2] 无 token 访问 /documents")
r2 = client.get("/documents")
print("no-token status:", r2.status_code)
assert r2.status_code == 401, r2.status_code

# 3) 带 token → 200
print("==> [3] 带 token 访问 /documents")
r3 = client.get("/documents", headers={"Authorization": f"Bearer {token}"})
print("with-token status:", r3.status_code)
assert r3.status_code == 200, r3.status_code

# 4) 评测（真实 LLM）
print("==> [4] 跑评测（真实 LLM，5 题）")
test_set = Path("D:/Projects/smart-kb-agent/data/evaluation/test_set_smart.json")
r4 = client.post(
    "/evaluation/run",
    json={"test_set_path": str(test_set), "top_k": 3},
    headers={"Authorization": f"Bearer {token}"},
)
print("eval status:", r4.status_code)
assert r4.status_code == 200, r4.text
d = r4.json()
keys = ("total", "keyword_accuracy", "source_accuracy", "refusal_accuracy", "overall_accuracy")
print("metrics:", {k: d.get(k) for k in keys})

print("==> P0 鉴权 + 评测验证通过 ==")