"""验证检查包：真实 HTTP 冒烟等。

这些模块属于验证脚手架，产品代码不 import 它们；`scripts/verify.py`
按名字 import `verify_checks.<module>` 并调用其 `run(ctx)`，约定：

  - 正常返回 → PASS
  - 抛 AssertionError → FAIL（消息里写清期望/实际，verify.py 用来点名 feature）
  - 抛 CheckInconclusive → INCONCLUSIVE（依赖缺、端口占用、进程起不来等环境原因）
"""


class CheckInconclusive(Exception):
    """环境原因导致检查拿不到可信结论。绝不折算成通过。"""
