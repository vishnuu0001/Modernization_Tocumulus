from unittest.mock import patch

import pytest

from services.modernizer.token_budgeting import (
    END_SENTINEL,
    FileContract,
    completeness_reasons,
    contract_from_prompt,
    estimate_output_tokens,
    growth_budgets,
    plan_budget,
    sentinel_comment,
    strip_sentinel,
)


def test_contract_estimate_scales_with_members_and_dependencies():
    small = FileContract("Demo.cs", "service", [{"name": "Get", "type": "method"}], [])
    large = FileContract(
        "Demo.cs", "service",
        [{"name": f"Method{index}", "type": "method"} for index in range(12)],
        ["Kafka", "Redis"],
    )
    assert estimate_output_tokens(large) > estimate_output_tokens(small) * 4


def test_plan_budget_uses_model_context_and_output_cap():
    contract = FileContract(
        "schema.sql", "default",
        [{"name": f"section{index}", "type": "method"} for index in range(12)],
        ["Oracle"],
    )
    with patch("services.llm.count_tokens", return_value=2000), patch(
        "services.llm.model_context_window", return_value=32768,
    ), patch("services.llm.model_max_output_cap", return_value=24576):
        budget = plan_budget("devstral-small-2:24b", ["system", "prompt"], contract)
    assert budget.prompt_tokens == 2000
    assert budget.available == 24576
    assert 2048 < budget.max_new_tokens < budget.available


def test_growth_budgets_reaches_capacity_without_a_fixed_12000_ceiling():
    budgets = growth_budgets(5000, 23000)
    assert budgets[0] == 5000
    assert budgets[-1] == 23000
    assert budgets == sorted(set(budgets))
    assert 12000 not in budgets


def test_completeness_gate_requires_sentinel_members_and_no_stubs():
    contract = contract_from_prompt(
        "schema.sql", "1. Create V_BALANCE\n2. Implement FN_GET_BALANCE", "sql",
    )
    complete = (
        "CREATE VIEW V_BALANCE AS SELECT 1 AS amount FROM dual;\n"
        "CREATE FUNCTION FN_GET_BALANCE RETURN NUMBER IS BEGIN RETURN 1; END;\n"
        f"-- {END_SENTINEL}\n"
    )
    assert completeness_reasons(complete, contract) == []
    assert END_SENTINEL not in strip_sentinel(complete)
    assert completeness_reasons("-- TODO\n", contract)


def test_numbered_deliverables_exclude_descriptive_context_bullets():
    contract = contract_from_prompt(
        "OracleDbScript/generated.sql",
        """## Context
- Oracle version: 19c
- ACCOUNTS: account_id, opening_balance

## Deliverables
1. Create the sample tables.
2. Create V_ACCOUNT_BALANCE.
3. Create FN_GET_ACCOUNT_BALANCE.

## Requirements
- Use LEFT JOIN and NVL.
- Use bind variables.
""",
        "sql",
    )

    assert len(contract.members) == 3
    assert [member["signature"] for member in contract.members] == [
        "Create the sample tables.",
        "Create V_ACCOUNT_BALANCE.",
        "Create FN_GET_ACCOUNT_BALANCE.",
    ]


def test_repeated_serialized_deliverable_block_is_deduplicated():
    block = """## Deliverables
1. Create the sample tables.
2. Create V_ACCOUNT_BALANCE.
3. Create FN_GET_ACCOUNT_BALANCE.
"""
    contract = contract_from_prompt(
        "database/generated.sql", f"{block}\nBusiness metadata\n{block}", "sql",
    )

    assert len(contract.members) == 3
    assert [member["signature"] for member in contract.members] == [
        "Create the sample tables.",
        "Create V_ACCOUNT_BALANCE.",
        "Create FN_GET_ACCOUNT_BALANCE.",
    ]


def test_one_numbered_deliverable_excludes_policy_bullets():
    contract = contract_from_prompt(
        "OracleDbScript/generated.sql",
        """1. Create procedure PROCESS_PAYMENT.
- Audit every change.
- Use bind variables.
- Include error handling.
""",
        "sql",
    )

    assert [member["signature"] for member in contract.members] == [
        "Create procedure PROCESS_PAYMENT.",
    ]


def test_contract_member_limit_fails_instead_of_silently_truncating():
    prompt = "\n".join(
        f"{index}. Create procedure TASK_{index}." for index in range(1, 82)
    )

    with pytest.raises(ValueError, match="81 explicit members.*limit is 80"):
        contract_from_prompt("generated.sql", prompt, "sql")


def test_commented_script_structure_beats_output_format_numbering():
    contract = contract_from_prompt(
        "OracleDbSchema/generated.sql",
        """## OUTPUT CONTRACT
1. Return one SQL block.
2. Do not add prose.
3. Do not use placeholders.
4. Terminate statements.

## SCRIPT STRUCTURE
```
-- 0. Header block and assumptions
-- 1. Session settings
-- 2. Optional clean-up block
-- 3. Sequences
-- 4. Tables and constraints
-- 5. Foreign keys
-- 6. Indexes
-- 7. Comments
-- 8. Triggers
-- 9. Views
-- 10. Grants
-- 11. Seed data
-- 12. Verification queries
-- 13. Footer
```
""",
        "sql",
    )

    assert len(contract.members) == 14
    assert contract.members[0]["signature"] == "Header block and assumptions"
    assert contract.members[-1]["signature"] == "Footer"
    assert estimate_output_tokens(contract) > 2400


def test_svg_uses_an_xml_safe_completion_sentinel():
    assert sentinel_comment("svg") == "<!-- <<<END_OF_FILE>>> -->"
