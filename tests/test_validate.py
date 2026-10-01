"""校验器测试:编造 id 拦截、tier 计算、THEORY 透出、旧正则降级路径。

使用 data/sample_kb.json:myst_c01/myst_c02 为 FACT,myst_c04 为 THEORY(带争议说明)。
"""

from app import validate
from app.kb import get_kb

kb = get_kb()


def test_valid_fact_citation():
    [st] = validate.validate_statements(
        [{"text": "出土于第三层", "claim_ids": ["myst_c01"]}], kb
    )
    assert st["tier"] == "FACT"
    assert not st["downgraded"]
    assert st["invalid_claim_ids"] == []


def test_fabricated_id_dropped_and_downgraded():
    [st] = validate.validate_statements(
        [{"text": "结论", "claim_ids": ["myst_c01", "c_fake_999"]}], kb
    )
    assert st["claim_ids"] == ["myst_c01"]          # 编造 id 被丢弃
    assert st["invalid_claim_ids"] == ["c_fake_999"]
    assert st["tier"] == "THEORY"                    # FACT 被无效引用降一级
    assert st["downgraded"] is True


def test_all_fabricated_becomes_model_guess():
    [st] = validate.validate_statements(
        [{"text": "瞎说", "claim_ids": ["nope_1", "nope_2"]}], kb
    )
    assert st["tier"] == "MODEL_GUESS"
    assert st["downgraded"] is True


def test_no_citation_is_model_guess():
    [st] = validate.validate_statements([{"text": "无引用陈述", "claim_ids": []}], kb)
    assert st["tier"] == "MODEL_GUESS"
    assert st["downgraded"] is False  # 没引用谈不上降级


def test_theory_shown_as_theory_with_dispute():
    [st] = validate.validate_statements(
        [{"text": "礼仪用器说", "claim_ids": ["myst_c04"]}], kb
    )
    assert st["tier"] == "THEORY"
    assert st["dispute_note"] and "学者乙" in st["dispute_note"]


def test_worst_tier_wins():
    [st] = validate.validate_statements(
        [{"text": "混合引用", "claim_ids": ["myst_c01", "myst_c04"]}], kb
    )
    assert st["tier"] == "THEORY"  # FACT+THEORY 取最差


def test_legacy_regex_fallback_path():
    st = validate.validate_answer_text("据 [myst_c01] 与 [c_made_up] 可知……。", kb)
    assert st["claim_ids"] == ["myst_c01"]
    assert st["invalid_claim_ids"] == ["c_made_up"]
    assert st["tier"] == "THEORY"
    assert st["downgraded"] is True
