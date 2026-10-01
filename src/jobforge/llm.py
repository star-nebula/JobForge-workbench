"""LLM 能力层（OpenAI 兼容 Chat Completions 协议）。

模型配置由用户在设置弹窗维护（存 SQLite app_settings，密钥不出本机），
支持任意 OpenAI 兼容服务：DeepSeek / 通义 / Moonshot / 智谱 / 硅基流动 /
OpenRouter / 本地 Ollama 等——base_url 填到 /v1 即可。

功能函数（greeting / analyze_match / polish_resume）与批量粗筛
（triage_chunk / triage_jobs）都只依赖注入的 config，chat 可在测试中 mock。
"""
import json
import re
from typing import Any, Dict, List, Optional

import requests

DEFAULT_TIMEOUT = 90


class LLMError(Exception):
    """LLM 调用失败（网络 / HTTP / 解析），message 面向用户可直接展示。"""


def chat(config: Dict, messages: List[Dict], timeout: int = DEFAULT_TIMEOUT,
         temperature: float = 0.4, max_tokens: int = 2000) -> str:
    """调一次 OpenAI 兼容 chat/completions，返回首条回复文本。"""
    base = (config.get("base_url") or "").strip().rstrip("/")
    model = (config.get("model") or "").strip()
    if not base or not model:
        raise LLMError("该模型配置缺少 Base URL 或模型名，请到「设置 → AI 模型」补全")
    url = f"{base}/chat/completions"
    headers = {"Content-Type": "application/json"}
    key = (config.get("api_key") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {"model": model, "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens}
    try:
        r = requests.post(url, headers=headers, json=body, timeout=timeout)
    except requests.Timeout:
        raise LLMError(f"LLM 请求超时（{timeout}s）：{base}")
    except requests.RequestException as e:
        raise LLMError(f"LLM 请求失败：{type(e).__name__}: {e}")
    if r.status_code != 200:
        raise LLMError(f"LLM 服务返回 HTTP {r.status_code}：{r.text[:200]}")
    try:
        return r.json()["choices"][0]["message"]["content"]
    except Exception:
        raise LLMError(f"LLM 响应结构异常：{r.text[:200]}")


def parse_json(text: str) -> Dict[str, Any]:
    """容错解析 LLM 输出里的 JSON 对象（剥 ``` 围栏、取首尾大括号）。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`").strip()
        if t[:4].lower() == "json":
            t = t[4:]
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise LLMError(f"LLM 未返回 JSON：{t[:120]}")
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"LLM 返回的 JSON 解析失败：{e}（原文开头：{t[:80]}）")


def _profile_brief(pdata: Dict) -> Dict:
    """从个人资料提取喂给 LLM 的字段（skills 兼容字符串）。"""
    skills = pdata.get("skills") or []
    if isinstance(skills, str):
        skills = [x.strip() for x in re.split(r"[，,、；;/\s]+", skills) if x.strip()]

    def s(k: str) -> str:
        v = pdata.get(k)
        return v.strip() if isinstance(v, str) else ""
    return {
        "name": s("name"),
        "target_position": s("target_position"),
        "city": s("city"),
        "expected_salary": s("expected_salary"),
        "skills": [x for x in skills if x],
        "summary": s("summary"),
        "experience": s("experience"),
    }


def greeting(config: Optional[Dict], pdata: Dict, job: Optional[Dict]) -> Dict:
    """生成 BOSS 打招呼语。有 config 走 LLM；config 为空走本地模板（降级不阻塞）。

    返回 {greeting, source: 'llm'|'template', model?, error?}。"""
    brief = _profile_brief(pdata)
    job = job or {}
    if not config:
        return {"greeting": _template_greeting(brief, job), "source": "template"}
    tags = [str(t).strip() for t in (job.get("tags") or []) if str(t).strip()]
    jd = (job.get("jd_text") or "").strip()
    jd_part = f"【JD 正文（截取）】\n{jd[:1200]}\n" if jd else ""
    user = (f"【目标岗位】{(job.get('title') or '').strip()} · {(job.get('company') or '').strip()}"
            f"{' · ' + str(job.get('salary')) if job.get('salary') else ''}\n"
            f"【岗位标签】{'、'.join(tags) if tags else '（无）'}\n"
            f"{jd_part}"
            f"【我的情况】姓名 {brief['name'] or '（匿名）'}；求职方向 {brief['target_position'] or '未填'}；"
            f"技能 {('、'.join(brief['skills'][:10])) if brief['skills'] else '未填'}；"
            f"经历摘要 {brief['experience'][:300] or '未填'}\n"
            "请直接输出打招呼语正文（不要任何解释、引号或署名）。")
    try:
        text = chat(config, [
            {"role": "system", "content": (
                "你是求职者在 BOSS 直聘上的沟通助手。根据岗位信息与求职者资料写一条开聊打招呼语："
                "60~120 字；口语自然、不谄媚不堆套话；点出与该 JD 最相关的 1~2 个匹配点（技能或经历）；"
                "表达明确的兴趣与沟通意愿；只能使用资料里给到的经历，绝不编造；不要邮件腔（禁「尊敬的HR」），不要署名。")},
            {"role": "user", "content": user},
        ], temperature=0.7, max_tokens=400)
        text = text.strip().strip('"「」')
        if not text:
            raise LLMError("LLM 返回了空内容")
        return {"greeting": text, "source": "llm", "model": config.get("model")}
    except LLMError as e:
        return {"greeting": _template_greeting(brief, job), "source": "template", "error": str(e)}


def _template_greeting(brief: Dict, job: Dict) -> str:
    """无 LLM 时的本地模板招呼语（只用真实资料字段）。"""
    title = (job.get("title") or "").strip()
    skills = brief["skills"][:3]
    skill_part = f"我熟悉{'、'.join(skills)}，" if skills else ""
    exp_part = f"{brief['experience'][:40]}……" if brief["experience"] else ""
    hello = f"您好！看到贵司「{title}」岗位" if title else "您好！看到贵司的招聘"
    match_part = "，与我的方向很契合。" if title else "。"
    return f"{hello}{match_part}{skill_part}{exp_part}期待能与您进一步沟通，方便的话可以看下我的简历，谢谢！"


def analyze_match(config: Dict, pdata: Dict, job: Dict) -> Dict:
    """LLM 匹配度分析。返回 {verdict, score, strengths, gaps, advice, model}。

    要求 LLM 只基于给定资料与 JD，不得臆测资料外信息。JD 正文可能不完整
    （懒渲染残缺/截断），标题/薪资/城市/标签作为硬信息必须纳入评分。"""
    brief = _profile_brief(pdata)
    tags = [str(t).strip() for t in (job.get("tags") or []) if str(t).strip()]
    jd = (job.get("jd_text") or "").strip()
    if not jd:
        raise LLMError("该岗位还没有 JD 正文，先在详情弹窗抓取 JD 再分析")
    user = (f"【岗位标题】{(job.get('title') or '').strip() or '未标注'} · "
            f"{(job.get('company') or '').strip() or '未标注'}\n"
            f"【薪资】{str(job.get('salary') or '').strip() or '未标注'}\n"
            f"【城市】{str(job.get('city') or '').strip() or '未标注'}\n"
            f"【岗位标签】{'、'.join(tags) if tags else '（无）'}\n"
            f"【JD 正文（截取，可能不完整）】\n{jd[:2500]}\n"
            f"【求职者】求职方向 {brief['target_position'] or '未填'}；"
            f"技能 {('、'.join(brief['skills'][:12])) if brief['skills'] else '未填'}；"
            f"经历 {brief['experience'][:800] or '未填'}；"
            f"期望 {brief['city'] or '不限'} {brief['expected_salary'] or ''}")
    text = chat(config, [
        {"role": "system", "content": (
            "你是资深招聘顾问，为求职者做岗位匹配分析。只输出一个 JSON 对象（不要 markdown 围栏、不要解释）："
            '{"verdict":"一句话总体结论","score":0到100的整数,"strengths":["优势1","优势2"],'
            '"gaps":["差距1"],"advice":["建议1"]}。'
            "score 综合技能/经验/条件给分；strengths 与 gaps 各 1~4 条且要具体到 JD 的要求；"
            "advice 给 1~3 条可执行建议（如补什么技能、面试怎么准备）。"
            "岗位标题、薪资、城市、标签是可靠的硬信息，必须纳入评分，不得遗漏；"
            "JD 正文可能被截断或不完整，JD 未提及的方面要用这些结构化信息补足判断——"
            "不得仅因 JD 没写就断定岗位不具备该条件，也不臆测资料外信息；"
            "若结论主要依赖标签/标题而非 JD 正文，在 verdict 里注明依据来源。")},
        {"role": "user", "content": user},
    ], temperature=0.3, max_tokens=900)
    d = parse_json(text)
    out = {
        "verdict": str(d.get("verdict") or "").strip(),
        "score": max(0, min(100, int(d.get("score") or 0))),
        "strengths": [str(x) for x in (d.get("strengths") or []) if str(x).strip()][:4],
        "gaps": [str(x) for x in (d.get("gaps") or []) if str(x).strip()][:4],
        "advice": [str(x) for x in (d.get("advice") or []) if str(x).strip()][:3],
        "model": config.get("model"),
    }
    if not out["verdict"]:
        raise LLMError("LLM 分析结果缺 verdict")
    return out


def polish_resume(config: Dict, pdata: Dict) -> Dict:
    """LLM 润色简历自由文本字段（summary / experience）。只改表达不添事实。

    返回 {summary, experience, model}（键恒存在，空字段原样返回空串）。"""
    brief = _profile_brief(pdata)
    user = (f"【求职方向】{brief['target_position'] or '未填'}\n"
            f"【当前个人简介】\n{brief['summary'] or '（空）'}\n"
            f"【当前工作经历】\n{brief['experience'] or '（空）'}")
    text = chat(config, [
        {"role": "system", "content": (
            "你是简历润色专家。在完全不改变事实的前提下润色简历文本："
            "强化量化成果的表达、动词开头、删除空话套话、合并啰嗦句；"
            "绝不新增事实、绝不编造数字或经历；保留原有换行分段。"
            '只输出一个 JSON 对象（不要围栏、不要解释）：{"summary":"润色后的个人简介","experience":"润色后的工作经历"}。'
            "某字段为空则返回空字符串。")},
        {"role": "user", "content": user},
    ], temperature=0.4, max_tokens=1600)
    d = parse_json(text)
    return {
        "summary": str(d.get("summary") or "").strip(),
        "experience": str(d.get("experience") or "").strip(),
        "model": config.get("model"),
    }


# ---------- L1 打包粗筛（triage）：用列表页结构化字段批量判 keep/drop ----------
TRIAGE_CHUNK = 10
# 耗时随块大小超线性增长（2026-09-30 真调实测：5 岗 11s、10 岗 42s、20 岗 >90s 直接超时），
# 默认 90s 的 chat 超时撑不住一块，粗筛单独放宽到 180s 并取 10 岗一块留足余量。
TRIAGE_TIMEOUT = 180


def _as_bool(v: Any) -> bool:
    """把模型可能给出的 keep 形态（true/false/字符串）归一成布尔。"""
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "0", "no", "否", "")
    return bool(v)


def _job_brief_line(no: int, job: Dict) -> str:
    """一岗一行的粗筛输入。刻意不含 jd_text 与 match_score：前者本阶段就没有，
    后者喂进去只会让模型照抄本地分，粗筛与初筛的一致性验证随之失效。"""
    tags = "、".join(str(t).strip() for t in (job.get("tags") or []) if str(t).strip()) or "无"
    fields = [str(job.get(k) or "未标注").strip()
              for k in ("title", "company", "salary", "city", "experience", "education")]
    return f"{no}. {' | '.join(fields)} | 标签: {tags}"


def triage_chunk(config: Dict, pdata: Dict, jobs: List[Dict]) -> Dict[int, Dict]:
    """一次调用粗筛一批岗位（≤TRIAGE_CHUNK 个），返回 {序号: {keep, reason}}。

    序号漏答不在此兜底，由 triage_jobs 统一按保守保留处理。"""
    brief = _profile_brief(pdata)
    lines = "\n".join(_job_brief_line(i, j) for i, j in enumerate(jobs, 1))
    user = (f"【求职者】求职方向 {brief['target_position'] or '未填'}；"
            f"技能 {('、'.join(brief['skills'][:12])) if brief['skills'] else '未填'}；"
            f"经历 {brief['experience'] or '未填'}\n"
            f"【期望】城市 {brief['city'] or '不限'}；薪资 {brief['expected_salary'] or '未填'}\n"
            f"【候选岗位】（每行格式：标题 | 公司 | 薪资 | 城市 | 经验 | 学历 | 标签。"
            f"编号仅用于回答）\n{lines}")
    text = chat(config, [
        {"role": "system", "content": (
            "你是求职者的岗位粗筛助手，判断每个岗位值不值得花成本去抓 JD 做精细匹配。"
            '只输出一个 JSON 对象（不要 markdown 围栏、不要解释）：'
            '{"jobs":[{"no":1,"keep":true,"reason":"不超过20字的依据"}]}。'
            "判定口径——召回优先：粗筛的目的是砍掉确定没戏的，不是挑出确定有戏的。"
            "只有硬信息明确显示不匹配才 keep=false，典型是：岗位性质与求职方向无关"
            "（如找前端而岗是机械、运维、算法、航空）、城市与期望城市冲突、"
            "经验或学历门槛远高于求职者现状。"
            "薪资一律不作为淘汰依据：求职者不设薪资硬下限，低于期望薪资的岗位照样值得精配。"
            "信息不足、含义模糊、岗位可能跨领域迁移的，一律 keep=true。"
            "不要因为标题含「高级」「专家」「资深」就判不匹配，职级词不构成门槛依据。"
            "本阶段没有 JD 正文：reason 只能说依据的那条硬信息，不得臆测或编造岗位要求。"
            "每个编号都要给出判定，不要遗漏、不要新增。")},
        {"role": "user", "content": user},
    ], temperature=0.2, max_tokens=max(400, 80 * len(jobs)), timeout=TRIAGE_TIMEOUT)
    d = parse_json(text)
    got: Dict[int, Dict] = {}
    for item in (d.get("jobs") or []):
        if not isinstance(item, dict):
            continue
        try:
            no = int(item.get("no"))
        except (TypeError, ValueError):
            continue
        if 1 <= no <= len(jobs):
            got[no] = {"keep": _as_bool(item.get("keep")),
                       "reason": str(item.get("reason") or "").strip()[:80]}
    return got


def triage_jobs(config: Dict, pdata: Dict, jobs: List[Dict],
                chunk_size: int = TRIAGE_CHUNK) -> List[Dict]:
    """按 chunk_size 切块粗筛（每块一次 LLM 调用），返回顺序与入参一致。

    每项 {platform, job_id, keep, reason, answered}；模型漏答的岗位保守 keep=true
    并置 answered=False，绝不因一次格式不对就丢岗。"""
    pdata = pdata or {}
    out: List[Dict] = []
    for i in range(0, len(jobs), chunk_size):
        chunk = jobs[i:i + chunk_size]
        got = triage_chunk(config, pdata, chunk)
        for no, j in enumerate(chunk, 1):
            d = got.get(no)
            out.append({
                "platform": j.get("platform") or "boss",
                "job_id": str(j.get("job_id") or ""),
                "keep": True if d is None else d["keep"],
                "reason": (d or {}).get("reason") or "模型未给出判定，保守保留",
                "answered": d is not None,
            })
    return out
