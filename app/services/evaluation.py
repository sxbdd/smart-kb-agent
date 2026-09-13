"""评测服务的服务层入口（V2 任务清单里约定的 ``app/services/evaluation.py``）。

为什么是这个 3 行的转发模块：评测的**真正实现**在 ``app/evaluation/runner.py``
（历史上就放在那里，`DEFAULT_TEST_SET`、指标口径、接口都在用它）。V2 的任务清单
把评测归到服务层，因此这里提供一个稳定的服务层入口，而不是把实现搬过来 ——
搬迁会同时改动 `app/api/routes_evaluation.py` 的导入与 `tests/test_evaluation.py`，
那属于别人的文件所有权。

两个入口指向**同一个函数对象**，不存在两份指标实现（改了 runner 就等于改了这里）。
"""
from __future__ import annotations

from app.evaluation.runner import DEFAULT_TEST_SET, REFUSAL_PHRASES, run_evaluation

__all__ = ["run_evaluation", "DEFAULT_TEST_SET", "REFUSAL_PHRASES"]
