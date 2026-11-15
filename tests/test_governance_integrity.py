# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: Modernization — tests (test_governance_integrity.py)
# Date: 2025-09-24
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Scope: Evidence-backed governance plan and honest contract checks
# ---------------------------------------------------------------------------
import json
import shutil
import unittest
import tempfile
from pathlib import Path

from services.governance import (
    ProjectStore, effective_contracts_for_validation, generate_contracts,
    generate_plan, infer_prompt_requirements, semantic_index, validate_contracts,
)
from services.modernizer.target_config import TARGET_STACKS


class GovernanceIntegrityTests(unittest.TestCase):
    def test_legacy_prompt_contract_is_migrated_from_its_authoritative_prompt(self):
        prompt = """Create one Oracle SQL file.
My tables:
- ACCOUNTS: [account_id, opening_balance]
- TRANSACTIONS: [txn_id, account_id, amount]
Include DDL and balance queries.
"""
        legacy = {
            "schema_version": 1,
            "target_stack": prompt,
            "contract_source": "prompt-requirements-manifest",
            "domain_models": [],
            "database_schema": [],
            "checksum": "legacy-checksum",
        }

        effective = effective_contracts_for_validation(legacy)

        self.assertEqual(["domain_models", "database_schema"], effective["required_contract_sections"])
        self.assertEqual(["ACCOUNTS", "TRANSACTIONS"], effective["domain_models"])
        self.assertEqual(
            ["ACCOUNTS", "TRANSACTIONS"],
            [item["name"] for item in effective["database_schema"]],
        )
        self.assertEqual("legacy-checksum", effective["migrated_from_contract_checksum"])
        self.assertTrue(validate_contracts(effective)["valid"])
        self.assertNotIn("required_contract_sections", legacy)

    def test_database_only_prompt_requires_only_its_applicable_contracts(self):
        prompt = """Create one Oracle SQL/PLSQL file.
My tables:
- ACCOUNTS: [account_id, account_no, opening_balance]
- TRANSACTIONS: [txn_id, account_id, amount]
Include DDL, balance queries, and a stored function.
"""
        contracts = generate_contracts({}, "Oracle Database", prompt)
        validation = validate_contracts(contracts)

        self.assertTrue(validation["valid"], validation["errors"])
        self.assertEqual(
            ["domain_models", "database_schema"],
            contracts["required_contract_sections"],
        )
        self.assertEqual(
            {"ACCOUNTS", "TRANSACTIONS"},
            {item["name"] for item in contracts["database_schema"]},
        )
        self.assertEqual([], contracts["dtos"])
        self.assertEqual([], contracts["route_definitions"])
        self.assertEqual([], contracts["authentication_model"])

    def test_every_catalog_language_accepts_a_prompt_without_unrequested_web_contracts(self):
        for stack_id, target in TARGET_STACKS.items():
            language = str(target.get("language") or "source")
            contracts = generate_contracts(
                {}, str(target.get("name") or stack_id),
                f"Generate one complete {language} source artifact.",
            )
            validation = validate_contracts(contracts)
            self.assertTrue(validation["valid"], f"{stack_id}: {validation['errors']}")
            self.assertEqual([], contracts["required_contract_sections"], stack_id)

    def test_prompt_contracts_are_derived_from_manifest_not_brief_regex_noise(self):
        prompt = """Create a .NET 10 service.
| Method | Route | Purpose |
|---|---|---|
| POST | /api/widgets | Create widget |
| GET | /api/widgets/{id} | Get widget |
Database schema containing:
- Widget
Roles:
- Operator
Integrations:
- Publish WidgetCreated events
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "project-brief.json").write_text(
                json.dumps({"project_prompt": prompt}), encoding="utf-8",
            )
            index = semantic_index(root)
        contracts = generate_contracts(index, "custom", prompt)
        validation = validate_contracts(contracts)

        self.assertTrue(validation["valid"], validation["errors"])
        self.assertEqual("prompt-requirements-manifest", contracts["contract_source"])
        self.assertEqual(2, len(contracts["api_contracts"]))
        self.assertNotIn("the", [value.casefold() for value in contracts["domain_models"]])
        self.assertNotIn("post", [value.casefold() for value in contracts["domain_models"]])
        self.assertNotEqual("project-brief.json", contracts["authentication_model"])

    def test_store_reconciles_missing_snapshot_files_from_copied_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "projects"
            source = Path(directory) / "source"
            source.mkdir()
            (source / "app.py").write_text("print('ok')", encoding="utf-8")
            store = ProjectStore(root)
            project = store.create_project("Example", source, "tester")
            snapshot_path = Path(project["snapshots"][0]["path"])
            shutil.rmtree(snapshot_path)

            reopened = ProjectStore(root)
            recovered = reopened.get_project(project["id"])

            self.assertEqual([], recovered["snapshots"])
            self.assertTrue(recovered["configuration"]["storage_recovery_required"])
            self.assertEqual(1, reopened.storage_reconciliation["removed"])
            self.assertTrue(Path(reopened.storage_reconciliation["backup"]).is_file())

    # Function: test_plan_exposes_unresolved_operational_decisions
    def test_plan_proposes_non_blocking_operational_decisions_for_confirmation(self):
        plan = generate_plan(
            {
                "project_type": "legacy",
                "summary": {"architecture": "monolith"},
                "requested_target": {
                    "architecture": None,
                    "deployment": None,
                    "database": "PostgreSQL",
                },
            },
            {
                "hierarchy": {"modules": {"billing": {}}},
                "database_access": [{"file": "billing.sql"}],
                "authentication_authorization_flow": ["OAuth"],
            },
            "Java 21 Spring Boot",
        )
        self.assertTrue(plan["ready_for_approval"], plan["unresolved_requirements"])
        self.assertEqual([], plan["unresolved_requirements"])
        self.assertTrue(plan["target_architecture"]["style"])
        self.assertTrue(plan["deployment_approach"])
        self.assertTrue(plan["cutover_approach"])
        self.assertTrue(plan["rollback_approach"])
        self.assertEqual("PENDING_CONFIRMATION", plan["confirmation_status"])
        self.assertEqual(4, len(plan["proposed_decisions"]))

    # Function: test_proposed_decisions_can_be_confirmed_without_creating_blockers
    def test_proposed_decisions_can_be_confirmed_without_creating_blockers(self):
        plan = generate_plan(
            {"project_type": "legacy", "requested_target": {}},
            {"hierarchy": {"modules": {"orders": {}}}},
            "Java 21 Spring Boot",
        )
        with tempfile.TemporaryDirectory() as directory:
            store = ProjectStore(Path(directory) / "projects")
            project = store.create_prompt_project(
                "Confirmation test", "Temporary governed baseline", "admin@example.test",
            )
            snapshot = store.add_json_snapshot(
                project["id"], "plans", plan, "admin@example.test", filename="plan.json",
            )
            confirmed_snapshot = store.update_plan(
                project["id"], snapshot["id"], {"decisions_confirmed": True}, "admin@example.test",
            )
            confirmed = json.loads(
                (Path(confirmed_snapshot["path"]) / "plan.json").read_text(encoding="utf-8")
            )
        self.assertEqual("CONFIRMED", confirmed["confirmation_status"])
        self.assertTrue(confirmed["decisions_confirmed"])
        self.assertTrue(confirmed["ready_for_approval"])
        self.assertEqual([], confirmed["unresolved_requirements"])
        self.assertTrue(all(item["status"] == "CONFIRMED" for item in confirmed["proposed_decisions"]))

    def test_plan_revisions_preserve_executable_target_metadata_across_lineage(self):
        plan = generate_plan(
            {"project_type": "legacy", "requested_target": {}},
            {"hierarchy": {"modules": {"trains": {}}}},
            "Java 21 with Spring Boot 3 and React 19",
        )
        custom_description = "Java 21 with Spring Boot 3, React 19, and PostgreSQL 17"
        with tempfile.TemporaryDirectory() as directory:
            store = ProjectStore(Path(directory) / "projects")
            project = store.create_prompt_project(
                "Target lineage test", "Modernize the train system", "admin@example.test",
            )
            original = store.add_json_snapshot(
                project["id"], "plans", plan, "admin@example.test",
                {"target_stack": "custom", "custom_stack_desc": custom_description},
                filename="plan.json",
            )
            first_revision = store.update_plan(
                project["id"], original["id"], {"decisions_confirmed": True},
                "admin@example.test",
            )
            second_revision = store.update_plan(
                project["id"], first_revision["id"], {"deployment_approach": "Kubernetes"},
                "admin@example.test",
            )

        self.assertEqual("custom", first_revision["metadata"]["target_stack"])
        self.assertEqual(custom_description, first_revision["metadata"]["custom_stack_desc"])
        self.assertEqual("custom", second_revision["metadata"]["target_stack"])
        self.assertEqual(custom_description, second_revision["metadata"]["custom_stack_desc"])

    # Function: test_contract_validator_never_claims_unperformed_alignment
    def test_contract_validator_never_claims_unperformed_alignment(self):
        result = validate_contracts({
            "domain_models": ["Account"],
            "interfaces": ["AccountService"],
            "route_definitions": [],
            "database_schema": [],
            "dependency_versions": {},
        })
        self.assertEqual("not_evaluated", result["checks"]["client_api_alignment"])
        self.assertEqual("not_evaluated", result["checks"]["dependency_compatibility"])

    # Function: test_prompt_brief_resolves_explicit_generation_requirements
    def test_prompt_brief_resolves_explicit_generation_requirements(self):
        prompt = (
            "Create a Java 21 Spring Boot 3 order service using an event-driven architecture. "
            "Use PostgreSQL with Spring Data JPA and Flyway. Include OAuth2/JWT authorization "
            "with ADMIN and ORDER_USER roles. Provide Dockerfiles, docker-compose.yml, and "
            "Kubernetes manifests."
        )
        inferred = infer_prompt_requirements(prompt)
        self.assertIn("Event-driven", inferred["architecture"])
        self.assertEqual("PostgreSQL + Spring Data JPA + Flyway", inferred["database"])
        self.assertIn("OAuth2", inferred["authorization"])
        self.assertIn("Kubernetes", inferred["deployment"])

        plan = generate_plan(
            {
                "project_type": "greenfield",
                "project_prompt": prompt,
                "requested_target": {
                    "framework": "Spring Boot 3",
                    "runtime": "Java 21",
                    "architecture": "",
                    "database": "",
                    "deployment": "",
                },
            },
            {"hierarchy": {"modules": {}}, "authentication_authorization_flow": []},
            "java21_spring",
        )
        self.assertTrue(plan["ready_for_approval"], plan["unresolved_requirements"])
        self.assertEqual([], plan["unresolved_requirements"])
        self.assertEqual([], plan["manual_tasks"])
        self.assertIn("JWT bearer validation", plan["security_changes"])

    # Function: test_prompt_project_uses_inferred_facts_and_non_blocking_defaults
    def test_prompt_project_uses_inferred_facts_and_non_blocking_defaults(self):
        prompt = (
            "Create a Full Stack Banking Application with Angular as Frontend and dotnet 10 "
            "as backend, deployed to AKS, using Azure Entra ID B2B. Utilize Dapper as ORM."
        )
        inferred = infer_prompt_requirements(prompt)
        self.assertEqual("Angular", inferred["frontend"])
        self.assertEqual(".NET 10", inferred["runtime"])
        self.assertIn("AKS", inferred["deployment"])
        self.assertIn("Entra ID", " ".join(inferred["authorization"]))
        self.assertIn("Dapper", inferred["database"])

        plan = generate_plan(
            {
                "project_type": "greenfield",
                "project_prompt": prompt,
                "requested_target": {},
            },
            {"hierarchy": {"modules": {}}, "authentication_authorization_flow": []},
            "custom",
        )
        self.assertTrue(plan["ready_for_approval"], plan["unresolved_requirements"])
        self.assertEqual([], plan["unresolved_requirements"])
        self.assertEqual([], plan["manual_tasks"])
        self.assertIn("Angular", plan["target_architecture"]["frontend"])
        self.assertIn("AKS", plan["deployment_approach"])

    # Function: test_delete_project_removes_catalog_and_quarantines_snapshots
    def test_delete_project_removes_catalog_and_quarantines_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProjectStore(Path(directory) / "projects")
            project = store.create_prompt_project(
                "Disposable", "Create a small test project", "admin@example.test",
            )
            result = store.delete_project(project["id"], "admin@example.test")
            self.assertTrue(result["deleted"])
            self.assertEqual([], store.list_projects())
            self.assertFalse((store.root / project["id"]).exists())
            self.assertTrue(any((store.root / ".trash").iterdir()))
            with self.assertRaises(KeyError):
                store.get_project(project["id"])


if __name__ == "__main__":
    unittest.main()
