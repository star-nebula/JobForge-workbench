"""llm 功能层测试：JSON 容错解析、模板降级、三个功能函数、L1 批量粗筛（chat 全部 mock，不真调外部服务）。"""
import json
import re

import pytest

from jobforge import llm
from jobforge.llm import LLMError, analyze_match, greeting, parse_json, polish_resume

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


def test_analyze_match_prompt_keeps_structured_fields(monkeypatch):
    """JD 不完整时的兜底：标题/薪资/城市/标签必须进 prompt，
    且 system 明确要求用结构化信息补足、不得因 JD 未提及而臆断缺失。"""
    captured = {}

    def fake_chat(cfg, msgs, **kw):
        captured["system"] = msgs[0]["content"]
        captured["user"] = msgs[1]["content"]
        return '{"verdict":"x","score":50,"strengths":[],"gaps":[],"advice":[]}'

    monkeypatch.setattr(llm, "chat", fake_chat)
    job = {"title": "前端工程师", "company": "某公司", "salary": "",
           "city": "", "tags": ["React"], "jd_text": "残缺的 JD"}
    analyze_match({"model": "m"}, PDATA, job)
    assert "【薪资】未标注" in captured["user"] and "【城市】未标注" in captured["user"]
    assert "【岗位标签】React" in captured["user"]
    assert "可能不完整" in captured["user"] and "残缺的 JD" in captured["user"]
    assert "必须纳入评分" in captured["system"]
    assert "不得仅因 JD 没写就断定岗位不具备该条件" in captured["system"]


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


# ---------- L1 打包粗筛 triage ----------
def _tjob(no: int, **over):
    j = {"platform": "boss", "job_id": f"j{no}", "title": f"前端工程师{no}",
         "company": f"公司{no}", "salary": "25-40K", "city": "上海",
         "experience": "3-5年", "education": "本科", "tags": ["React", "Vue"],
         "jd_text": "岗位职责：绝不外传的机密内容", "match_score": 71}
    j.update(over)
    return j


def test_triage_prompt_uses_only_structured_fields(monkeypatch):
    """粗筛阶段既没有 JD，也绝不能看本地初筛分——把 match_score 喂进去模型只会照抄，
    之后拿「粗筛 vs 精配一致性」验证粗筛就变成自证。"""
    captured = {}

    def fake_chat(cfg, msgs, **kw):
        captured["system"] = msgs[0]["content"]
        captured["user"] = msgs[1]["content"]
        return '{"jobs":[{"no":1,"keep":true,"reason":"方向对口"}]}'

    monkeypatch.setattr(llm, "chat", fake_chat)
    llm.triage_chunk({"model": "m"}, PDATA, [_tjob(1)])
    u = captured["user"]
    assert "前端工程师1" in u and "25-40K" in u and "上海" in u and "React、Vue" in u
    assert "求职方向 前端工程师" in u and "【期望】城市 上海" in u
    assert "绝不外传的机密内容" not in u          # 不喂 JD
    assert "71" not in u                          # 不喂本地初筛分
    assert "召回优先" in captured["system"] and "一律 keep=true" in captured["system"]
    assert "职级词不构成门槛依据" in captured["system"]
    # 2026-09-30 用户裁定「薪资不设硬下限」：低于期望的岗位照样要精配，薪资只能进上下文不能进淘汰依据
    assert "薪资一律不作为淘汰依据" in captured["system"]


def test_triage_chunk_maps_index_and_clamps_reason(monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: (
        '{"jobs":[{"no":1,"keep":true,"reason":"' + "长" * 200 + '"},'
        '{"no":2,"keep":false,"reason":"岗位性质无关"}]}'))
    got = llm.triage_chunk({"model": "m"}, PDATA, [_tjob(1), _tjob(2)])
    assert got[1]["keep"] is True and len(got[1]["reason"]) == 80
    assert got[2]["keep"] is False and got[2]["reason"] == "岗位性质无关"


def test_triage_as_bool_accepts_string_false(monkeypatch):
    """模型常把布尔写成字符串，bool("false") 是 True——keep 不能直接取 bool。"""
    monkeypatch.setattr(llm, "chat", lambda *a, **k: (
        '{"jobs":[{"no":1,"keep":"false","reason":"x"},{"no":2,"keep":"true","reason":"y"}]}'))
    got = llm.triage_chunk({"model": "m"}, PDATA, [_tjob(1), _tjob(2)])
    assert got[1]["keep"] is False and got[2]["keep"] is True


def test_triage_jobs_chunks_and_remaps_index_to_job_id(monkeypatch):
    """每块的编号都从 1 起，跨块必须换回各自的 job_id：块首判 drop，drop 的岗位
    应正好是各块第一个（j1/j8/j15/j22/j29/j36/j43），错位一个就算映射反了。"""
    def fake_chat(cfg, msgs, **kw):
        user = msgs[1]["content"]
        nos = [int(m) for m in re.findall(r"^(\d+)\. ", user, re.M)]
        return json.dumps({"jobs": [{"no": n, "keep": n != 1, "reason": f"r{n}"} for n in nos]})

    monkeypatch.setattr(llm, "chat", fake_chat)
    jobs = [_tjob(i) for i in range(1, 46)]
    out = llm.triage_jobs({"model": "m"}, PDATA, jobs, chunk_size=7)
    assert [o["job_id"] for o in out] == [f"j{i}" for i in range(1, 46)]
    dropped = {o["job_id"] for o in out if not o["keep"]}
    assert dropped == {"j1", "j8", "j15", "j22", "j29", "j36", "j43"}
    assert all(o["answered"] for o in out)


def test_triage_jobs_conservative_when_model_skips_index(monkeypatch):
    """模型漏答某个编号不能丢岗：保守 keep=true 并置 answered=False，让上层数出没答的。"""
    monkeypatch.setattr(llm, "chat",
                        lambda *a, **k: '{"jobs":[{"no":1,"keep":false,"reason":"x"}]}')
    out = llm.triage_jobs({"model": "m"}, PDATA, [_tjob(1), _tjob(2)], chunk_size=5)
    assert out[0]["keep"] is False and out[0]["answered"] is True
    assert out[1]["keep"] is True and out[1]["answered"] is False
    assert "保守保留" in out[1]["reason"]


def test_triage_chunk_ignores_out_of_range_index(monkeypatch):
    """越界 / 非数字编号不得污染映射，剩下的岗位一律走保守保留。"""
    monkeypatch.setattr(llm, "chat", lambda *a, **k: (
        '{"jobs":[{"no":99,"keep":false,"reason":"越界"},{"no":"abc","keep":false}]}'))
    out = llm.triage_jobs({"model": "m"}, PDATA, [_tjob(1)], chunk_size=5)
    assert out[0]["keep"] is True and out[0]["answered"] is False


def test_triage_jobs_empty_makes_no_call(monkeypatch):
    called = []
    monkeypatch.setattr(llm, "chat", lambda *a, **k: called.append(1) or "{}")
    assert llm.triage_jobs({"model": "m"}, PDATA, []) == []
    assert called == []


def test_triage_chunk_raises_on_garbage(monkeypatch):
    """模型整块没返回 JSON 要抛 LLMError，由端点回给用户，不静默当成全部 drop。"""
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "抱歉，我无法判断")
    with pytest.raises(LLMError):
        llm.triage_chunk({"model": "m"}, PDATA, [_tjob(1)])
