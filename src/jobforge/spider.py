"""
招聘平台爬虫模块
- 目前仅支持 Boss 直聘（2026-09-22 用户决定：只用 BOSS，删 zhaopin/51job 代码）
- 行为契约：crawl() 固定返回 {jobs: [...], source: 'real'|'error', platform: str}
- 真实爬取失败 / 无登录态 / 网络不可达时，返回 source='error' 与 error 字段
- 单页 page 参数（不做区间循环），与前端入参保持一致
- 2026-09-26 岗位列表抓取迁移原生通道：crawl_boss 委托 fetch_jd_native
  view-source 同源读 joblist.json（浏览器真实登录态），旧 requests+静态
  cookie 直调已删除（stoken 失效 + 直调风控风险）
"""
import difflib
import json
import os
import random
import re
import time
import hashlib
from typing import List, Dict, Any, Optional

from jobforge import fetch_gate, fetch_jd_native, paths

# ---------- 通用配置 ----------
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 城市编码（Boss 直聘）
BOSS_CITY_CODES = {
    "北京": "101010100", "上海": "101020100", "广州": "101280100",
    "深圳": "101280600", "杭州": "101210100", "南京": "101190100",
    "成都": "101270100", "武汉": "101200100", "西安": "101110100",
    "苏州": "101190400", "全国": "100010000",
}


def _city_name(value: str) -> str:
    """取城市名前段并去掉「市」后缀：「上海·闵行区」→「上海」、「杭州市」→「杭州」。"""
    name = re.split(r"[·\s\-—]", str(value or "").strip())[0]
    return name[:-1] if name.endswith("市") else name


def _city_code(city: str) -> Optional[str]:
    """城市名 → BOSS 城市编码；认不出来返回 None，由调用方显式报错。

    空值与「全国/不限」→ 全国码。**不认识的名称绝不兜底成全国**：这里原来是
    `get(city, "100010000")`，profile 还没有城市字段时静默按全国抓，09-22/09-25
    三批（0 上海 / 67 异地）就是这么进库的（2026-09-30 核查 first_seen_at 批次）。
    """
    name = _city_name(city)
    if not name or name in ("全国", "不限", "全部"):
        return BOSS_CITY_CODES["全国"]
    return BOSS_CITY_CODES.get(name)


def is_same_city(job_city: str, expected_city: str) -> bool:
    """L0 硬门槛：岗位城市是否属于期望城市（纯本地二值判定，不参与评分）。

    期望城市为空/「全国/不限」→ 不限城市，一律 True。
    岗位城市为空（抓取未给）→ True：不知道 ≠ 不匹配，与 calc_match_score 的中性分同口径。
    """
    exp = _city_name(expected_city)
    if not exp or exp in ("全国", "不限", "全部"):
        return True
    job = _city_name(job_city)
    return True if not job else job == exp

# Cookie 文件路径（data/cookies.json），由浏览器登录后写入
COOKIES_FILE = paths.data("cookies.json")


def _load_cookies(platform: str) -> Optional[Dict[str, str]]:
    """从 cookies.json 读对应平台的登录态 cookie。返回 dict 或 None。

    cookies.json 结构：{"boss": {"name1": "val1", ...}}
    目前只用 boss；其他平台 cookie 字段若存在会被忽略。
    """
    if not os.path.exists(COOKIES_FILE):
        return None
    try:
        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
            all_cookies = json.load(f)
        return all_cookies.get(platform) or None
    except Exception:
        return None


# ============================================================
#  Boss 直聘
# ============================================================
def crawl_boss(query: str, city: str = "全国", page: int = 1,
               use_mock: bool = False) -> Dict[str, Any]:
    """Boss 直聘岗位搜索（原生通道，2026-09-26 迁移）。
    通过 fetch_jd_native.crawl_boss_native 以 view-source 同源读 joblist.json，
    浏览器自带真实登录态（含动态 __zp_stoken__）。旧 requests+静态 cookie 直调
    已删除：静态 stoken 几分钟即失效（code=37），直调接口有风控风险。
    前提：桌面 Chrome 已打开并登录 zhipin.com；抓取接管键鼠约 8~15 秒，
    与 JD 抓取共享 18~35 秒节流。use_mock 参数已废弃（保留兼容签名）。
    """
    city_code = _city_code(city)
    if not city_code:
        supported = "、".join(c for c in BOSS_CITY_CODES if c != "全国")
        return {"jobs": [], "source": "error", "platform": "boss",
                "error": f"城市「{city}」没有 BOSS 城市编码（可选：{supported}、全国；"
                         f"或在个人资料里改城市）。已中止抓取，不会静默按全国搜。"}
    try:
        data = fetch_jd_native.crawl_boss_native(query, city_code, page)
    except fetch_gate.Stopped:
        # 用户点了「结束」：不是失败，向上抛出由 server 转成 stopped 结果
        raise
    except fetch_jd_native.NativeError as e:
        return {"jobs": [], "source": "error", "platform": "boss", "error": str(e)}
    except Exception as e:
        return {"jobs": [], "source": "error", "platform": "boss",
                "error": f"原生通道异常: {type(e).__name__}: {e}"}
    raw = data.get("zpData", {}).get("jobList", []) or []
    jobs = [_normalize_boss(j) for j in raw]
    return {"jobs": jobs, "source": "real", "platform": "boss"}


def _normalize_boss(item: Dict) -> Dict:
    """把 Boss 原始字段标准化为统一结构。
    BOSS 用 encryptJobId 作为对外 ID（真实 jobId 不暴露），用作去重主键足够稳定。
    """
    brand = item.get("brandName") or item.get("brandName_", "")
    city_name = item.get("cityName", "")
    area = item.get("areaDistrict", "") or ""
    eid = item.get("encryptJobId") or item.get("jobId") or ""
    return {
        "platform": "boss",
        "job_id": str(eid),
        "title": item.get("jobName", ""),
        "company": brand,
        "company_logo": item.get("brandLogo", ""),
        "salary": item.get("salaryDesc", ""),
        "city": f"{city_name}·{area}".strip("·"),
        "experience": item.get("jobExperience", ""),
        "education": item.get("jobDegree", ""),
        "tags": item.get("skills", []) or item.get("jobLabels", []) or [],
        "url": f"https://www.zhipin.com/job_detail/{eid}.html",
        "publish_time": item.get("lastModifyTime", ""),
    }


# ============================================================
#  统一入口
# ============================================================
PLATFORMS = {
    "boss": crawl_boss,
}


def crawl(platform: str, query: str, city: str = "全国", page: int = 1,
          use_mock: bool = False) -> Dict[str, Any]:
    """统一爬取入口。
    platform: boss | all（目前只剩 boss，all 等同 boss）
    返回固定结构：{jobs, source, platform, error?}
    source ∈ 'real' | 'error'。
    """
    # 目前只支持 boss；all 与未知 platform 都走 boss
    return crawl_boss(query, city, page, use_mock)


# ============================================================
#  简历关键词提取 + 匹配度计算
# ============================================================
def extract_resume_keywords(resume_text: str) -> Dict[str, Any]:
    """从简历文本提取目标岗位、技能、城市等（P2 升级：增加 section 分段）。
    返回字段：target_position / city / skills / expected_salary / sections。
    sections = {basics, education, experience, skills, projects} 各段原文，便于前端展示与后续 LLM 抽取。
    """
    text = resume_text or ""

    # 提取期望城市
    city_match = re.search(r"(期望城市|工作地点|所在地)[：:\s]*([\u4e00-\u9fa5]+)", text)
    city = city_match.group(2) if city_match else "全国"

    # 提取期望岗位
    pos_match = re.search(
        r"(求职意向|目标岗位|期望职位|应聘职位)[：:\s]*([\u4e00-\u9fa5A-Za-z0-9·/]+)", text
    )
    target = pos_match.group(2).strip() if pos_match else ""

    # 提取技能段
    skill_match = re.search(
        r"(技能(?:专长|栈)?|掌握(?:技术)?|技术栈)[：:\s]*([\s\S]*?)(?=\n\s*(?:工作|项目|教育|个人|$))",
        text,
    )
    skills_block = skill_match.group(2) if skill_match else text
    skills = [s.strip() for s in re.split(r"[，,、；;·/\s]+", skills_block)
              if s.strip() and 1 < len(s.strip()) <= 30]
    skills = list(dict.fromkeys(skills))[:15]

    # 期望薪资
    sal_match = re.search(r"(期望薪资|薪资)[：:\s]*(\d+[-~]\d+\s*[Kk千万]?)(?![\d.])", text)
    salary = sal_match.group(2).strip() if sal_match else ""

    # ----- P2: 按 section 切分简历原文 -----
    sections = _split_resume_sections(text)

    return {
        "target_position": target,
        "city": city,
        "skills": skills,
        "expected_salary": salary,
        "sections": sections,
    }


def _split_resume_sections(text: str) -> Dict[str, str]:
    """按常见简历标题切分原文，返回各段文本。标题：基本信息/教育/工作/技能/项目。"""
    # 标题模式：行首独立标题词（带或不带冒号）
    title_re = re.compile(
        r"^\s*(个人信息|基本信息|个人资料|教育(?:背景|经历)?|工作(?:经历|经验)|"
        r"技能(?:专长|栈)?|掌握(?:技术)?|技术栈|项目(?:经历|经验)?|"
        r"实习经历|自我评价|求职意向|期望薪资|期望城市)\s*[：:]*\s*$",
        re.MULTILINE,
    )
    matches = list(title_re.finditer(text))
    sections = {"basics": "", "education": "", "experience": "", "skills": "", "projects": ""}
    if not matches:
        # 没识别到分段：把全文塞 basics
        sections["basics"] = text.strip()
        return sections
    # 标题 → section key 映射
    title_to_key = {
        "个人信息": "basics", "基本信息": "basics", "个人资料": "basics",
        "教育": "education", "教育背景": "education", "教育经历": "education",
        "工作经历": "experience", "工作经验": "experience", "工作": "experience", "实习经历": "experience",
        "技能": "skills", "技能专长": "skills", "技能栈": "skills", "掌握": "skills", "掌握技术": "skills", "技术栈": "skills",
        "项目经历": "projects", "项目经验": "projects", "项目": "projects",
        # 未单独成段的关键词也归 basics，避免内容被静默丢弃
        "求职意向": "basics", "期望薪资": "basics", "期望城市": "basics", "自我评价": "basics",
    }
    # 首个标题之前的导语（姓名 + 意向/城市/薪资行）归 basics
    preamble = text[: matches[0].start()].strip()
    if preamble:
        sections["basics"] = preamble
    for i, m in enumerate(matches):
        title = m.group(1)
        key = title_to_key.get(title)
        if not key:
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end].strip()
        if content:
            # 已存在则追加（同 key 多次出现）
            if sections[key]:
                sections[key] += "\n" + content
            else:
                sections[key] = content
    # 兜底：如果分段没匹配到技能，全文 fallback
    if not sections["skills"]:
        sections["skills"] = text.strip()
    return sections


# ---------- 匹配度计算（2026-09-30 重写：去保底/下限，修区分度） ----------
# 技能词归一：小写 → 去掉字母/数字/中文/+#以外的符号（node.js→nodejs、spring boot→springboot）
# → 同义词收敛（js/ts/vue3/nodejs 等指向标准形）。java 与 javascript 归一后不等，杜绝前缀假命中。
_SKILL_SYNONYMS = {
    "js": "javascript", "ts": "typescript", "nodejs": "node", "vuejs": "vue",
    "vue2": "vue", "vue3": "vue", "reactjs": "react", "golang": "go",
    "py": "python", "postgresql": "postgres", "ml": "machinelearning",
}


def _norm_skill(s: str) -> str:
    t = re.sub(r"[^0-9a-z\u4e00-\u9fa5+#]", "", (s or "").lower())
    return _SKILL_SYNONYMS.get(t, t)


_SENIORITY_RE = re.compile(r"资深|高级|中级|初级|实习|校招|应届|专家|资深|lead|senior|junior", re.I)
# BOSS 标签里混着「3-5年 / 本科 / xx专业 / 前端开发经验」这类要求标签——不是技能，不计入技能分母
_REQ_TAG_RE = re.compile(r"年|届|经验|专业|学历|本科|大专|硕士|博士|在校|全职|兼职")
# 意向岗位里的通用角色词——剥离后剩下的才是领域词（高级前端工程师 → 前端）
_ROLE_WORD_RE = re.compile(r"工程师|架构师|开发|师|专员|经理|主管|顾问|专员|人员|岗")


def _parse_salary_k(s: str):
    """薪资字符串 → (min, max)，统一折算成 K；解析失败返回 None。

    支持 11-13K / 2-3万 / 8千-1.2万（两端各自带单位）/ 1.3-2万 / 22-35K·13薪（取前两数）。"""
    if not s:
        return None
    t = (s or "").lower().replace("，", "").replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)(万|千|k)?\s*[~\-～至]\s*(\d+(?:\.\d+)?)(万|千|k)?", t)
    if not m:
        return None

    def val(num: str, unit: str) -> float:
        v = float(num)
        if unit == "万":
            v *= 10.0
        return v  # 千 / k / 缺省都按 K

    lo = val(m.group(1), m.group(2) or m.group(4) or "")   # 「2-3万」：末位单位回溯作用到首位
    hi = val(m.group(3), m.group(4) or "")
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def calc_match_score(job: Dict, resume_keywords: Dict[str, Any]) -> Dict[str, Any]:
    """计算岗位与简历的 4 维匹配度（0-100 真实刻度，无保底分/无下限夹逼）。

    2026-09-30 重写（原版三处失真：技能分母用简历技能数导致天花板 ~67、
    经验维子串/前缀匹配方向失真、overall 下限 40 压扁分布）：
    - 技能：命中岗位标签数 / 岗位标签总数（分母=岗位要求面），词归一后全等匹配；
    - 经验：求职意向去职级词后与标题（去括注）做序列相似度分档；
    - 薪资：区间 IoU，单位归一到 K（万/千/K 混排、小数、反向区间都兜住）；
    - 未知输入给 50 中性分（不知道 ≠ 不匹配）。
    返回 {overall, skills_match, experience_match, salary_match, location_match, reasoning}。
    """
    job_tags = [t for t in (job.get("tags") or []) if str(t).strip()]
    tag_norms = [_norm_skill(str(t)) for t in job_tags]
    # 只把「技能样」标签当技能要求（滤掉 3-5年/本科/xx专业/xx经验 这类要求标签）
    skill_tag_norms = [tn for t, tn in zip(job_tags, tag_norms) if not _REQ_TAG_RE.search(str(t))]
    job_title = (job.get("title") or "").lower()
    resume_skills = [str(s) for s in (resume_keywords.get("skills") or []) if str(s).strip()]
    target = (resume_keywords.get("target_position") or "").lower().strip()
    resume_city = (resume_keywords.get("city") or "全国").lower()
    expected_salary = resume_keywords.get("expected_salary") or ""
    job_city = (job.get("city") or "").lower()
    job_salary = job.get("salary") or ""

    # 1) 技能匹配度：简历技能命中「技能样」岗位标签的比例（分母=岗位技能要求数）
    if not resume_skills or not skill_tag_norms:
        skills_match = 50                      # 任一缺失＝无法判断，中性分
    else:
        skill_norms = [_norm_skill(s) for s in resume_skills]
        hits = sum(1 for sn in skill_norms if sn and sn in skill_tag_norms)
        skills_match = min(100, round(hits / len(skill_tag_norms) * 100))

    # 2) 经验匹配度：意向岗位剥掉职级词与通用角色词得「领域词」（前端），
    #    领域词命中标题（且有角色词佐证）即高分；否则退回序列相似度分档
    if not target:
        experience_match = 50
    else:
        core = _SENIORITY_RE.sub("", target).strip(" ·-/") or target
        domain = core
        for w in ("工程师", "架构师", "开发", "师", "专员", "经理", "主管", "顾问", "人员", "岗"):
            domain = domain.replace(w, "")
        domain = domain.strip(" ·-/") or core
        title_clean = re.sub(r"[（(【\[].*?[）)】\]]", "", job_title).strip()
        if domain and domain in title_clean:
            # 意向本身无角色词（如「前端」）时领域词命中即可；否则标题应有角色佐证
            experience_match = 100 if (core == domain or _ROLE_WORD_RE.search(title_clean)) else 80
        elif core in title_clean:
            experience_match = 95
        else:
            ratio = difflib.SequenceMatcher(None, core, title_clean).ratio()
            experience_match = (85 if ratio >= 0.65 else
                                60 if ratio >= 0.5 else
                                35 if ratio >= 0.35 else 10)

    # 3) 薪资匹配度：期望区间与岗位区间的 IoU（单位统一折 K）
    r_range, j_range = _parse_salary_k(expected_salary), _parse_salary_k(job_salary)
    if not r_range or not j_range:
        salary_match = 50                      # 任一缺失/解析失败给中性分
    else:
        overlap = max(0.0, min(r_range[1], j_range[1]) - max(r_range[0], j_range[0]))
        union = max(r_range[1], j_range[1]) - min(r_range[0], j_range[0])
        salary_match = round(overlap / union * 100) if union > 0 else 50

    # 4) 地点匹配度：期望城市 vs 岗位城市
    if resume_city == "全国" or not resume_city:
        location_match = 70
    elif not job_city:
        location_match = 50
    elif resume_city in job_city or job_city in resume_city:
        location_match = 100
    else:
        location_match = 30

    # overall 加权：skills 35% + exp 30% + salary 20% + loc 15%（真实刻度，不设下限）
    overall = round(skills_match * 0.35 + experience_match * 0.30
                    + salary_match * 0.20 + location_match * 0.15)
    overall = max(0, min(100, overall))

    # reasoning：拼接可解释文本
    skill_norms = [_norm_skill(s) for s in resume_skills]
    hit_skills = [s for s, sn in zip(resume_skills, skill_norms)
                  if sn and sn in skill_tag_norms][:5]
    reasons = []
    if hit_skills:
        reasons.append(f"技能命中 {len(hit_skills)} 项：{', '.join(hit_skills)}")
    if target:
        core = _SENIORITY_RE.sub("", target).strip(" ·-/") or target
        if core and core in re.sub(r"[（(【\[].*?[）)】\]]", "", job_title).strip():
            reasons.append(f"目标岗位「{target}」与标题匹配")
    if r_range and j_range:
        reasons.append(f"薪资期望 {expected_salary} vs 岗位 {job_salary}")
    if resume_city != "全国" and resume_city in job_city:
        reasons.append(f"城市「{resume_city}」匹配")
    if not reasons:
        reasons.append("匹配点较少，建议人工评估")
    reasoning = "；".join(reasons)

    return {
        "overall": overall,
        "skills_match": skills_match,
        "experience_match": experience_match,
        "salary_match": salary_match,
        "location_match": location_match,
        "reasoning": reasoning,
    }
