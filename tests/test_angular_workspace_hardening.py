# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: Modernization — tests (test_angular_workspace_hardening.py)
# Date: 2026-08-15
# ---------------------------------------------------------------------------
"""Regression coverage for a real, previously-observed non-deterministic
failure: a dotnet+Angular full-stack generation that built successfully once
started failing the whole-project build with

    Error: This command is not available when running the Angular CLI
    outside a workspace.

with no source change in between. Root cause: `angular.json` is deterministic
scaffolding (`_frontend_scaffold_files`), but nothing verified it actually
survived into the final output — an LLM-authored duplicate at a slightly
different relative path could silently take its place (the "does the LLM's
planned file list already cover this path" duplicate check accepted a
`scaffold_basenames` set but never actually consulted it), and nothing
re-checked or repaired an angular.json that came out missing or structurally
broken before the build ran.

This is a permanent hardening fix — see prompt_pipeline.py's
_pf_ensure_angular_workspace_scaffold docstring. Do not remove or relax
these tests without an explicit request; they exist specifically to catch a
regression of the failure mode described above.
"""
import json
import unittest

from services.modernizer.prompt_pipeline import (
    _pf_angular_workspace_is_valid,
    _pf_ensure_angular_workspace_scaffold,
    _pf_harden_framework_closure,
    _pf_is_scaffold_duplicate,
    _pf_repair_invalid_msal_account_token_access,
    _pf_repair_undeclared_angular_material_theme,
)


ANGULAR_PACKAGE_JSON = json.dumps({
    "name": "createafullstacksolutionforabank", "version": "0.0.1", "private": True,
    "scripts": {"ng": "ng", "start": "ng serve", "build": "ng build", "test": "ng test"},
    "dependencies": {"@angular/core": "^17.0.0", "@angular/common": "^17.0.0"},
    "devDependencies": {"@angular/cli": "^17.0.0"},
})


class AngularWorkspaceValidityTests(unittest.TestCase):
    # Function: test_missing_content_is_invalid
    def test_missing_content_is_invalid(self):
        self.assertFalse(_pf_angular_workspace_is_valid(None))
        self.assertFalse(_pf_angular_workspace_is_valid(""))

    # Function: test_malformed_json_is_invalid
    def test_malformed_json_is_invalid(self):
        self.assertFalse(_pf_angular_workspace_is_valid("{not json"))

    # Function: test_json_without_projects_is_invalid
    def test_json_without_projects_is_invalid(self):
        # An LLM "helpfully" rewriting angular.json can drop the one key
        # that actually makes it a workspace file while keeping it valid
        # JSON — this must still be caught.
        self.assertFalse(_pf_angular_workspace_is_valid(json.dumps({"version": 1})))
        self.assertFalse(_pf_angular_workspace_is_valid(json.dumps({"version": 1, "projects": {}})))

    # Function: test_project_without_build_architect_is_invalid
    def test_project_without_build_architect_is_invalid(self):
        broken = json.dumps({"version": 1, "projects": {"app": {"projectType": "application"}}})
        self.assertFalse(_pf_angular_workspace_is_valid(broken))

    # Function: test_well_formed_workspace_is_valid
    def test_well_formed_workspace_is_valid(self):
        good = json.dumps({
            "version": 1,
            "projects": {"app": {"architect": {"build": {"builder": "@angular-devkit/build-angular:browser"}}}},
        })
        self.assertTrue(_pf_angular_workspace_is_valid(good))


class EnsureAngularWorkspaceScaffoldTests(unittest.TestCase):
    # Function: test_missing_angular_json_is_synthesized
    def test_missing_angular_json_is_synthesized(self):
        output = {"Bank/frontend/package.json": ANGULAR_PACKAGE_JSON}
        _pf_ensure_angular_workspace_scaffold(
            output, "Bank/frontend/", json.loads(ANGULAR_PACKAGE_JSON),
        )
        self.assertIn("Bank/frontend/angular.json", output)
        self.assertTrue(_pf_angular_workspace_is_valid(output["Bank/frontend/angular.json"]))

    # Function: test_broken_angular_json_is_replaced
    def test_broken_angular_json_is_replaced(self):
        output = {
            "Bank/frontend/package.json": ANGULAR_PACKAGE_JSON,
            # Present, valid JSON, but not a workspace file — exactly the
            # kind of "close but not recognized" file that reproduced the
            # real failure and that a naive "file exists" check would miss.
            "Bank/frontend/angular.json": json.dumps({"note": "not a real angular workspace file"}),
        }
        _pf_ensure_angular_workspace_scaffold(
            output, "Bank/frontend/", json.loads(ANGULAR_PACKAGE_JSON),
        )
        self.assertTrue(_pf_angular_workspace_is_valid(output["Bank/frontend/angular.json"]))

    # Function: test_valid_angular_json_is_left_alone
    def test_valid_angular_json_is_left_alone(self):
        existing = json.dumps({
            "version": 1,
            "projects": {"app": {"architect": {"build": {"builder": "@angular-devkit/build-angular:browser"}}}},
            "customMarker": "do-not-touch",
        })
        output = {
            "Bank/frontend/package.json": ANGULAR_PACKAGE_JSON,
            "Bank/frontend/angular.json": existing,
        }
        _pf_ensure_angular_workspace_scaffold(
            output, "Bank/frontend/", json.loads(ANGULAR_PACKAGE_JSON),
        )
        self.assertEqual(existing, output["Bank/frontend/angular.json"])


class HardenFrameworkClosureAngularTests(unittest.TestCase):
    # Function: test_full_stack_project_missing_angular_json_is_repaired_before_build
    def test_full_stack_project_missing_angular_json_is_repaired_before_build(self):
        """End-to-end through the actual pre-build hardening pass, using an
        output shape representative of a real dotnet+Angular generation
        (backend .cs files + a bare frontend/package.json, no angular.json
        at all) — reproduces the exact gap that let `ng build` fail
        "outside a workspace" with no source change."""
        output = {
            "Bank/backend/Program.cs": "// minimal backend placeholder\n",
            "Bank/frontend/package.json": ANGULAR_PACKAGE_JSON,
            "Bank/frontend/src/main.ts": "// bootstrap\n",
        }
        _pf_harden_framework_closure(output)
        self.assertIn("Bank/frontend/angular.json", output)
        self.assertTrue(_pf_angular_workspace_is_valid(output["Bank/frontend/angular.json"]))

    # Function: test_non_angular_frontend_is_untouched
    def test_non_angular_frontend_is_untouched(self):
        output = {
            "Bank/frontend/package.json": json.dumps({
                "name": "bank", "dependencies": {"react": "^18.0.0"},
            }),
        }
        _pf_harden_framework_closure(output)
        self.assertNotIn("Bank/frontend/angular.json", output)


class MsalAccountTokenRepairTests(unittest.TestCase):
    # Function: test_account_info_access_token_is_replaced_with_silent_acquisition
    def test_account_info_access_token_is_replaced_with_silent_acquisition(self):
        path = "Bank/frontend/src/app/core/auth/auth.interceptor.ts"
        output = {path: """import { MsalService } from '@azure/msal-angular';
export class AuthInterceptor {
  constructor(private msalService: MsalService) {}
  token(): string | undefined {
    return this.msalService.instance.getActiveAccount()?.accessToken;
  }
}
"""}

        _pf_repair_invalid_msal_account_token_access(output)

        repaired = output[path]
        self.assertNotIn("getActiveAccount()?.accessToken", repaired)
        self.assertIn("acquireTokenSilent", repaired)
        self.assertIn("({ accessToken })", repaired)
        self.assertIn("environment.azureAdClientId", repaired)

    # Function: test_valid_msal_interceptor_is_unchanged
    def test_valid_msal_interceptor_is_unchanged(self):
        path = "Bank/frontend/src/app/core/auth/auth.interceptor.ts"
        valid = """import { MsalService } from '@azure/msal-angular';
export class AuthInterceptor {
  constructor(private msalService: MsalService) {}
  token() { return this.msalService.instance.acquireTokenSilent({ scopes: [] }); }
}
"""
        output = {path: valid}

        _pf_repair_invalid_msal_account_token_access(output)

        self.assertEqual(valid, output[path])


class AngularMaterialThemeRepairTests(unittest.TestCase):
    # Function: test_undeclared_material_theme_import_is_removed
    def test_undeclared_material_theme_import_is_removed(self):
        path = "Bank/frontend/src/styles.css"
        output = {
            path: "@import '@angular/material/prebuilt-themes/indigo-pink.css';\nbody { margin: 0; }\n",
        }

        _pf_repair_undeclared_angular_material_theme(
            output, "Bank/frontend/", {"dependencies": {"@angular/core": "^17.0.0"}},
        )

        self.assertEqual("body { margin: 0; }\n", output[path])

    # Function: test_declared_material_theme_import_is_retained
    def test_declared_material_theme_import_is_retained(self):
        path = "Bank/frontend/src/styles.css"
        css = "@import '@angular/material/prebuilt-themes/indigo-pink.css';\n"
        output = {path: css}

        _pf_repair_undeclared_angular_material_theme(
            output, "Bank/frontend/", {"dependencies": {"@angular/material": "^17.0.0"}},
        )

        self.assertEqual(css, output[path])


class ScaffoldDuplicateBasenameFallbackTests(unittest.TestCase):
    # Function: test_exact_path_match_still_works
    def test_exact_path_match_still_works(self):
        output = {"Bank/frontend/angular.json": "{}"}
        self.assertTrue(_pf_is_scaffold_duplicate(
            "frontend/angular.json", "Bank", output, (), {"angular.json"}, False, "csharp",
        ))

    # Function: test_basename_fallback_catches_a_path_the_exact_check_would_miss
    def test_basename_fallback_catches_a_path_the_exact_check_would_miss(self):
        # The LLM's own file plan proposed a path that doesn't exactly equal
        # what the deterministic scaffold wrote (missing the "frontend/"
        # segment) — before this fix, the exact-path check alone let this
        # through, and the LLM would generate a second, competing
        # angular.json that could clobber the good one depending on write
        # order.
        output = {"Bank/frontend/angular.json": "{}"}
        self.assertTrue(_pf_is_scaffold_duplicate(
            "angular.json", "Bank", output, (), {"angular.json"}, False, "csharp",
        ))

    # Function: test_unrelated_file_is_not_a_duplicate
    def test_unrelated_file_is_not_a_duplicate(self):
        output = {"Bank/frontend/angular.json": "{}"}
        self.assertFalse(_pf_is_scaffold_duplicate(
            "frontend/src/app/app.component.ts", "Bank", output, (), {"angular.json"}, False, "csharp",
        ))


if __name__ == "__main__":
    unittest.main()
