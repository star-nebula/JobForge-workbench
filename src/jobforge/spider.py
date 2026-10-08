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


def split_cities(city: str) -> List[str]:
    """「广州、深圳」→ ["广州", "深圳"]；去重保序，单城市/空串原样（剥「市」后缀）。

    抓取层逐城调用；空串返回 []（调用方自行兜底「全国」）。"""
    if not city:
        return []
    seen, out = set(), []
    for part in re.split(r"[，,、/＋+]|\s+(?:或|和|及)\s+", str(city)):
        name = _city_name(part)
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


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

    期望城市支持多城（「广州、深圳」→ 任一命中即 True）。
    期望城市为空/「全国/不限」→ 不限城市，一律 True。
    岗位城市为空（抓取未给）→ True：不知道 ≠ 不匹配，与 calc_match_score 的中性分同口径。
    """
    exp_list = split_cities(expected_city)
    if not exp_list or exp_list == ["全国"] or exp_list == ["不限"] or exp_list == ["全部"]:
        return True
    job = _city_name(job_city)
    return True if not job else job in exp_list

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

    city 支持多城串（「广州、深圳」）：BOSS 单次搜索只接受一个城市编码，
    逐城各抓一页后按 job_id 去重合并；单城失败不拖垮整体，错误汇总进 error。
    """
    cities = split_cities(city) or [""]
    if len(cities) > 1 and not all(_city_code(c) for c in cities):
        bad = "、".join(c for c in cities if not _city_code(c))
        supported = "、".join(c for c in BOSS_CITY_CODES if c != "全国")
        return {"jobs": [], "source": "error", "platform": "boss",
                "error": f"城市「{bad}」没有 BOSS 城市编码（可选：{supported}、全国；"
                         f"或在个人资料里改城市）。已中止抓取，不会静默按全国搜。"}
    jobs: List[Dict] = []
    seen_ids: set = set()
    errors: List[str] = []
    stopped: Optional[fetch_gate.Stopped] = None
    for c in cities:
        city_code = _city_code(c)
        if not city_code:
            supported = "、".join(c for c in BOSS_CITY_CODES if c != "全国")
            return {"jobs": [], "source": "error", "platform": "boss",
                    "error": f"城市「{c}」没有 BOSS 城市编码（可选：{supported}、全国；"
                             f"或在个人资料里改城市）。已中止抓取，不会静默按全国搜。"}
        try:
            data = fetch_jd_native.crawl_boss_native(query, city_code, page)
        except fetch_gate.Stopped as e:
            # 用户点了「结束」：记录住，已完成城市的岗位照常返回，循环终止
            stopped = e
            break
        except fetch_jd_native.NativeError as e:
            errors.append(f"[{c or '全国'}] {e}")
            continue
        except Exception as e:
            errors.append(f"[{c or '全国'}] 原生通道异常: {type(e).__name__}: {e}")
            continue
        raw = data.get("zpData", {}).get("jobList", []) or []
        for j in raw:
            n = _normalize_boss(j)
            jid = n.get("job_id") or ""
            if jid and jid in seen_ids:
                continue
            if jid:
                seen_ids.add(jid)
            jobs.append(n)
    if stopped is not None:
        raise fetch_gate.Stopped(f"{stopped}（已完成 {len(jobs)} 个岗位的抓取，均已保留）")
    if not jobs and errors:
        return {"jobs": [], "source": "error", "platform": "boss", "error": "；".join(errors)}
    return {"jobs": jobs, "source": "real", "platform": "boss",
            "error": "；".join(errors) if errors else None}


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
_MD_META_LINE_RE = re.compile(
    r"^\s*(?:[#>\-*+]\s*|\d+[.)]\s*)?(?:"
    r"姓名|性别|出生年月|年龄|籍贯|民族|政治面貌|电话|手机|(?:电子)?邮箱|"
    r"个人博客|博客|github|gitee|求职意向|期望城市|工作地点|所在地|期望薪资"
    r")\s*[：:]", re.I,
)


def _normalize_markdown(text: str) -> str:
    """Markdown 简历 → 纯文本。纯文本简历经此函数应原样返回（幂等无害）。

    处理顺序：先拆表格行（管道分隔的两列基本信息），再去行内装饰
    （加粗/斜体/行内代码/链接），最后剥标题井号与列表符号。
    """
    if not text:
        return text
    lines = []
    for line in text.split("\n"):
        # 表格行「| 姓名：xx | 电话：yy |」→ 去管道，字段行交给下面的 _MD_META_LINE_RE 重排
        if line.lstrip().startswith("|"):
            line = re.sub(r"^\s*\|?\s*|\s*\|?\s*$", "", line)
            line = re.sub(r"\s*\|\s*", "\n", line)
            lines.append(line)
            continue
        # 图片/链接：[文字](地址) → 文字；<https://x> / <a@b.c> → 去尖括号（裸 URL 无 < >）
        line = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 \2", line)
        line = re.sub(r"<(https?://[^>]+)>", r"\1", line)
        line = re.sub(r"<([\w.+-]+@[\w.-]+)>", r"\1", line)
        line = re.sub(r"`([^`]*)`", r"\1", line)
        line = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", line)
        # 标题井号 / 引用 / 列表符号
        line = re.sub(r"^\s{0,3}(#{1,6})\s+", "", line)
        line = re.sub(r"^\s{0,3}(?:>\s?|[-*+]\s+|\d+[.)]\s+)", "", line)
        lines.append(line)
    return "\n".join(lines)


def _merge_two_column_basics(text: str) -> str:
    """两列并排的基本信息（姓名/电话在同行）→ 每字段一行，保证标签正则可命中。

    「姓名：xx 出生年月：yy」这类同号拼接行，标签后至下一个已知标签之间
    的内容就是该字段值；未知前缀（如整行只有「男 2003年5月」）保留原行不丢信息。
    """
    out = []
    for line in text.split("\n"):
        if not _MD_META_LINE_RE.match(line):
            out.append(line)
            continue
        head, _, rest = line.partition("：") if "：" in line else line.partition(":")
        head = head.strip().lstrip("# ").strip()
        parts, cur = [], head + "：" + rest
        # 在已知标签处二次切分（值内不会再次出现「标签：」形态）。
        # 注意：不能放裸「博客」，否则「个人博客」会在 个人|博客 处被二次劈开
        for seg in re.split(
            r"(?=(?:姓名|性别|出生年月|年龄|电话|手机|(?:电子)?邮箱|个人博客|github|求职意向|期望城市|期望薪资)[：:])",
            cur, flags=re.I,
        ):
            if seg.strip():
                parts.append(seg.strip())
        out.extend(parts)
    return "\n".join(out)


# 技能段切分后的噪声词：动词/程度副词/描述性短语——不是技能标签，入表会污染匹配
_SKILL_STOPWORDS = {
    "熟悉", "掌握", "熟练", "精通", "了解", "深入理解", "理解", "具备", "具备经验",
    "等", "以及", "并", "与", "和", "的", "可", "能", "运用", "运用于", "完成",
    "使用", "熟练使用", "开发", "设计", "实现", "落地", "经验", "能力", "方案",
    "方向", "基础", "扎实", "良好", "深度", "底层原理", "原理", "架构", "机制",
    "开发经验", "工程化", "调试", "调优", "优化", "中", "后", "前端", "后端",
    "工程", "依托", "相关", "领域", "场景", "手段", "思想", "策略", "流程",
    "自主推理", "推理", "编排", "训练", "标注", "建模", "检索", "部署", "缓存",
    "微调", "压缩", "量化", "蒸馏", "剪枝", "分类", "识别", "生成", "解析",
    "企业级部署", "高并发", "分布式", "自动化", "可视化",
}
# 程度副词/动词前缀：「熟悉LoRA」→「LoRA」
_SKILL_LEAD_RE = re.compile(
    r"^(?:深入理解|深入掌握|熟练使用|熟练掌握|独立设计|落地设计|熟悉|熟练|精通|掌握|了解|理解|依托|具备|使用|采用|基于|落地|设计|实现|有)+"
)
# 尾缀泛词：「LangChain框架」→「LangChain」、「调用经验」→「调用」（后者随长度/噪声规则丢弃）
_SKILL_TRAIL_RE = re.compile(
    r"(?:落地经验|实战经验|开发经验|调用经验|整套业务系统|业务系统|经验|框架|引擎|协议|数据库|方案|能力)+$"
)
# 已知多词技术名（小写无空格形态）：仅合并表内组合，避免把纯文本简历里
# 空格分隔的独立技能（JavaScript TypeScript React）误串成一个词
_MULTIWORD_SKILLS = {
    "functioncalling", "huggingface", "machinelearning", "deeplearning",
    "knowledgegraph", "fewshot", "zeroshot", "restapi", "pytestcov",
}
# 描述性中缀：「P-Tuning等微调方案与」→ 截到「P-Tuning」
_SKILL_CONNECT_RE = re.compile(r"[与和及的、等]+")


def _clean_skill_tokens(tokens) -> list:
    """技能段切碎后的词清洗：剥装饰符与动词前缀、去噪声词与描述性短语。"""
    out = []
    for t in tokens:
        t = t.strip().strip("*`_#").strip()
        while True:
            t2 = _SKILL_LEAD_RE.sub("", t).strip()
            if t2 == t:
                break
            t = t2
        t = t.split("等", 1)[0].strip()
        t = _SKILL_CONNECT_RE.sub("", t.rstrip("。；;，,、")).strip()
        t = _SKILL_TRAIL_RE.sub("", t).strip()
        if not t or len(t) < 2 or len(t) > 16 or t in _SKILL_STOPWORDS:
            continue
        if re.fullmatch(r"[\u4e00-\u9fa5（）()]+", t) and len(t) >= 6:
            continue
        out.append(t)
    return out


def _merge_known_multiword(skills: list) -> list:
    """把相邻且拼接后命中已知多词技术名的两个词合并（Function+Calling→FunctionCalling）。

    只认白名单表，杜绝把纯文本简历里空格分隔的独立技能串成伪词。
    """
    if len(skills) < 2:
        return skills
    out = [skills[0]]
    for t in skills[1:]:
        if (out and (out[-1] + t).lower() in _MULTIWORD_SKILLS):
            out[-1] = out[-1] + t
        else:
            out.append(t)
    return out


def extract_resume_keywords(resume_text: str) -> Dict[str, Any]:
    """从简历文本提取目标岗位、技能、城市等（P2 升级：增加 section 分段）。
    支持 Markdown 简历（标题井号/加粗/列表/表格/链接自动归一化）与纯文本简历。
    返回字段：target_position / city / skills / expected_salary / sections。
    sections = {basics, education, experience, skills, projects} 各段原文，便于前端展示与后续 LLM 抽取。
    """
    text = _normalize_markdown(resume_text or "")
    text = _merge_two_column_basics(text)

    # 提取期望城市（支持多城：「广州、深圳」顿号/逗号/斜杠分隔）
    city_match = re.search(r"(期望城市|工作地点|所在地)[：:\s]*([\u4e00-\u9fa5]+(?:[，,、/][\u4e00-\u9fa5]+)*)", text)
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
    skills = _clean_skill_tokens(
        s for s in re.split(r"[，,、；;·/：:\s]+", skills_block)
        if s.strip()
    )
    skills = _merge_known_multiword(skills)
    skills = list(dict.fromkeys(skills))[:20]

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
    # 标题模式：行首独立标题词（带或不带冒号；兼容 Markdown 标题，井号已在归一化时剥掉）
    title_re = re.compile(
        r"^\s*(个人信息|基本信息|个人资料|教育(?:背景|经历)?|工作(?:经历|经验)?|"
        r"专业技能|技能(?:专长|栈)?|掌握(?:技术)?|技术栈|项目(?:经历|经验)?|"
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
        "技能": "skills", "专业技能": "skills", "技能专长": "skills", "技能栈": "skills", "掌握": "skills", "掌握技术": "skills", "技术栈": "skills",
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
