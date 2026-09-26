"""求职信模板 _build_cover_letter 测试（经 server 导入；零 LLM、不编造事实）。"""
from server import _build_cover_letter


def test_letter_with_job():
    pdata = {
        "name": "张三", "phone": "13800138000", "email": "z@x.com",
        "target_position": "前端", "city": "上海", "expected_salary": "30-50K",
        "skills": ["React", "TypeScript", "Vue"],
        "summary": "五年前端经验。",
        "experience": "负责核心页面重构，性能提升 40%。",
    }
    job = {"title": "高级前端工程师", "company": "某公司", "tags": ["React", "TypeScript"]}
    letter = _build_cover_letter(pdata, job)
    assert "某公司 · 高级前端工程师" in letter
    assert "张三" in letter
    assert "React、TypeScript、Vue" in letter
    assert "期望工作地点为上海" in letter
    assert "13800138000 · z@x.com" in letter


def test_letter_generic_without_job():
    letter = _build_cover_letter({"name": "张三"}, None)
    assert "寻找新的职业机会" in letter
    assert "【你的姓名】" not in letter


def test_letter_missing_fields_use_placeholders_not_fabrication():
    letter = _build_cover_letter({}, None)
    assert "【你的姓名】" in letter
    assert "上海" not in letter and "30-50K" not in letter
