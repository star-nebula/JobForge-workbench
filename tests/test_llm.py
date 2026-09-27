"""llm 功能层测试：JSON 容错解析、模板降级、三个功能函数（chat 全部 mock，不真调外部服务）。"""
import pytest

import llm
from llm import LLMError, analyze_match, greeting, parse_json, polish_resume

PDATA = {
    "name": "张三", "target_position": "前端工程师", "city": "上海",
    "expected_salary": "30-50K", "skills": ["React", "TypeScript"],
    "summary": "五年前端经验。", "experience": "负责核心页面重构。",
}
JOB = {"title": "高级前端工程师", "company": "某公司", "salary": "30-50K",
       "tags": ["React"], "jd_text": "岗位职责：\n1. 负责前端开发"}


# ---------- parse_json ----------
def test_parse_json_plain():
    assert parse_json('{"a":1}') == {"a": 1}


def test_parse_json_fenced_and_wrapped():
    assert parse_json('```json\n{"a": 2}\n```') == {"a": 2}
    assert parse_json('结果如下：{"a": 3} 以上。') == {"a": 3}


def test_parse_json_rejects_non_json():
    with pytest.raises(LLMError):
        parse_json("抱歉，我无法回答")


# ---------- greeting ----------
def test_template_greeting_uses_only_real_fields():
    out = llm._template_greeting({"skills": ["React", "Vue"], "experience": "五年开发"},
                                 {"title": "前端工程师"})
    assert "前端工程师" in out and "React" in out
    assert "某公司" not in out          # 不编造资料外的信息


def test_greeting_template_fallback_without_config():
    r = greeting(None, PDATA, JOB)
    assert r["source"] == "template" and r["greeting"]


def test_greeting_llm(monkeypatch):
    monkeypatch.setattr(llm, "chat",
                        lambda cfg, msgs, **kw: "您好！看到贵司前端岗位，我熟悉 React，期待沟通。")
    r = greeting({"base_url": "https://x/v1", "model": "m"}, PDATA, JOB)
    assert r["source"] == "llm" and "React" in r["greeting"]


def test_greeting_llm_error_falls_back_to_template(monkeypatch):
    def boom(*a, **k):
        raise LLMError("LLM 请求超时")
    monkeypatch.setattr(llm, "chat", boom)
    r = greeting({"base_url": "https://x/v1", "model": "m"}, PDATA, JOB)
    assert r["source"] == "template" and "超时" in r.get("error", "")


# ---------- analyze_match ----------
def test_analyze_match_parses_llm_json(monkeypatch):
    fake = ('{"verdict":"匹配度较高","score":78,"strengths":["React 经验对口"],'
            '"gaps":["缺 Node 深度"],"advice":["准备性能优化案例"]}')
    monkeypatch.setattr(llm, "chat", lambda *a, **k: fake)
    r = analyze_match({"model": "m"}, PDATA, JOB)
    assert r["score"] == 78
    assert r["strengths"] == ["React 经验对口"]
    assert r["model"] == "m"


def test_analyze_match_clamps_score(monkeypatch):
    monkeypatch.setattr(llm, "chat",
                        lambda *a, **k: '{"verdict":"x","score":999,"strengths":[],"gaps":[],"advice":[]}')
    assert analyze_match({"model": "m"}, PDATA, JOB)["score"] == 100


def test_analyze_match_requires_jd():
    with pytest.raises(LLMError):
        analyze_match({"model": "m"}, PDATA, {"tags": [], "jd_text": ""})


# ---------- polish_resume ----------
def test_polish_resume(monkeypatch):
    monkeypatch.setattr(llm, "chat",
                        lambda *a, **k: '{"summary":"更精炼的简介","experience":""}')
    r = polish_resume({"model": "m"}, PDATA)
    assert r["summary"] == "更精炼的简介"
    assert r["experience"] == ""        # 空字段保持空，不生成内容


# ---------- chat 基础校验 ----------
def test_chat_requires_base_and_model():
    with pytest.raises(LLMError):
        llm.chat({}, [{"role": "user", "content": "hi"}])
