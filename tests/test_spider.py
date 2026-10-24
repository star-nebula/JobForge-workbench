"""extract_resume_keywords / 城市判定 / crawl_boss 测试。

语义锁定：Markdown 简历归一后与纯文本同链路；城市编码认不出返回 None（不兜底全国）；
L0 同城判定为纯本地二值，不参与评分（匹配判断只有 AI 分析一条链路）。
"""
from jobforge import spider as sp
from jobforge.spider import (_city_code, extract_resume_keywords, is_same_city,
                             split_cities)


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


# ---------- Markdown 简历解析（2026-10-08：归一化后与纯文本同链路） ----------

def test_extract_markdown_full():
    """Markdown 简历：井号标题/加粗/列表/尖括号邮箱/两列基本信息。"""
    text = (
        "# 基本信息\n\n"
        "姓名：刘昊晴    出生年月：2003年5月\n"
        "性别：男    求职意向：AI应用开发工程师\n"
        "电话：13392786414    邮箱：<liu@example.com>\n"
        "个人博客：https://example.com/vault/\n\n"
        "# 教育经历\n\n2021~2025 岭南师范学院 人工智能\n\n"
        "# 专业技能\n\n"
        "**大模型与RAG、多Agent开发：**\n\n"
        "- 深入理解 Transformer 架构，掌握 LLM 底层原理，具备 DeepSeek、Qwen 等大模型调用经验\n"
        "- 熟悉LoRA、P-Tuning等微调方案，熟练 LangChain框架\n"
        "- 依托 Function Calling 与 ReAct 自主推理，掌握 MCP、A2A 协议\n\n"
        "# 工作经历\n\n### **2026.07 ~ 2026.08 | 品质生活 | AI技术负责人**\n\n从0搭建AI部门。\n\n"
        "# 项目经历\n\n### 选品简报系统 2025.10 ~ 2026.05\n\n基于LangGraph+DeepSeek。\n"
    )
    kw = extract_resume_keywords(text)
    assert kw["target_position"] == "AI应用开发工程师"
    # sections 五桶正确分段（井号标题剥掉后仍可识别）
    assert "岭南师范学院" in kw["sections"]["education"]
    assert "AI技术负责人" in kw["sections"]["experience"]
    assert "LangGraph" in kw["sections"]["projects"]
    # 技能表无动词前缀/噪声/装饰符
    skills = kw["skills"]
    for s in ("Transformer", "LLM", "DeepSeek", "Qwen", "LoRA", "LangChain",
              "FunctionCalling", "ReAct", "MCP", "A2A"):
        assert s in skills, f"缺少 {s}: {skills}"
    assert not any(s.startswith(("熟悉", "掌握", "深入")) for s in skills)
    assert "**" not in " ".join(skills)


def test_extract_markdown_two_column_basics():
    """两列基本信息：同行「邮箱：xx 邮箱：yy」在已知标签处拆行。"""
    text = ("姓名：张三    电话：13312345678    邮箱：<a@b.com>\n"
            "求职意向：后端开发\n\n专业技能\nPython, Go, Redis\n")
    kw = extract_resume_keywords(text)
    assert "电话：13312345678" in kw["sections"]["basics"]
    assert "邮箱：a@b.com" in kw["sections"]["basics"]
    assert kw["target_position"] == "后端开发"


def test_extract_skill_multiword_merge_whitelist():
    """多词技术名只按白名单合并：Function Calling 合并；空格分隔的独立技能不串词。"""
    kw = extract_resume_keywords("技能专长：Function Calling ReAct\n")
    assert "FunctionCalling" in kw["skills"] and "ReAct" in kw["skills"]
    kw2 = extract_resume_keywords("技能专长：JavaScript TypeScript React\n")
    assert kw2["skills"] == ["JavaScript", "TypeScript", "React"]


def test_extract_plain_text_still_works():
    """纯文本简历回归：归一化对纯文本幂等，原解析语义不变。"""
    text = ("李四\n求职意向：前端工程师\n期望城市：上海\n期望薪资：30-50K\n"
            "技能专长：JavaScript TypeScript React\n工作经历：\n负责开发")
    kw = extract_resume_keywords(text)
    assert kw["target_position"] == "前端工程师"
    assert kw["city"] == "上海"
    assert kw["expected_salary"] == "30-50K"
    assert "JavaScript" in kw["skills"] and "React" in kw["skills"]


# ---------- 多城市支持（2026-10-08：城市串拆分 + L0 门槛多城命中） ----------

def test_split_cities():
    """城市串拆分：顿号/逗号/斜杠/「市」后缀/去重保序。"""
    assert split_cities("广州、深圳") == ["广州", "深圳"]
    assert split_cities("广州，深圳/杭州") == ["广州", "深圳", "杭州"]
    assert split_cities("广州市") == ["广州"]
    assert split_cities("") == []
    assert split_cities("全国") == ["全国"]
    assert split_cities("深圳、深圳") == ["深圳"]


def test_is_same_city_multi():
    """L0 门槛多城语义：任一命中即 True；单城与全国行为不变。"""
    assert is_same_city("深圳·南山区", "广州、深圳") is True
    assert is_same_city("广州", "广州、深圳") is True
    assert is_same_city("上海·浦东新区", "广州、深圳") is False
    assert is_same_city("", "广州、深圳") is True
    assert is_same_city("深圳", "全国") is True
    assert is_same_city("深圳", "") is True


def test_extract_resume_multi_city():
    """简历解析期望城市支持多城串（原正则遇顿号截断只取到第一城）。"""
    kw = extract_resume_keywords("期望城市：广州、深圳\n求职意向：AI应用开发工程师\n")
    assert kw["city"] == "广州、深圳"


def test_crawl_boss_multi_city_error_aggregates():
    """crawl_boss 多城：城市码全认识时逐城抓取；mock 掉原生通道验证合并与错误聚合。"""
    from jobforge import spider as sp

    calls = []

    def fake_native(query, city_code, page=1):
        calls.append(city_code)
        if city_code == sp.BOSS_CITY_CODES["深圳"]:
            raise sp.fetch_jd_native.NativeError("风控")
        return {"zpData": {"jobList": [
            {"encryptJobId": f"job-{city_code}-1", "jobName": "AI工程师",
             "brandName": "A公司", "cityName": "广州", "skills": ["Python"]},
            {"encryptJobId": "job-dup", "jobName": "重复岗",
             "brandName": "B公司", "cityName": "广州", "skills": []},
        ]}}

    orig = sp.fetch_jd_native.crawl_boss_native
    sp.fetch_jd_native.crawl_boss_native = fake_native
    try:
        r = sp.crawl_boss("AI", "广州、深圳")
        assert calls == [sp.BOSS_CITY_CODES["广州"], sp.BOSS_CITY_CODES["深圳"]]
        assert r["source"] == "real"
        # 深圳失败不拖垮广州；job-dup 只出现一次
        assert len(r["jobs"]) == 2
        assert sum(1 for j in r["jobs"] if j["job_id"] == "job-dup") == 1
        assert r["error"] and "深圳" in r["error"]
        # 全部失败 → source=error
        calls.clear()
        r2 = sp.crawl_boss("AI", "深圳")
        assert r2["source"] == "error" and "风控" in r2["error"]
    finally:
        sp.fetch_jd_native.crawl_boss_native = orig


def test_crawl_boss_multi_city_unknown_rejected():
    """多城串中有不认识的城市 → 显式报错，绝不静默按全国搜（沿用单城语义）。"""
    from jobforge import spider as sp
    r = sp.crawl_boss("AI", "广州、宜宾")
    assert r["source"] == "error" and "宜宾" in r["error"]

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
