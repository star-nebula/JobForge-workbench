"""compute_profile_score 规则引擎测试（完整度 62 + 质量 38 = 100）。"""
from jobforge.profile_score import compute_profile_score

# 除 resume_text 外全真实合格的资料：62 + 技能丰富度 10 + 量化 18 = 90
GOOD = {
    "name": "张三",
    "phone": "13800138000",
    "email": "zhangsan@example.com",
    "target_position": "高级前端工程师",
    "city": "上海",
    "expected_salary": "30-50K",
    "skills": ["JavaScript", "TypeScript", "React", "Vue", "Node.js", "性能优化"],
    "summary": "五年前端开发经验，主导多个中大型 C 端项目的架构设计与性能优化，熟悉工程化体系建设与团队协作。",
    "experience": "负责核心页面重构，首屏加载提升 40%；搭建组件库，需求交付效率提升 30%；优化构建流程，构建时间降低 60%；带 3 人小组完成年度目标。",
    "education": "某某大学 计算机本科",
}


def test_empty_profile():
    r = compute_profile_score({})
    assert r["score"] == 0
    assert r["level"] == "待完善"
    assert all(not c["ok"] for c in r["checks"])
    assert len(r["checks"]) == 13


def test_good_profile_scores_90():
    r = compute_profile_score(GOOD)
    assert r["score"] == 90
    assert r["level"] == "优秀"
    assert r["score"] == sum(c["got"] for c in r["checks"])


def test_full_profile_caps_at_100():
    r = compute_profile_score({**GOOD, "resume_text": "简历原文"})
    assert r["score"] == 100


def test_partial_profile():
    r = compute_profile_score({"name": "张三", "phone": "13800138000", "skills": ["React"]})
    # 姓名 6 + 电话 6 + 技能(1 项不足 3) 6 = 18
    assert r["score"] == 18
    assert r["level"] == "待完善"
    by_k = {c["k"]: c for c in r["checks"]}
    assert by_k["name"]["ok"] and by_k["phone"]["ok"]
    assert not by_k["email"]["ok"]
    assert by_k["skills"]["got"] == 6


def test_invalid_phone_fails():
    r = compute_profile_score({**GOOD, "phone": "123"})
    by_k = {c["k"]: c for c in r["checks"]}
    assert not by_k["phone"]["ok"]


def test_quantified_needs_three_hits():
    weak = {**GOOD, "experience": "负责页面开发与日常迭代。"}
    r = compute_profile_score(weak)
    by_k = {c["k"]: c for c in r["checks"]}
    assert not by_k["quantified"]["ok"]
    mid = {**GOOD, "experience": "性能提升 20%，交付效率提升 30%。"}
    by_k = {c["k"]: c for c in compute_profile_score(mid)["checks"]}
    assert by_k["quantified"]["ok"]


def test_phone_message_distinguishes_prefix_from_length():
    """文案分两支：11 位但 12x 号段不能再报「不是 11 位」——报错要能指向真因。"""
    data = {"name": "张三", "phone": "12345678900"}
    checks = {c["k"]: c for c in compute_profile_score(data)["checks"]}
    assert checks["phone"]["ok"] is False
    assert "号段不对" in checks["phone"]["msg"] and "不是 11 位" not in checks["phone"]["msg"]

    data2 = {"name": "张三", "phone": "1381234567"}   # 10 位
    checks2 = {c["k"]: c for c in compute_profile_score(data2)["checks"]}
    assert "不是 11 位" in checks2["phone"]["msg"]

    data3 = {"name": "张三", "phone": "13812345678"}
    checks3 = {c["k"]: c for c in compute_profile_score(data3)["checks"]}
    assert checks3["phone"]["ok"] is True
