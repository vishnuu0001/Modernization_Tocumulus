from services.generation_evaluator import evaluate_case_definition, load_evaluation_cases


def test_required_generation_evaluation_matrix_is_governance_complete():
    cases = load_evaluation_cases()
    assert {case["id"] for case in cases} == {
        "banking-angular-dotnet-aks", "microservices-java-react-aws",
        "cobol-batch", "python-fastapi-postgres",
    }
    results = [evaluate_case_definition(case) for case in cases]
    assert all(result["passed"] for result in results)
    assert all(result["input_ids"] and result["output_ids"] for result in results)
    assert all(result["rule_ids"] and result["standards"] for result in results)


def test_client_guideline_wins_over_run_default_and_conflict_is_logged():
    cases = {case["id"]: case for case in load_evaluation_cases()}
    result = evaluate_case_definition(cases["python-fastapi-postgres"])
    assert result["conflicts"]
    assert result["conflicts"][0]["winner_rule_id"] == "G-001"
