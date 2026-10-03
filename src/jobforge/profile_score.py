"""个人资料评分规则引擎（纯本地规则，无 LLM 依赖）。

compute_profile_score(data) -> {score, level, checks}
- checks: [{k, n, ok, got, w, msg}]，n=检查项名，got=得分，w=满分，msg=给人看的说明
- score: 0-100；level: 优秀/良好/及格/待完善
前端「评分环 + 优化建议」都从 checks 派生：失败项即建议项。
"""
import re
from typing import Any, Dict, List

_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
_EMAIL_RE = re.compile(r"^\S+@\S+\.\S+$")
# 量化成果：百分比 / 提升·降低·节省·增长类动词 / 数字+万·W / GMV / 倍
_QUANT_RE = re.compile(r"(\d+\s*%|[提升降低节省增长翻][高升低][:：]?\d|提升|降低|节省|增长|\d+(\.\d+)?\s*[万亿W]|\d+\s*倍|GMV|LCP|ROI)", re.IGNORECASE)


def _s(v: Any) -> str:
    return (v or "").strip() if isinstance(v, str) else ("" if v is None else str(v)).strip()


def _skills(v: Any) -> List[str]:
    if isinstance(v, list):
        return [x.strip() for x in v if str(x).strip()]
    return [x for x in re.split(r"[，,、；;/\s]+", _s(v)) if x]


def compute_profile_score(data: Dict) -> Dict[str, Any]:
    """按完整度（62 分）+ 质量（38 分）打分，checks 同时是优化建议的数据源。"""
    data = data or {}
    skills = _skills(data.get("skills"))
    summary = _s(data.get("summary"))
    experience = _s(data.get("experience"))
    resume_text = _s(data.get("resume_text"))
    checks: List[Dict[str, Any]] = []

    def add(k: str, n: str, w: int, ok: bool, ok_msg: str, bad_msg: str, got: int = None):
        if got is None:
            got = w if ok else 0
        checks.append({"k": k, "n": n, "ok": ok, "w": w, "got": got, "msg": ok_msg if ok else bad_msg})

    # ---- 完整度（62 分）----
    add("name", "姓名", 6, bool(_s(data.get("name"))), "姓名已填写", "补上姓名，导出简历必需")
    # 手机号判定 = 11 位 + 13-19 号段（12x 是服务号不是手机号）；文案分两支，
    # 否则「填了 11 位却报不是 11 位」这种自相矛盾的提示会让人无从排查
    _phone = _s(data.get("phone"))
    _phone_11 = _phone.isdigit() and len(_phone) == 11
    add("phone", "电话", 6, bool(_PHONE_RE.match(_phone)), "电话格式正确",
        "11 位但号段不对：手机号应为 13-19 开头（12x 是服务号）" if _phone_11
        else "电话缺失或不是 11 位手机号（HR 联系不上你）")
    add("email", "邮箱", 6, bool(_EMAIL_RE.match(_s(data.get("email")))),
        "邮箱格式正确", "邮箱缺失或格式不对")
    add("target_position", "意向岗位", 8, bool(_s(data.get("target_position"))),
        "意向岗位已填写", "缺意向岗位——智能抓取靠它生成搜索词")
    add("city", "意向城市", 5, bool(_s(data.get("city"))), "意向城市已填写", "补上意向城市，岗位地点匹配会用")
    add("expected_salary", "期望薪资", 5, bool(_s(data.get("expected_salary"))),
        "期望薪资已填写", "补上期望薪资，薪资维度匹配会用")
    add("skills", "技能标签", 9, len(skills) >= 3, f"已有 {len(skills)} 项技能标签",
        "技能少于 3 项，抓取匹配和关键词覆盖都吃亏", got=(9 if len(skills) >= 3 else (6 if skills else 0)))
    add("summary", "个人简介", 6, len(summary) >= 30, f"简介 {len(summary)} 字", "简介不足 30 字，写一段 30 字以上的概括")
    add("experience", "工作经历", 8, len(experience) >= 50, f"工作经历 {len(experience)} 字",
        "工作经历不足 50 字，展开写职责与成果")
    add("education", "教育经历", 3, bool(_s(data.get("education"))), "教育经历已填写", "补上教育经历")

    # ---- 质量（38 分）----
    add("skills_rich", "技能丰富度", 10, len(skills) >= 6, f"{len(skills)} 项技能，覆盖面好",
        f"技能仅 {len(skills)} 项，建议补到 6 项以上提高岗位命中率")
    quant_hits = len(_QUANT_RE.findall(experience))
    add("quantified", "经历量化成果", 18, quant_hits >= 3, f"{quant_hits} 处量化描述，说服力强",
        "工作经历缺少量化成果（%、提升 X 倍、省 X 万等），面试官最看重这个",
        got=(18 if quant_hits >= 3 else (12 if quant_hits >= 1 else 0)))
    add("resume_text", "简历原文", 10, bool(resume_text), "简历原文已保存，可直接智能抓取",
        "还没有保存简历原文——在右侧上传/粘贴简历并采纳即可（智能抓取需要）")

    score = min(100, sum(c["got"] for c in checks))
    level = "优秀" if score >= 85 else "良好" if score >= 70 else "及格" if score >= 50 else "待完善"
    return {"score": score, "level": level, "checks": checks}
