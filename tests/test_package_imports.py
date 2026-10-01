"""包导入回归：防「目录重构漏改平级导入」类隐患复发。

2026-09-29 实测事故：fetch_jd.py 函数内懒导入 `from fetch_jd_native import ...`
是 2026-09-28 目录重构（根目录平铺 → src/jobforge 单包）的漏网旧式导入——模块级
import 与单元测试都探不到，只有真实抓取走到 native 分支才炸
（ModuleNotFoundError: No module named 'fetch_jd_native'，用户点「重新抓取 JD」触发）。
两道防线：
1. 静态扫描：包内禁止「不带 jobforge. 前缀的同包平级导入」（懒导入也能扫出）；
2. 子进程冒烟：按 server 实际拉起方式（-m jobforge.fetch_jd，cwd=项目根）走参数不足
   路径——只打印 JSON、不触发任何抓取，验证包在真实子进程环境可引导。
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
PKG = SRC / "jobforge"

# jobforge 包内全部模块名（tools/ 子包同算平级）
SIBLINGS = {p.stem for p in PKG.rglob("*.py") if p.stem != "__init__"}
_FLAT_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+(" + "|".join(sorted(SIBLINGS)) + r")\b", re.M)


def test_no_flat_sibling_imports():
    """包内一律 jobforge.xxx 绝对导入，平级裸导入（含函数内懒导入）视为回归。"""
    offenders = []
    for f in sorted(PKG.rglob("*.py")):
        text = f.read_text(encoding="utf-8")
        for m in _FLAT_IMPORT.finditer(text):
            line = text[:m.start()].count("\n") + 1
            offenders.append(f"{f.relative_to(PKG)}:{line}: {m.group(0).strip()}")
    assert not offenders, "发现平级裸导入（应改为 jobforge.xxx 绝对导入）：\n" + "\n".join(offenders)


def test_fetch_jd_subprocess_boots():
    """按 server 的真实拉起方式引导 fetch_jd 子进程：cwd=项目根 + PYTHONPATH=src，
    参数不足只打印错误 JSON（不触发抓取）——包级导入必须全部可解析。
    父进程按 UTF-8 解码：stdout 的 JSON 契约已钉死 UTF-8（2026-10-01，与 server 端一致）。"""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(SRC)
    r = subprocess.run(
        [sys.executable, "-m", "jobforge.fetch_jd"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        cwd=str(SRC.parent), env=env,
    )
    assert r.returncode == 0, f"子进程引导失败：{(r.stderr or '')[-500:]}"
    out = json.loads(r.stdout.strip())
    assert out["ok"] is False and "参数不足" in out["error"]
