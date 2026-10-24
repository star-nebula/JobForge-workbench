"""一次性迁移：删掉 seen_jobs 的本地规则评分两列（match_score / score_detail）。

由来（2026-10-13 用户裁定）：本地 4 维规则分整族下线，匹配判断只剩 AI 分析一条链路。
代码侧已不再读写这两列，库里的列留着只会让「下一个 AI」误以为它们还活着。

不进 db.init_db()：那是每个临时库测试夹具都会跑的启动路径，不可逆的删列不该在那里
静默发生。本脚本手工执行一次即可，幂等——列已不存在时直接退出。

用法（先停服）：
    venv\\Scripts\\python.exe tools\\migrate_drop_local_score.py
    # 先在副本上演练，确认真的库被动之前行为已经对
    venv\\Scripts\\python.exe tools\\migrate_drop_local_score.py --db data\\jobs.copy.db
"""
import argparse
import os
import socket
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from jobforge import paths  # noqa: E402

import sqlite3  # noqa: E402

DROP_COLUMNS = ("match_score", "score_detail")


def _server_up(port=8080):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _columns(c):
    return [r[1] for r in c.execute("PRAGMA table_info(seen_jobs)").fetchall()]


def _snapshot(c):
    return c.execute("SELECT COUNT(*) FROM seen_jobs").fetchone()[0], \
        c.execute("SELECT COUNT(*) FROM seen_jobs WHERE llm_analysis IS NOT NULL").fetchone()[0]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # 中文 Windows 默认 cp936，输出会乱码
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="删除 seen_jobs 的本地规则评分两列（不可回退）")
    ap.add_argument("--db", default=None, help="目标库路径，默认 data/jobs.db；演练时指向副本")
    db_path = ap.parse_args().db or paths.data("jobs.db")
    if not os.path.exists(db_path):
        print(f"找不到库：{db_path}")
        return 1
    if _server_up():
        print("127.0.0.1:8080 上还有实例在跑——先停服再迁移（并发改结构会撞锁）。")
        return 1

    conn = sqlite3.connect(db_path)
    with conn:
        cols = _columns(conn)
        todo = [col for col in DROP_COLUMNS if col in cols]
        before = _snapshot(conn)
        print(f"迁移前 列={cols}\n        行数/已分析={before[0]}/{before[1]}")
        if not todo:
            print("两列均已不存在，无需迁移（幂等退出）。")
            return 0

        # 删列不可回退：动手前先给这份库做一个带标记的热备，回滚只需覆盖回去
        bdir = os.path.join(paths.DATA_DIR, "backups")
        os.makedirs(bdir, exist_ok=True)
        stamp = time.strftime("backup-%Y%m%d-%H%M%S") + "-pre-drop-local-score"
        dest = os.path.join(bdir, stamp, "jobs.db")
        os.makedirs(os.path.dirname(dest), exist_ok=True)   # stamp 那一层不建，connect 会静默失败在事务里
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
        print(f"已热备到 data/backups/{stamp}/jobs.db")

        for col in todo:
            conn.execute(f"ALTER TABLE seen_jobs DROP COLUMN {col}")
            print(f"已删除列 {col}")

        after_cols = _columns(conn)
        after = _snapshot(conn)
        print(f"迁移后 列={after_cols}\n        行数/已分析={after[0]}/{after[1]}")
        if after != before:
            print("!! 行数发生变化，请立即用上面的备份覆盖回滚并检查脚本")
            return 1
        left = [col for col in DROP_COLUMNS if col in after_cols]
        print("迁移完成" if not left else f"!! 仍未删除：{left}")
        return 0 if not left else 1


if __name__ == "__main__":
    sys.exit(main())
