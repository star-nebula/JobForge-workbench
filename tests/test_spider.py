"""extract_resume_keywords / calc_match_score 测试。

2026-09-30 calc_match_score 重写后的语义锁定：
技能分母=岗位标签数、词归一后全等匹配；经验维去职级词做序列相似度分档；
薪资单位归一到 K；overall 真实 0-100 无下限；未知输入给 50 中性分。
"""
from jobforge import spider as sp
from jobforge.spider import (_city_code, _parse_salary_k, calc_match_score,
                             extract_resume_keywords, is_same_city)


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


# ---------- 薪资解析：单位归一 ----------

def test_parse_salary_units():
    assert _parse_salary_k("11-13K") == (11.0, 13.0)
    assert _parse_salary_k("2-3万") == (20.0, 30.0)          # 万 → ×10
    assert _parse_salary_k("8千-1.2万") == (8.0, 12.0)        # 两端各自带单位
    assert _parse_salary_k("22-35K·13薪") == (22.0, 35.0)     # 薪资后缀不干扰
    assert _parse_salary_k("1.3-2万") == (13.0, 20.0)         # 小数
    assert _parse_salary_k("35-22K") == (22.0, 35.0)          # 反向区间兜底
    assert _parse_salary_k("面议") is None
    assert _parse_salary_k("") is None


# ---------- 技能维：分母=岗位标签数，归一后全等 ----------

def test_skill_no_false_java_hit():
    """JavaScript ≠ Java：归一后全等匹配，杜绝子串前缀假命中。"""
    kw = {"skills": ["JavaScript"], "target_position": "", "city": "上海", "expected_salary": ""}
    job = {"title": "x", "city": "上海", "salary": "", "tags": ["Java"]}
    assert calc_match_score(job, kw)["skills_match"] == 0
    job["tags"] = ["JavaScript"]
    assert calc_match_score(job, kw)["skills_match"] == 100


def test_skill_normalization_hits():
    """node.js↔node、js↔javascript、vue3↔vue 归一后命中。"""
    kw = {"skills": ["Node.js", "js", "Vue3"], "target_position": "", "city": "上海", "expected_salary": ""}
    job = {"title": "x", "city": "上海", "salary": "", "tags": ["node", "javascript", "Vue"]}
    assert calc_match_score(job, kw)["skills_match"] == 100


def test_skill_denominator_is_job_tags():
    """分母=岗位技能标签数：6 个标签命中 3 个 → 50；命中 0 个 → 0（无保底）。"""
    kw = {"skills": ["React", "Vue", "TypeScript"], "target_position": "", "city": "上海", "expected_salary": ""}
    job = {"title": "x", "city": "上海", "salary": "", "tags": ["React", "Vue", "TypeScript", "Java", "Go", "Python"]}
    assert calc_match_score(job, kw)["skills_match"] == 50
    job["tags"] = ["Java", "Go"]
    assert calc_match_score(job, kw)["skills_match"] == 0


def test_skill_ignores_requirement_tags():
    """「3-5年 / 本科 / xx经验」是要求标签不是技能：不计分母，全滤掉给中性 50。"""
    kw = {"skills": ["React", "JavaScript"], "target_position": "", "city": "上海", "expected_salary": ""}
    job = {"title": "x", "city": "上海", "salary": "", "tags": ["3-5年", "本科"]}
    assert calc_match_score(job, kw)["skills_match"] == 50
    job["tags"] = ["JavaScript", "React", "前端开发经验", "计算机/软件工程相关专业"]
    assert calc_match_score(job, kw)["skills_match"] == 100   # 分母只剩 2 个真技能标签


# ---------- 经验维：去职级词 + 相似度分档 ----------

def test_experience_direction():
    """方向修正：真前端的标题拿高分，蹭「高级」二字的无关岗拿低分。"""
    kw = {"skills": [], "target_position": "高级前端工程师", "city": "上海", "expected_salary": ""}
    good = {"title": "前端开发工程师", "city": "上海", "salary": "", "tags": []}
    bait = {"title": "AI智能体高级应用工程师", "city": "上海", "salary": "", "tags": []}
    e_good = calc_match_score(good, kw)["experience_match"]
    e_bait = calc_match_score(bait, kw)["experience_match"]
    assert e_good == 100                       # 相似度约 0.83 ≥ 0.8
    assert e_bait <= 35                        # 只有「工程师」公共块
    assert e_good > e_bait


def test_experience_seniority_word_ignored():
    """「高级前端开发工程师」也应高分：职级词不参与比对，不因缺「高级」被扣。"""
    kw = {"skills": [], "target_position": "高级前端工程师", "city": "上海", "expected_salary": ""}
    job = {"title": "高级前端开发工程师", "city": "上海", "salary": "", "tags": []}
    assert calc_match_score(job, kw)["experience_match"] >= 85


def test_experience_domain_word_matches_dev_titles():
    """「Web前端开发」「前端工程师」都该高分：领域词「前端」命中即算，不被
    「工程师 vs 开发」的字面差异拖进低分档。"""
    kw = {"skills": [], "target_position": "高级前端工程师", "city": "上海", "expected_salary": ""}
    for title in ("Web前端开发(J14909)", "前端工程师", "web 前端工程师"):
        job = {"title": title, "city": "上海", "salary": "", "tags": []}
        assert calc_match_score(job, kw)["experience_match"] == 100, title
    # 裸「前端」标题：领域词命中但无角色词佐证 → 80
    job = {"title": "前端", "city": "上海", "salary": "", "tags": []}
    assert calc_match_score(job, kw)["experience_match"] == 80


# ---------- 薪资/城市/overall ----------

def test_match_score_four_dims():
    job = {"title": "前端开发工程师", "company": "X", "city": "上海",
           "salary": "25-35K", "tags": ["React", "TypeScript"]}
    kw = {"skills": ["React", "TypeScript", "Vue"], "target_position": "前端",
          "city": "上海", "expected_salary": "30-50K"}
    sd = calc_match_score(job, kw)
    # 技能：2 标签全命中 → 100；经验：意向核心在标题 → 100；薪资重叠 5/25 → 20；城市 → 100
    assert sd["skills_match"] == 100
    assert sd["experience_match"] == 100
    assert sd["salary_match"] == 20
    assert sd["location_match"] == 100
    assert sd["overall"] == 84                 # 35+30+4+15，无下限夹逼


def test_match_score_unknown_is_neutral():
    """简历要素缺失＝无法判断 → 各维 50 中性分，不再给保底低分。"""
    job = {"title": "前端", "city": "上海", "salary": "", "tags": []}
    kw = {"skills": [], "target_position": "", "city": "北京", "expected_salary": ""}
    sd = calc_match_score(job, kw)
    assert sd["skills_match"] == 50
    assert sd["experience_match"] == 50
    assert sd["salary_match"] == 50


def test_match_score_city_mismatch():
    job = {"title": "前端", "city": "上海", "salary": "", "tags": []}
    kw = {"skills": ["React"], "target_position": "前端", "city": "北京", "expected_salary": ""}
    sd = calc_match_score(job, kw)
    assert sd["location_match"] == 30


def test_match_score_real_spread():
    """无关岗位 overall 应落到明显低位（<40），不再被下限托到 40+。"""
    kw = {"skills": ["React", "Vue"], "target_position": "前端工程师",
          "city": "上海", "expected_salary": "30-50K"}
    job = {"title": "高级机械设计工程师", "city": "上海", "salary": "8-10K",
           "tags": ["机械设计", "装箱码垛", "CAD"]}
    sd = calc_match_score(job, kw)
    assert sd["overall"] < 40
    assert sd["skills_match"] == 0


def test_match_score_reasoning_mentions_hits():
    job = {"title": "前端", "city": "上海", "salary": "30-40K", "tags": ["React"]}
    kw = {"skills": ["React"], "target_position": "前端", "city": "上海", "expected_salary": "30-40K"}
    sd = calc_match_score(job, kw)
    assert "技能命中" in sd["reasoning"]


# ---------- 城市编码解析 + L0 同城判定 ----------

def test_city_code_normalizes_area_and_suffix():
    assert _city_code("上海") == "101020100"
    assert _city_code("上海市") == "101020100"
    assert _city_code("上海·闵行区") == "101020100"
    assert _city_code("") == "100010000"          # 未填 = 不限城市
    assert _city_code("全国") == "100010000"
    assert _city_code("不限") == "100010000"


def test_city_code_unknown_is_none_not_nationwide():
    """认不出的城市必须返回 None。旧实现兜底成全国码，profile 没有城市字段时
    静默按全国抓了三批（67 个异地岗入库，2026-09-30 核查 first_seen_at 批次）。"""
    assert _city_code("宁波") is None
    assert _city_code("纽约") is None


def test_crawl_boss_refuses_unknown_city(monkeypatch):
    """未知城市当场报错，且绝不发起抓取（否则就是静默按全国搜）。"""
    def boom(*a, **k):
        raise AssertionError("未知城市不该发起原生抓取")
    monkeypatch.setattr(sp.fetch_jd_native, "crawl_boss_native", boom)
    r = sp.crawl_boss("前端", "宁波")
    assert r["source"] == "error" and "没有 BOSS 城市编码" in r["error"]


def test_crawl_boss_passes_normalized_city_code(monkeypatch):
    seen = {}
    monkeypatch.setattr(sp.fetch_jd_native, "crawl_boss_native",
                        lambda q, code, page: seen.update(code=code) or {"zpData": {"jobList": []}})
    r = sp.crawl_boss("前端", "上海·闵行区")
    assert seen["code"] == "101020100" and r["source"] == "real"


def test_is_same_city():
    assert is_same_city("上海·闵行区", "上海") is True
    assert is_same_city("上海市", "上海") is True
    assert is_same_city("北京·朝阳区", "上海") is False
    assert is_same_city("", "上海") is True         # 抓取没给城市 ≠ 不匹配
    assert is_same_city("北京", "") is True         # 没填期望城市就不设门槛
    assert is_same_city("北京", "全国") is True
