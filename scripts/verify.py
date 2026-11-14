#!/usr/bin/env python
"""JobForge 统一验证入口。

一条命令跑完全部检查：

    python scripts/verify.py              # 全部：依赖预检 + pytest 回归 + 真实 HTTP 冒烟
    python scripts/verify.py --fast       # 只跑 HTTP 冒烟（约 15s，改后端时用）
    python scripts/verify.py --only http  # 只跑名字包含该子串的检查
    python scripts/verify.py -v           # 展开每项检查的完整输出

退出码约定（供 CI / 交接审阅使用）：

    0    全部检查通过
    1    有检查失败（输出中点名失败的 feature 并附原始错误要点）
    2    inconclusive：依赖缺失 / 环境跑不起来，未得到可信结论
         （绝不把跑不起来当成通过）

设计约束：
  - 本脚本与 scripts/verify_checks/ 不进产品运行路径，产品代码不 import 它们。
  - HTTP 冒烟起真进程，但库指向临时文件，绝不读写 data/jobs.db。
  - 子进程一律以 utf-8 输出，避免 Windows 默认 gbk 让中文日志中途炸掉。
"""
from __future__ import annotations

import argparse
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
CHECKS_DIR = SCRIPTS_DIR / "verify_checks"
SUITE_TIMEOUT = 600

# Windows 控制台默认 gbk：产品代码里的 ✓ / ⏱ 之类符号会让 print 直接抛
# UnicodeEncodeError，检查还没断言就先炸了。整个入口统一切到 utf-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PASS, FAIL, INCONCLUSIVE = "PASS", "FAIL", "INCONCLUSIVE"

# 依赖预检：(模块名, pip 包名, 用到它的 feature 说明)
REQUIRED_MODULES = [
    ("fastapi", "fastapi", "HTTP 路由与服务启动"),
    ("uvicorn", "uvicorn", "真进程 HTTP 冒烟"),
    ("starlette", "starlette", "中间件与错误包络"),
    ("pydantic", "pydantic", "请求体校验"),
    ("requests", "requests", "LLM 客户端"),
    ("pytest", "pytest", "回归套件"),
]

# 真实 HTTP 冒烟检查（模块名在 scripts/verify_checks/ 下，必须实现 run(ctx)）
HTTP_CHECKS = [
    ("http_smoke", "http/smoke"),
]


class Report:
    """收集每项的结论，最后统一汇总与给出退出码。"""

    def __init__(self, verbose: bool):
        self.verbose = verbose
        self.rows: list[tuple[str, str, str, str, float]] = []
        self.started = time.perf_counter()

    def add(self, feature: str, status: str, detail: str, output: str = "", elapsed: float = 0.0):
        self.rows.append((feature, status, detail, output, elapsed))
        line = f"[{status:<12}] {feature:<28} {detail}"
        if elapsed:
            line += f"  ({elapsed:.1f}s)"
        print(line, flush=True)
        if self.verbose and output:
            for chunk in output.rstrip().splitlines():
                print(f"    | {chunk}", flush=True)

    def status_of(self, feature: str) -> str | None:
        for name, status, _d, _o, _e in self.rows:
            if name == feature:
                return status
        return None

    def failures(self):
        return [row for row in self.rows if row[1] == FAIL]

    def inconclusive(self):
        return [row for row in self.rows if row[1] == INCONCLUSIVE]

    def summary(self) -> int:
        elapsed = time.perf_counter() - self.started
        passed = sum(1 for row in self.rows if row[1] == PASS)
        failed = self.failures()
        unknown = self.inconclusive()

        print("\n" + "=" * 78, flush=True)
        print(f"结果: {passed} 通过, {len(failed)} 失败, {len(unknown)} inconclusive "
              f"| 耗时 {elapsed:.1f}s", flush=True)
        print("=" * 78, flush=True)

        if failed:
            print("\n失败项（需要修的就是这些 feature）:", flush=True)
            for feature, _s, detail, output, _e in failed:
                print(f"  - {feature}: {detail}", flush=True)
                tail = [line for line in output.rstrip().splitlines() if line.strip()][-15:]
                for line in tail:
                    print(f"      {line}", flush=True)
        if unknown:
            print("\n未能得出结论（不计为通过）:", flush=True)
            for feature, _s, detail, _o, _e in unknown:
                print(f"  - {feature}: {detail}", flush=True)

        if failed:
            print("\nVERIFY FAILED", flush=True)
            return 1
        if unknown:
            print("\nVERIFY INCONCLUSIVE", flush=True)
            return 2
        print("\nVERIFY OK", flush=True)
        return 0


def _run(cmd: list[str], cwd: Path, timeout: int = SUITE_TIMEOUT) -> tuple[int, str]:
    """以 utf-8 跑子进程，返回 (退出码, 合并输出)。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.pop("PYTHONPATH", None)     # 让空的 PYTHONPATH 之类从外部渗透进来干扰 import
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.output or b"").decode("utf-8", "replace")
        return 124, output + f"\n[verify] 超时（>{timeout}s）"
    except FileNotFoundError as exc:
        return 127, f"[verify] 可执行文件不存在：{exc}"
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def preflight(report: Report) -> bool:
    """依赖预检。任何缺项都直接判 inconclusive，不进入检查阶段。"""
    print("── 依赖预检 " + "─" * 62, flush=True)
    missing = []
    for module, package, feature in REQUIRED_MODULES:
        try:
            importlib.import_module(module)
        except Exception as exc:  # ImportError 以外也接住：装坏了同样是环境问题
            missing.append((module, package, feature, f"{type(exc).__name__}: {exc}"))

    if missing:
        for module, package, feature, err in missing:
            report.add(
                f"deps/{module}",
                INCONCLUSIVE,
                f"缺依赖，{feature} 无法验证（pip install {package}）",
                err,
            )
        print(
            f"\n[verify] 依赖不全，跳过全部检查。先安装：\n"
            f"    {sys.executable} -m pip install -r requirements-dev.txt\n",
            flush=True,
        )
        return False

    report.add("deps/installed", PASS, f"{len(REQUIRED_MODULES)} 个关键依赖可导入（{sys.executable}）")

    # 产品包必须能从 src 导入——导入不了说明源码结构坏了，比任何检查都先决
    try:
        env_src = PROJECT_ROOT / "src"
        code = ("import sys; sys.path.insert(0, r'%s');"
                "import jobforge.server as s; print(s.APP_VERSION)" % env_src)
        rc, out = _run([sys.executable, "-c", code], cwd=PROJECT_ROOT, timeout=120)
        if rc != 0:
            report.add("deps/product-import", INCONCLUSIVE,
                       "jobforge.server 导入失败，检查无法进行", out)
            return False
        report.add("deps/product-import", PASS, f"jobforge.server 可导入（APP_VERSION {out.strip()}）")
    except Exception as exc:
        report.add("deps/product-import", INCONCLUSIVE, "无法确认产品包可导入", repr(exc))
        return False

    return True


def _selected(only: str | None, group: str) -> bool:
    """--only 的分组过滤：没给就全跑，给了就只跑名字命中的那一组。"""
    return only is None or only in group


def run_pytest(report: Report) -> None:
    """仓库现有 pytest 回归套件。

    注意口径：这套测试大量直接调 route 函数、DB 全 mock，能锁住逻辑分支，
    但不能代表「接口被真实覆盖」——真实链路由 http/smoke 负责。
    """
    print("\n── pytest 回归套件 " + "─" * 58, flush=True)
    started = time.perf_counter()
    code, output = _run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider"],
        cwd=PROJECT_ROOT,
    )
    elapsed = time.perf_counter() - started
    if code == 0:
        tail = [line for line in output.strip().splitlines() if line.strip()][-1:]
        report.add("pytest/suite", PASS, f"tests/ 全绿（{tail[0] if tail else 'no summary'}）",
                   elapsed=elapsed)
    elif code == 124:
        report.add("pytest/suite", INCONCLUSIVE, f"pytest 超时（>{SUITE_TIMEOUT}s），未得到结论", output,
                   elapsed)
    else:
        report.add("pytest/suite", FAIL, f"tests/ 有失败（pytest 退出码 {code}）", output, elapsed)


def run_http_checks(report: Report, only: str | None, workspace: Path) -> None:
    """真实 HTTP 冒烟：每条在独立临时目录里跑，库永不指向 data/。"""
    print("\n── 真实 HTTP 冒烟 " + "─" * 58, flush=True)
    sys.path.insert(0, str(SCRIPTS_DIR))
    from verify_checks import CheckInconclusive

    for module_name, feature in HTTP_CHECKS:
        if only and only not in module_name and only not in feature:
            continue
        started = time.perf_counter()
        case_dir = workspace / module_name
        case_dir.mkdir(parents=True, exist_ok=True)
        try:
            module = importlib.import_module(f"verify_checks.{module_name}")
            ctx = {
                "case_dir": case_dir,
                "server_log": "",
                "log": lambda msg: print(f"    · {msg}", flush=True),
            }
            module.run(ctx)
            report.add(feature, PASS, getattr(module, "DESCRIPTION", ""),
                       elapsed=time.perf_counter() - started)
        except AssertionError as exc:
            detail = str(exc).splitlines()[0] if str(exc).strip() else "断言失败"
            report.add(feature, FAIL, f"断言失败: {detail}",
                       traceback.format_exc(), time.perf_counter() - started)
        except CheckInconclusive as exc:
            report.add(feature, INCONCLUSIVE, f"环境原因，未得出结论：{exc}",
                       traceback.format_exc(), time.perf_counter() - started)
        except Exception:
            report.add(feature, FAIL, "检查过程中抛异常",
                       traceback.format_exc(), time.perf_counter() - started)


def main() -> int:
    parser = argparse.ArgumentParser(description="JobForge 一键验证")
    parser.add_argument("--fast", action="store_true", help="只跑依赖预检 + 真实 HTTP 冒烟")
    parser.add_argument("--only", default=None, help="只跑名字包含该子串的检查（如 http / pytest）")
    parser.add_argument("-v", "--verbose", action="store_true", help="展开每项检查的完整输出")
    args = parser.parse_args()

    print("=" * 78, flush=True)
    print(f"JobForge verify | {PROJECT_ROOT} | python {sys.version.split()[0]}", flush=True)
    print(f"Python: {sys.executable}", flush=True)
    print("=" * 78, flush=True)

    report = Report(verbose=args.verbose)
    if not preflight(report):
        return report.summary()

    only = args.only
    workspace = Path(tempfile.mkdtemp(prefix="jobforge-verify-"))
    baseline = len(report.rows)
    try:
        if not args.fast and _selected(only, "pytest"):
            run_pytest(report)
        run_http_checks(report, only, workspace)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    # --only 拼错时一条检查都不会跑，若不拦就会被当成「全绿」——空转必须显形。
    if len(report.rows) == baseline:
        report.add(
            "selector/matched",
            FAIL,
            f"--only {only!r} 没有匹配任何检查（跑了个空），本次没有任何东西被验证",
        )

    return report.summary()


if __name__ == "__main__":
    sys.exit(main())
