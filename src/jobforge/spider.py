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
    city_code = BOSS_CITY_CODES.get(city, "100010000")
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


def calc_match_score(job: Dict, resume_keywords: Dict[str, Any]) -> Dict[str, Any]:
    """计算岗位与简历的 4 维匹配度（P3 升级）。
    返回 {overall, skills_match, experience_match, salary_match, location_match, reasoning}。
    每维度 0-100，overall 是加权平均。
    """
    job_tags = set(t.lower() for t in job.get("tags", []))
    job_title = job.get("title", "").lower()
    resume_skills = [s.lower() for s in resume_keywords.get("skills", [])]
    target = (resume_keywords.get("target_position") or "").lower()
    resume_city = (resume_keywords.get("city") or "全国").lower()
    expected_salary = resume_keywords.get("expected_salary") or ""
    job_city = (job.get("city") or "").lower()
    job_salary = job.get("salary") or ""

    # 1) 技能匹配度：命中的技能比例（jaccard-lite）
    if not resume_skills:
        skills_match = 30
    else:
        hits = sum(1 for s in resume_skills if any(s in t or t in s for t in job_tags))
        skills_match = min(100, 40 + int(hits / max(1, len(resume_skills)) * 60))

    # 2) 经验匹配度：目标岗位关键词命中标题
    if not target:
        experience_match = 50
    else:
        tm = 0
        if target in job_title: tm += 60
        elif any(k in job_title for k in target.split() if len(k) >= 2): tm += 40
        # 标题前 2 字（核心词）命中
        core = target[:2]
        if core and core in job_title: tm += 20
        experience_match = min(100, tm + 30)

    # 3) 薪资匹配度：期望薪资区间是否落入岗位薪资区间
    if not expected_salary or not job_salary:
        salary_match = 50  # 任一缺失给中分
    else:
        try:
            rmin, rmax = map(int, re.findall(r"\d+", expected_salary))
            jmin, jmax = map(int, re.findall(r"\d+", job_salary))
            # 区间重叠比例
            overlap = max(0, min(rmax, jmax) - max(rmin, jmin))
            union = max(rmax, jmax) - min(rmin, jmin)
            salary_match = min(100, int(overlap / max(1, union) * 100)) if union > 0 else 50
        except Exception:
            salary_match = 50

    # 4) 地点匹配度：期望城市 vs 岗位城市
    if resume_city == "全国" or not resume_city:
        location_match = 70
    elif not job_city:
        location_match = 50
    elif resume_city in job_city or job_city in resume_city:
        location_match = 100
    else:
        location_match = 30

    # overall 加权：skills 35% + exp 30% + salary 20% + loc 15%
    overall = int(skills_match * 0.35 + experience_match * 0.30 + salary_match * 0.20 + location_match * 0.15)
    overall = max(40, min(100, overall))

    # reasoning：拼接可解释文本
    hit_skills = [s for s in resume_skills if any(s in t or t in s for t in job_tags)][:5]
    reasons = []
    if hit_skills:
        reasons.append(f"技能命中 {len(hit_skills)} 项：{', '.join(hit_skills)}")
    if target and target in job_title:
        reasons.append(f"目标岗位「{target}」与标题完全匹配")
    if expected_salary and job_salary:
        reasons.append(f"薪资区间期望 {expected_salary} vs 岗位 {job_salary}")
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
