"""LLM 能力层（OpenAI 兼容 Chat Completions 协议）。

模型配置由用户在设置弹窗维护（存 SQLite app_settings，密钥不出本机），
支持任意 OpenAI 兼容服务：DeepSeek / 通义 / Moonshot / 智谱 / 硅基流动 /
OpenRouter / 本地 Ollama 等——base_url 填到 /v1 即可。

三个功能函数（greeting / analyze_match / polish_resume）只依赖注入的
config，chat 可在测试中 mock。
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
