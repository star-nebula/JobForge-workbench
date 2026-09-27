"""server 层纯函数测试：密钥脱敏 + 模型配置选型 + 批量分析预检（db/llm mock，不碰真实库）。"""
import pytest

import llm
import server
from server import AnalyzeBatchReq, _get_llm_config, _mask_key


def test_mask_key():
    assert _mask_key("sk-abcdef123456") == "****3456"
    assert _mask_key("abc") == "****"
    assert _mask_key("") == ""


@pytest.fixture
def llm_settings(monkeypatch):
    configs = [
        {"id": "a", "name": "DeepSeek", "base_url": "https://x/v1", "model": "m1", "api_key": "k1"},
        {"id": "b", "name": "Qwen", "base_url": "https://y/v1", "model": "m2", "api_key": "k2"},
    ]
    monkeypatch.setattr(server.db, "get_app_settings",
                        lambda: {"llm": {"configs": configs, "active_id": "b"}})
    return configs


def test_get_llm_config_active_first(llm_settings):
    assert _get_llm_config()["id"] == "b"


def test_get_llm_config_explicit_id(llm_settings):
    assert _get_llm_config("a")["id"] == "a"


def test_get_llm_config_unknown_id_falls_back_to_active(llm_settings):
    assert _get_llm_config("不存在的")["id"] == "b"


def test_get_llm_config_empty(monkeypatch):
    monkeypatch.setattr(server.db, "get_app_settings", lambda: {})
    assert _get_llm_config() is None


def test_analyze_batch_preflight_rejects_bad_config(monkeypatch):
    """批量启动前必须 ping 通模型，配置不可用当场拦下（2026-09-26 假配置空转事故的回归）。"""
    monkeypatch.setattr(server, "_get_llm_config",
                        lambda mid=None: {"base_url": "https://x/v1", "model": "m"})
    monkeypatch.setattr(server.llm, "chat",
                        lambda *a, **k: (_ for _ in ()).throw(llm.LLMError("HTTP 401")))
    r = server.start_analyze_batch(AnalyzeBatchReq())
    assert r["ok"] is False and "配置不可用" in r["error"]


def test_analyze_batch_preflight_rejects_native_not_ready(monkeypatch):
    """批量启动前必须原生通道就绪（Chrome 开着 zhipin），否则非缓存岗位全失败（2026-09-27 事故回归）。"""
    monkeypatch.setattr(server, "_get_llm_config",
                        lambda mid=None: {"base_url": "https://x/v1", "model": "m"})
    monkeypatch.setattr(server.llm, "chat", lambda *a, **k: "pong")
    monkeypatch.setattr(server, "_native_channel_status",
                        lambda: {"ok": False, "chrome_found": False, "chrome_ready": False, "error": "未找到 Chrome"})
    r = server.start_analyze_batch(AnalyzeBatchReq())
    assert r["ok"] is False and "原生通道未就绪" in r["error"]


def test_analyze_batch_no_jobs_no_start(monkeypatch):
    monkeypatch.setattr(server, "_get_llm_config",
                        lambda mid=None: {"base_url": "https://x/v1", "model": "m"})
    monkeypatch.setattr(server.llm, "chat", lambda *a, **k: "pong")
    monkeypatch.setattr(server, "_native_channel_status",
                        lambda: {"ok": True, "chrome_found": True, "chrome_ready": True, "throttle_sec": 0})
    monkeypatch.setattr(server.db, "list_jobs", lambda status=None: [])
    r = server.start_analyze_batch(AnalyzeBatchReq())
    assert r["ok"] is True and r["started"] is False
