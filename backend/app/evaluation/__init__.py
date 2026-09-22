"""评估体系（Prompt 22）：EvaluationCase 案例契约与案例库。

- schemas.py：EvaluationCase Schema（单一数据契约，pydantic-only，不反向依赖执行面）；
- cases.py：20 个评估案例（案例库，pytest 校验完整性与覆盖度，Runner 22.2 消费）。
"""
