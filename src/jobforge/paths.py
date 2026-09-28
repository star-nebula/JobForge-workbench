"""项目路径的唯一来源。

代码在 src/jobforge/、运行时数据在 data/，两者解耦：搬代码不会带走数据。
所有模块的数据文件路径一律从这里取，不要再各自用 __file__ 往上推算——
否则目录一动，真实数据就被静默留在旧位置，程序在新位置另起一份空库。
"""
import os

SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # <项目根>/src
PROJECT_ROOT = os.path.dirname(SRC_DIR)                                 # 项目根
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
WEB_DIR = os.path.join(PROJECT_ROOT, "web")


def data(*parts: str) -> str:
    """data/ 下的文件绝对路径（顺带确保 data/ 存在，免去首启动建目录）。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, *parts)


def module_file(dotted: str) -> str:
    """包内模块的磁盘路径，供子进程启动前做存在性检查。"""
    return os.path.join(SRC_DIR, *dotted.split(".")) + ".py"
