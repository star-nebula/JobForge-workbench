"""验证专用启动垫片：用真 uvicorn 起产品 app，但把库指到临时文件。

为什么需要它：`jobforge.db.DB_FILE` 在 import 时就算死成 `data/jobs.db`，而
真实库里有用户自己的求职数据。HTTP 冒烟必须发会写库的真实 POST，所以只能
在 import 产品代码之后、lifespan 建表之前把 `db.DB_FILE` 换掉——这一步
等价于 tests/conftest.py 里 autouse 的 isolated_db，只是搬到了跨进程场景。

这里不复制、不改造产品代码：起的是 `jobforge.server.app` 本体，路由、
中间件、异常兜底、SQLite 持久化全是真路径。本文件属于验证脚手架，
产品代码不 import 它。
"""
import argparse
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="JobForge verify HTTP shim")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--db", required=True, help="临时 SQLite 路径（绝不指向 data/jobs.db）")
    args = parser.parse_args()

    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    src_dir = os.path.join(project_root, "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    from jobforge import db

    db_path = os.path.abspath(args.db)
    # 保命闸：真实库里有用户的求职数据，指错一次就可能被冒烟写坏。
    # 仓库 data/ 一律拒绝，其余（临时目录）放行。
    real_data_dir = os.path.join(os.path.abspath(project_root), "data") + os.sep
    if db_path.lower().startswith(real_data_dir.lower()):
        print(f"[verify-shim] 拒绝把库指到真实数据目录：{db_path}", file=sys.stderr)
        return 3
    db.DB_FILE = db_path

    # 与 server.__main__ 保持一致：真实入口在 uvicorn.run 前把实际端口写进
    # JOBFORGE_PORT，/api/whoami 与重启判定都靠它自报端口。漏了这步，冒烟里
    # whoami 会报 8080 默认值，掩盖「端口自报错了」这类真问题。
    os.environ["JOBFORGE_PORT"] = str(args.port)

    import uvicorn

    from jobforge import server

    print(f"[verify-shim] app={server.APP_VERSION} db={db.DB_FILE} port={args.port}", flush=True)
    uvicorn.run(server.app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
