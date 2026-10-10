"""让 tests 直接 import 包（src/jobforge），无需先设 PYTHONPATH。

autouse 的 isolated_db 把 db.DB_FILE 指到每个测试独享的 tmp 空库——
没有任何测试该碰开发者本机的真实 data/jobs.db（此前一批测试依赖
开发者机器上恰好存在的 profile 数据才能通过，换台干净电脑就挂）。
isolated_gate 同理把抓取闸门文件指到 tmp 并复位：真实 data/fetch_gate.json
里残留的 stopped=true 会让 scrape_from_resume 静默早退，测试假死成「0 条」。
个别测试要特定 profile/数据时，照旧在自己的 fixture/用例里再
monkeypatch（本 fixture 先执行，后设的覆盖先设的）。
"""
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import pytest

from jobforge import db, fetch_gate


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """每个测试独享空库：DB_FILE 指向 tmp 并建好表，防测试读写真实数据。"""
    monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "jobs.db"))
    db.init_db()
    yield


@pytest.fixture(autouse=True)
def isolated_gate(tmp_path, monkeypatch):
    """每个测试独享闸门文件并复位，防测试读到开发机上的真实暂停/停止信号。"""
    monkeypatch.setenv("JOBFORGE_GATE_FILE", str(tmp_path / "fetch_gate.json"))
    fetch_gate.clear()
    yield
