"""extract_resume_keywords / calc_match_score 测试。"""
from spider import calc_match_score, extract_resume_keywords


def test_extract_basic():
    text = ("李四\n求职意向：前端工程师\n期望城市：上海\n期望薪资：30-50K\n"
            "技能专长：JavaScript TypeScript React\n工作经历：\n负责开发")
    kw = extract_resume_keywords(text)
    assert kw["target_position"] == "前端工程师"
    assert kw["city"] == "上海"
    assert kw["expected_salary"] == "30-50K"
    assert "JavaScript" in kw["skills"] and "React" in kw["skills"]


def test_extract_empty_defaults():
    kw = extract_resume_keywords("")
    assert kw["city"] == "全国"
    assert kw["target_position"] == ""
    assert kw["skills"] == []


def test_match_score_four_dims():
    job = {"title": "前端开发工程师", "company": "X", "city": "上海",
           "salary": "25-35K", "tags": ["React", "TypeScript"]}
    kw = {"skills": ["React", "TypeScript", "Vue"], "target_position": "前端",
          "city": "上海", "expected_salary": "30-50K"}
    sd = calc_match_score(job, kw)
    # 技能 2/3 命中 → 40+40=80；标题完全命中+核心词 → 100；
    # 薪资重叠 5/25 → 20；城市相等 → 100；overall = 28+30+4+15 = 77
    assert sd["skills_match"] == 80
    assert sd["experience_match"] == 100
    assert sd["salary_match"] == 20
    assert sd["location_match"] == 100
    assert sd["overall"] == 77


def test_match_score_city_mismatch():
    job = {"title": "前端", "city": "上海", "salary": "", "tags": []}
    kw = {"skills": [], "target_position": "前端", "city": "北京", "expected_salary": ""}
    sd = calc_match_score(job, kw)
    assert sd["location_match"] == 30
    assert sd["skills_match"] == 30      # 无技能给保底 30


def test_match_score_reasoning_mentions_hits():
    job = {"title": "前端", "city": "上海", "salary": "30-40K", "tags": ["React"]}
    kw = {"skills": ["React"], "target_position": "前端", "city": "上海", "expected_salary": "30-40K"}
    sd = calc_match_score(job, kw)
    assert "技能命中" in sd["reasoning"]
