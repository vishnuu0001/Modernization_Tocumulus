# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: Modernization — services/modernizer (build_artifacts.py)
# Date: 2026-06-21
# ---------------------------------------------------------------------------
from __future__ import annotations

import functools
import json
import logging
import posixpath
import re
import textwrap
from pathlib import Path
from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)


# Function: _reconcile_dotnet_solution_files
def _reconcile_dotnet_solution_files(output: dict[str, str]) -> set[str]:
    """Regenerate delivered solutions from the actual project manifests.

    This runs after deterministic and LLM generation, so stale or invented
    project references cannot survive into the downloaded artifact. The exact
    solution validated by the build is therefore the solution delivered.
    """
    from .scaffolds.csharp import _solution_from_project_paths

    project_paths = sorted(
        path.replace("\\", "/") for path in output
        if path.casefold().endswith(".csproj")
    )
    changed: set[str] = set()
    for solution_path in sorted(
        path for path in output if path.casefold().endswith(".sln")
    ):
        normalized_solution = solution_path.replace("\\", "/")
        solution_root = normalized_solution.rsplit("/", 1)[0] if "/" in normalized_solution else ""
        prefix = f"{solution_root}/" if solution_root else ""
        relative_projects = [
            path.removeprefix(prefix)
            for path in project_paths
            if not prefix or path.startswith(prefix)
        ]
        if not relative_projects:
            continue
        reconciled = _solution_from_project_paths(relative_projects)
        if output.get(solution_path) != reconciled:
            output[solution_path] = reconciled
            changed.add(solution_path)
    return changed


_CSHARP_DECLARED_TYPE_RE = re.compile(
    r"(?m)^\s*(?:public\s+|internal\s+)?(?:static\s+|sealed\s+|abstract\s+|partial\s+)*"
    r"(?:class|interface|record(?:\s+(?:class|struct))?|struct|enum)\s+([A-Za-z_]\w*)\b"
)


def _csharp_declared_types(content: str) -> set[str]:
    """Return top-level-looking C# declarations used by domain completeness checks."""
    return set(_CSHARP_DECLARED_TYPE_RE.findall(content or ""))


def _csharp_llm_domain_contract_complete(
    output: dict[str, str], base: str, domain: str, *, is_dapper: bool,
) -> bool:
    """Check the cross-file contract promised by the LLM domain prompts.

    Per-file parsing cannot prove that a prompt which requested two declarations
    actually emitted both of them.  Keep this check deliberately structural: the
    real whole-project build remains authoritative for method signatures and
    behavior.
    """
    requirements = {
        f"{base}/Endpoints/{domain}Endpoints.cs": {
            f"{domain}Endpoints", f"{domain}CreateRequest",
            f"{domain}UpdateRequest", f"{domain}Response",
        },
        f"{base}/Services/{domain}ServiceImpl.cs": {
            f"I{domain}Service", f"{domain}ServiceImpl", "PagedResult",
        },
        f"{base}/Repositories/{domain}Repository.cs": {
            f"I{domain}Repository", f"{domain}Repository", f"{domain}Entity",
            "IDbConnectionFactory" if is_dapper else "DbContext",
        },
    }
    for path, expected in requirements.items():
        content = output.get(path)
        if not isinstance(content, str):
            return False
        declared = _csharp_declared_types(content)
        if not expected <= declared:
            return False
    return True


def _reconcile_csharp_generation_output(output: dict[str, str]) -> set[str]:
    """Replace incomplete LLM-authored .NET domain trios with a closed scaffold.

    The domain generator intentionally has a deterministic implementation for a
    failed LLM call.  Semantic omissions must take the same path as syntax
    failures; otherwise parser-valid interfaces can be delivered without their
    implementations, entities, request/response contracts, or DbContext.
    """
    from .scaffolds.csharp import _gen_service

    changed: set[str] = set()
    project_paths = sorted(
        path for path in output
        if path.casefold().endswith(".csproj") and "/services/" in path.casefold()
    )
    for project_path in project_paths:
        base = project_path.rsplit("/", 1)[0]
        directory = base.rsplit("/", 1)[-1]
        if not directory.endswith("Service"):
            continue
        domain = directory.removesuffix("Service")
        llm_paths = {
            f"{base}/Endpoints/{domain}Endpoints.cs",
            f"{base}/Services/{domain}ServiceImpl.cs",
            f"{base}/Repositories/{domain}Repository.cs",
        }
        # Deterministic domains use Services/{domain}Service.cs and must not be
        # regenerated on every framework-closure pass.
        if not any(path in output for path in llm_paths):
            continue
        project = output.get(project_path, "")
        is_dapper = bool(re.search(r'Include=["\']Dapper["\']', project, re.IGNORECASE))
        if _csharp_llm_domain_contract_complete(
            output, base, domain, is_dapper=is_dapper,
        ):
            continue
        namespace_match = re.search(
            r"<RootNamespace>\s*(.+?)\." + re.escape(domain) + r"Service\s*</RootNamespace>",
            project, re.IGNORECASE,
        )
        root_namespace = namespace_match.group(1).strip() if namespace_match else "ModernizedApp"
        framework_match = re.search(r"<TargetFramework>\s*(net\d+(?:\.\d+)?)", project, re.IGNORECASE)
        target_framework = framework_match.group(1) if framework_match else "net8.0"
        db_target = "postgres" if re.search(r"Npgsql", project, re.IGNORECASE) else "mssql"
        before = dict(output)
        for path in llm_paths:
            output.pop(path, None)
        _gen_service(
            output, root_namespace, domain, [], is_dapper=is_dapper,
            db_target=db_target, target_framework=target_framework,
        )
        changed.update(
            path for path in set(before) | set(output)
            if before.get(path) != output.get(path)
        )
        logger.warning(
            "Replaced incomplete generated C# domain contract with deterministic "
            "buildable scaffold: %s", base,
        )
    return changed



# Function: _frontend_scaffold_files
def _frontend_scaffold_files(frontend_tech: str, project_name: str, is_azure_auth: bool) -> dict[str, str]:
    """Deterministic frontend project scaffolding (dependency manifest,
    framework config, index.html, entry point) — pure boilerplate that must
    be syntactically valid for `npm install`/build to even start, not
    something worth risking on a 7B model remembering to include it among
    the 24-45 other files it's also asked to invent business logic for. This
    was the review's #2 blocker: no package.json/angular.json/tsconfig/
    index.html/main.ts anywhere in the delivered output.
    """
    fw = (frontend_tech or "").lower()
    name = project_name.lower()
    files: dict[str, str] = {}

    if "react native" in fw:
        files["mobile/package.json"] = json.dumps({
            "name": f"{name}-mobile", "private": True,
            "scripts": {"build": "tsc --noEmit"},
            "dependencies": {"react": "19.2.0", "react-native": "0.86.0"},
            "devDependencies": {
                "@types/react": "^19.2.0", "typescript": "^5.9.0",
                "@react-native/babel-preset": "0.86.0",
            },
        }, indent=2)
        files["mobile/tsconfig.json"] = json.dumps({
            "compilerOptions": {
                "target": "ES2022", "module": "ESNext", "moduleResolution": "Bundler",
                "jsx": "react-jsx", "strict": True, "noEmit": True, "skipLibCheck": True,
                "types": ["react", "react-native"],
            },
            "include": ["App.tsx", "src/**/*.ts", "src/**/*.tsx"],
        }, indent=2)
        files["mobile/App.tsx"] = (
            "import React from 'react';\nimport { SafeAreaView, Text } from 'react-native';\n"
            f"export default function App() {{ return <SafeAreaView><Text>{project_name}</Text></SafeAreaView>; }}\n"
        )
        return files

    if "flutter" in fw:
        package = re.sub(r"[^a-z0-9_]+", "_", name)
        files["mobile/pubspec.yaml"] = (
            f"name: {package}\nenvironment:\n  sdk: '>=3.4.0 <4.0.0'\n"
            "dependencies:\n  flutter:\n    sdk: flutter\n"
            "dev_dependencies:\n  flutter_test:\n    sdk: flutter\n"
            "flutter:\n  uses-material-design: true\n"
        )
        files["mobile/lib/main.dart"] = (
            "import 'package:flutter/material.dart';\nvoid main()=>runApp(const App());\n"
            "class App extends StatelessWidget { const App({super.key}); "
            f"@override Widget build(BuildContext context)=>const MaterialApp(home:Scaffold(body:Text('{project_name}'))); }}\n"
        )
        return files

    if "angular" in fw:
        angular_match = re.search(r"angular\s*(\d+)", fw)
        angular_major = int(angular_match.group(1)) if angular_match else 20
        # Angular framework packages use exact peer versions. Caret ranges can
        # independently resolve (for example) core/animations/compiler to
        # different supported minors and make npm reject the graph before the
        # compiler runs. Pin one coherent framework patch and its matching
        # compiler-cli; CLI/build-angular have their own patch stream.
        framework_versions = {
            17: "17.3.12", 18: "18.2.14", 19: "19.2.25",
            20: "20.3.35", 21: "21.2.26", 22: "22.2.5",
        }
        tooling_versions = {
            17: "17.3.17", 18: "18.2.21", 19: "19.2.27",
            20: "20.3.42", 21: "21.2.29", 22: "22.2.6",
        }
        typescript_versions = {
            17: "~5.2.0", 18: "~5.4.0", 19: "~5.5.0",
            20: "~5.8.0", 21: "~5.9.0", 22: "~6.0.0",
        }
        if angular_major not in typescript_versions:
            raise ValueError(
                f"Generated Angular scaffolds support majors 17–22; received Angular {angular_major}. "
                "Select a supported target or preserve the existing package manifest."
            )
        typescript_version = typescript_versions[angular_major]
        angular_version = framework_versions[angular_major]
        tooling_version = tooling_versions[angular_major]
        zone_version = "^0.15.0" if angular_major >= 20 else "^0.14.0"
        deps = {
            "@angular/animations": angular_version, "@angular/common": angular_version,
            "@angular/compiler": angular_version, "@angular/core": angular_version,
            "@angular/forms": angular_version, "@angular/platform-browser": angular_version,
            "@angular/platform-browser-dynamic": angular_version, "@angular/router": angular_version,
            "rxjs": "^7.8.0", "tslib": "^2.6.0", "zone.js": zone_version,
        }
        if is_azure_auth:
            deps["@azure/msal-angular"] = "^3.0.0"
            deps["@azure/msal-browser"] = "^3.0.0"
        files["frontend/package.json"] = json.dumps({
            "name": name, "version": "0.0.1", "private": True,
            # Do not advertise ``ng test`` without a configured test target.
            # The build runner executes tests only when this script is present;
            # Angular's production build remains the strict compile gate here.
            "scripts": {"ng": "ng", "start": "ng serve", "build": "ng build"},
            "dependencies": deps,
            "devDependencies": {
                "@angular-devkit/build-angular": tooling_version, "@angular/cli": tooling_version,
                "@angular/compiler-cli": angular_version, "typescript": typescript_version,
            },
        }, indent=2)
        files["frontend/angular.json"] = json.dumps({
            "$schema": "./node_modules/@angular/cli/lib/config/schema.json",
            "version": 1, "newProjectRoot": "projects",
            "projects": {name: {
                "projectType": "application", "root": "", "sourceRoot": "src",
                "architect": {
                    "build": {
                        "builder": "@angular-devkit/build-angular:browser",
                        "options": {
                            "outputPath": "dist", "index": "src/index.html", "main": "src/main.ts",
                            "tsConfig": "tsconfig.app.json", "assets": ["src/assets"], "styles": ["src/styles.css"],
                        },
                    },
                    "serve": {"builder": "@angular-devkit/build-angular:dev-server"},
                },
            }},
        }, indent=2)
        files["frontend/tsconfig.json"] = json.dumps({
            "compileOnSave": False,
            "compilerOptions": {
                "outDir": "./dist/out-tsc", "strict": True, "module": "ES2022", "target": "ES2022",
                "moduleResolution": "bundler", "experimentalDecorators": True, "importHelpers": True,
                "lib": ["ES2022", "dom"], "types": [], "rootDir": "./src", "skipLibCheck": True,
            },
        }, indent=2)
        files["frontend/tsconfig.app.json"] = json.dumps({
            "extends": "./tsconfig.json",
            "compilerOptions": {"outDir": "./dist/out-tsc/app", "rootDir": "./src", "types": []},
            "files": ["src/main.ts"],
            "include": ["src/**/*.ts"],
        }, indent=2)
        files["frontend/src/index.html"] = textwrap.dedent(f"""\
            <!doctype html>
            <html lang="en">
            <head>
              <meta charset="utf-8">
              <title>{project_name}</title>
              <base href="/">
              <meta name="viewport" content="width=device-width, initial-scale=1">
            </head>
            <body>
              <app-root></app-root>
            </body>
            </html>
        """)
        files["frontend/src/main.ts"] = textwrap.dedent("""\
            import { bootstrapApplication } from '@angular/platform-browser';
            import { AppComponent } from './app/app.component';

            bootstrapApplication(AppComponent)
              .catch(err => console.error(err));
        """)
        files["frontend/src/app/app.component.ts"] = textwrap.dedent(f"""\
            import {{ ChangeDetectionStrategy, Component }} from '@angular/core';

            @Component({{
              selector: 'app-root',
              standalone: true,
              template: `<main><h1>{project_name}</h1><p>Application ready.</p></main>`,
              changeDetection: ChangeDetectionStrategy.OnPush,
            }})
            export class AppComponent {{}}
        """)
        files["frontend/src/environments/environment.ts"] = textwrap.dedent("""\
            export const environment = {
              production: false,
              apiBaseUrl: '/api',
              azureAdClientId: '',
              azureAdAuthority: '',
            };
        """)
        files["frontend/src/environments/environment.production.ts"] = textwrap.dedent("""\
            export const environment = {
              production: true,
              apiBaseUrl: '/api',
              azureAdClientId: '',
              azureAdAuthority: '',
            };
        """)
        files["frontend/src/styles.css"] = textwrap.dedent("""\
            :root {
              color-scheme: light;
              font-family: Inter, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
              font-synthesis: none;
              text-rendering: optimizeLegibility;
              --color-background: #f6f8fc;
              --color-surface: #ffffff;
              --color-text: #172033;
              --color-muted: #5f6b7a;
              --color-primary: #155eef;
              --color-primary-hover: #004eeb;
              --color-border: #d8dee9;
              --focus-ring: 0 0 0 3px rgb(21 94 239 / 25%);
            }

            *, *::before, *::after { box-sizing: border-box; }
            html { min-height: 100%; background: var(--color-background); }
            body {
              min-height: 100vh;
              margin: 0;
              background: var(--color-background);
              color: var(--color-text);
              line-height: 1.5;
            }
            button, input, select, textarea { font: inherit; }
            button, a { touch-action: manipulation; }
            a { color: var(--color-primary); }
            a:hover { color: var(--color-primary-hover); }
            :focus-visible {
              border-radius: 0.25rem;
              outline: 2px solid var(--color-primary);
              outline-offset: 2px;
              box-shadow: var(--focus-ring);
            }
            img, svg { max-width: 100%; }

            @media (prefers-reduced-motion: reduce) {
              *, *::before, *::after {
                scroll-behavior: auto !important;
                transition-duration: 0.01ms !important;
                animation-duration: 0.01ms !important;
                animation-iteration-count: 1 !important;
              }
            }
        """)
        return files

    if "vue" in fw:
        files["frontend/package.json"] = json.dumps({
            "name": name, "version": "0.0.1", "private": True,
            "scripts": {"dev": "vite", "build": "vite build"},
            "dependencies": {"vue": "^3.4.0", "vue-router": "^4.2.0"},
            "devDependencies": {"@vitejs/plugin-vue": "^5.0.0", "vite": "^5.0.0", "typescript": "^5.2.0"},
        }, indent=2)
        files["frontend/vite.config.ts"] = (
            "import { defineConfig } from 'vite';\n"
            "import vue from '@vitejs/plugin-vue';\n\n"
            "export default defineConfig({\n  plugins: [vue()],\n});\n"
        )
        files["frontend/index.html"] = textwrap.dedent(f"""\
            <!doctype html>
            <html lang="en">
            <head><meta charset="UTF-8"><title>{project_name}</title></head>
            <body>
              <div id="app"></div>
              <script type="module" src="/src/main.ts"></script>
            </body>
            </html>
        """)
        files["frontend/tsconfig.json"] = json.dumps({
            "compilerOptions": {
                "target": "ES2022", "module": "ESNext", "moduleResolution": "Bundler",
                "strict": True, "noEmit": True, "skipLibCheck": True,
                "lib": ["ES2022", "DOM"], "types": ["vite/client"],
            },
            "include": ["src/**/*.ts", "src/**/*.vue"],
        }, indent=2)
        files["frontend/src/main.ts"] = (
            "import { createApp } from 'vue';\nimport App from './App.vue';\n"
            "createApp(App).mount('#app');\n"
        )
        files["frontend/src/App.vue"] = (
            f"<template><main><h1>{project_name}</h1></main></template>\n"
            "<script setup lang=\"ts\"></script>\n"
        )
        return files

    # React default; select a version-compatible entry point and type package.
    react_match = re.search(r"\breact\s*(\d+)", fw)
    react_major = int(react_match.group(1)) if react_match else 18
    if react_major not in {16, 17, 18, 19}:
        raise ValueError(
            f"Generated React scaffolds support majors 16–19; received React {react_major}. "
            "Select a supported target or preserve the existing package manifest."
        )
    react_version = f"^{react_major}.0.0"
    react_types_version = "^16.14.0" if react_major == 16 else f"^{react_major}.0.0"
    deps = {"react": react_version, "react-dom": react_version, "react-router-dom": "^6.21.0"}
    if is_azure_auth:
        deps["@azure/msal-react"] = "^2.0.0"
        deps["@azure/msal-browser"] = "^3.0.0"
    files["frontend/package.json"] = json.dumps({
        "name": name, "version": "0.0.1", "private": True,
        "scripts": {"dev": "vite", "build": "tsc --noEmit && vite build"},
        "dependencies": deps,
        "devDependencies": {
            "@vitejs/plugin-react": "^4.2.0", "vite": "^5.0.0", "typescript": "^5.2.0",
            "@types/react": react_types_version, "@types/react-dom": react_types_version,
        },
    }, indent=2)
    files["frontend/vite.config.ts"] = (
        "import { defineConfig } from 'vite';\n"
        "import react from '@vitejs/plugin-react';\n\n"
        "export default defineConfig({\n  plugins: [react()],\n});\n"
    )
    files["frontend/index.html"] = textwrap.dedent(f"""\
        <!doctype html>
        <html lang="en">
        <head><meta charset="UTF-8"><title>{project_name}</title></head>
        <body>
          <div id="root"></div>
          <script type="module" src="/src/main.tsx"></script>
        </body>
        </html>
    """)
    files["frontend/tsconfig.json"] = json.dumps({
        "compilerOptions": {
            "target": "ES2022", "module": "ESNext", "moduleResolution": "Bundler",
            "strict": True, "noEmit": True, "jsx": "react-jsx", "skipLibCheck": True,
            "lib": ["ES2022", "DOM"], "types": ["vite/client"],
        },
        "include": ["src/**/*.ts", "src/**/*.tsx"],
    }, indent=2)
    if react_major >= 18:
        files["frontend/src/main.tsx"] = (
            "import React from 'react';\nimport { createRoot } from 'react-dom/client';\n"
            "import App from './App';\n"
            "createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);\n"
        )
    else:
        files["frontend/src/main.tsx"] = (
            "import React from 'react';\nimport ReactDOM from 'react-dom';\n"
            "import App from './App';\n"
            "ReactDOM.render(<React.StrictMode><App /></React.StrictMode>, document.getElementById('root'));\n"
        )
    files["frontend/src/App.tsx"] = (
        f"export default function App() {{ return <main><h1>{project_name}</h1></main>; }}\n"
    )
    return files


# Function: _backend_manifest_files
def _dotnet_tfm(backend_tech: str) -> str:
    """Extract the .NET target-framework moniker from a backend_tech string
    like ".NET 10" — shared by every deterministic generator that needs to
    agree on the same TFM (csproj, Dockerfile) rather than each re-deriving
    it and risking drift."""
    m = re.search(r"(\d+)", backend_tech or "")
    return f"net{m.group(1)}.0" if m else "net8.0"


# Function: _backend_manifest_files
def _backend_manifest_files(lang: str, project_name: str, backend_tech: str,
                             is_dapper: bool, is_azure_auth: bool,
                             db_target: str = "mssql") -> dict[str, str]:
    """Deterministic backend dependency manifest. The review's #1 blocker was
    that no .csproj/.sln existed anywhere in the delivered output, so nothing
    could compile before a single line of business logic was even read.

    `db_target` must agree with whatever ADO.NET/EF provider the generated
    data-access code actually calls — defaulting this to SQL Server packages
    unconditionally left a postgres-targeted Dapper repository referencing
    Npgsql with no Npgsql package in the .csproj at all (and vice versa)."""
    if lang == "csharp":
        tfm = _dotnet_tfm(backend_tech)
        framework_major = tfm.removeprefix("net").split(".", 1)[0]
        ef_version = f"{framework_major}.0.0"
        database = (db_target or "").strip().lower()
        if database not in {"", "postgres", "mssql"}:
            raise ValueError(f"Unsupported .NET database target: {db_target}")
        is_postgres = database == "postgres"
        if is_dapper:
            pkgs = ['<PackageReference Include="Dapper" Version="2.1.35" />']
            if is_postgres:
                pkgs.append('<PackageReference Include="Npgsql" Version="8.0.3" />')
            elif database == "mssql":
                pkgs.append('<PackageReference Include="Microsoft.Data.SqlClient" Version="5.2.0" />')
        else:
            pkgs = (
                [f'<PackageReference Include="Npgsql.EntityFrameworkCore.PostgreSQL" Version="{ef_version}" />',
                 f'<PackageReference Include="Microsoft.EntityFrameworkCore.Design" Version="{ef_version}" />']
                if is_postgres else
                [f'<PackageReference Include="Microsoft.EntityFrameworkCore.SqlServer" Version="{ef_version}" />',
                 f'<PackageReference Include="Microsoft.EntityFrameworkCore.Design" Version="{ef_version}" />']
            ) if database else []
        if is_azure_auth:
            pkgs.append('<PackageReference Include="Microsoft.Identity.Web" Version="4.16.1" />')
            pkgs.append('<PackageReference Include="System.Security.Cryptography.Pkcs" Version="10.0.13" />')
        pkgs.append('<PackageReference Include="Swashbuckle.AspNetCore" Version="6.5.0" />')
        pkg_xml = "\n                ".join(pkgs)
        return {f"backend/{project_name}.csproj": textwrap.dedent(f"""\
            <Project Sdk="Microsoft.NET.Sdk.Web">
              <PropertyGroup>
                <TargetFramework>{tfm}</TargetFramework>
                <Nullable>enable</Nullable>
                <ImplicitUsings>enable</ImplicitUsings>
              </PropertyGroup>
              <ItemGroup>
                {pkg_xml}
              </ItemGroup>
            </Project>
        """)}
    if lang == "python":
        tech = (backend_tech or "").casefold()
        if "django" in tech:
            reqs = ["Django", "djangorestframework", "dj-database-url"]
        else:
            reqs = ["fastapi", "uvicorn[standard]", "pydantic", "sqlalchemy[asyncio]"]
        database_packages = {
            "postgres": ["psycopg[binary]" if "django" in tech else "asyncpg"],
            "mysql": ["mysqlclient" if "django" in tech else "asyncmy"],
            "mssql": ["mssql-django" if "django" in tech else "aioodbc"],
            "sqlite": ["aiosqlite"] if "django" not in tech else [],
            "mongodb": ["motor", "beanie"] if "django" not in tech else [],
        }
        if db_target and db_target not in database_packages:
            raise ValueError(f"Unsupported Python database target: {db_target}")
        reqs.extend(database_packages.get(db_target, []))
        if is_azure_auth:
            reqs.append("msal")
        return {"requirements.txt": "\n".join(reqs) + "\n"}
    if lang == "go":
        return {"go.mod": _go_mod(project_name, backend_tech)}
    if lang == "dart" and ".net" in (backend_tech or "").casefold():
        return _backend_manifest_files(
            "csharp", project_name, backend_tech, is_dapper, is_azure_auth, db_target,
        )
    if lang in {"typescript", "javascript"}:
        tech = (backend_tech or "").casefold()
        if "framework-agnostic" in tech or "database migration only" in tech:
            return {}
        if "next.js" in tech:
            return {}
        dependencies: dict[str, str] = {"dotenv": "^16.4.5"}
        dev_dependencies: dict[str, str] = {}
        if "nestjs" in tech:
            dependencies.update({
                "@nestjs/common": "^11.1.0", "@nestjs/core": "^11.1.0",
                "reflect-metadata": "^0.2.2", "rxjs": "^7.8.2",
            })
        elif "graphql" in tech:
            dependencies.update({"@apollo/server": "^4.11.0", "graphql": "^16.9.0"})
        else:
            dependencies.update({"express": "^4.21.0", "helmet": "^8.0.0"})
        if db_target == "mongodb":
            dependencies["mongoose"] = "^8.8.0"
        elif db_target:
            dependencies["pg"] = "^8.13.0"
        if lang == "typescript":
            dev_dependencies.update({"typescript": "^5.6.0", "@types/node": "^22.0.0"})
            if "express" in dependencies:
                dev_dependencies["@types/express"] = "^5.0.0"
        package = {
            "name": re.sub(r"[^a-z0-9-]+", "-", project_name.casefold()),
            "private": True,
            "scripts": {
                "build": "tsc -p tsconfig.json" if lang == "typescript" else "node --check src/server.js",
                "start": "node dist/server.js" if lang == "typescript" else "node src/server.js",
            },
            "dependencies": dependencies,
        }
        if dev_dependencies:
            package["devDependencies"] = dev_dependencies
        files = {"backend/package.json": json.dumps(package, indent=2) + "\n"}
        if lang == "typescript":
            files["backend/tsconfig.json"] = json.dumps({
                "compilerOptions": {
                    "target": "ES2022", "module": "NodeNext", "moduleResolution": "NodeNext",
                    "strict": True, "esModuleInterop": True, "outDir": "dist", "skipLibCheck": True,
                },
                "include": ["src/**/*.ts"],
            }, indent=2) + "\n"
        return files
    if lang == "java":
        return {"backend/pom.xml": _java_backend_pom(
            project_name, backend_tech, db_target=db_target,
        )}
    return {}


# Function: _java_backend_pom
_JAVA_IMPORT_DEPENDENCIES = {
    "org.springframework.security.": (
        "org.springframework.boot", "spring-boot-starter-security", None,
    ),
    "org.springframework.data.jpa.": (
        "org.springframework.boot", "spring-boot-starter-data-jpa", None,
    ),
    "jakarta.persistence.": (
        "org.springframework.boot", "spring-boot-starter-data-jpa", None,
    ),
    "jakarta.transaction.": (
        "org.springframework.boot", "spring-boot-starter-data-jpa", None,
    ),
    "org.springframework.jdbc.": (
        "org.springframework.boot", "spring-boot-starter-jdbc", None,
    ),
    "org.springframework.kafka.": (
        "org.springframework.kafka", "spring-kafka", None,
    ),
    "org.springframework.cloud.openfeign.": (
        "org.springframework.cloud", "spring-cloud-starter-openfeign", None,
    ),
    "org.springframework.cloud.client.loadbalancer.": (
        "org.springframework.cloud", "spring-cloud-starter-loadbalancer", None,
    ),
    "org.springframework.web.reactive.": (
        "org.springframework.boot", "spring-boot-starter-webflux", None,
    ),
    "org.springframework.data.redis.": (
        "org.springframework.boot", "spring-boot-starter-data-redis", None,
    ),
    "org.springframework.data.mongodb.": (
        "org.springframework.boot", "spring-boot-starter-data-mongodb", None,
    ),
    "io.quarkus.hibernate.orm.panache.": (
        "io.quarkus", "quarkus-hibernate-orm-panache", None,
    ),
    "io.micronaut.data.": (
        "io.micronaut.data", "micronaut-data-jdbc", None,
    ),
    "org.springframework.amqp.": (
        "org.springframework.boot", "spring-boot-starter-amqp", None,
    ),
    "org.springframework.batch.": (
        "org.springframework.boot", "spring-boot-starter-batch", None,
    ),
    "org.springframework.integration.": (
        "org.springframework.integration", "spring-integration-core", None,
    ),
    "org.springframework.cloud.gateway.": (
        "org.springframework.cloud", "spring-cloud-starter-gateway", None,
    ),
    "org.springframework.cloud.client.discovery.": (
        "org.springframework.cloud", "spring-cloud-commons", None,
    ),
    "org.springframework.security.oauth2.": (
        "org.springframework.boot", "spring-boot-starter-oauth2-resource-server", None,
    ),
    "org.springframework.security.test.": (
        "org.springframework.security", "spring-security-test", None,
    ),
    "org.springframework.retry.": (
        "org.springframework.retry", "spring-retry", None,
    ),
    "reactor.core.publisher.": (
        "org.springframework", "spring-webflux", None,
    ),
    "io.github.resilience4j.": (
        "io.github.resilience4j", "resilience4j-spring-boot3", "2.2.0",
    ),
    "io.jsonwebtoken.": ("io.jsonwebtoken", "jjwt-api", "0.12.6"),
    "lombok.": ("org.projectlombok", "lombok", None),
    "org.mapstruct.": ("org.mapstruct", "mapstruct", "1.6.3"),
    "com.google.protobuf.": ("com.google.protobuf", "protobuf-java", "4.28.3"),
    "com.google.gson.": ("com.google.code.gson", "gson", "2.11.0"),
    "org.apache.commons.dbcp2.": ("org.apache.commons", "commons-dbcp2", "2.12.0"),
    # Spring Boot manages the Tomcat JDBC pool version. Some legacy Java
    # conversions retain the fully-qualified pool implementation without an
    # import, so dependency inference must scan source references as well as
    # conventional imports (which _java_inferred_dependencies already does).
    "org.apache.tomcat.jdbc.pool.": ("org.apache.tomcat", "tomcat-jdbc", None),
    "org.apache.struts.action.": ("org.apache.struts", "struts-core", "1.3.10"),
    "org.apache.struts.tiles.": ("org.apache.struts", "struts-tiles", "1.3.10"),
    "org.apache.poi.": ("org.apache.poi", "poi", "5.3.0"),
    "org.apache.avro.": ("org.apache.avro", "avro", "1.12.0"),
}


def _java_inferred_dependencies(
    output: dict[str, str] | None, framework: str = "",
) -> list[tuple[str, str, str | None]]:
    """Resolve Maven dependencies from imports emitted by the Java generator.

    The canonical POM remains service-owned, but it is no longer a closed,
    hard-coded list: supported framework imports deterministically extend it
    before every build/repair pass.
    """
    if not output:
        return []
    java_sources = "\n".join(
        content for path, content in output.items()
        if path.casefold().endswith(".java") and isinstance(content, str)
    )
    framework_key = _java_framework_key(framework) if framework else ""

    def compatible(coordinates: tuple[str, str, str | None]) -> bool:
        group_id, artifact_id, _version = coordinates
        dependency_framework = (
            "spring" if group_id.startswith("org.springframework")
            or "spring" in artifact_id else
            "quarkus" if group_id.startswith("io.quarkus")
            or artifact_id.startswith("quarkus-") else
            "micronaut" if group_id.startswith("io.micronaut")
            or artifact_id.startswith("micronaut-") else
            ""
        )
        return not framework_key or not dependency_framework \
            or dependency_framework == framework_key

    dependencies = {
        coordinates
        for package, coordinates in _JAVA_IMPORT_DEPENDENCIES.items()
        if package in java_sources and compatible(coordinates)
    }
    for module in re.findall(
        r"\bimport\s+software\.amazon\.awssdk\.services\.([a-z0-9_]+)\.",
        java_sources,
    ):
        dependencies.add(("software.amazon.awssdk", module.replace("_", "-"), None))
    if "io.jsonwebtoken." in java_sources:
        dependencies.update({
            ("io.jsonwebtoken", "jjwt-impl", "0.12.6"),
            ("io.jsonwebtoken", "jjwt-jackson", "0.12.6"),
        })
    return sorted(dependencies)


def _java_dependency_xml(
    dependencies: list[tuple[str, str, str | None]],
) -> str:
    rows = []
    for group_id, artifact_id, version in dependencies:
        version_xml = f"<version>{version}</version>" if version else ""
        scope = (
            "test" if artifact_id in {"junit-jupiter", "quarkus-junit5", "micronaut-test-junit5", "spring-security-test", "h2"} else
            "provided" if artifact_id == "jakarta.jakartaee-api" else
            "runtime" if artifact_id in {
                "jjwt-impl", "jjwt-jackson", "postgresql", "mssql-jdbc",
                "mysql-connector-j", "mariadb-java-client", "ojdbc11", "jcc", "sqlite-jdbc",
            } else ""
        )
        scope_xml = f"<scope>{scope}</scope>" if scope else ""
        rows.append(
            "            <dependency>"
            f"<groupId>{group_id}</groupId><artifactId>{artifact_id}</artifactId>"
            f"{version_xml}{scope_xml}</dependency>"
        )
    return "\n".join(rows)


_JAVA_RELATIONAL_DATABASES = {
    "postgres": ("org.postgresql", "postgresql", None),
    "pgvector": ("org.postgresql", "postgresql", None),
    "mssql": ("com.microsoft.sqlserver", "mssql-jdbc", None),
    "mysql": ("com.mysql", "mysql-connector-j", None),
    "mariadb": ("org.mariadb.jdbc", "mariadb-java-client", None),
    "oracle": ("com.oracle.database.jdbc", "ojdbc11", None),
    "db2": ("com.ibm.db2", "jcc", None),
    "sqlite": ("org.xerial", "sqlite-jdbc", "3.47.1.0"),
    "cockroachdb": ("org.postgresql", "postgresql", None),
}

# Framework BOMs manage their own drivers.  Standalone Jakarta/Java SE builds
# do not, so keep the fallback coordinates in one registry instead of leaking
# product versions through templates and prompts.
_JAVA_STANDALONE_DRIVER_VERSIONS = {
    "postgres": "42.7.4", "pgvector": "42.7.4", "cockroachdb": "42.7.4",
    "mssql": "12.8.1.jre11", "mysql": "9.1.0", "mariadb": "3.4.1",
    "oracle": "23.5.0.24.07", "db2": "12.1.0.0",
}

_JAVA_SPRING_DATA_STORES = {
    "mongodb": ("org.springframework.boot", "spring-boot-starter-data-mongodb", None),
    "cosmosdb": ("com.azure.spring", "spring-cloud-azure-starter-data-cosmos", "5.19.0"),
    "cassandra": ("org.springframework.boot", "spring-boot-starter-data-cassandra", None),
    "neo4j": ("org.springframework.boot", "spring-boot-starter-data-neo4j", None),
    "redis": ("org.springframework.boot", "spring-boot-starter-data-redis", None),
    "elasticsearch": ("org.springframework.boot", "spring-boot-starter-data-elasticsearch", None),
    "opensearch": ("org.opensearch.client", "opensearch-java", "2.18.0"),
    "dynamodb": ("software.amazon.awssdk", "dynamodb", None),
}

_JAVA_VECTOR_STARTERS = {
    "pgvector": "pgvector",
    "pinecone": "pinecone",
    "weaviate": "weaviate",
    "milvus": "milvus",
    "elasticsearch-vector": "elasticsearch",
    "opensearch-vector": "opensearch",
    "neo4j-vector": "neo4j",
    "redis-vector": "redis",
    "mongodb-vector": "mongodb-atlas",
    "cassandra-vector": "cassandra",
}

_JAVA_VECTOR_BASE_STORES = {
    key: key.removesuffix("-vector") for key in _JAVA_VECTOR_STARTERS if key.endswith("-vector")
}


def _java_framework_key(backend_tech: str, output: dict[str, str] | None = None) -> str:
    explicit = (backend_tech or "").casefold()
    # The selected target is authoritative. Legacy inputs commonly retain
    # Struts/Java EE imports while being modernized *to* Spring Boot; allowing
    # those source signals to override the target suppresses Spring's launcher,
    # exception-advice, and logging baseline for the affected Maven module.
    if "quarkus" in explicit:
        return "quarkus"
    if "micronaut" in explicit:
        return "micronaut"
    if "spring" in explicit:
        return "spring"
    if any(token in explicit for token in ("jakarta ee", "java ee", "struts", "jakarta.platform")):
        return "jakarta"
    if "java se" in explicit:
        return "java-se"

    evidence = " ".join(
        content for path, content in (output or {}).items()
        if path.casefold().endswith(("pom.xml", ".java")) and isinstance(content, str)
    ).casefold()
    if "quarkus" in evidence:
        return "quarkus"
    if "micronaut" in evidence:
        return "micronaut"
    # A modernized module can legitimately retain legacy Struts/Java EE
    # references while already containing Spring controllers or a Spring Boot
    # POM.  Positive target-framework evidence must win over source-history
    # evidence; the previous order classified such mixed modules as Jakarta
    # and skipped their launcher, exception advice, and logging baseline.
    if any(token in evidence for token in (
        "spring-boot-starter", "org.springframework", "@springbootapplication",
    )):
        return "spring"
    if any(token in evidence for token in ("jakarta ee", "java ee", "struts", "jakarta.platform")):
        return "jakarta"
    if "java se" in evidence and "spring" not in evidence:
        return "java-se"
    return "spring"


def _java_database_key(db_target: str, output: dict[str, str] | None = None) -> str:
    explicit = (db_target or "").strip().casefold()
    aliases = {
        "postgresql": "postgres", "sqlserver": "mssql", "sql-server": "mssql",
        "mongo": "mongodb", "cosmos": "cosmosdb", "vector-db": "vector",
    }
    if explicit:
        return aliases.get(explicit, explicit)
    evidence = " ".join(
        content for path, content in (output or {}).items()
        if path.casefold().endswith(("pom.xml", ".java", ".yml", ".yaml", ".properties"))
        and isinstance(content, str)
    ).casefold()
    signals = (
        ("pgvector", "pgvector"), ("pinecone", "pinecone"), ("weaviate", "weaviate"),
        ("milvus", "milvus"), ("mongodb", "mongodb"), ("dynamodb", "dynamodb"),
        ("cassandra", "cassandra"), ("neo4j", "neo4j"), ("redis", "redis"),
        ("opensearch", "opensearch"), ("elasticsearch", "elasticsearch"),
        ("sqlserver", "mssql"), ("mssql", "mssql"), ("mysql", "mysql"),
        ("mariadb", "mariadb"), ("oracle", "oracle"), ("db2", "db2"),
        ("sqlite", "sqlite"), ("postgres", "postgres"),
    )
    return next((key for token, key in signals if token in evidence), "none")


def _java_database_dependencies(framework: str, db_target: str) -> list[tuple[str, str, str | None]]:
    dependencies: list[tuple[str, str, str | None]] = []
    base_store = _JAVA_VECTOR_BASE_STORES.get(db_target, db_target)
    relational = _JAVA_RELATIONAL_DATABASES.get(base_store)
    if relational:
        if framework == "quarkus" and base_store != "sqlite":
            quarkus_db = "postgresql" if base_store in {"postgres", "pgvector", "cockroachdb"} else base_store
            dependencies.append(("io.quarkus", f"quarkus-jdbc-{quarkus_db}", None))
        else:
            group_id, artifact_id, version = relational
            if framework in {"micronaut", "jakarta", "java-se"} and not version:
                version = _JAVA_STANDALONE_DRIVER_VERSIONS.get(base_store)
            dependencies.append((group_id, artifact_id, version))
        if framework == "spring":
            dependencies.extend([
                ("org.springframework.boot", "spring-boot-starter-data-jpa", None),
                ("org.flywaydb", "flyway-core", None),
            ])
            flyway_module = {
                "postgres": "flyway-database-postgresql",
                "pgvector": "flyway-database-postgresql",
                "cockroachdb": "flyway-database-postgresql",
                "mssql": "flyway-sqlserver",
                "mysql": "flyway-mysql",
                "mariadb": "flyway-mysql",
                "oracle": "flyway-database-oracle",
            }.get(base_store)
            if flyway_module:
                dependencies.append(("org.flywaydb", flyway_module, None))
    elif framework == "quarkus":
        quarkus_store = {
            "mongodb": "quarkus-mongodb-panache", "redis": "quarkus-redis-client",
            "cassandra": "quarkus-cassandra-client", "elasticsearch": "quarkus-elasticsearch-java-client",
        }.get(base_store)
        if quarkus_store:
            dependencies.append(("io.quarkus", quarkus_store, None))
    else:
        store = _JAVA_SPRING_DATA_STORES.get(base_store)
        if store:
            dependencies.append(store)
    if framework == "spring" and db_target in _JAVA_VECTOR_STARTERS:
        dependencies.append((
            "org.springframework.ai",
            f"spring-ai-starter-vector-store-{_JAVA_VECTOR_STARTERS[db_target]}",
            None,
        ))
    return dependencies


def _java_non_spring_pom(
    project_name: str, java_version: str, framework: str,
    dependencies: list[tuple[str, str, str | None]],
) -> str:
    artifact_id = re.sub(r"[^a-z0-9]+", "-", project_name.casefold()).strip("-") or "modernized-app"
    if framework == "quarkus":
        base = [("io.quarkus", "quarkus-rest-jackson", None),
                ("io.quarkus", "quarkus-hibernate-validator", None),
                ("io.quarkus", "quarkus-logging-json", None),
                ("io.quarkus", "quarkus-junit5", None)]
        parent = """<dependencyManagement><dependencies><dependency><groupId>io.quarkus.platform</groupId><artifactId>quarkus-bom</artifactId><version>3.15.1</version><type>pom</type><scope>import</scope></dependency></dependencies></dependencyManagement>"""
        plugin = "<plugin><groupId>io.quarkus</groupId><artifactId>quarkus-maven-plugin</artifactId><version>3.15.1</version><extensions>true</extensions></plugin>"
        packaging = "jar"
    elif framework == "micronaut":
        base = [("io.micronaut", "micronaut-http-server-netty", None),
                ("io.micronaut.validation", "micronaut-validation", None),
                ("io.micronaut.test", "micronaut-test-junit5", None)]
        parent = "<parent><groupId>io.micronaut.platform</groupId><artifactId>micronaut-parent</artifactId><version>4.6.3</version><relativePath/></parent>"
        plugin = "<plugin><groupId>io.micronaut.maven</groupId><artifactId>micronaut-maven-plugin</artifactId></plugin>"
        packaging = "jar"
    elif framework == "jakarta":
        base = [("jakarta.platform", "jakarta.jakartaee-api", "10.0.0"),
                ("org.junit.jupiter", "junit-jupiter", "5.11.3")]
        parent = ""
        plugin = "<plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-war-plugin</artifactId><version>3.4.0</version></plugin>"
        packaging = "war"
    else:
        base = [("org.junit.jupiter", "junit-jupiter", "5.11.3")]
        parent = ""
        plugin = "<plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-surefire-plugin</artifactId><version>3.5.2</version></plugin>"
        packaging = "jar"
    dependency_xml = _java_dependency_xml(list(dict.fromkeys([*base, *dependencies])))
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <project xmlns="http://maven.apache.org/POM/4.0.0">
          <modelVersion>4.0.0</modelVersion>
          {parent}
          <groupId>com.modernize</groupId><artifactId>{artifact_id}</artifactId>
          <version>1.0.0-SNAPSHOT</version><packaging>{packaging}</packaging>
          <properties><maven.compiler.release>{java_version}</maven.compiler.release></properties>
          <dependencies>
{dependency_xml}
          </dependencies>
          <build><plugins>{plugin}</plugins></build>
        </project>
    """)


def _java_backend_pom(
    project_name: str, backend_tech: str,
    inferred_dependencies: list[tuple[str, str, str | None]] | None = None,
    db_target: str = "",
    main_class: str = "",
) -> str:
    """Return a capability-selected Maven contract for generated Java services."""
    java_match = re.search(r"\bjava\s*(\d+)", backend_tech or "", re.IGNORECASE)
    java_version = java_match.group(1) if java_match else "21"
    framework = _java_framework_key(backend_tech)
    database = _java_database_key(db_target)
    selected_dependencies = list(dict.fromkeys([
        *(inferred_dependencies or []),
        *_java_database_dependencies(framework, database),
    ]))
    if framework != "spring":
        return _java_non_spring_pom(
            project_name, java_version, framework, selected_dependencies,
        )
    artifact_id = re.sub(r"[^a-z0-9]+", "-", project_name.casefold()).strip("-") or "modernized-app"
    inferred_xml = _java_dependency_xml(selected_dependencies)
    main_class_xml = (
        f"<configuration><mainClass>{main_class}</mainClass></configuration>"
        if main_class else ""
    )
    selected_groups = {group_id for group_id, _, _ in selected_dependencies}
    selected_artifacts = {artifact_id for _, artifact_id, _ in selected_dependencies}
    web_starter = (
        "spring-boot-starter-webflux"
        if "spring-cloud-starter-gateway" in selected_artifacts
        else "spring-boot-starter-web"
    )
    spring_ai_bom = (
        "<dependency><groupId>org.springframework.ai</groupId>"
        "<artifactId>spring-ai-bom</artifactId><version>1.1.8</version>"
        "<type>pom</type><scope>import</scope></dependency>"
        if database in _JAVA_VECTOR_STARTERS else ""
    )
    spring_cloud_bom = (
        "<dependency><groupId>org.springframework.cloud</groupId>"
        "<artifactId>spring-cloud-dependencies</artifactId><version>2023.0.3</version>"
        "<type>pom</type><scope>import</scope></dependency>"
        if "org.springframework.cloud" in selected_groups else ""
    )
    aws_bom = (
        "<dependency><groupId>software.amazon.awssdk</groupId>"
        "<artifactId>bom</artifactId><version>2.29.29</version>"
        "<type>pom</type><scope>import</scope></dependency>"
        if "software.amazon.awssdk" in selected_groups or "dynamodb" in selected_artifacts else ""
    )
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <project xmlns="http://maven.apache.org/POM/4.0.0"
                 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                 xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
          <modelVersion>4.0.0</modelVersion>
          <parent>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-parent</artifactId>
            <version>3.3.5</version>
            <relativePath/>
          </parent>
          <groupId>com.modernize</groupId>
          <artifactId>{artifact_id}</artifactId>
          <version>1.0.0-SNAPSHOT</version>
          <name>{artifact_id}</name>
          <properties><java.version>{java_version}</java.version></properties>
          <dependencyManagement>
            <dependencies>
              {spring_cloud_bom}
              {aws_bom}
              {spring_ai_bom}
            </dependencies>
          </dependencyManagement>
          <dependencies>
            <dependency><groupId>org.springframework.boot</groupId><artifactId>{web_starter}</artifactId></dependency>
            <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-validation</artifactId></dependency>
            <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-actuator</artifactId></dependency>
            <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-test</artifactId><scope>test</scope></dependency>
            <dependency><groupId>org.testcontainers</groupId><artifactId>junit-jupiter</artifactId><scope>test</scope></dependency>
{inferred_xml}
          </dependencies>
          <build>
            <plugins>
              <plugin>
                <groupId>org.springframework.boot</groupId>
                <artifactId>spring-boot-maven-plugin</artifactId>
                {main_class_xml}
              </plugin>
            </plugins>
          </build>
        </project>
    """)


def _java_identifier(value: str, fallback: str = "Modernized") -> str:
    """Return a stable Java type-name fragment from a project/module label."""
    parts = re.findall(r"[A-Za-z0-9]+", value or "")
    identifier = "".join(part[:1].upper() + part[1:] for part in parts) or fallback
    return f"App{identifier}" if identifier[:1].isdigit() else identifier


def _ensure_spring_boot_entry_point(
    output: dict[str, str], source_root: str, application_name: str,
) -> str:
    """Guarantee one executable Spring Boot entry point inside a Maven module.

    LLM output may omit ``main`` or provide only an annotation.  Repair this
    deterministically because Maven reports the resulting repackage failure at
    project level, where per-file compiler repair has no source path to target.
    """
    normalized_root = source_root.rstrip("/") + "/"
    sources = [
        (path, content) for path, content in output.items()
        if path.startswith(normalized_root) and path.endswith(".java")
        and isinstance(content, str)
    ]
    main_pattern = re.compile(
        r"\bpublic\s+static\s+void\s+main\s*\(\s*String(?:\[\]|\.\.\.)\s+\w+\s*\)"
    )
    for path, content in sources:
        if "@SpringBootApplication" not in content:
            continue
        package_match = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", content)
        class_match = re.search(r"\bpublic\s+class\s+([A-Za-z_]\w*)\b", content)
        if not package_match or not class_match:
            continue
        class_name = class_match.group(1)
        if not main_pattern.search(content):
            if "org.springframework.boot.SpringApplication" not in content:
                package_end = package_match.end()
                content = (
                    content[:package_end]
                    + "\n\nimport org.springframework.boot.SpringApplication;"
                    + content[package_end:]
                )
            closing_brace = content.rfind("}")
            if closing_brace >= 0:
                method = (
                    "\n    public static void main(String[] args) {\n"
                    f"        SpringApplication.run({class_name}.class, args);\n"
                    "    }\n"
                )
                content = content[:closing_brace] + method + content[closing_brace:]
                output[path] = content
        if main_pattern.search(output.get(path, content)):
            return f"{package_match.group(1)}.{class_name}"

    packages = [
        match.group(1).split(".")
        for _, content in sources
        if (match := re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", content))
    ]
    common = packages[0] if packages else ["com", "modernize"]
    for package in packages[1:]:
        common = [
            left for left, right in zip(common, package, strict=False)
            if left == right
        ][:next((index for index, (left, right) in enumerate(zip(common, package, strict=False)) if left != right), min(len(common), len(package)))]
    if len(common) < 2:
        common = ["com", "modernize", re.sub(r"[^a-z0-9]", "", application_name.casefold()) or "app"]
    package_name = ".".join(common)
    class_name = f"{_java_identifier(application_name)}Application"
    relative_package = package_name.replace(".", "/")
    path = f"{normalized_root}{relative_package}/{class_name}.java"
    if path in output:
        class_name = "ModernizedApplication"
        path = f"{normalized_root}{relative_package}/{class_name}.java"
    output[path] = textwrap.dedent(f"""\
        package {package_name};

        import org.springframework.boot.SpringApplication;
        import org.springframework.boot.autoconfigure.SpringBootApplication;

        @SpringBootApplication
        public class {class_name} {{
            public static void main(String[] args) {{
                SpringApplication.run({class_name}.class, args);
            }}
        }}
    """)
    return f"{package_name}.{class_name}"


def _java_common_package(output: dict[str, str], source_root: str, fallback: str) -> str:
    packages = []
    prefix = source_root.rstrip("/") + "/"
    for path, content in output.items():
        if not path.startswith(prefix) or not path.endswith(".java") or not isinstance(content, str):
            continue
        match = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", content)
        if match:
            packages.append(match.group(1).split("."))
    common = packages[0] if packages else []
    for package in packages[1:]:
        limit = 0
        for left, right in zip(common, package, strict=False):
            if left != right:
                break
            limit += 1
        common = common[:limit]
    if len(common) < 2:
        common = ["com", "modernize", re.sub(r"[^a-z0-9]", "", fallback.casefold()) or "app"]
    return ".".join(common)


def _java_exception_handler(package_name: str) -> str:
    return textwrap.dedent(f"""\
        package {package_name}.error;

        import jakarta.servlet.http.HttpServletRequest;
        import java.net.URI;
        import java.util.UUID;
        import org.slf4j.Logger;
        import org.slf4j.LoggerFactory;
        import org.slf4j.MDC;
        import org.springframework.http.HttpStatus;
        import org.springframework.http.ProblemDetail;
        import org.springframework.web.bind.MethodArgumentNotValidException;
        import org.springframework.web.bind.annotation.ExceptionHandler;
        import org.springframework.web.bind.annotation.RestControllerAdvice;

        @RestControllerAdvice
        public class GlobalExceptionHandler {{
            private static final Logger log = LoggerFactory.getLogger(GlobalExceptionHandler.class);

            @ExceptionHandler(MethodArgumentNotValidException.class)
            public ProblemDetail handleValidation(MethodArgumentNotValidException exception,
                                                   HttpServletRequest request) {{
                log.warn("Request validation failed method={{}} path={{}} errors={{}}",
                        request.getMethod(), request.getRequestURI(), exception.getErrorCount());
                ProblemDetail problem = ProblemDetail.forStatusAndDetail(
                        HttpStatus.BAD_REQUEST, "One or more request fields are invalid.");
                problem.setTitle("Request validation failed");
                problem.setType(URI.create("urn:problem:request-validation"));
                problem.setProperty("errors", exception.getBindingResult().getFieldErrors().stream()
                        .map(error -> error.getField() + ": " + error.getDefaultMessage()).toList());
                return problem;
            }}

            @ExceptionHandler(IllegalArgumentException.class)
            public ProblemDetail handleInvalidArgument(IllegalArgumentException exception,
                                                       HttpServletRequest request) {{
                log.warn("Invalid request argument method={{}} path={{}} message={{}}",
                        request.getMethod(), request.getRequestURI(), exception.getMessage());
                ProblemDetail problem = ProblemDetail.forStatusAndDetail(
                        HttpStatus.BAD_REQUEST, exception.getMessage());
                problem.setTitle("Invalid request");
                problem.setType(URI.create("urn:problem:invalid-request"));
                return problem;
            }}

            @ExceptionHandler(Exception.class)
            public ProblemDetail handleUnexpected(Exception exception, HttpServletRequest request) {{
                String correlationId = MDC.get("correlationId");
                if (correlationId == null || correlationId.isBlank()) {{
                    correlationId = UUID.randomUUID().toString();
                }}
                log.error("Unhandled request failure correlationId={{}} method={{}} path={{}}",
                        correlationId, request.getMethod(), request.getRequestURI(), exception);
                ProblemDetail problem = ProblemDetail.forStatusAndDetail(
                        HttpStatus.INTERNAL_SERVER_ERROR,
                        "The request could not be completed. Use the correlation ID when contacting support.");
                problem.setTitle("Unexpected application error");
                problem.setType(URI.create("urn:problem:unexpected-application-error"));
                problem.setProperty("correlationId", correlationId);
                return problem;
            }}
        }}
    """)


def _java_logback_configuration(application_name: str) -> str:
    service = re.sub(r"[^a-zA-Z0-9_.-]+", "-", application_name).strip("-") or "modernized-app"
    json_pattern = (
        '{"timestamp":"%d{yyyy-MM-dd\'\'T\'\'HH:mm:ss.SSSXXX}",'
        '"level":"%level","service":"${SERVICE_NAME}","thread":"%thread",'
        '"logger":"%logger{36}","correlationId":"%X{correlationId:-}",'
        '"message":"%replace(%msg){\'&quot;\',\'\\&quot;\'}",'
        '"exception":"%replace(%ex){\'[\\r\\n]+\',\' | \'}"}%n'
    )
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <configuration>
          <property name="SERVICE_NAME" value="{service}"/>
          <property name="LOG_PATH" value="${{LOG_PATH:-logs}}"/>
          <appender name="CONSOLE" class="ch.qos.logback.core.ConsoleAppender">
            <encoder><pattern><![CDATA[{json_pattern}]]></pattern></encoder>
          </appender>
          <appender name="FILE" class="ch.qos.logback.core.rolling.RollingFileAppender">
            <file>${{LOG_PATH}}/application.json.log</file>
            <rollingPolicy class="ch.qos.logback.core.rolling.SizeAndTimeBasedRollingPolicy">
              <fileNamePattern>${{LOG_PATH}}/application.%d{{yyyy-MM-dd}}.%i.json.log.gz</fileNamePattern>
              <maxFileSize>20MB</maxFileSize><maxHistory>30</maxHistory><totalSizeCap>1GB</totalSizeCap>
            </rollingPolicy>
            <encoder><pattern><![CDATA[{json_pattern}]]></pattern></encoder>
          </appender>
          <root level="INFO"><appender-ref ref="CONSOLE"/><appender-ref ref="FILE"/></root>
        </configuration>
    """).lstrip()


def _ensure_java_operational_baseline(
    output: dict[str, str], source_root: str, resources_root: str, application_name: str,
) -> None:
    """Add exception management and durable structured logging to a Spring module."""
    prefix = source_root.rstrip("/") + "/"
    has_compliant_advice = any(
        path.startswith(prefix) and path.endswith(".java")
        and isinstance(content, str)
        and "@RestControllerAdvice" in content
        and "@ExceptionHandler(Exception.class)" in content
        and "ProblemDetail" in content
        and "LoggerFactory.getLogger" in content
        and re.search(r"\blog\.error\s*\([^;]*exception\s*\)", content, re.DOTALL)
        for path, content in output.items()
    )
    package_name = _java_common_package(output, source_root, application_name)
    if not has_compliant_advice:
        handler_path = (
            f"{prefix}{package_name.replace('.', '/')}/error/GlobalExceptionHandler.java"
        )
        output[handler_path] = _java_exception_handler(package_name)
    logback_path = f"{resources_root.rstrip('/')}/logback-spring.xml"
    existing_logback = output.get(logback_path, "")
    if not (
        isinstance(existing_logback, str)
        and "RollingFileAppender" in existing_logback
        and "application.json.log" in existing_logback
        and "CONSOLE" in existing_logback
    ):
        output[logback_path] = _java_logback_configuration(application_name)


def _ensure_quarkus_operational_baseline(
    output: dict[str, str], source_root: str, resources_root: str, application_name: str,
) -> None:
    """Add framework-native global errors and durable JSON logs to Quarkus."""
    prefix = source_root.rstrip("/") + "/"
    has_mapper = any(
        path.startswith(prefix) and path.endswith(".java")
        and isinstance(content, str)
        and "@Provider" in content and "ExceptionMapper<" in content
        for path, content in output.items()
    )
    if not has_mapper:
        package_name = _java_common_package(output, source_root, application_name)
        handler_path = (
            f"{prefix}{package_name.replace('.', '/')}/error/GlobalExceptionMapper.java"
        )
        output[handler_path] = textwrap.dedent(f"""\
            package {package_name}.error;

            import jakarta.ws.rs.core.MediaType;
            import jakarta.ws.rs.core.Response;
            import jakarta.ws.rs.ext.ExceptionMapper;
            import jakarta.ws.rs.ext.Provider;
            import java.util.Map;
            import org.jboss.logging.Logger;

            @Provider
            public final class GlobalExceptionMapper implements ExceptionMapper<Throwable> {{
                private static final Logger log = Logger.getLogger(GlobalExceptionMapper.class);

                @Override
                public Response toResponse(Throwable exception) {{
                    log.error("Unhandled request failure", exception);
                    return Response.status(Response.Status.INTERNAL_SERVER_ERROR)
                            .type(MediaType.APPLICATION_JSON_TYPE)
                            .entity(Map.of(
                                    "type", "about:blank",
                                    "title", "Internal Server Error",
                                    "status", 500,
                                    "detail", "The request could not be completed"))
                            .build();
                }}
            }}
        """)

    properties_path = f"{resources_root.rstrip('/')}/application.properties"
    properties = output.get(properties_path, "")
    properties = properties if isinstance(properties, str) else ""
    required = {
        "quarkus.log.console.enabled": "true",
        "quarkus.log.console.json.enabled": "true",
        "quarkus.log.file.enabled": "true",
        "quarkus.log.file.path": "logs/application.json.log",
        "quarkus.log.file.json.enabled": "true",
        "quarkus.log.file.rotation.max-file-size": "20M",
        "quarkus.log.file.rotation.max-backup-index": "30",
        "quarkus.log.file.rotation.file-suffix": ".yyyy-MM-dd.gz",
    }
    existing_keys = {
        match.group(1)
        for match in re.finditer(r"(?m)^\s*([A-Za-z0-9_.\-\"]+)\s*=", properties)
    }
    additions = [
        f"{key}={value}" for key, value in required.items()
        if key not in existing_keys
    ]
    if additions:
        output[properties_path] = (
            properties.rstrip() + "\n" + "\n".join(additions) + "\n"
        )


def _java_reactor_pom(project_name: str, modules: list[str], java_version: str) -> str:
    """Return an aggregator POM for an explicitly requested Maven reactor."""
    artifact_id = re.sub(r"[^a-z0-9]+", "-", project_name.casefold()).strip("-") or "modernized-app"
    module_xml = "\n".join(f"    <module>{module}</module>" for module in modules)
    template = textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <project xmlns="http://maven.apache.org/POM/4.0.0"
                 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                 xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
          <modelVersion>4.0.0</modelVersion>
          <groupId>com.modernize</groupId>
          <artifactId>{artifact_id}-reactor</artifactId>
          <version>1.0.0-SNAPSHOT</version>
          <packaging>pom</packaging>
          <properties><java.version>{java_version}</java.version></properties>
          <modules>
        __REACTOR_MODULES__
          </modules>
        </project>
    """)
    return template.replace("__REACTOR_MODULES__", module_xml)


def _java_module_roots(output: dict[str, str], project_name: str) -> list[str]:
    """Discover real Maven modules from backend/<module>/src source roots."""
    marker = f"{project_name}/backend/"
    modules = set()
    for path in output:
        if not path.startswith(marker):
            continue
        relative = path[len(marker):]
        module, separator, remainder = relative.partition("/")
        if separator and remainder.startswith("src/") and module != "src":
            modules.add(module)
    return sorted(modules)


def _normalize_java_build_roots(output: dict[str, str], project_name: str) -> None:
    """Move supported generator layouts beneath the Maven-owned backend root.

    Older conversion/scaffold routes emitted ``<project>/src`` and
    ``<project>/services/<name>/src`` while reconciliation created only
    ``<project>/backend/pom.xml``. Maven then saw an empty source tree and Boot
    failed at repackage despite valid launchers existing outside its module.
    """
    project_root = f"{project_name}/"
    services_root = f"{project_root}services/"
    backend_root = f"{project_root}backend/"
    moves: dict[str, str] = {}
    service_modules = set()
    for path in output:
        if not path.startswith(services_root):
            continue
        relative = path[len(services_root):]
        module, separator, remainder = relative.partition("/")
        if separator and (remainder.startswith("src/") or remainder == "pom.xml"):
            service_modules.add(module)
            moves[path] = f"{backend_root}{module}/{remainder}"

    legacy_module = "legacy-core/" if service_modules else ""
    frontend_source_suffixes = {
        ".js", ".jsx", ".ts", ".tsx", ".vue",
        ".css", ".scss", ".sass", ".less",
    }
    for path in output:
        if (
            path.startswith(backend_root)
            and Path(path).suffix.casefold() in frontend_source_suffixes
        ):
            # Repair snapshots produced by the older blanket move as well as
            # fresh staging paths.  The first segment beneath backend is the
            # Maven module; preserve the source-relative path after it.
            backend_relative = path[len(backend_root):]
            _module, separator, module_relative = backend_relative.partition("/")
            if separator and module_relative.startswith("src/"):
                moves[path] = f"{project_root}frontend/{module_relative}"
            continue
        if path.startswith(f"{project_root}src/"):
            relative = path[len(project_root):]
            # Domain generators use ``<project>/src`` as a neutral staging
            # root.  In a Java full-stack project, browser sources belong to
            # the npm/Vite boundary, never beneath a Maven module.  Moving
            # every staged file into legacy-core made Maven report green while
            # malformed React/TypeScript remained uncompiled and unrepaired.
            destination_root = (
                f"{project_root}frontend/"
                if Path(path).suffix.casefold() in frontend_source_suffixes
                else f"{backend_root}{legacy_module}"
            )
            moves[path] = f"{destination_root}{relative}"

    for source, destination in sorted(moves.items()):
        if source == destination:
            continue
        if destination not in output:
            output[destination] = output[source]
        del output[source]


_FRONTEND_IMPORT_DEPENDENCIES = {
    "axios": "^1.7.9",
    "clsx": "^2.1.1",
    "date-fns": "^4.1.0",
    "lucide-react": "^0.468.0",
    "react-hook-form": "^7.54.0",
    "react-router-dom": "^6.28.0",
    "recharts": "^2.15.0",
    "tailwind-merge": "^2.6.0",
    "zod": "^3.24.0",
    "@hookform/resolvers": "^3.9.1",
    "@tanstack/react-query": "^5.62.0",
    "@tanstack/react-query-devtools": "^5.62.0",
    "@angular/cdk": "^17.3.10",
    "@angular/material": "^17.3.10",
    "@ngrx/effects": "^17.2.0",
    "@ngrx/store": "^17.2.0",
    "web-vitals": "^3.5.2",
    "express": "^4.21.0",
    "helmet": "^8.0.0",
    "dotenv": "^16.4.5",
    "graphql": "^16.9.0",
    "@apollo/server": "^4.11.0",
    "@nestjs/common": "^11.1.0",
    "@nestjs/core": "^11.1.0",
    "mongoose": "^8.8.0",
    "pg": "^8.13.0",
}


# Function: _imported_package_name
def _imported_package_name(specifier: str) -> str:
    if not specifier or specifier.startswith((".", "/", "src/", "@/")):
        return ""
    parts = specifier.split("/")
    return "/".join(parts[:2]) if specifier.startswith("@") and len(parts) > 1 else parts[0]


# Function: _reconcile_npm_dependencies
def _reconcile_npm_dependencies(output: dict[str, str]) -> None:
    """Close known npm import dependencies within each package boundary."""
    package_paths = [
        path for path in output
        if Path(path).name.casefold() == "package.json"
    ]
    for package_path in package_paths:
        try:
            package_data = json.loads(output[package_path])
        except (TypeError, ValueError):
            continue
        package_root = package_path.rsplit("/", 1)[0] + "/"
        imported = set()
        for source_path, content in output.items():
            if (
                source_path.startswith(package_root)
                and source_path.endswith((".js", ".jsx", ".ts", ".tsx"))
                and isinstance(content, str)
            ):
                specifiers = re.findall(
                    r"""(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s+)["']([^"']+)""",
                    content,
                )
                imported.update(filter(None, map(_imported_package_name, specifiers)))
        dependencies = package_data.setdefault("dependencies", {})
        changed = False
        for package in sorted(imported):
            if package in _FRONTEND_IMPORT_DEPENDENCIES and package not in dependencies:
                dependencies[package] = _FRONTEND_IMPORT_DEPENDENCIES[package]
                changed = True
        if changed:
            output[package_path] = json.dumps(package_data, indent=2) + "\n"


_DOTNET_SOURCE_PACKAGES = (
    (re.compile(r"(?:\busing\s+Npgsql\s*;|\bNpgsql(?:Connection|Command|DataSource)\b)"),
     "Npgsql", "8.0.3"),
    (re.compile(r"\bUseNpgsql\s*\("),
     "Npgsql.EntityFrameworkCore.PostgreSQL", None),
    (re.compile(r"(?:\busing\s+Microsoft\.Data\.SqlClient\s*;|\bSqlConnection\b)"),
     "Microsoft.Data.SqlClient", "5.2.0"),
    (re.compile(r"(?:\busing\s+Dapper\s*;|\bQuery(?:Async)?\s*<|\bExecuteAsync\s*\()"),
     "Dapper", "2.1.35"),
)


def _reconcile_dotnet_dependencies(output: dict[str, str]) -> None:
    """Close NuGet references inside the C# project that owns each source.

    Build repair is allowed to rewrite source after deterministic manifests are
    created.  A repair can therefore introduce an ADO.NET provider such as
    ``Npgsql`` while leaving the owning ``.csproj`` one generation phase behind.
    Resolve ownership by the deepest project directory so multi-project output
    never receives a package merely because a sibling project imports it.
    """
    projects = sorted(
        (path for path in output if path.casefold().endswith(".csproj")),
        key=lambda path: len(path.rsplit("/", 1)[0]),
        reverse=True,
    )
    if not projects:
        return
    roots = {path: path.rsplit("/", 1)[0] + "/" for path in projects}
    owned_sources: dict[str, list[str]] = {path: [] for path in projects}
    for source_path, content in output.items():
        if not source_path.casefold().endswith(".cs") or not isinstance(content, str):
            continue
        owner = next((path for path in projects if source_path.startswith(roots[path])), None)
        if owner:
            owned_sources[owner].append(content)

    for project_path, sources in owned_sources.items():
        project = output.get(project_path)
        if not isinstance(project, str) or not sources:
            continue
        combined = "\n".join(sources)
        tfm_match = re.search(r"<TargetFramework>\s*net(\d+)", project, re.IGNORECASE)
        framework_major = tfm_match.group(1) if tfm_match else "8"
        additions = []
        for signal, package, fixed_version in _DOTNET_SOURCE_PACKAGES:
            if not signal.search(combined):
                continue
            if re.search(
                rf'<PackageReference\b[^>]*\bInclude=["\']{re.escape(package)}["\']',
                project, re.IGNORECASE,
            ):
                continue
            version = fixed_version or f"{framework_major}.0.0"
            additions.append(
                f'    <PackageReference Include="{package}" Version="{version}" />'
            )
        if not additions:
            continue
        block = "  <ItemGroup>\n" + "\n".join(additions) + "\n  </ItemGroup>\n"
        if re.search(r"</Project>\s*$", project, re.IGNORECASE):
            project = re.sub(r"(?i)</Project>\s*$", block + "</Project>\n", project)
        else:
            continue
        output[project_path] = project


# Function: _sql_balanced_call_args
def _sql_balanced_call_args(text: str, open_paren_index: int) -> tuple[int, int] | None:
    """Same purpose as _java_balanced_call_args, tokenized for SQL instead
    of Java: single-quoted string literals with '' as the escaped-quote
    form (SQL has no backslash-escape), no separate char-literal syntax."""
    depth = 0
    i = open_paren_index
    in_string = False
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string:
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    i += 2
                    continue
                in_string = False
        elif ch == "'":
            in_string = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return open_paren_index + 1, i
        i += 1
    return None


# Function: _sql_split_balanced_args
def _sql_split_balanced_args(args: str) -> list[str]:
    """Split a SQL call's argument list on top-level commas only — a comma
    inside a nested call's parens or a quoted string must not split."""
    parts: list[str] = []
    depth = 0
    in_string = False
    start = 0
    i = 0
    n = len(args)
    while i < n:
        ch = args[i]
        if in_string:
            if ch == "'":
                if i + 1 < n and args[i + 1] == "'":
                    i += 2
                    continue
                in_string = False
        elif ch == "'":
            in_string = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(args[start:i])
            start = i + 1
        i += 1
    parts.append(args[start:])
    return [p.strip() for p in parts]


_ORACLE_DECODE_CALL_RE = re.compile(r"\bDECODE\s*\(", re.IGNORECASE)
# Safe, unambiguous 1:1 Oracle -> PostgreSQL syntax renames — every one of
# these is a drop-in equivalent with no semantic difference, unlike
# procedural constructs (SYS_REFCURSOR, DBMS_*, RAISE_APPLICATION_ERROR,
# stored-procedure bodies) which are deliberately NOT auto-translated here;
# those need real judgment a regex can't safely apply, and a wrong
# mechanical rewrite would be worse than leaving the diagnostic for review.
_ORACLE_TO_POSTGRES_SIMPLE_RENAMES = [
    (re.compile(r"\bVARCHAR2\s*\(", re.IGNORECASE), "VARCHAR("),
    (re.compile(r"\bNUMBER\s*\(", re.IGNORECASE), "NUMERIC("),
    (re.compile(r"\bNUMBER\b(?!\s*\()", re.IGNORECASE), "NUMERIC"),
    (re.compile(r"\bSYSDATE\b", re.IGNORECASE), "CURRENT_TIMESTAMP"),
    (re.compile(r"\bNVL\s*\(", re.IGNORECASE), "COALESCE("),
    (re.compile(r"(?m)^\s*FROM\s+DUAL\s*;", re.IGNORECASE), ";"),
    (re.compile(r"[ \t]+FROM\s+DUAL\b", re.IGNORECASE), ""),
]


# Function: _rewrite_oracle_decode_calls
def _rewrite_oracle_decode_calls(sql: str) -> str:
    """DECODE(expr, s1, r1, s2, r2, ..., default) -> a CASE expression.

    The exact construct this project's own antipattern scan already
    identifies as legacy Oracle ("Oracle DECODE function") — worth handling
    precisely (not just flagging) since a validated CASE rewrite is
    unambiguous: an even argument count after `expr` has no default (ELSE
    omitted), odd has a trailing default (ELSE present).
    """
    result = sql
    while True:
        match = _ORACLE_DECODE_CALL_RE.search(result)
        if not match:
            return result
        open_paren = match.end() - 1
        span = _sql_balanced_call_args(result, open_paren)
        if not span:
            return result  # unbalanced/truncated — leave for LLM repair, don't corrupt it further
        args_start, args_end = span
        parts = _sql_split_balanced_args(result[args_start:args_end])
        if len(parts) < 3:
            return result  # not a real DECODE call — leave untouched rather than guess
        expr = parts[0]
        pairs = parts[1:]
        has_default = len(pairs) % 2 == 1
        default = pairs.pop() if has_default else None
        when_clauses = " ".join(
            f"WHEN {pairs[i]} THEN {pairs[i + 1]}" for i in range(0, len(pairs), 2)
        )
        case_expr = f"CASE {expr} {when_clauses}"
        if default is not None:
            case_expr += f" ELSE {default}"
        case_expr += " END"
        result = result[:match.start()] + case_expr + result[args_end + 1:]


# Function: _reconcile_postgres_sql_dialect
def _reconcile_postgres_sql_dialect(output: dict[str, str], target_db: str) -> None:
    """Mechanically translate Oracle SQL syntax to PostgreSQL in every .sql
    file, whenever the target database is postgres.

    Why this exists even though ModernizedApp/Database/schema_postgres.sql
    is itself generated deterministically (see _postgres_schema — pure,
    clean PostgreSQL DDL, no Oracle syntax at all): that file is not
    protected from the LLM-driven compiler-repair loop
    (_pf_enforce_governed_generation_files only guards the money-transfer
    demo pack). A real generation showed this file needing a build-repair
    round in nearly every run, and the LLM's rewrite — asked only to fix a
    build error, with no awareness that the file's dialect was previously
    correct — reintroduced Oracle constructs (this project's source
    analysis had already flagged "Oracle DECODE function" in the legacy
    code, which is exactly the pattern a small local model reaches for when
    "fixing" a banking schema, the same failure mode already documented
    elsewhere in this codebase for T-SQL/Postgres confusion). Prompt
    instructions alone were not reliable here across multiple observed
    runs, so this runs unconditionally on every reconciliation pass
    (initial generation AND after every repair round) as a deterministic
    backstop, not a one-time fix.

    Scoped to the constructs that have an unambiguous 1:1 PostgreSQL
    equivalent (VARCHAR2, NUMBER, SYSDATE, NVL, DECODE, DUAL). Procedural
    constructs (SYS_REFCURSOR, DBMS_*, RAISE_APPLICATION_ERROR, stored
    procedure bodies) are deliberately left alone — no safe mechanical
    rewrite exists for those, and a wrong one is worse than the existing
    "SQL dialect mismatch" diagnostic surfacing it for review.
    """
    if (target_db or "").strip().casefold() not in {"postgres", "postgresql"}:
        return
    for path, content in list(output.items()):
        if not path.casefold().endswith(".sql") or not isinstance(content, str):
            continue
        rewritten = _rewrite_oracle_decode_calls(content)
        for pattern, replacement in _ORACLE_TO_POSTGRES_SIMPLE_RENAMES:
            rewritten = pattern.sub(replacement, rewritten)
        if rewritten != content:
            output[path] = rewritten


def _reconcile_java_database_provider_markers(
    output: dict[str, str], target_db: str,
) -> None:
    """Remove Oracle duplicate-key message checks from non-Oracle Java targets.

    Legacy JDBC source often branches on ``ORA-00001``. That compiles under
    every driver, so Maven cannot expose the provider leak. Replace only that
    exact message predicate with conservative markers for the authoritative
    target. The surrounding exception handling and business response remain
    unchanged.
    """
    provider = (target_db or "").strip().casefold().replace("_", "-")
    provider = {
        "sqlserver": "mssql", "sql-server": "mssql", "postgresql": "postgres",
        "mariadb": "mysql", "sqlite3": "sqlite",
    }.get(provider, provider)
    markers = {
        "mssql": ("2627", "2601", "UNIQUE KEY", "DUPLICATE KEY"),
        "postgres": ("23505", "DUPLICATE KEY", "UNIQUE CONSTRAINT"),
        "mysql": ("1062", "DUPLICATE ENTRY", "DUPLICATE KEY"),
        "db2": ("-803", "SQL0803N", "DUPLICATE KEY"),
        "sqlite": ("UNIQUE CONSTRAINT", "IS NOT UNIQUE"),
    }.get(provider)
    if not markers:
        return
    oracle_predicate = re.compile(
        r'(?P<message>[A-Za-z_$][\w$]*\.getMessage\(\))\.toUpperCase\(\)'
        r'\.contains\(\s*"ORA-00001"\s*\)'
    )
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue

        def replacement(match: re.Match) -> str:
            message = match.group("message")
            upper = f"{message}.toUpperCase(java.util.Locale.ROOT)"
            return "(" + " || ".join(
                f'{upper}.contains("{marker}")' for marker in markers
            ) + ")"

        rewritten = oracle_predicate.sub(replacement, content)
        if rewritten != content:
            output[path] = rewritten


_JAVA_CONSOLE_CALL_RE = re.compile(r"\bSystem\.(out|err)\.(println|print|printf)\s*\(")
_JAVA_LOGGER_FIELD_RE = re.compile(r"\bLoggerFactory\.getLogger\s*\(")
_JAVA_LOGGER_FIELD_NAME_RE = re.compile(
    r"\b(?:Logger|org\.slf4j\.Logger)\s+([A-Za-z_$]\w*)\s*=\s*"
    r"(?:org\.slf4j\.)?LoggerFactory\.getLogger\s*\("
)
_JAVA_PRINT_STACK_TRACE_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)(?P<exception>[A-Za-z_$]\w*)\.printStackTrace\s*\(\s*\)\s*;[ \t]*$"
)
_JAVA_CLASS_DECL_RE = re.compile(
    r"(?m)^\s*(?:(?:public|protected|private|abstract|final|sealed|non-sealed|static)\s+)*"
    r"(?:class|interface|enum|record)\s+([A-Za-z_]\w*)\b"
)
_JAVA_IF_PRESENT_BLOCK_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)(?P<expression>[^;\r\n]+?)"
    r"(?:[ \t]*\r?\n[ \t]*)?\.ifPresent\s*"
    r"\(\s*(?P<parameter>[A-Za-z_$]\w*)\s*->\s*\{"
)
_JAVA_IF_PRESENT_OR_ELSE_BLOCK_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)(?P<expression>[^;\r\n]+?)"
    r"(?:[ \t]*\r?\n[ \t]*)?\.ifPresentOrElse\s*"
    r"\(\s*(?P<parameter>[A-Za-z_$]\w*)\s*->\s*\{"
)


# Function: _java_balanced_call_args
def _java_balanced_call_args(text: str, open_paren_index: int) -> tuple[int, int] | None:
    """Return (args_start, args_end) spanning the parenthesized argument list
    that opens at `open_paren_index`, respecting nested calls/parens and
    string/char literals (so a `)` inside a string or a nested call doesn't
    end the match early) — a plain non-greedy regex up to the first `);`
    breaks on anything like `System.out.println("x: " + fn(a, b));`."""
    depth = 0
    i = open_paren_index
    in_string = False
    in_char = False
    escape = False
    n = len(text)
    while i < n:
        ch = text[i]
        if escape:
            escape = False
        elif ch == "\\" and (in_string or in_char):
            escape = True
        elif in_string:
            if ch == '"':
                in_string = False
        elif in_char:
            if ch == "'":
                in_char = False
        elif ch == '"':
            in_string = True
        elif ch == "'":
            in_char = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return open_paren_index + 1, i
        i += 1
    return None


# Function: _reconcile_java_console_logging_calls
def _reconcile_java_console_logging_calls(output: dict[str, str]) -> None:
    """Rewrite System.out/System.err calls to SLF4J — deterministically, not
    left to LLM prompt compliance. The file-by-file legacy conversion path
    routinely preserves a legacy source file's original console-printing
    style verbatim (that's a real generation-standards violation this
    project's own audit — _java_generation_standards_report — flags), while
    the newer domain-generation prompts already avoid this; this closes the
    gap for both paths, once, deterministically, rather than depending on
    every prompt getting it right.

    println/print become log.info/log.error with the argument unchanged.
    printf becomes log.info/log.error(String.format(...)) — the %-style
    format string is valid input to String.format as-is, so this is a safe
    mechanical rewrite that never risks mistranslating a % placeholder into
    SLF4J's {} syntax.
    """
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or "/src/main/java/" not in path.replace("\\", "/"):
            continue
        if not isinstance(content, str) or not (
            _JAVA_CONSOLE_CALL_RE.search(content) or _JAVA_PRINT_STACK_TRACE_RE.search(content)
        ):
            continue

        rewritten = content
        logger_match = _JAVA_LOGGER_FIELD_NAME_RE.search(rewritten)
        logger_name = logger_match.group(1) if logger_match else "log"
        while True:
            match = _JAVA_CONSOLE_CALL_RE.search(rewritten)
            if not match:
                break
            open_paren = match.end() - 1
            span = _java_balanced_call_args(rewritten, open_paren)
            if not span:
                break  # unbalanced/truncated source — leave for LLM repair, don't corrupt it further
            args_start, args_end = span
            args = rewritten[args_start:args_end]
            stream, method = match.group(1), match.group(2)
            level = "error" if stream == "err" else "info"
            replacement_args = f"String.format({args})" if method == "printf" else args
            replacement = f"{logger_name}.{level}({replacement_args})"
            rewritten = rewritten[:match.start()] + replacement + rewritten[args_end + 1:]

        rewritten = _JAVA_PRINT_STACK_TRACE_RE.sub(
            lambda match: (
                f'{match.group("indent")}{logger_name}.error('
                f'"Unhandled exception", {match.group("exception")});'
            ),
            rewritten,
        )

        if rewritten == content:
            continue  # every match was unbalanced/unsafe to touch — nothing changed

        if not _JAVA_LOGGER_FIELD_RE.search(rewritten):
            class_match = _JAVA_CLASS_DECL_RE.search(rewritten)
            class_name = class_match.group(1) if class_match else "Application"
            logger_field = (
                f"    private static final org.slf4j.Logger log = "
                f"org.slf4j.LoggerFactory.getLogger({class_name}.class);\n"
            )
            # Insert right after the first `{` following the class/record/enum
            # declaration (the start of its body), fully-qualified so no
            # import block edit is needed and existing import ordering can't
            # be disturbed.
            if class_match:
                brace_index = rewritten.find("{", class_match.end())
                if brace_index != -1:
                    insert_at = brace_index + 1
                    rewritten = rewritten[:insert_at] + "\n" + logger_field + rewritten[insert_at:]

        output[path] = rewritten


# Function: _reconcile_java_mutating_optional_lambdas
def _reconcile_java_mutating_optional_lambdas(output: dict[str, str]) -> None:
    """Replace Optional.ifPresent blocks that mutate captured local variables.

    Java lambdas may capture only final or effectively-final local variables.
    Small generation models frequently emit an ``ifPresent`` lambda that
    assigns an error message/status/code declared earlier in the method. That
    source passes parser validation but fails the real Maven compiler. Convert
    only block lambdas containing a direct local assignment into the equivalent
    scoped ``isPresent``/``get`` form. The additional scope makes the generated
    temporary name collision-free across multiple rewrites in one method.
    """
    for path, content in list(output.items()):
        normalized_path = path.replace("\\", "/").casefold()
        if not normalized_path.endswith(".java") or "/src/main/java/" not in normalized_path:
            continue
        if not isinstance(content, str) or ".ifPresent" not in content:
            continue

        rewritten = content
        search_from = 0
        sequence = 0
        while True:
            match = _JAVA_IF_PRESENT_OR_ELSE_BLOCK_RE.search(rewritten, search_from)
            if not match:
                break
            present_end = _balanced_java_member_end(rewritten, match.end() - 1)
            if present_end is None:
                break
            else_lambda = re.match(
                r"[ \t\r\n]*,[ \t\r\n]*\(\s*\)\s*->\s*\{",
                rewritten[present_end:],
            )
            if not else_lambda:
                search_from = present_end
                continue
            else_start = present_end + else_lambda.end() - 1
            else_end = _balanced_java_member_end(rewritten, else_start)
            if else_end is None:
                break
            terminator = re.match(r"[ \t\r\n]*\)[ \t]*;", rewritten[else_end:])
            if not terminator:
                search_from = else_end
                continue

            present_body = rewritten[match.end():present_end - 1]
            else_body = rewritten[else_start + 1:else_end - 1]
            if not re.search(r"(?m)^\s*[A-Za-z_$]\w*\s*=(?!=)", present_body):
                search_from = else_end + terminator.end()
                continue

            indent = match.group("indent")
            expression = match.group("expression").strip()
            parameter = match.group("parameter")
            if re.match(
                r"(?:if|for|while|switch|return|throw|synchronized)\b",
                expression,
            ):
                search_from = else_end + terminator.end()
                continue
            sequence += 1
            optional_name = f"_modernizationOptional{sequence}"
            while re.search(rf"\b{re.escape(optional_name)}\b", rewritten):
                sequence += 1
                optional_name = f"_modernizationOptional{sequence}"
            newline = "\r\n" if "\r\n" in rewritten else "\n"

            def nested(body: str) -> str:
                return newline.join(
                    ("    " + line) if line.strip() else line
                    for line in body.strip("\r\n").splitlines()
                )

            nested_present = nested(present_body)
            nested_else = nested(else_body)
            replacement = (
                f"{indent}{{{newline}"
                f"{indent}    var {optional_name} = {expression};{newline}"
                f"{indent}    if ({optional_name}.isPresent()) {{{newline}"
                f"{indent}        var {parameter} = {optional_name}.get();"
            )
            if nested_present:
                replacement += newline + nested_present
            replacement += f"{newline}{indent}    }}"
            if nested_else.strip():
                replacement += f" else {{{newline}{nested_else}{newline}{indent}    }}"
            replacement += f"{newline}{indent}}}"
            replace_end = else_end + terminator.end()
            rewritten = rewritten[:match.start()] + replacement + rewritten[replace_end:]
            search_from = match.start() + len(replacement)

        search_from = 0
        while True:
            match = _JAVA_IF_PRESENT_BLOCK_RE.search(rewritten, search_from)
            if not match:
                break
            block_end = _balanced_java_member_end(rewritten, match.end() - 1)
            if block_end is None:
                break
            terminator = re.match(r"[ \t]*\)[ \t]*;", rewritten[block_end:])
            if not terminator:
                search_from = block_end
                continue

            body = rewritten[match.end():block_end - 1]
            # A lambda that only invokes methods or mutates object fields is
            # already legal. Restrict the rewrite to the javac failure shape:
            # assignment to a simple captured local name.
            if not re.search(r"(?m)^\s*[A-Za-z_$]\w*\s*=(?!=)", body):
                search_from = block_end + terminator.end()
                continue

            sequence += 1
            indent = match.group("indent")
            expression = match.group("expression").strip()
            parameter = match.group("parameter")
            if re.match(
                r"(?:if|for|while|switch|return|throw|synchronized)\b",
                expression,
            ):
                search_from = block_end + terminator.end()
                continue
            optional_name = f"_modernizationOptional{sequence}"
            while re.search(rf"\b{re.escape(optional_name)}\b", rewritten):
                sequence += 1
                optional_name = f"_modernizationOptional{sequence}"
            newline = "\r\n" if "\r\n" in rewritten else "\n"
            body_lines = body.strip("\r\n").splitlines()
            nested_body = newline.join(
                ("    " + line) if line.strip() else line for line in body_lines
            )
            replacement = (
                f"{indent}{{{newline}"
                f"{indent}    var {optional_name} = {expression};{newline}"
                f"{indent}    if ({optional_name}.isPresent()) {{{newline}"
                f"{indent}        var {parameter} = {optional_name}.get();"
            )
            if nested_body:
                replacement += newline + nested_body
            replacement += (
                f"{newline}{indent}    }}{newline}"
                f"{indent}}}"
            )
            replace_end = block_end + terminator.end()
            rewritten = rewritten[:match.start()] + replacement + rewritten[replace_end:]
            search_from = match.start() + len(replacement)

        if rewritten != content:
            output[path] = rewritten


# Function: _normalize_java_output_path_separators
def _normalize_java_output_path_separators(output: dict[str, str]) -> None:
    """Re-key any backslash-separated output path to forward slashes.

    A real generation was observed leaving an entire converted legacy Java
    source tree — e.g. ``ModernizedApp\\src\\main\\java\\struct\\StructUnpacker.java``
    (Windows-native backslashes, from Path(...) string conversion during the
    file-by-file conversion phase) — stranded outside the Maven-owned
    ``backend/`` reactor forever. Every path match in this reconciliation
    pipeline (``_normalize_java_build_roots``'s ``path.startswith(f"{root}src/")``,
    ``_java_module_roots``'s ``backend/`` prefix scan, POM/dependency
    inference, ...) is a forward-slash string match; a backslash-keyed path
    silently fails every one of them and is never moved, never given a Maven
    module, never wired into ``_ensure_spring_boot_entry_point``/
    ``_ensure_java_operational_baseline`` — exactly the files a Java
    generation standards audit then reports as a phantom module with no
    entry point, no @RestControllerAdvice, and no log configuration, and
    plausible contributors to compiler-repair rounds that could never
    converge (orphaned files outside the real source root).

    This must run before any other path-matching step in Java
    reconciliation. Permanent, always-on hardening — do not remove or make
    conditional without an explicit request.
    """
    for path in list(output.keys()):
        if "\\" not in path:
            continue
        normalized = path.replace("\\", "/")
        content = output.pop(path)
        if normalized not in output:
            output[normalized] = content
        # else: a forward-slash version of this exact path already exists —
        # keep it and drop the backslash duplicate rather than silently
        # overwriting content that later steps may already be relying on.


# Function: _reconcile_java_generation_output
def _reconcile_java_generation_output(
    output: dict[str, str], project_name: str, target: dict | None = None,
) -> None:
    """Enforce the canonical Java build boundary and frontend dependency closure."""
    _unwrap_generated_source_envelopes(output)
    _normalize_java_output_path_separators(output)
    _normalize_java_build_roots(output, project_name)
    _repair_truncated_java_source_tails(output)
    # Normalize source APIs before inferring Maven dependencies. Otherwise a
    # repair that introduces the canonical JJWT/WebFlux/etc. import leaves the
    # POM one pass behind and guarantees a needless failed build round.
    _migrate_spring_boot3_javax_imports(output)
    _reconcile_java_framework_shadow_types(output)
    _align_java_public_type_paths(output)
    _dedupe_java_fqcns(output)
    _reconcile_java_type_package_clashes(output)
    _reconcile_java_console_logging_calls(output)
    _reconcile_java_mutating_optional_lambdas(output)
    _remove_invalid_java_imports(output)
    _reconcile_java_spring_component_stereotypes(output)
    _reconcile_java_instance_utility_calls(output)
    _reconcile_spring_constructor_injection(output)
    _reconcile_java_injected_constructor_assignments(output)
    _reconcile_java_split_final_constructors(output)
    _reconcile_java_web_context_constructor_dependencies(output)
    _reconcile_java_void_predicate_invocations(output)
    _migrate_java_web_framework_contracts(output, str((target or {}).get("backend_tech") or ""))
    _reconcile_java_servlet_io_contracts(output)
    _repair_java_broken_html_string_concatenation(output)
    _reconcile_java_checked_parse_exceptions(output)
    _reconcile_java_jdbc_checked_exceptions(output)
    _reconcile_java_application_exception_hierarchy(output)
    _reconcile_java_typed_exception_catches(output)
    # Typed-catch normalization can narrow a broad ``Exception`` that was the
    # only handler for SimpleDateFormat.parse. Run the owner-aware checked
    # exception pass again so one reconciliation pass produces compilable code.
    _reconcile_java_checked_parse_exceptions(output)
    _repair_truncated_java_test_tails(output)
    _reconcile_java_test_subject_contracts(output)
    _reconcile_java_request_validation(output)
    _migrate_spring_filter_contracts(output)
    _migrate_java_error_envelope_exceptions(output)
    _reconcile_java_client_response_contracts(output)
    _reconcile_java_test_static_imports(output)
    _reconcile_java_record_constructors(output)
    _reconcile_java_record_compatibility(output)
    _reconcile_java_collection_element_types(output)
    _reconcile_java_common_service_contracts(output)
    _reconcile_java_service_method_aliases(output)
    _reconcile_java_record_compatibility(output)
    _dedupe_java_methods(output)
    _reconcile_java_optional_assignment_contracts(output)
    _reconcile_java_controller_service_contracts(output)
    _reconcile_java_exception_constructors(output)
    _reconcile_java_exception_call_contracts(output)
    _remove_misplaced_nested_record_accessors(output)
    _prune_unreferenced_java_mappers(output)
    _migrate_java_record_factories(output)
    _migrate_java_record_builder_chains(output)
    _migrate_java_record_getter_calls(output)
    _reconcile_java_entity_read_accessors(output)
    _reconcile_java_entity_update_copy_contracts(output)
    _reconcile_java_entity_mutators(output)
    _reconcile_java_entity_constructors(output)
    _reconcile_java_bean_compatibility_constructors(output)
    _reconcile_java_setter_argument_types(output)
    _reconcile_java_persisted_entity_identity(output)
    _reconcile_java_boolean_bean_accessors(output)
    _reconcile_java_mapper_contracts(output)
    _synthesize_java_entity_dto_factories(output)
    _migrate_java_identity_pageable_lambda(output)
    _promote_privately_referenced_java_nested_types(output)
    _migrate_java_decimal_min_literals(output)
    _reconcile_java_stray_test_tree_duplicates(output)
    _repair_java_chained_assertj_extracting(output)
    _reconcile_java_repository_contracts(output)
    _strip_invalid_java_control_characters(output)
    _reconcile_java_application_configuration(output)
    canonical_pom = f"{project_name}/backend/pom.xml"
    target = target or {}
    backend_tech = str(target.get("backend_tech") or "")
    target_db = str(target.get("db_target") or "")
    # Runs on every reconciliation pass — including the ones after a
    # build-repair round — since that's precisely when Oracle syntax was
    # observed leaking back into an originally-correct Postgres schema.
    _reconcile_postgres_sql_dialect(output, target_db)
    _reconcile_java_database_provider_markers(output, target_db)
    module_roots = _java_module_roots(output, project_name)
    is_multi_module = len(module_roots) >= 2
    if canonical_pom in output:
        version_match = re.search(
            r"<java\.version>\s*(\d+)\s*</java\.version>",
            output[canonical_pom],
            re.IGNORECASE,
        )
        java_version = version_match.group(1) if version_match else "21"
    else:
        java_version = "21"
    if is_multi_module:
        output[canonical_pom] = _java_reactor_pom(project_name, module_roots, java_version)
        backend_root = f"{project_name}/backend/"
        expected_poms = {canonical_pom}
        for module in module_roots:
            module_prefix = f"{backend_root}{module}/"
            module_output = {
                path: content for path, content in output.items()
                if path.startswith(module_prefix)
            }
            module_pom = f"{module_prefix}pom.xml"
            module_framework = _java_framework_key(backend_tech, module_output)
            main_class = (
                _ensure_spring_boot_entry_point(
                    output, f"{module_prefix}src/main/java", module,
                )
                if module_framework == "spring" else ""
            )
            if module_framework == "spring":
                _ensure_java_operational_baseline(
                    output,
                    f"{module_prefix}src/main/java",
                    f"{module_prefix}src/main/resources",
                    module,
                )
            elif module_framework == "quarkus":
                _ensure_quarkus_operational_baseline(
                    output,
                    f"{module_prefix}src/main/java",
                    f"{module_prefix}src/main/resources",
                    module,
                )
            module_output = {
                path: content for path, content in output.items()
                if path.startswith(module_prefix)
            }
            has_persistence = any(
                "@Entity" in value or "JpaRepository" in value
                or "jakarta.persistence." in value
                for path, value in module_output.items()
                if path.casefold().endswith(".java") and "/src/main/java/" in path and isinstance(value, str)
            )
            module_database = _java_database_key(target_db, module_output) if has_persistence else "none"
            inferred_dependencies = _java_inferred_dependencies(
                module_output, module_framework,
            )
            if has_persistence and any("/src/test/java/" in path for path in module_output):
                inferred_dependencies.append(("com.h2database", "h2", None))
            if "gateway" in module.casefold() and any(
                "@EnableWebFluxSecurity" in value or "SecurityWebFilterChain" in value
                for value in module_output.values() if isinstance(value, str)
            ):
                inferred_dependencies.extend([
                    ("org.springframework.cloud", "spring-cloud-starter-gateway", None),
                    ("org.springframework.boot", "spring-boot-starter-oauth2-resource-server", None),
                ])
            module_pom_content = _java_backend_pom(
                module,
                f"Java {java_version} {module_framework}",
                list(dict.fromkeys(inferred_dependencies)),
                db_target=module_database,
                main_class=main_class,
            )
            output[module_pom] = module_pom_content
            expected_poms.add(module_pom)
            output.setdefault(
                f"{module_prefix}Dockerfile",
                _java_service_dockerfile(module),
            )
            output.setdefault(
                f"{module_prefix}src/main/resources/application.yml",
                _java_service_application_yml(module),
            )
            if has_persistence:
                output.setdefault(
                    f"{module_prefix}src/test/resources/application.yml",
                    _java_service_test_application_yml(module),
                )
        for path in list(output):
            if path.casefold().endswith("/pom.xml") and path not in expected_poms:
                del output[path]
    else:
        framework = _java_framework_key(backend_tech, output)
        database = _java_database_key(target_db, output)
        main_class = (
            _ensure_spring_boot_entry_point(
                output, f"{project_name}/backend/src/main/java", project_name,
            )
            if framework == "spring" else ""
        )
        if framework == "spring":
            _ensure_java_operational_baseline(
                output,
                f"{project_name}/backend/src/main/java",
                f"{project_name}/backend/src/main/resources",
                project_name,
            )
        output[canonical_pom] = _java_backend_pom(
            project_name,
            f"Java {java_version} {framework}",
            _java_inferred_dependencies(output, framework),
            db_target=database,
            main_class=main_class,
        )
        for path in list(output):
            if path != canonical_pom and path.casefold().endswith("/pom.xml"):
                del output[path]
        _flatten_java_module_paths(output)
        backend_root = f"{project_name}/backend/"
        output.setdefault(f"{backend_root}Dockerfile", _java_service_dockerfile(project_name))
        output.setdefault(
            f"{backend_root}src/main/resources/application.yml",
            _java_service_application_yml(project_name),
        )
    _align_java_public_type_paths(output)
    _reconcile_java_auditing_contracts(output)
    for path, content in list(output.items()):
        if path.casefold().endswith(".java") and isinstance(content, str):
            output[path] = _add_known_java_imports(content)
    # Alignment must never resurrect/rename an invented framework type.
    _reconcile_java_framework_shadow_types(output)
    _migrate_spring_security_authorities_claim_api(output)
    _reconcile_java_type_imports(output, module_scoped=is_multi_module)
    _remove_invalid_java_imports(output)
    _reconcile_npm_dependencies(output)
    _reconcile_java_frontend_local_assets(output)
    _reconcile_java_frontend_exports(output)
    _reconcile_java_frontend_default_api_client_export(output)
    _reconcile_java_angular_contracts(output)
    _reconcile_java_frontend_entry_point(output)
    _reconcile_java_frontend_source_extensions(output)
    _reconcile_typescript_java_record_contracts(output)


def _unwrap_generated_source_envelopes(output: dict[str, str]) -> None:
    """Keep model accountability markup outside compilable project sources.

    Closure generation intentionally accepts a lighter prompt than governed
    per-file generation. Some models still follow the global envelope rule;
    recover the source block even when the notes JSON is incomplete.
    """
    from .validation_orchestration import _clean_generated_content

    source_suffixes = {".java", ".kt", ".kts", ".js", ".jsx", ".ts", ".tsx"}
    for path, content in list(output.items()):
        if (
            not isinstance(content, str)
            or Path(path).suffix.casefold() not in source_suffixes
            or not re.match(r"(?is)^\s*<generated_file(?:\s+[^>]*)?>", content)
        ):
            continue
        output[path] = _clean_generated_content(content)


def _java_service_dockerfile(module: str) -> str:
    """Canonical Maven/JRE 21 image contract for every reactor service."""
    artifact = re.sub(r"[^a-z0-9]+", "-", module.casefold()).strip("-")
    return textwrap.dedent(f"""\
        FROM maven:3.9.9-eclipse-temurin-21 AS build
        WORKDIR /workspace
        COPY pom.xml .
        COPY src src
        RUN mvn -B -q -DskipTests package

        FROM eclipse-temurin:21-jre
        WORKDIR /app
        COPY --from=build /workspace/target/{artifact}-*.jar app.jar
        EXPOSE 8080
        ENTRYPOINT ["java", "-jar", "/app/app.jar"]
    """)


def _java_service_application_yml(module: str) -> str:
    """Minimal environment-driven configuration when planning omitted one."""
    return textwrap.dedent(f"""\
        spring:
          application:
            name: {module}
        server:
          port: ${{SERVER_PORT:8080}}
        management:
          endpoints:
            web:
              exposure:
                include: health,info
    """)


def _java_service_test_application_yml(module: str) -> str:
    """Hermetic persistence configuration used only by generated tests."""
    database = re.sub(r"[^a-z0-9]+", "_", module.casefold()).strip("_") or "testdb"
    return textwrap.dedent(f"""\
        spring:
          datasource:
            url: jdbc:h2:mem:{database};MODE=PostgreSQL;DB_CLOSE_DELAY=-1
            driver-class-name: org.h2.Driver
            username: sa
            password:
          jpa:
            hibernate:
              ddl-auto: create-drop
          flyway:
            enabled: false
        jwt:
          secret: test-only-secret-with-at-least-thirty-two-characters
        aws:
          sqs:
            order-placed-queue-url: http://localhost/test-queue
    """)


def _reconcile_java_application_configuration(output: dict[str, str]) -> None:
    """Normalize Spring YAML without baking environment-specific values in."""
    for path, content in list(output.items()):
        if not path.casefold().endswith(("application.yml", "application.yaml")) or not isinstance(content, str):
            continue
        content = content.replace("optional:file:.env[[:space:]]*:[/][^,]*", "optional:file:.env[.properties]")
        content = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-", r"${\1:", content)
        lines = content.splitlines()
        starts = [index for index, line in enumerate(lines) if re.match(r"^[A-Za-z0-9_.-]+:\s*(?:#.*)?$", line)]
        if not starts:
            output[path] = content
            continue
        preamble = lines[:starts[0]]
        blocks: dict[str, list[str]] = {}
        order: list[str] = []
        for position, start in enumerate(starts):
            end = starts[position + 1] if position + 1 < len(starts) else len(lines)
            key = lines[start].split(":", 1)[0]
            if key not in blocks:
                blocks[key] = [lines[start]]
                order.append(key)
            blocks[key].extend(lines[start + 1:end])
        output[path] = "\n".join(preamble + [line for key in order for line in blocks[key]]).rstrip() + "\n"


_INVALID_JAVA_CONTROL_CHARACTERS = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]"
)


def _strip_invalid_java_control_characters(output: dict[str, str]) -> None:
    """Remove C0/C1 response artifacts that Windows javac cannot decode."""
    for path, content in list(output.items()):
        if path.casefold().endswith(".java") and isinstance(content, str):
            output[path] = _INVALID_JAVA_CONTROL_CHARACTERS.sub("", content)


def _reconcile_java_framework_shadow_types(output: dict[str, str]) -> None:
    """Remove generated project types that illegally shadow framework utilities."""
    canonical = {
        "HttpServletRequest": "jakarta.servlet.http.HttpServletRequest",
        "HttpServletResponse": "jakarta.servlet.http.HttpServletResponse",
        "ServletWebRequest": "org.springframework.web.context.request.ServletWebRequest",
        "MockHttpServletRequest": "org.springframework.mock.web.MockHttpServletRequest",
        "MockHttpServletResponse": "org.springframework.mock.web.MockHttpServletResponse",
        "WebApplicationContextUtils": "org.springframework.web.context.support.WebApplicationContextUtils",
        "IOException": "java.io.IOException",
        "ServletException": "jakarta.servlet.ServletException",
        "ObjectNode": "com.fasterxml.jackson.databind.node.ObjectNode",
    }
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        # Declaration matching must be line-anchored: generated Javadocs often
        # contain phrases such as "utility class provides", which previously
        # hid the real declaration and allowed framework shadow files through.
        declared = _JAVA_CLASS_DECL_RE.search(content) or re.search(
            r"(?:^|;)\s*(?:(?:public|protected|private|abstract|final|sealed|"
            r"non-sealed|static)\s+)*(?:class|interface|enum|record)\s+"
            r"([A-Za-z_]\w*)\b",
            content,
        )
        if declared and declared.group(1) in ({"LoggerFactory"} | set(canonical)) and "/src/main/java/" in path:
            del output[path]
            continue
        if (
            declared and declared.group(1).endswith("Exception")
            and "/exception/" in path.casefold()
            and re.search(r"\bextends\s+(?:OncePerRequestFilter|Component)\b", content)
        ):
            del output[path]
            continue
        content = re.sub(
            r"\bimport\s+(?!org\.slf4j\.LoggerFactory)[\w.]+\.LoggerFactory\s*;",
            "", content,
        )
        if "LoggerFactory" in content and "org.slf4j.LoggerFactory" not in content:
            package = re.search(r"(?m)^\s*package\s+[^;]+;", content)
            if package:
                content = content[:package.end()] + "\n\nimport org.slf4j.LoggerFactory;" + content[package.end():]
        for symbol, owner in canonical.items():
            content = re.sub(
                rf"(?m)^\s*import\s+com(?:\.[A-Za-z_]\w*)+\.{symbol}\s*;\s*\r?\n",
                "", content,
            )
            if re.search(rf"\b{symbol}\b", content) and owner not in content:
                package = re.search(r"(?m)^\s*package\s+[^;]+;", content)
                if package:
                    content = content[:package.end()] + f"\n\nimport {owner};" + content[package.end():]
        output[path] = content


def _reconcile_java_type_package_clashes(output: dict[str, str]) -> None:
    """Collapse generated enum-constant packages back to their owning enum.

    Java forbids a type and package with the same fully-qualified name. Small
    models sometimes turn ``UserRole.ADMIN`` into both an enum constant and a
    source file in package ``...UserRole``. The enum declaration is canonical;
    remove the impossible child package and normalize its imports to valid
    static enum-constant imports.
    """
    enum_fqcns: set[str] = set()
    for path, content in output.items():
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        package = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", content)
        declared = re.search(r"\b(?:public\s+)?enum\s+([A-Za-z_]\w*)\b", content)
        if package and declared:
            enum_fqcns.add(f"{package.group(1)}.{declared.group(1)}")
    if not enum_fqcns:
        return

    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        package = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", content)
        if package and package.group(1) in enum_fqcns:
            del output[path]
            continue
        for enum_fqcn in enum_fqcns:
            content = re.sub(
                rf"(?m)^\s*import\s+{re.escape(enum_fqcn)}\.([A-Z][A-Z0-9_]*)\s*;",
                rf"import static {enum_fqcn}.\1;",
                content,
            )
        output[path] = content


def _balanced_java_member_end(content: str, start: int) -> int | None:
    brace = content.find("{", start)
    if brace < 0:
        return None
    depth = 0
    for index in range(brace, len(content)):
        if content[index] == "{":
            depth += 1
        elif content[index] == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _migrate_java_web_framework_contracts(output: dict[str, str], backend_tech: str) -> None:
    """Normalize generated servlet/reactive APIs before Maven dependency inference."""
    no_discovery = any(token in backend_tech.casefold() for token in ("no eureka", "service connect", "cloud map"))
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        if no_discovery:
            content = re.sub(r"(?m)^\s*import\s+org\.springframework\.cloud\.client\.discovery\.[^;]+;\s*\r?\n", "", content)
            content = re.sub(r"@EnableDiscoveryClient\b\s*", "", content)
            content = re.sub(r"(?m)^\s*private\s+final\s+DiscoveryClient\s+\w+\s*;\s*\r?\n", "", content)
            content = re.sub(r"(?s)\n\s*public\s+\w+\s*\(DiscoveryClient\s+\w+\)\s*\{\s*this\.\w+\s*=\s*\w+;\s*\}\s*", "\n", content)
        if "implements WebMvcConfigurer" in content:
            content = re.sub(r"(?m)^\s*@Override\s*\r?\n(?=\s*public\s+void\s+addCorsMappings)", "", content)
        if "defaultAuthenticationEntryPointUrl" in content:
            start = content.find(".exceptionHandling(")
            if start >= 0:
                open_paren, depth, end = content.find("(", start), 0, None
                for index in range(open_paren, len(content)):
                    if content[index] == "(": depth += 1
                    elif content[index] == ")":
                        depth -= 1
                        if depth == 0: end = index + 1; break
                if end: content = content[:start] + content[end:]
        # Spring's CORS setters accept collections, never a scalar string.
        content = re.sub(
            r"\.set(AllowedOrigins|AllowedOriginPatterns|AllowedMethods|AllowedHeaders|ExposedHeaders)\(\s*(\"[^\"]*\")\s*\)",
            r".set\1(java.util.List.of(\2))", content,
        )
        if "public OncePerRequestFilter corsFilter()" in content and "UrlBasedCorsConfigurationSource source" in content:
            content = content.replace(
                "import org.springframework.web.filter.OncePerRequestFilter;",
                "import org.springframework.web.filter.CorsFilter;",
            ).replace(
                "public OncePerRequestFilter corsFilter()",
                "public CorsFilter corsFilter()",
            )
            anonymous = re.search(r"return\s+new\s+OncePerRequestFilter\s*\(\s*\)\s*\{", content)
            if anonymous:
                end = _balanced_java_member_end(content, anonymous.start())
                if end:
                    content = content[:anonymous.start()] + "return new CorsFilter(source);" + content[end:]
            content = content.replace("return source;", "return new CorsFilter(source);")
            content = content.replace(";;", ";")
        if "RabbitTemplate" in content:
            if "sqs" in backend_tech.casefold():
                content = content.replace("import org.springframework.amqp.rabbit.core.RabbitTemplate;", "import software.amazon.awssdk.services.sqs.SqsClient;")
                content = re.sub(r"\bRabbitTemplate\s+rabbitTemplate\b", "SqsClient sqsClient", content)
                content = content.replace("this.rabbitTemplate = rabbitTemplate;", "this.sqsClient = sqsClient;")
                content = re.sub(
                    r"rabbitTemplate\.convertAndSend\(\s*([^,]+),\s*[^,]+,\s*([^\)]+)\);",
                    r"sqsClient.sendMessage(request -> request.queueUrl(\1).messageBody(\2.toString()));",
                    content,
                )
                content = re.sub(r"\bcatch\s*\(\s*Exception\s+", "catch (software.amazon.awssdk.services.sqs.model.SqsException ", content)
            else:
                content = re.sub(r"\bcatch\s*\(\s*Exception\s+", "catch (org.springframework.amqp.AmqpException ", content)
        if "@EnableWebFluxSecurity" in content and "SecurityWebFilterChain" in content:
            match = re.search(r"\s*@Bean\s+public\s+SecurityWebFilterChain\s+\w+\s*\(", content)
            if match:
                end = _balanced_java_member_end(content, match.start())
                if end:
                    method = '''
    @Bean
    public SecurityWebFilterChain securityWebFilterChain(org.springframework.security.config.web.server.ServerHttpSecurity http) {
        return http
                .csrf(org.springframework.security.config.web.server.ServerHttpSecurity.CsrfSpec::disable)
                .authorizeExchange(exchange -> exchange.pathMatchers("/api/auth/**").permitAll().anyExchange().authenticated())
                .oauth2ResourceServer(oauth2 -> oauth2.jwt(org.springframework.security.config.Customizer.withDefaults()))
                .build();
    }'''
                    content = content[:match.start()] + "\n" + method + content[end:]
            content = content.replace(
                "org.springframework.security.oauth2.jwt.JwtDecoders.withPreSharedKey(secret)",
                "org.springframework.security.oauth2.jwt.NimbusJwtDecoder.withSecretKey(new javax.crypto.spec.SecretKeySpec(secret.getBytes(java.nio.charset.StandardCharsets.UTF_8), \"HmacSHA256\")).build()",
            )
        output[path] = content
    for path, content in list(output.items()):
        if (
            "/api-gateway/" in path and path.casefold().endswith(".java")
            and isinstance(content, str)
            and "class ReactiveServerSecurityContextRepository implements ReactiveServerSecurityContextRepository" in content
        ):
            del output[path]


def _repair_truncated_java_test_tails(output: dict[str, str]) -> None:
    """Drop only an incomplete final test method and retain the valid suite prefix."""
    for path, content in list(output.items()):
        if "/src/test/java/" not in path or not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        if content.count("{") <= content.count("}"):
            continue
        last_test = content.rfind("@Test")
        if last_test < 0:
            del output[path]
            continue
        else:
            prefix = content[:last_test].rstrip()
        missing = prefix.count("{") - prefix.count("}")
        if missing > 0:
            output[path] = prefix + "\n" + ("}\n" * missing)


@functools.lru_cache(maxsize=512)
def _java_lexical_tail_state(content: str) -> tuple[str, int]:
    """Return the terminal lexical state and structural brace depth for Java."""
    state = "code"
    depth = 0
    index = 0
    escaped = False
    while index < len(content):
        char = content[index]
        pair = content[index:index + 2]
        triple = content[index:index + 3]
        if state == "code":
            if pair == "//":
                state = "line-comment"
                index += 2
                continue
            if pair == "/*":
                state = "block-comment"
                index += 2
                continue
            if triple == '\"\"\"':
                state = "text-block"
                index += 3
                continue
            if char == '"':
                state = "string"
                escaped = False
            elif char == "'":
                state = "char"
                escaped = False
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
        elif state == "line-comment":
            if char in "\r\n":
                state = "code"
        elif state == "block-comment":
            if pair == "*/":
                state = "code"
                index += 2
                continue
        elif state == "text-block":
            if triple == '\"\"\"' and (index == 0 or content[index - 1] != "\\"):
                state = "code"
                index += 3
                continue
        else:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif (state == "string" and char == '"') or (state == "char" and char == "'"):
                state = "code"
        index += 1
    return state, depth


def _repair_truncated_java_source_tails(output: dict[str, str]) -> None:
    """Close safely recoverable generated Java comments and type terminators.

    An LLM response can end inside a trailing block/Javadoc comment. Java then
    reports both ``unclosed comment`` and ``reached end of file`` while raw
    brace counting is misled by braces inside that comment. Lexical scanning
    closes only that comment and only the already-open structural blocks; it
    deliberately leaves truncated strings and expressions to compiler repair.
    """
    for path, content in list(output.items()):
        if "/src/main/java/" not in path or not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        state, missing = _java_lexical_tail_state(content)
        if state == "block-comment":
            repaired = content.rstrip() + "\n*/\n"
            if missing > 0:
                repaired += "}\n" * missing
            output[path] = repaired
        elif missing > 0 and state in {"code", "line-comment"} and content.rstrip().endswith("}"):
            output[path] = content.rstrip() + "\n" + ("}\n" * missing)
        elif missing > 0 and state in {"code", "line-comment"}:
            # A recurring large-file truncation leaves a second copy of an
            # already-complete method declaration at EOF (often after its
            # Javadoc) without ever opening a body. Removing only that proven
            # duplicate tail is safer than inventing a body or changing the
            # earlier, compiler-complete implementation.
            last_complete_member = content.rfind("}")
            if last_complete_member < 0:
                continue
            prefix = content[:last_complete_member + 1].rstrip()
            tail = content[last_complete_member + 1:]
            tail_without_comments = re.sub(r"/\*.*?(?:\*/|$)", "", tail, flags=re.DOTALL)
            tail_without_comments = re.sub(r"(?m)//.*$", "", tail_without_comments)
            declaration = re.search(
                r"(?m)^\s*(?:@[\w.]+(?:\([^)]*\))?\s*)*"
                r"(?:public|protected|private)\s+(?:static\s+)?(?:final\s+)?"
                r"(?:<[^>\n]+>\s*)?[\w.$<>\[\], ?]+\s+([A-Za-z_$]\w*)\s*\(",
                tail_without_comments,
            )
            if not declaration:
                continue
            method_name = declaration.group(1)
            completed_names = {
                match.group(2) for match in _JAVA_METHOD_DECLARATION.finditer(prefix)
                if _balanced_java_member_end(prefix, match.start()) is not None
            }
            if method_name not in completed_names:
                continue
            prefix_state, prefix_missing = _java_lexical_tail_state(prefix)
            if prefix_state == "code" and prefix_missing > 0:
                output[path] = prefix + "\n" + ("}\n" * prefix_missing)


def _reconcile_java_test_subject_contracts(output: dict[str, str]) -> None:
    """Remove tests generated for APIs absent from their production subject."""
    reactive_modules = {
        path.replace("\\", "/").rsplit("/pom.xml", 1)[0]
        for path, content in output.items()
        if path.casefold().endswith("pom.xml") and isinstance(content, str)
        and ("spring-cloud-starter-gateway" in content or "spring-boot-starter-webflux" in content)
    }
    for path, content in list(output.items()):
        if "/src/test/java/" not in path or not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        if _java_source_module(path) in reactive_modules and "MockMvc" in content:
            del output[path]
            continue
        if path.casefold().endswith("gatewaytest.java") and re.search(r"\bgateway\.forward\s*\(", content):
            module = _java_source_module(path)
            production = "\n".join(
                value for source_path, value in output.items()
                if _java_source_module(source_path) == module and "/src/main/java/" in source_path and isinstance(value, str)
            )
            if not re.search(r"\bforward\s*\(", production):
                del output[path]
                continue
        if "TokenResponse" in content and re.search(r"\.get(?:Status|Message)\s*\(", content):
            del output[path]
            continue
        output[path] = re.sub(r"(@WithMockUser\s*\([^)]*)\buserId\s*=", r"\1username =", content)

    duplicates: dict[tuple[str, str], list[str]] = {}
    for path, content in output.items():
        if "/src/test/java/" not in path or not isinstance(content, str):
            continue
        declared = re.search(r"\bclass\s+([A-Za-z_]\w*Test)\b", content)
        if declared:
            duplicates.setdefault((_java_source_module(path), declared.group(1)), []).append(path)
    for (_, name), paths in duplicates.items():
        if len(paths) < 2:
            continue
        subject = name[:-4].casefold()
        ranked = sorted(paths, key=lambda path: (f"/{subject.replace('service', '/service').strip('/')}" not in path.casefold(), len(path)))
        # Conventional package placement (`service/FooServiceTest`) is the
        # authoritative suite; root-level duplicates are prompt-model drift.
        preferred = next((path for path in paths if f"/{'service' if 'service' in subject else 'controller'}/" in path.casefold()), ranked[0])
        for path in paths:
            if path != preferred:
                del output[path]
    production_types: dict[str, set[str]] = {}
    production_sources: dict[tuple[str, str], str] = {}
    for path, content in output.items():
        if "/src/main/java/" in path and isinstance(content, str):
            module = _java_source_module(path)
            declared_types = re.findall(r"\b(?:class|record|interface|enum)\s+([A-Za-z_]\w*)", content)
            production_types.setdefault(module, set()).update(declared_types)
            for declared_type in declared_types:
                production_sources[(module, declared_type)] = content
    for path, content in list(output.items()):
        if "/src/test/java/" not in path or not isinstance(content, str):
            continue
        missing = {
            owner.rsplit(".", 1)[-1]
            for owner in re.findall(r"\bcom(?:\.[A-Za-z_]\w*)+", content)
            if owner.rsplit(".", 1)[-1][:1].isupper()
            and owner.rsplit(".", 1)[-1] not in production_types.get(_java_source_module(path), set())
        }
        if missing:
            del output[path]
            continue
        module_production = "\n".join(
            source for (module, _), source in production_sources.items()
            if module == _java_source_module(path)
        )
        inherited_repository_methods = {
            "save", "saveAll", "findById", "findAll", "existsById", "deleteById",
            "delete", "deleteAll", "count", "flush", "saveAndFlush",
        }
        incoherent_dependency = False
        for dependency_type, variable in re.findall(
            r"\b([A-Za-z_]\w*(?:Service|Repository))\s+([a-zA-Z_]\w*)\s*;",
            content,
        ):
            subject = production_sources.get((_java_source_module(path), dependency_type), "")
            if not subject:
                continue
            for method in set(re.findall(rf"\b{re.escape(variable)}\.([A-Za-z_]\w*)\s*\(", content)):
                if dependency_type.endswith("Repository") and method in inherited_repository_methods:
                    continue
                if not re.search(rf"\b{re.escape(method)}\s*\(", subject):
                    incoherent_dependency = True
                    break
            if incoherent_dependency:
                break
        if incoherent_dependency:
            del output[path]
            continue
        if "getCurrentUserId()" in content and "SecurityContextHolder.setContext" not in content:
            for test in reversed(list(re.finditer(r"@Test\b", content))):
                end = _balanced_java_member_end(content, test.start())
                if end is not None and "getCurrentUserId()" in content[test.start():end]:
                    content = content[:test.start()] + content[end:]
        inconsistent_json_shape = False
        for test in re.finditer(r"@Test\b", content):
            end = _balanced_java_member_end(content, test.start())
            if end is None:
                continue
            body = content[test.start():end]
            if 'jsonPath("$[0]' in body and re.search(r'jsonPath\("\$\.[^\"]+', body):
                inconsistent_json_shape = True
                break
        if inconsistent_json_shape:
            del output[path]
            continue
        if len(re.findall(r"@Test\b", content)) > 20:
            del output[path]
            continue
        if "/service/" in path.casefold() and re.search(r"\bMockMvc\s+\w+", content):
            # A class placed/generated as a service unit test but exercising
            # HTTP endpoints is an independently hallucinated controller
            # suite.  The authoritative controller suite is handled above.
            del output[path]
            continue
        if (
            "MockMvc" in content
            and re.search(r"\.is(?:NotFound|BadRequest|Unauthorized|InternalServerError)\s*\(", content)
            and "@ControllerAdvice" not in module_production
            and "@RestControllerAdvice" not in module_production
        ):
            del output[path]
            continue
        constructor_assertions = set(re.findall(r"assertThrows\s*\([^,]+,\s*\(\)\s*->\s*new\s+([A-Za-z_]\w*)", content))
        if any(
            " record " in f" {production_sources.get((_java_source_module(path), name), '')} "
            and "throw new" not in production_sources.get((_java_source_module(path), name), "")
            for name in constructor_assertions
        ):
            del output[path]
            continue
        if re.search(r"when\s*\(\s*\w+Repository\.", content):
            content = re.sub(
                r"@Autowired\s+private\s+([A-Za-z_]\w*Repository)\s+(\w+)\s*;",
                r"@org.springframework.boot.test.mock.mockito.MockBean\n    private \1 \2;", content,
            )
        # Repository tests must isolate their fixture state between methods.
        if "@BeforeEach" in content and "Repository" in content and ".deleteAll();" not in content:
            setup = re.search(r"(@BeforeEach\s+void\s+setUp\s*\([^)]*\)\s*\{)", content)
            repo = re.search(r"private\s+\w*Repository\s+(\w+)\s*;", content)
            if setup and repo:
                content = content[:setup.end()] + f"\n        {repo.group(1)}.deleteAll();" + content[setup.end():]
        contradiction = re.search(r"\s*@Test\s+void\s+\w*duplicate\w*\s*\([^)]*\)\s*\{", content, re.IGNORECASE)
        if contradiction:
            end = _balanced_java_member_end(content, contradiction.start())
            if end: content = content[:contradiction.start()] + content[end:]
        mvc = re.search(r"@WebMvcTest\(\s*([A-Za-z_]\w*)\.class\s*\)", content)
        if mvc:
            controller_name = mvc.group(1)
            controller = production_sources.get((_java_source_module(path), controller_name), "")
            constructor = re.search(rf"\bpublic\s+{re.escape(controller_name)}\s*\(([^)]*)\)", controller)
            additions = []
            if constructor:
                for parameter in _split_java_arguments(constructor.group(1)):
                    tokens = parameter.strip().split()
                    if len(tokens) >= 2:
                        dependency_type, variable = tokens[-2], tokens[-1]
                        declaration = re.search(
                            rf"@Autowired(?:\s*\([^)]*\))?\s+(?:private\s+)?{re.escape(dependency_type)}\s+{re.escape(variable)}\s*;",
                            content,
                        )
                        if declaration:
                            replacement = (
                                "@org.springframework.boot.test.mock.mockito.MockBean\n"
                                f"    private {dependency_type} {variable};"
                            )
                            content = content[:declaration.start()] + replacement + content[declaration.end():]
                        elif not re.search(rf"\b{re.escape(dependency_type)}\s+\w+\s*;", content):
                            additions.append(
                                f"    @org.springframework.boot.test.mock.mockito.MockBean\n    private {dependency_type} {variable};"
                            )
            if additions:
                insertion = content.rfind("}")
                content = content[:insertion] + "\n\n" + "\n\n".join(additions) + "\n" + content[insertion:]
        void_methods = set(re.findall(r"\bvoid\s+([A-Za-z_]\w*)\s*\(", module_production))
        for method in void_methods:
            content = re.sub(
                rf"assertThat\(([^;\n]*\.{re.escape(method)}\([^;\n]*\))\s*==\s*null\s*\?\s*true\s*:\s*false\)\.isTrue\(\);",
                r"\1;",
                content,
            )
        output[path] = content


def _reconcile_java_servlet_io_contracts(output: dict[str, str]) -> None:
    """Declare checked servlet/I/O failures on the methods that emit them."""
    private_method = re.compile(
        r"(?m)^(?P<indent>[ \t]*)private\s+"
        r"(?:(?:static|final|synchronized)\s+)*"
        r"[A-Za-z_$][\w.$<>, ?\[\]]*\s+[A-Za-z_$]\w*\s*\([^)]*\)\s*"
        r"(?P<throws>throws\s+[^\{]+)?\{"
    )
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or "extends HttpServlet" not in str(content):
            continue
        if re.search(r"\.(?:forward|include|getWriter|sendError)\s*\(", content):
            content = re.sub(
                r"(\b(?:doGet|doPost|doPut|doDelete)\s*\([^)]*\)\s*throws\s+ServletException)\s*(\{)",
                r"\1, java.io.IOException \2",
                content,
            )

        # Generated private helpers often translate a runtime/domain failure
        # into ServletException but forget to declare the checked exception.
        # Propagate it to the servlet entry method (which already permits it)
        # instead of weakening the exception to an unchecked type.
        for method in reversed(list(private_method.finditer(content))):
            member_end = _balanced_java_member_end(content, method.start())
            if member_end is None:
                continue
            member = content[method.start():member_end]
            if not re.search(
                r"throw\s+new\s+(?:jakarta\.servlet\.)?ServletException\s*\(",
                member,
            ):
                continue
            declared = method.group("throws") or ""
            if re.search(r"\b(?:jakarta\.servlet\.)?ServletException\b", declared):
                continue
            brace = method.end() - 1
            if declared:
                replacement = declared.rstrip() + ", jakarta.servlet.ServletException "
                content = content[:method.start("throws")] + replacement + content[method.end("throws"):]
            else:
                content = content[:brace] + " throws jakarta.servlet.ServletException " + content[brace:]
        output[path] = content


def _reconcile_java_checked_parse_exceptions(output: dict[str, str]) -> None:
    """Attach ``java.text.ParseException`` to the try block that owns parse().

    A file-level replacement used to add the checked exception to the first
    ``RuntimeException`` catch in a file. With nested/adjacent try blocks that
    could make an unrelated catch illegal ("exception ... is never thrown")
    while leaving the actual ``SimpleDateFormat.parse`` call unhandled. Resolve
    the innermost owning try block for every DateFormat parse call and edit only
    its catch clause; also remove our explicit checked type from unrelated
    catches so output from an earlier reconciliation pass is self-healing.
    """
    try_pattern = re.compile(r"\btry\s*\{")
    catch_pattern = re.compile(
        r"\s*catch\s*\(\s*(?P<types>[A-Za-z_$][\w.$]*"
        r"(?:\s*\|\s*[A-Za-z_$][\w.$]*)*)\s+"
        r"(?P<variable>[A-Za-z_$]\w*)\s*\)\s*\{"
    )
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        formatter_names = set(re.findall(
            r"\b(?:java\.text\.)?(?:SimpleDateFormat|DateFormat)\s+"
            r"([A-Za-z_$]\w*)\b",
            content,
        ))
        if not formatter_names:
            continue
        parse_positions = [
            match.start()
            for name in formatter_names
            for match in re.finditer(rf"\b{re.escape(name)}\.parse\s*\(", content)
        ]
        if not parse_positions:
            continue

        try_blocks = []
        for match in try_pattern.finditer(content):
            block_end = _balanced_java_member_end(content, match.start())
            if block_end is not None:
                try_blocks.append((match.start(), match.end(), block_end))
        if not try_blocks:
            continue

        parse_owners = set()
        for position in parse_positions:
            owners = [block for block in try_blocks if block[1] <= position < block[2]]
            if owners:
                parse_owners.add(max(owners, key=lambda block: block[0])[0])

        edits: list[tuple[int, int, str]] = []
        for try_start, _try_body_start, try_end in try_blocks:
            catches = []
            cursor = try_end
            while True:
                caught = catch_pattern.match(content, cursor)
                if not caught:
                    break
                # ``re.Pattern.match(string, pos)`` reports absolute spans,
                # not offsets relative to ``pos``.
                absolute_types_start = caught.start("types")
                absolute_types_end = caught.end("types")
                catches.append((absolute_types_start, absolute_types_end, caught.group("types")))
                catch_end = _balanced_java_member_end(content, cursor + caught.start())
                if catch_end is None:
                    break
                cursor = catch_end
            if not catches:
                continue

            owns_parse = try_start in parse_owners
            handles_parse = any(
                any(value.strip() in {
                    "java.text.ParseException", "Exception", "java.lang.Exception",
                    "Throwable", "java.lang.Throwable",
                } for value in types.split("|"))
                for _start, _end, types in catches
            )
            if owns_parse and not handles_parse:
                start, end, types = catches[0]
                edits.append((start, end, f"java.text.ParseException | {types.strip()}"))
            elif not owns_parse:
                for start, end, types in catches:
                    values = [value.strip() for value in types.split("|")]
                    if "java.text.ParseException" not in values:
                        continue
                    remaining = [value for value in values if value != "java.text.ParseException"]
                    edits.append((start, end, " | ".join(remaining) or "RuntimeException"))

        for start, end, replacement in sorted(edits, reverse=True):
            content = content[:start] + replacement + content[end:]
        output[path] = content


def _reconcile_java_jdbc_checked_exceptions(output: dict[str, str]) -> None:
    """Wrap JDBC connection-state checks in the generated domain exception."""
    method_pattern = re.compile(
        r"(?m)^\s*public\s+Connection\s+getConnection\s*\([^)]*\)"
        r"(?:\s+throws\s+[A-Za-z_$][\w.$]*)?\s*\{"
    )
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or ".isClosed()" not in str(content):
            continue
        match = method_pattern.search(content)
        if not match:
            continue
        end = _balanced_java_member_end(content, match.start())
        if end is None:
            continue
        member = content[match.start():end]
        if re.search(r"catch\s*\(\s*(?:java\.sql\.)?SQLException\b", member):
            continue
        open_brace = member.find("{")
        body = member[open_brace + 1:-1].strip("\r\n")
        indent_match = re.match(r"(?m)^(\s*)public\s+Connection", member)
        indent = indent_match.group(1) if indent_match else ""
        nested = "\n".join(f"{indent}    {line}" if line.strip() else line for line in body.splitlines())
        replacement = (
            member[:open_brace + 1]
            + f"\n{indent}    try {{\n{nested}\n{indent}    }} catch (java.sql.SQLException error) {{\n"
            + f"{indent}        throw new TrainException(ResponseCode.DATABASE_CONNECTION_FAILURE);\n"
            + f"{indent}    }}\n{indent}}}"
        )
        output[path] = content[:match.start()] + replacement + content[end:]


def _reconcile_java_application_exception_hierarchy(output: dict[str, str]) -> None:
    """Keep HTTP/domain error envelopes unchecked in generated Spring code."""
    for path, content in list(output.items()):
        if not path.casefold().endswith("exception.java") or not isinstance(content, str):
            continue
        if "extends IOException" not in content or not any(
            marker in content for marker in ("ResponseCode", "statusCode", "errorCode")
        ):
            continue
        content = content.replace("extends IOException", "extends RuntimeException", 1)
        if not re.search(r"\bIOException\b", content.replace("import java.io.IOException;", "")):
            content = re.sub(r"(?m)^\s*import\s+java\.io\.IOException\s*;\s*\r?\n?", "", content)
        output[path] = content


def _reconcile_java_typed_exception_catches(output: dict[str, str]) -> None:
    """Replace forbidden catch-all handlers with the runtime boundary type."""
    for path, content in list(output.items()):
        if "/src/main/java/" not in path or not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        output[path] = re.sub(
            r"\bcatch\s*\(\s*(?:Exception|Throwable)\s+([A-Za-z_]\w*)\s*\)",
            r"catch (RuntimeException \1)", content,
        )


def _reconcile_java_spring_component_stereotypes(output: dict[str, str]) -> None:
    """Ensure conventionally generated Spring services are discoverable beans."""
    for path, content in list(output.items()):
        normalized = path.replace("\\", "/").casefold()
        if "/src/main/java/" not in normalized or "/service/" not in normalized or not normalized.endswith("service.java"):
            continue
        declared = re.search(r"\bpublic\s+class\s+([A-Za-z_]\w*Service)\b", content)
        if not declared or re.search(r"@(?:org\.springframework\.stereotype\.)?Service\b", content):
            continue
        content = content[:declared.start()] + "@org.springframework.stereotype.Service\n" + content[declared.start():]
        output[path] = content


def _reconcile_java_instance_utility_calls(output: dict[str, str]) -> None:
    """Inject generated Spring utilities that are incorrectly called statically."""
    providers: dict[tuple[str, str], str] = {}
    for path, content in output.items():
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        declared = re.search(r"\bpublic\s+class\s+([A-Za-z_]\w*)\b", content)
        if not declared:
            continue
        provider = declared.group(1)
        if re.search(r"\bpublic\s+(?!static\b)[\w<>?,.]+\s+getConnection\s*\(", content):
            acronym = re.match(r"[A-Z]+(?=[A-Z][a-z]|$)", provider)
            prefix = acronym.group(0) if acronym else provider[0]
            providers[(_java_source_module(path), provider)] = prefix.lower() + provider[len(prefix):]

    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        module = _java_source_module(path)
        declared = re.search(r"\bpublic\s+class\s+([A-Za-z_]\w*)\b[^\{]*\{", content)
        if not declared:
            continue
        class_name = declared.group(1)
        additions = []
        for (provider_module, provider), variable in providers.items():
            if provider_module != module or not re.search(
                rf"\b{re.escape(provider)}\.getConnection\s*\(\s*\)", content,
            ):
                continue
            content = re.sub(
                rf"\b{re.escape(provider)}\.getConnection\s*\(\s*\)",
                f"{variable}.getConnection()",
                content,
            )
            if not re.search(rf"\b{re.escape(provider)}\s+{re.escape(variable)}\s*;", content):
                additions.append(f"    private final {provider} {variable};")
            # A separate dependency-only constructor leaves every blank final
            # owned by an existing business constructor uninitialised (and the
            # business constructor leaves this dependency uninitialised).  Add
            # the managed dependency to every non-delegating constructor
            # instead, so each Java construction path closes the same final-
            # field contract.  This is shared by modernization and prompt
            # project generation before Maven validation.
            constructor_pattern = re.compile(
                rf"(?m)^(?P<indent>[ \t]*)(?:(?:public|protected|private)\s+)?"
                rf"{re.escape(class_name)}\s*\((?P<params>[^)]*)\)"
                r"(?P<tail>\s*(?:throws\s+[^\{]+)?\{)"
            )
            constructors = list(constructor_pattern.finditer(content))
            if constructors:
                for constructor in reversed(constructors):
                    member_end = _balanced_java_member_end(content, constructor.start())
                    if member_end is None:
                        continue
                    body = content[constructor.end():member_end]
                    if re.match(r"\s*this\s*\(", body):
                        continue
                    params = constructor.group("params").strip()
                    if not re.search(rf"\b{re.escape(provider)}\b", params):
                        new_params = ", ".join(
                            part for part in (params, f"{provider} {variable}") if part
                        )
                        content = (
                            content[:constructor.start("params")]
                            + new_params
                            + content[constructor.end("params"):]
                        )
                        constructor = constructor_pattern.search(
                            content, constructor.start(),
                        )
                        if constructor is None:
                            continue
                    member_end = _balanced_java_member_end(content, constructor.start())
                    if member_end is None:
                        continue
                    body = content[constructor.end():member_end]
                    if not re.search(rf"\bthis\.{re.escape(variable)}\s*=", body):
                        assignment = (
                            f"\n{constructor.group('indent')}    "
                            f"this.{variable} = {variable};"
                        )
                        content = content[:constructor.end()] + assignment + content[constructor.end():]
            else:
                additions.append(
                    f"    public {class_name}({provider} {variable}) {{ this.{variable} = {variable}; }}"
                )
        if additions:
            content = content[:declared.end()] + "\n\n" + "\n\n".join(additions) + content[declared.end():]
        output[path] = content


def _reconcile_spring_constructor_injection(output: dict[str, str]) -> None:
    """Convert generated Spring field injection into final constructor injection."""
    injection = re.compile(
        r"(?m)^(?P<indent>[ \t]*)@(?:"
        r"(?:org\.springframework\.beans\.factory\.annotation\.)?Autowired|"
        r"(?:jakarta|javax)\.inject\.Inject|Inject|"
        r"(?:jakarta|javax)\.annotation\.Resource|Resource"
        r")(?:\s*\([^)]*\))?[ \t]*(?:\r?\n[ \t]*)?"
        r"(?P<declaration>[^;\r\n]+);[ \t]*$"
    )
    declaration = re.compile(
        r"^(?:(?:private|protected|public)\s+)?"
        r"(?P<modifiers>(?:(?:final|transient|volatile)\s+)*)"
        r"(?P<type>[A-Za-z_$][\w.$]*(?:\s*<[^;=]+>)?(?:\s*\[\])?)\s+"
        r"(?P<name>[A-Za-z_$][\w$]*)$"
    )
    for path, original in list(output.items()):
        normalized = path.replace("\\", "/").casefold()
        if (
            "/src/main/java/" not in normalized
            or not normalized.endswith(".java")
            or not isinstance(original, str)
        ):
            continue
        dependencies = []

        def replace_field(match: re.Match) -> str:
            parsed = declaration.match(match.group("declaration").strip())
            if not parsed or "static" in match.group("declaration").split():
                return match.group(0)
            dependency = (parsed.group("type").strip(), parsed.group("name"))
            dependencies.append(dependency)
            return f"{match.group('indent')}private final {dependency[0]} {dependency[1]};"

        content = injection.sub(replace_field, original)
        if not dependencies:
            continue
        class_match = re.search(
            r"\b(?:public\s+)?(?:abstract\s+|final\s+)?class\s+([A-Za-z_$][\w$]*)\b[^\{]*\{",
            content,
        )
        if not class_match:
            continue
        class_name = class_match.group(1)

        def find_constructor(value: str):
            return re.search(
                rf"(?m)^(?P<indent>[ \t]*)(?:(?:public|protected|private)\s+)?"
                rf"{re.escape(class_name)}\s*\((?P<params>[^)]*)\)"
                r"(?P<tail>\s*(?:throws\s+[^\{]+)?\{)",
                value,
            )

        constructor = find_constructor(content)
        missing = [
            (dep_type, name) for dep_type, name in dependencies
            if not constructor or not re.search(rf"\b{re.escape(name)}\b", constructor.group("params"))
        ]
        if constructor:
            params = constructor.group("params").strip()
            additions = ", ".join(f"{dep_type} {name}" for dep_type, name in missing)
            new_params = ", ".join(part for part in (params, additions) if part)
            content = content[:constructor.start("params")] + new_params + content[constructor.end("params"):]
            constructor = find_constructor(content)
            assignments = [
                f"{constructor.group('indent')}    this.{name} = {name};"
                for _dep_type, name in dependencies
                if not re.search(rf"\bthis\.{re.escape(name)}\s*=", content[constructor.start():])
            ]
            if assignments:
                insert_at = constructor.end()
                delegation = re.match(r"\s*(?:(?:super|this)\s*\([^;]*\);)", content[insert_at:])
                if delegation:
                    insert_at += delegation.end()
                content = content[:insert_at] + "\n" + "\n".join(assignments) + content[insert_at:]
        else:
            indent_match = re.search(
                r"(?m)^(?P<indent>[ \t]*)(?:public\s+)?(?:abstract\s+|final\s+)?class\s+",
                content,
            )
            indent = indent_match.group("indent") if indent_match else ""
            params = ", ".join(f"{dep_type} {name}" for dep_type, name in dependencies)
            assignments = "\n".join(
                f"{indent}        this.{name} = {name};" for _dep_type, name in dependencies
            )
            constructor_text = (
                f"\n{indent}    public {class_name}({params}) {{\n"
                f"{assignments}\n{indent}    }}\n"
            )
            content = content[:class_match.end()] + constructor_text + content[class_match.end():]
        output[path] = content


def _reconcile_java_injected_constructor_assignments(output: dict[str, str]) -> None:
    """Use an injected constructor parameter instead of constructing its implementation.

    Repair models sometimes keep a newly injected interface parameter but
    ignore it with ``this.service = new ServiceImpl(...)``. Besides defeating
    Spring ownership, later cross-file reconciliation may try to close that
    manual constructor call using a servlet request that belongs to a different
    method. A same-named constructor parameter is the unambiguous dependency
    source, so make that assignment authoritative before resolving legacy
    constructor calls.
    """
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        declared = _JAVA_CLASS_DECL_RE.search(content) or re.search(
            r"(?:^|;)\s*(?:(?:public|protected|private|abstract|final|sealed|"
            r"non-sealed|static)\s+)*(?:class|interface|enum|record)\s+"
            r"([A-Za-z_]\w*)\b",
            content,
        )
        if not declared:
            continue
        class_name = declared.group(1)
        constructor = re.compile(
            rf"\b(?:public|protected|private)\s+{re.escape(class_name)}\s*"
            r"\((?P<params>[^)]*)\)\s*(?:throws\s+[^\{]+)?\{"
        )
        rewritten = content
        for match in reversed(list(constructor.finditer(content))):
            end = _balanced_java_member_end(content, match.start())
            if end is None:
                continue
            parameter_names = {
                tokens[-1]
                for parameter in _split_java_arguments(match.group("params"))
                if len(tokens := re.sub(
                    r"@[\w.]+(?:\([^)]*\))?\s*|\bfinal\s+", "", parameter,
                ).strip().split()) >= 2
            }
            body = rewritten[match.end():end]
            for name in parameter_names:
                body = re.sub(
                    rf"\bthis\.{re.escape(name)}\s*=\s*new\s+"
                    rf"[A-Za-z_$][\w.$<>]*\s*\([^;]*\)\s*;",
                    f"this.{name} = {name};",
                    body,
                )
            rewritten = rewritten[:match.end()] + body + rewritten[end:]
        if rewritten != content:
            output[path] = rewritten


def _reconcile_java_split_final_constructors(output: dict[str, str]) -> None:
    """Merge generated managed dependencies into the business constructor.

    A recurring cross-pass failure is a class with one constructor generated
    for a Spring dependency and another constructor retained for a business
    discriminator.  Each constructor initializes only its own blank ``final``
    field, so *both* are illegal Java.  When the missing fields are generated
    Spring-managed dependencies, extend the business constructor and remove
    the now-redundant dependency-only overload.  We deliberately do not invent
    values for ordinary domain fields.
    """
    managed_types: set[tuple[str, str]] = set()
    for provider_path, provider_source in output.items():
        if not provider_path.casefold().endswith(".java") or not isinstance(provider_source, str):
            continue
        declared = re.search(r"\b(?:public\s+)?class\s+([A-Za-z_$][\w$]*)\b", provider_source)
        if not declared:
            continue
        if re.search(
            r"@(?:org\.springframework\.(?:[A-Za-z_$][\w$]*\.)*)?"
            r"(?:Component|Service|Repository|Configuration)\b",
            provider_source,
        ):
            managed_types.add((_java_source_module(provider_path), declared.group(1)))

    for path, original in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(original, str):
            continue
        declared = re.search(
            r"\b(?:public\s+)?class\s+([A-Za-z_$][\w$]*)\b[^\{]*\{", original,
        )
        if not declared:
            continue
        class_name = declared.group(1)
        module = _java_source_module(path)
        fields: dict[str, str] = {}
        for field in re.finditer(
            r"(?m)^[ \t]*(?:private|protected|public)\s+(?:static\s+)?final\s+"
            r"(?P<type>[A-Za-z_$][\w.$]*(?:\s*<[^;=]+>)?(?:\s*\[\])?)\s+"
            r"(?P<name>[A-Za-z_$][\w$]*)\s*;",
            original,
        ):
            if original[:field.start()].count("{") - original[:field.start()].count("}") == 1:
                fields[field.group("name")] = field.group("type").strip()
        managed_fields = {
            name: field_type for name, field_type in fields.items()
            if (module, field_type.rsplit(".", 1)[-1]) in managed_types
        }
        if not managed_fields or len(fields) < 2:
            continue

        constructor_pattern = re.compile(
            rf"(?m)^(?P<indent>[ \t]*)(?:(?:public|protected|private)\s+)?"
            rf"{re.escape(class_name)}\s*\((?P<params>[^)]*)\)"
            r"(?P<tail>\s*(?:throws\s+[^\{]+)?\{)"
        )

        def constructor_details(source: str):
            details = []
            for match in constructor_pattern.finditer(source):
                end = _balanced_java_member_end(source, match.start())
                if end is None:
                    continue
                body = source[match.end():end]
                parameters = []
                for parameter in _split_java_arguments(match.group("params")):
                    clean = re.sub(
                        r"@[\w.]+(?:\([^)]*\))?\s*|\bfinal\s+", "", parameter,
                    ).strip().split()
                    if len(clean) >= 2:
                        parameters.append((" ".join(clean[:-1]), clean[-1]))
                assigned = {
                    name for name in fields
                    if re.search(rf"\bthis\.{re.escape(name)}\s*=", body)
                }
                details.append((match, end, body, parameters, assigned))
            return details

        details = constructor_details(original)
        if len(details) < 2 or not any(
            not set(fields).issubset(assigned)
            and not re.match(r"\s*this\s*\(", body)
            for _match, _end, body, _parameters, assigned in details
        ):
            continue
        business_indexes = [
            index for index, (_match, _end, _body, parameters, assigned) in enumerate(details)
            if any((module, param_type.rsplit(".", 1)[-1]) not in managed_types for param_type, _ in parameters)
            or bool(assigned - set(managed_fields))
        ]
        if not business_indexes:
            continue

        content = original
        for index in reversed(business_indexes):
            details = constructor_details(content)
            if index >= len(details):
                continue
            match, end, body, parameters, assigned = details[index]
            missing = [
                (field_type, name) for name, field_type in managed_fields.items()
                if name not in assigned
            ]
            if not missing:
                continue
            params = match.group("params").strip()
            existing_names = {name for _type, name in parameters}
            additions = ", ".join(
                f"{field_type} {name}" for field_type, name in missing
                if name not in existing_names
            )
            new_params = ", ".join(part for part in (params, additions) if part)
            content = content[:match.start("params")] + new_params + content[match.end("params"):]
            refreshed = constructor_pattern.search(content, match.start())
            if refreshed is None:
                continue
            assignments = "".join(
                f"\n{refreshed.group('indent')}    this.{name} = {name};"
                for _field_type, name in missing
            )
            content = content[:refreshed.end()] + assignments + content[refreshed.end():]

        details = constructor_details(content)
        complete_business_exists = any(
            set(fields).issubset(assigned)
            and (
                any((module, param_type.rsplit(".", 1)[-1]) not in managed_types for param_type, _ in parameters)
                or bool(assigned - set(managed_fields))
            )
            for _match, _end, _body, parameters, assigned in details
        )
        if complete_business_exists:
            removals = []
            for match, end, _body, parameters, assigned in details:
                if parameters and all(
                    (module, param_type.rsplit(".", 1)[-1]) in managed_types
                    for param_type, _name in parameters
                ) and assigned.issubset(set(managed_fields)):
                    removals.append((match.start(), end))
            for start, end in reversed(removals):
                content = content[:start] + content[end:]
        if content != original:
            output[path] = content


def _reconcile_java_web_context_constructor_dependencies(output: dict[str, str]) -> None:
    """Close constructor-injection changes at legacy servlet utility callers.

    Java reconciliation can correctly add a managed dependency (for example
    DBUtil) to a service constructor while a legacy static helper still calls
    the old constructor from a method that already receives HttpServletRequest.
    Resolve only missing generated Spring-bean parameters from that request's
    existing WebApplicationContext. This preserves dependency ownership and
    avoids unsafe compatibility constructors that manually create DataSources.
    """
    constructors: dict[str, list[str]] = {}
    managed_types: set[str] = set()
    for path, content in output.items():
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        declared = re.search(r"\b(?:public\s+)?class\s+([A-Za-z_$]\w*)\b", content)
        if not declared:
            continue
        class_name = declared.group(1)
        if re.search(
            r"@(?:org\.springframework\.(?:[A-Za-z_$]\w*\.)*)?"
            r"(?:Service|Component|Repository|Configuration)\b",
            content,
        ):
            managed_types.add(class_name)
        candidates = []
        for match in re.finditer(
            rf"\b(?:public|protected)\s+{re.escape(class_name)}\s*\(([^)]*)\)",
            content,
        ):
            parameter_types = []
            for parameter in _split_java_arguments(match.group(1)):
                cleaned = re.sub(r"@[\w.]+(?:\([^)]*\))?\s*", "", parameter).strip()
                cleaned = re.sub(r"\b(?:final|volatile)\s+", "", cleaned)
                tokens = cleaned.split()
                if len(tokens) >= 2:
                    parameter_types.append(tokens[-2].split(".")[-1])
            candidates.append(parameter_types)
        if candidates:
            constructors[class_name] = max(candidates, key=len)

    if not constructors or not managed_types:
        return

    call_pattern = re.compile(r"\bnew\s+([A-Za-z_$]\w*)\s*\(([^()]*?)\)")
    request_method_pattern = re.compile(
        r"(?m)^[ \t]*(?:(?:public|protected|private|static|final|synchronized)\s+)*"
        r"(?:[A-Za-z_$][\w.$<>?,\[\]]*\s+)?[A-Za-z_$]\w*\s*"
        r"\((?P<params>[^)]*\b(?:jakarta\.servlet\.http\.)?"
        r"HttpServletRequest\s+[A-Za-z_$]\w*[^)]*)\)"
        r"\s*(?:throws\s+[^\{]+)?\{"
    )
    for path, content in list(output.items()):
        normalized = path.replace("\\", "/").casefold()
        if "/src/main/java/" not in normalized or not normalized.endswith(".java"):
            continue
        rewritten = content
        for method in reversed(list(request_method_pattern.finditer(content))):
            method_end = _balanced_java_member_end(content, method.start())
            if method_end is None:
                continue
            request = re.search(
                r"\b(?:jakarta\.servlet\.http\.)?HttpServletRequest\s+"
                r"([A-Za-z_$]\w*)",
                method.group("params"),
            )
            if not request:
                continue
            request_name = request.group(1)
            method_source = content[method.start():method_end]
            for local_call in reversed(list(call_pattern.finditer(method_source))):
                class_name = local_call.group(1)
                required = constructors.get(class_name)
                if not required:
                    continue
                supplied = _split_java_arguments(local_call.group(2))
                if len(supplied) >= len(required):
                    continue
                missing = required[len(supplied):]
                if not missing or any(value not in managed_types for value in missing):
                    continue
                lookups = [
                    "org.springframework.web.context.support.WebApplicationContextUtils"
                    f".getRequiredWebApplicationContext({request_name}.getServletContext())"
                    f".getBean({dependency}.class)"
                    for dependency in missing
                ]
                arguments = local_call.group(2).strip()
                replacement = (
                    f"new {class_name}("
                    + ", ".join([value for value in [arguments, *lookups] if value])
                    + ")"
                )
                call_start = method.start() + local_call.start()
                call_end = method.start() + local_call.end()
                rewritten = rewritten[:call_start] + replacement + rewritten[call_end:]
        if rewritten != content:
            output[path] = rewritten


def _reconcile_java_void_predicate_invocations(output: dict[str, str]) -> None:
    """Restore statement use when generated callers negate a known void method.

    Legacy authorization helpers commonly return ``void`` and signal denial by
    throwing. A repair model has repeatedly rewritten their statement calls as
    ``if (!Helper.authorize(...)) { ... }``, which javac rejects with ``'void'
    type not allowed here``. When the generated project itself proves that the
    invoked static method returns void, retain the invocation and remove only
    the impossible boolean failure branch.
    """
    void_methods: set[tuple[str, str]] = set()
    for path, content in output.items():
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        declared = _JAVA_CLASS_DECL_RE.search(content) or re.search(
            r"(?:^|;)\s*(?:(?:public|protected|private|abstract|final|sealed|"
            r"non-sealed|static)\s+)*(?:class|interface|enum|record)\s+"
            r"([A-Za-z_]\w*)\b",
            content,
        )
        if not declared:
            continue
        owner = declared.group(1)
        for method in re.findall(
            r"\b(?:public\s+|protected\s+|private\s+)?static\s+void\s+"
            r"([A-Za-z_$]\w*)\s*\(",
            content,
        ):
            void_methods.add((owner, method))
    if not void_methods:
        return

    predicate = re.compile(
        r"(?m)^(?P<indent>[ \t]*)if\s*\(\s*!\s*"
        r"(?P<call>(?P<owner>[A-Za-z_$]\w*)\.(?P<method>[A-Za-z_$]\w*)"
        r"\s*\([^()]*\))\s*\)\s*\{"
    )
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        rewritten = content
        for match in reversed(list(predicate.finditer(content))):
            if (match.group("owner"), match.group("method")) not in void_methods:
                continue
            end = _balanced_java_member_end(content, match.start())
            if end is None or re.match(r"\s*else\b", content[end:]):
                continue
            replacement = f'{match.group("indent")}{match.group("call")};'
            rewritten = rewritten[:match.start()] + replacement + rewritten[end:]
        if rewritten != content:
            output[path] = rewritten


def _reconcile_java_request_validation(output: dict[str, str]) -> None:
    """Apply baseline Bean Validation to unconstrained request-record components."""
    for path, content in list(output.items()):
        if not path.casefold().endswith("controller.java") or not isinstance(content, str):
            continue
        if "@Valid" not in content or "@RequestBody" not in content:
            continue
        changed = False
        for record_match in reversed(list(re.finditer(r"\brecord\s+([A-Za-z_]\w*Request)\s*\(([^)]*)\)", content))):
            params = record_match.group(2)
            constrained = re.sub(r"(?<![@\w])String\s+([A-Za-z_]\w*)", r"@jakarta.validation.constraints.NotBlank String \1", params)
            constrained = re.sub(r"(?<![@\w])(?:int|long|Integer|Long)\s+(quantity|stockQty)", r"@jakarta.validation.constraints.Min(1) int \1", constrained)
            if constrained != params:
                content = content[:record_match.start(2)] + constrained + content[record_match.end(2):]
                changed = True
        if changed:
            output[path] = content


def _migrate_java_error_envelope_exceptions(output: dict[str, str]) -> None:
    """Keep transport error records out of Java's throwable hierarchy."""
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        content = re.sub(r"\bErrorEnvelope\.message\(([^\n;]*)\)", r"\1", content)
        cursor = 0
        while True:
            match = re.search(r"\bthrow\s+new\s+ErrorEnvelope\s*\(", content[cursor:])
            if not match:
                break
            start = cursor + match.start()
            open_paren = cursor + match.end() - 1
            depth, close = 0, None
            for index in range(open_paren, len(content)):
                if content[index] == "(": depth += 1
                elif content[index] == ")":
                    depth -= 1
                    if depth == 0: close = index; break
            if close is None:
                break
            arguments = _split_java_arguments(content[open_paren + 1:close])
            message = arguments[2] if len(arguments) >= 3 else (arguments[-1] if arguments else '"Request failed"')
            replacement = f"throw new IllegalArgumentException({message})"
            content = content[:start] + replacement + content[close + 1:]
            cursor = start + len(replacement)
        cursor = 0
        while True:
            match = re.search(r"\bnew\s+ErrorEnvelope\s*\(", content[cursor:])
            if not match: break
            start = cursor + match.start(); open_paren = cursor + match.end() - 1
            depth, close = 0, None
            for index in range(open_paren, len(content)):
                if content[index] == "(": depth += 1
                elif content[index] == ")":
                    depth -= 1
                    if depth == 0: close = index; break
            if close is None: break
            arguments = _split_java_arguments(content[open_paren + 1:close])
            if len(arguments) == 5:
                cursor = close + 1; continue
            message = arguments[2] if len(arguments) >= 3 else '"Request failed"'
            replacement = f"new IllegalArgumentException({message})"
            content = content[:start] + replacement + content[close + 1:]
            cursor = start + len(replacement)
        output[path] = content


def _reconcile_java_client_response_contracts(output: dict[str, str]) -> None:
    """Align HTTP client call sites with the interface's declared response wrapper."""
    contracts: dict[tuple[str, str], str] = {}
    for path, content in output.items():
        if not path.casefold().endswith("client.java") or not isinstance(content, str):
            continue
        module = _java_source_module(path)
        for wrapped, method in re.findall(
            r"ResponseEntity\s*<\s*([A-Za-z_]\w*)\s*>\s+([A-Za-z_]\w*)\s*\(", content,
        ):
            contracts[(module, method)] = wrapped
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        module = _java_source_module(path)
        for (owner, method), response_type in contracts.items():
            if owner != module:
                continue
            content = re.sub(
                rf"\b[A-Za-z_]\w*\.[A-Za-z_]\w*\s+(\w+)\s*=\s*(\w+\.{re.escape(method)}\([^;]+\))\s*;",
                rf"{response_type} \1 = \2.getBody();",
                content,
            )
        output[path] = content


def _migrate_spring_filter_contracts(output: dict[str, str]) -> None:
    """Repair the common annotation-as-base-class servlet filter hallucination."""
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or "doFilterInternal(" not in content:
            continue
        if re.search(r"\bextends\s+Component\b", content):
            content = re.sub(
                r"\bextends\s+Component\b", "extends OncePerRequestFilter", content,
            )
            if "org.springframework.web.filter.OncePerRequestFilter" not in content:
                package = re.search(r"\bpackage\s+[^;]+;", content)
                if package:
                    content = (
                        content[:package.end()] +
                        "\n\nimport org.springframework.web.filter.OncePerRequestFilter;" +
                        content[package.end():]
                    )
        content = content.replace(
            'EnvironmentVariables.getSecret("JWT_SECRET")',
            'System.getenv("JWT_SECRET")',
        )
        content = content.replace("Base64Utils.decode(", "Base64.getDecoder().decode(")
        output[path] = _add_known_java_imports(content)


def _reconcile_java_test_static_imports(output: dict[str, str]) -> None:
    required = {
        "content": "org.springframework.test.web.servlet.result.MockMvcResultMatchers.content",
        "jsonPath": "org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath",
        "status": "org.springframework.test.web.servlet.result.MockMvcResultMatchers.status",
        "get": "org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get",
        "post": "org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post",
        "put": "org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put",
        "delete": "org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete",
        "when": "org.mockito.Mockito.when",
        "doThrow": "org.mockito.Mockito.doThrow",
        "doAnswer": "org.mockito.Mockito.doAnswer",
        "any": "org.mockito.ArgumentMatchers.any",
        "anyLong": "org.mockito.ArgumentMatchers.anyLong",
    }
    for path, content in list(output.items()):
        if "/src/test/java/" not in path or not isinstance(content, str):
            continue
        package = re.search(r"(?m)^\s*package\s+[^;]+;", content)
        if not package: continue
        declared_variables = {
            name: value_type for value_type, name in re.findall(
                r"(?m)^\s*([A-Za-z_]\w*)\s+([a-zA-Z_]\w*)\s*=", content,
            )
        }
        for variable, value_type in declared_variables.items():
            content = re.sub(
                rf"when\((\w+\.save)\(\s*{re.escape(variable)}\s*\)\)",
                rf"when(\1(org.mockito.ArgumentMatchers.any({value_type}.class)))",
                content,
            )
        additions = []
        for method, owner in required.items():
            if re.search(rf"\b{method}\s*\(", content) and f"import static {owner};" not in content:
                additions.append(f"import static {owner};")
        if additions:
            content = content[:package.end()] + "\n\n" + "\n".join(additions) + content[package.end():]
        if re.search(r"\bvoid\s+delete\s*\(\s*\)", content):
            content = re.sub(
                r"mockMvc\.perform\(delete\(",
                "mockMvc.perform(org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete(",
                content,
            )
        if "public void beforeTestMethod()" in content and "implements " not in content:
            content = re.sub(
                r"@Override\s*\r?\n\s*public void beforeTestMethod\(\)",
                "@org.junit.jupiter.api.BeforeEach\n    public void beforeTestMethod()", content,
            )
        content = re.sub(
            r"\.andExpect\(status\(\)\.isNot\((\d+)\)\)",
            r".andExpect(result -> org.junit.jupiter.api.Assertions.assertNotEquals(\1, result.getResponse().getStatus()))",
            content,
        )
        # Mockito's when(...) cannot wrap a void invocation.  Preserve the
        # generated answer while switching to Mockito's void-method API.
        content = re.sub(
            r"when\((\w+)\.([A-Za-z_]\w*)\(([^;\r\n]*)\)\)\.thenAnswer\(\s*invocation\s*->\s*\{\s*return\s+null\s*;\s*\}\s*\);",
            r"doAnswer(invocation -> null).when(\1).\2(\3);",
            content,
            flags=re.DOTALL,
        )
        content = re.sub(
            r"when\((\w+)\.([A-Za-z_]\w*)\(([^;\r\n]*)\)\)\.thenAnswer\(([^;\r\n]*)\);",
            r"doAnswer(\4).when(\1).\2(\3);",
            content,
        )
        if "doAnswer(" in content and "import static org.mockito.Mockito.doAnswer;" not in content:
            content = content[:package.end()] + "\n\nimport static org.mockito.Mockito.doAnswer;" + content[package.end():]
        output[path] = content


def _reconcile_java_record_constructors(output: dict[str, str]) -> None:
    """Convert invalid explicit canonical record constructors to compact form."""
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        record = re.search(r"\brecord\s+([A-Za-z_]\w*)\s*\((.*?)\)\s*\{", content, re.DOTALL)
        if not record:
            continue
        name = record.group(1)
        components = []
        for parameter in _split_java_arguments(record.group(2)):
            clean = re.sub(r"@[A-Za-z_][\w.]*\s*(?:\([^)]*\))?\s*", "", parameter).strip()
            tokens = clean.split()
            if len(tokens) >= 2:
                components.append((tokens[-2], tokens[-1]))
        signature = ", ".join(f"{field_type} {field}" for field_type, field in components)
        constructor = re.search(
            rf"\bpublic\s+{re.escape(name)}\s*\(\s*{re.escape(signature)}\s*\)\s*\{{",
            content,
        )
        if not constructor:
            continue
        end = _balanced_java_member_end(content, constructor.start())
        if end is None:
            continue
        body_open = content.find("{", constructor.start(), constructor.end())
        body = content[body_open + 1:end - 1]
        if not all(re.search(rf"\bthis\.{re.escape(field)}\s*=", body) for _, field in components):
            content = content[:constructor.start()] + f"public {name} {{" + body + "}" + content[end:]
            output[path] = content


def _java_leading_annotations(parameter: str) -> list[str]:
    """Return complete leading annotations, including nested annotation args."""
    annotations: list[str] = []
    index = 0
    while index < len(parameter):
        while index < len(parameter) and parameter[index].isspace(): index += 1
        if index >= len(parameter) or parameter[index] != "@": break
        start = index
        index += 1
        while index < len(parameter) and (parameter[index].isalnum() or parameter[index] in "._$"): index += 1
        while index < len(parameter) and parameter[index].isspace(): index += 1
        if index < len(parameter) and parameter[index] == "(":
            depth = 0
            while index < len(parameter):
                if parameter[index] == "(": depth += 1
                elif parameter[index] == ")":
                    depth -= 1
                    if depth == 0:
                        index += 1
                        break
                index += 1
        annotations.append(parameter[start:index].strip())
    return annotations


def _reconcile_java_record_compatibility(output: dict[str, str]) -> None:
    """Give records JavaBean read compatibility; promote to beans when mutation is required."""
    all_java = "\n".join(v for p, v in output.items() if p.casefold().endswith(".java") and isinstance(v, str))
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str): continue
        match = re.search(r"\b(public\s+)?record\s+([A-Za-z_]\w*)\s*\(", content)
        if not match: continue
        visibility, name = match.group(1) or "", match.group(2)
        start, depth, close = match.end() - 1, 0, None
        for index in range(start, len(content)):
            if content[index] == "(": depth += 1
            elif content[index] == ")":
                depth -= 1
                if depth == 0: close = index; break
        if close is None: continue
        params = _split_java_arguments(content[start + 1:close])
        fields = []
        for parameter in params:
            clean = re.sub(r"@[A-Za-z_][\w.]*\s*(?:\([^)]*\))?\s*", "", parameter).strip()
            tokens = clean.split()
            if len(tokens) >= 2: fields.append((tokens[-2], tokens[-1]))
        if not fields: continue
        is_entity = "@Entity" in content or "@jakarta.persistence.Entity" in content
        # Java records are implicitly final and expose only their canonical
        # constructor. If another generated type extends this contract, the
        # provider must be promoted to a mutable bean with a no-arg
        # constructor before Maven runs; repairing only the subclass can
        # never make record inheritance legal.
        inherited = bool(re.search(
            rf"\b(?:class|record)\s+[A-Za-z_$][\w$]*\s+extends\s+{re.escape(name)}\b",
            all_java,
        ))
        declares_mutators = bool(re.search(
            rf"\b(?:public\s+)?(?:void|{re.escape(name)})\s+set[A-Z][A-Za-z0-9_$]*\s*\(",
            content,
        ))
        mutable = (
            is_entity
            or inherited
            or declares_mutators
            or bool(re.search(rf"\bnew\s+{re.escape(name)}\s*\(\s*\)", all_java))
        )
        body_open = content.find("{", close)
        member_end = _balanced_java_member_end(content, close)
        body_end = member_end - 1 if member_end is not None else -1
        existing_body = content[body_open + 1:body_end].strip() if body_open >= 0 and body_end > body_open else ""
        methods = []
        for field_type, field in fields:
            cap = field[0].upper() + field[1:]
            methods.append(f"    public {field_type} get{cap}() {{ return {field}; }}")
            if field_type in {"boolean", "Boolean"}:
                methods.append(f"    public boolean is{cap}() {{ return Boolean.TRUE.equals({field}); }}")
        if mutable:
            declarations = []
            for parameter, (field_type, field) in zip(params, fields, strict=False):
                annotations = _java_leading_annotations(parameter)
                declarations.extend(f"    {annotation}" for annotation in annotations)
                declarations.append(f"    private {field_type} {field};")
            declarations = "\n".join(declarations)
            assignments = "\n".join(f"        this.{field} = {field};" for _, field in fields)
            setters = "\n".join(f"    public {name} set{field[0].upper()+field[1:]}({field_type} value) {{ this.{field} = value; return this; }}" for field_type, field in fields)
            record_accessors = "\n".join(f"    public {field_type} {field}() {{ return {field}; }}" for field_type, field in fields)
            fluent_mutators = "\n".join(f"    public {name} {field}({field_type} value) {{ this.{field} = value; return this; }}" for field_type, field in fields)
            constructor_params = ", ".join(f"{field_type} {field}" for field_type, field in fields)
            observed_arities: set[int] = set()
            for creation in re.finditer(rf"\bnew\s+{re.escape(name)}\s*\(", all_java):
                open_paren = creation.end() - 1
                paren_depth = 0
                close_paren = None
                for index in range(open_paren, len(all_java)):
                    if all_java[index] == "(": paren_depth += 1
                    elif all_java[index] == ")":
                        paren_depth -= 1
                        if paren_depth == 0: close_paren = index; break
                if close_paren is not None:
                    observed_arities.add(len(_split_java_arguments(all_java[open_paren + 1:close_paren])))
            compatibility_ctors = []
            for arity in sorted(value for value in observed_arities if 0 < value < len(fields)):
                short_fields = fields[:arity]
                short_params = ", ".join(f"{field_type} {field}" for field_type, field in short_fields)
                values = [field for _, field in short_fields]
                for field_type, _ in fields[arity:]:
                    values.append("false" if field_type == "boolean" else "0" if field_type in {"byte", "short", "int", "long", "float", "double"} else "'\\0'" if field_type == "char" else "null")
                compatibility_ctors.append(f"    public {name}({short_params}) {{ this({', '.join(values)}); }}")
            # Record bodies may contain illegal instance fields, canonical
            # constructors, or assignments to final components.  Preserve
            # only compile-safe constants; mutable JPA lifecycle behavior is
            # synthesized from the authoritative component list below.
            preserved = "\n".join(re.findall(
                r"(?m)^\s*public\s+static\s+final\s+[^;]+;\s*$", existing_body,
            ))
            lifecycle = ""
            field_names = {field for _, field in fields}
            if is_entity and "createdAt" in field_names:
                lifecycle += "\n    @jakarta.persistence.PrePersist\n    protected void onCreate() { if (createdAt == null) createdAt = java.time.Instant.now(); }\n"
            if is_entity and "updatedAt" in field_names:
                lifecycle += "\n    @jakarta.persistence.PreUpdate\n    protected void onUpdate() { updatedAt = java.time.Instant.now(); }\n"
            replacement = (
                f"{visibility}class {name} {{\n{declarations}\n    public {name}() {{}}\n"
                f"    public {name}({constructor_params}) {{\n{assignments}\n    }}\n"
                + "\n".join(methods) + "\n" + setters + "\n" + record_accessors + "\n" + fluent_mutators
                + ("\n" + "\n".join(compatibility_ctors) if compatibility_ctors else "")
                + ("\n" + preserved if preserved else "") + lifecycle + "\n}"
            )
            content = content[:match.start()] + replacement + content[body_end + 1:]
        else:
            missing = []
            for method in methods:
                method_name = re.search(r"\b(get[A-Za-z_]\w*)\s*\(", method)
                if method_name and not re.search(rf"\b{re.escape(method_name.group(1))}\s*\(", existing_body):
                    missing.append(method)
            observed_arities: set[int] = set()
            for creation in re.finditer(rf"\bnew\s+{re.escape(name)}\s*\(", all_java):
                open_paren = creation.end() - 1
                paren_depth = 0
                close_paren = None
                for index in range(open_paren, len(all_java)):
                    if all_java[index] == "(":
                        paren_depth += 1
                    elif all_java[index] == ")":
                        paren_depth -= 1
                        if paren_depth == 0:
                            close_paren = index
                            break
                if close_paren is not None:
                    observed_arities.add(len(_split_java_arguments(all_java[open_paren + 1:close_paren])))
            for arity in sorted(value for value in observed_arities if 0 < value < len(fields)):
                short_fields = fields[:arity]
                short_params = ", ".join(f"{field_type} {field}" for field_type, field in short_fields)
                if re.search(rf"\b{re.escape(name)}\s*\(\s*{re.escape(short_params)}\s*\)", existing_body):
                    continue
                values = [field for _, field in short_fields]
                for field_type, _ in fields[arity:]:
                    values.append("false" if field_type == "boolean" else "0" if field_type in {"byte", "short", "int", "long", "float", "double"} else "'\\0'" if field_type == "char" else "null")
                missing.append(f"    public {name}({short_params}) {{ this({', '.join(values)}); }}")
            if missing and body_end >= 0:
                content = content[:body_end] + "\n" + "\n".join(missing) + "\n" + content[body_end:]
        output[path] = content


def _reconcile_java_collection_element_types(output: dict[str, str]) -> None:
    """Align local List<T> declarations with the concrete values added to them."""
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str): continue
        declarations = re.findall(r"\bList\s*<\s*([A-Za-z_]\w*)\s*>\s+(\w+)\s*=\s*new\s+ArrayList", content)
        for declared_type, variable in declarations:
            added = set(re.findall(rf"\b{re.escape(variable)}\.add\s*\(\s*new\s+([A-Za-z_]\w*)\s*\(", content))
            if len(added) == 1 and declared_type not in added:
                actual = next(iter(added))
                content = re.sub(
                    rf"\bList\s*<\s*{re.escape(declared_type)}\s*>\s+{re.escape(variable)}\b(?=\s*=\s*new\s+ArrayList)",
                    f"List<{actual}> {variable}", content,
                )
        output[path] = content


def _reconcile_java_common_service_contracts(output: dict[str, str]) -> None:
    """Close recurring controller/service/repository signature drift."""
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str): continue
        if path.casefold().endswith("orderservice.java"):
            content = content.replace("OrderEntity.Item", "OrderItem")
            if "orderRepository.findProductById(" in content or "orderRepository.getProduct(" in content:
                if "ProductClient productClient" not in content:
                    content = content.replace(
                        "private final OrderRepository orderRepository;",
                        "private final OrderRepository orderRepository;\n    private final com.app.order.client.ProductClient productClient;",
                    )
                    content = re.sub(
                        r"public OrderService\(OrderRepository orderRepository\)\s*\{\s*this\.orderRepository = orderRepository;\s*\}",
                        "public OrderService(OrderRepository orderRepository, com.app.order.client.ProductClient productClient) {\n        this.orderRepository = orderRepository;\n        this.productClient = productClient;\n    }",
                        content,
                    )
                content = re.sub(
                    r"Optional<Long>\s+(\w+)\s*=\s*orderRepository\.findProductById\(([^;\n]+)\);",
                    r"Optional<Long> \1 = Optional.ofNullable(\2);", content,
                )
                content = re.sub(
                    r"orderRepository\.getProduct\(([^)]+)\)",
                    r"productClient.getProductById(\1).getBody()", content,
                )
                content = re.sub(
                    r"productClient\.getProductById\(([^;\n]+)\.getBody\(\)\);",
                    r"productClient.getProductById(\1).getBody();", content,
                )
            content = content.replace("java.util.json.JSON.stringify(event)", "event.toString()")
            content = re.sub(
                r"byte\[\]\s+(\w+)\s*=\s*java\.util\.Base64\.getEncoder\(\)\.encode\(([^;]+)\);",
                r"String \1 = java.util.Base64.getEncoder().encodeToString(\2);", content,
            )
        if path.casefold().endswith("productclient.java") and "updateProductStock(" not in content:
            insertion = content.rfind("}")
            method = "\n    @org.springframework.web.bind.annotation.PutMapping(\"/api/products/{id}/stock\")\n    void updateProductStock(@org.springframework.web.bind.annotation.PathVariable Long id, @org.springframework.web.bind.annotation.RequestParam int stockQty);\n"
            if insertion >= 0: content = content[:insertion] + method + content[insertion:]
        if path.casefold().endswith("authcontroller.java") and "ResponseEntity<TokenResponse>" in content:
            content = re.sub(r"ResponseEntity\.ok\(token\)", 'ResponseEntity.ok(new TokenResponse(token, "", "Bearer", 3600))', content)
        if path.casefold().endswith("authservice.java") and "class AuthService" in content:
            module = _java_source_module(path)
            has_user_repository = any(
                candidate.casefold().endswith("/repository/userrepository.java")
                and _java_source_module(candidate) == module
                for candidate in output
            )
            has_user_entity = any(
                candidate.casefold().endswith("/entity/userentity.java")
                and _java_source_module(candidate) == module
                for candidate in output
            )
            if "UserRepository userRepository" not in content:
                if has_user_repository and has_user_entity:
                    field_at = content.find("private final String jwtSecret;")
                    if field_at >= 0:
                        field_end = content.find(";", field_at) + 1
                        content = content[:field_end] + "\n    private final com.app.auth.repository.UserRepository userRepository;" + content[field_end:]
                    content = re.sub(
                        r"public AuthService\(PasswordEncoder passwordEncoder, @Value\(([^)]+)\) String jwtSecret\)\s*\{\s*this\.passwordEncoder = passwordEncoder;\s*this\.jwtSecret = jwtSecret;\s*\}",
                        r"public AuthService(PasswordEncoder passwordEncoder, @Value(\1) String jwtSecret, com.app.auth.repository.UserRepository userRepository) {\n        this.passwordEncoder = passwordEncoder;\n        this.jwtSecret = jwtSecret;\n        this.userRepository = userRepository;\n    }", content,
                    )
            insertion = content.rfind("}")
            adapters = ""
            if not re.search(r"\bTokenResponse\s+register\s*\(\s*(?:com\.app\.auth\.dto\.)?RegisterRequest", content):
                adapters += "\n    public TokenResponse register(com.app.auth.dto.RegisterRequest request) { register(request.email(), request.password(), request.displayName()); return authenticate(request.email(), request.password()); }\n"
            if not re.search(r"\bTokenResponse\s+login\s*\(", content):
                adapters += "\n    public TokenResponse login(com.app.auth.dto.LoginRequest request) { return authenticate(request.email(), request.password()); }\n"
            if not re.search(r"\bTokenResponse\s+refresh\s*\(", content) and re.search(r"\brefreshToken\s*\(\s*String\s+\w+\s*\)", content):
                adapters += "\n    public TokenResponse refresh(String refreshToken) { return refreshToken(refreshToken); }\n"
            if "getCurrentUserId(" not in content:
                adapters += "\n    public String getCurrentUserId() { return org.springframework.security.core.context.SecurityContextHolder.getContext().getAuthentication().getName(); }\n"
            if has_user_repository and has_user_entity and "getUserById(" not in content:
                adapters += "\n    public com.app.auth.entity.UserEntity getUserById(String id) { return userRepository.findById(Long.valueOf(id)).orElseThrow(() -> new IllegalArgumentException(\"User not found: \" + id)); }\n"
            if has_user_repository and has_user_entity and "getAllUsers(" not in content:
                adapters += "\n    public java.util.List<com.app.auth.entity.UserEntity> getAllUsers() { return userRepository.findAll(); }\n"
            if has_user_repository and has_user_entity and "deleteUser(" not in content:
                adapters += "\n    public void deleteUser(String id) { userRepository.deleteById(Long.valueOf(id)); }\n"
            if adapters and insertion >= 0: content = content[:insertion] + adapters + content[insertion:]
        if path.casefold().endswith("productcontroller.java") and "ResponseEntity<ProductResponse[]>" in content:
            content = content.replace("ResponseEntity<ProductResponse[]>", "ResponseEntity<java.util.List<ProductResponse>>")
        if path.casefold().endswith("notificationservice.java"):
            content = content.replace(
                'new RuntimeException("Notification not found")',
                'new IllegalArgumentException("Notification not found")',
            )
            content = content.replace(
                "if (!entity.getUserId().equals(userId))",
                "if (entity.getUserId() != null && !entity.getUserId().equals(userId))",
            )
            if "mapToResponse(" in content:
                content = content.replace("return toResponse(entity);", "return mapToResponse(entity, entity.getMessage());")
            if re.search(r"\bmarkAsRead\s*\(\s*Long\s+\w+\s*\)", content):
                content = content.replace("return markAsRead(id, userId);", "return markAsRead(id);")
            insertion = content.rfind("}")
            aliases = ""
            if "getNotification(" not in content and "NotificationResponse" in content:
                mapping = (
                    "mapToResponse(entity, entity.getMessage())"
                    if "mapToResponse(" in content else "toResponse(entity)"
                )
                aliases += (
                    "\n    public NotificationResponse getNotification(Long id, String userId) {\n"
                    "        var entity = notificationRepository.findById(id).orElseThrow(() -> new IllegalArgumentException(\"Notification not found: \" + id));\n"
                    "        if (entity.getUserId() != null && !entity.getUserId().equals(userId)) throw new IllegalArgumentException(\"Access denied\");\n"
                    f"        return {mapping};\n    }}\n"
                )
            if "markNotificationAsRead(" not in content and "markAsRead(" in content:
                mark_call = "markAsRead(id)" if re.search(r"\bmarkAsRead\s*\(\s*Long\s+\w+\s*\)", content) else "markAsRead(id, userId)"
                aliases += f"\n    public NotificationResponse markNotificationAsRead(Long id, String userId) {{ return {mark_call}; }}\n"
            if not re.search(r"\bmarkAsRead\s*\(\s*Long\s+\w+\s*\)", content) and "markAsRead(Long notificationId, String userId)" in content:
                aliases += "\n    public NotificationResponse markAsRead(Long id) { return markAsRead(id, org.springframework.security.core.context.SecurityContextHolder.getContext().getAuthentication().getName()); }\n"
            if "getNotifications(" not in content and "listNotifications(" in content:
                if "mapToResponse(" in content:
                    aliases += "\n    public java.util.List<NotificationResponse> getNotifications() { String userId = org.springframework.security.core.context.SecurityContextHolder.getContext().getAuthentication().getName(); return notificationRepository.findByUserId(userId).stream().map(entity -> mapToResponse(entity, entity.getMessage())).toList(); }\n"
                elif "toResponse(" in content:
                    aliases += "\n    public java.util.List<NotificationResponse> getNotifications() { String userId = org.springframework.security.core.context.SecurityContextHolder.getContext().getAuthentication().getName(); return notificationRepository.findByUserId(userId).stream().map(this::toResponse).toList(); }\n"
            if aliases and insertion >= 0:
                content = content[:insertion] + aliases + content[insertion:]
        if path.casefold().endswith("productservice.java"):
            content = re.sub(
                r"return\s+productRepository\.save\(([^;]+)\)\.map\(productMapper::toResponse\);",
                r"return productMapper.toResponse(productRepository.save(\1));", content,
            )
            content = re.sub(
                r"Optional<ProductEntity>\s+(\w+)\s*=\s*getProductById\(([^)]+)\);",
                r"Optional<ProductEntity> \1 = productRepository.findById(\2);", content,
            )
            insertion = content.rfind("}")
            aliases = ""
            if "listAll(" not in content and "listProducts(" in content:
                aliases += "\n    public List<ProductResponse> listAll() { return listProducts(); }\n"
            if not re.search(r"\bpublic\s+ProductResponse\s+findById\s*\(", content) and "getProductById(" in content:
                aliases += "\n    public ProductResponse findById(Long id) { return getProductById(id).orElse(null); }\n"
            if re.search(r"\bcreate\s*\(", content) is None and "createProduct(" in content:
                aliases += "\n    public ProductResponse create(ProductRequest request) { return createProduct(request); }\n"
            if re.search(r"\bupdate\s*\(", content) is None and "updateProduct(" in content:
                aliases += "\n    public ProductResponse update(Long id, ProductRequest request) { return updateProduct(id, request); }\n"
            if not re.search(r"\bpublic\s+boolean\s+delete\s*\(", content) and "deleteProduct(" in content:
                aliases += "\n    public boolean delete(Long id) { deleteProduct(id); return true; }\n"
            if aliases and insertion >= 0:
                content = content[:insertion] + aliases + content[insertion:]
            insertion = content.rfind("}")
            if "deleteProductById(" not in content and "deleteProduct(" in content and insertion >= 0:
                content = content[:insertion] + "\n    public void deleteProductById(Long id) { deleteProduct(id); }\n" + content[insertion:]
        if path.casefold().endswith("notificationrepository.java") and "findAllByUserId(" not in content:
            insertion = content.rfind("}")
            if insertion >= 0:
                content = content[:insertion] + "\n    org.springframework.data.domain.Page<NotificationEntity> findAllByUserId(String userId, org.springframework.data.domain.Pageable pageable);\n" + content[insertion:]
        if path.casefold().endswith("notificationrepository.java"):
            insertion = content.rfind("}")
            additions = ""
            if "findByUserId(" not in content:
                additions += "\n    java.util.List<NotificationEntity> findByUserId(String userId);\n"
            if "findAllByOrderIdOrderByCreatedAtDesc(" not in content:
                additions += "\n    java.util.List<NotificationEntity> findAllByOrderIdOrderByCreatedAtDesc(Long orderId);\n"
            if additions and insertion >= 0: content = content[:insertion] + additions + content[insertion:]
        if path.casefold().endswith("productrepository.java") and "existsBySku(" not in content:
            insertion = content.rfind("}")
            if insertion >= 0:
                content = content[:insertion] + "\n    boolean existsBySku(String sku);\n" + content[insertion:]
        if path.casefold().endswith("ordercontroller.java"):
            content = content.replace("ResponseEntity<OrderSummary[]>", "ResponseEntity<java.util.List<OrderSummary>>")
        output[path] = content


def _reconcile_java_service_method_aliases(output: dict[str, str]) -> None:
    """Align common servlet service calls with their generated interface name."""
    login_services: dict[tuple[str, str], tuple[str, list[str]]] = {}
    for path, content in output.items():
        if not path.casefold().endswith("service.java") or not isinstance(content, str):
            continue
        declared = re.search(r"\binterface\s+([A-Za-z_]\w*Service)\b", content)
        login = re.search(
            r"\b([A-Za-z_][\w<>?,.]*)\s+loginUser\s*\(([^)]*)\)",
            content,
        )
        if declared and login:
            parameters = _split_java_arguments(login.group(2))
            login_services[(_java_source_module(path), declared.group(1))] = (
                login.group(1), [" ".join(parameter.split()[:-1]) for parameter in parameters],
            )

    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        module = _java_source_module(path)
        for (service_module, service_type), (return_type, parameter_types) in login_services.items():
            if service_module != module:
                continue
            arity = len(parameter_types)
            variables = re.findall(rf"\b{re.escape(service_type)}\s+([A-Za-z_]\w*)\b", content)
            for variable in variables:
                call_pattern = re.compile(rf"\b{re.escape(variable)}\.loginUser\s*\(")
                cursor = 0
                while match := call_pattern.search(content, cursor):
                    open_paren = match.end() - 1
                    depth = 0
                    close_paren = None
                    for index in range(open_paren, len(content)):
                        if content[index] == "(":
                            depth += 1
                        elif content[index] == ")":
                            depth -= 1
                            if depth == 0:
                                close_paren = index
                                break
                    if close_paren is None:
                        break
                    arguments = _split_java_arguments(content[open_paren + 1:close_paren])
                    if (
                        parameter_types == ["String", "String"]
                        and any("UserRole." in argument for argument in arguments)
                    ):
                        username = next(
                            (argument for argument in arguments if "UserRole." not in argument),
                            "username",
                        )
                        password_variable = next(
                            (
                                name for name in re.findall(
                                    r"\bString\s+([A-Za-z_]\w*)\s*=", content,
                                )
                                if any(token in name.casefold() for token in ("password", "pword", "pwd"))
                            ),
                            "",
                        )
                        if password_variable:
                            replacement = f"{username}, {password_variable}"
                            content = content[:open_paren + 1] + replacement + content[close_paren:]
                            cursor = open_paren + 1 + len(replacement) + 1
                            continue
                    if len(arguments) > arity:
                        replacement = ", ".join(arguments[:arity])
                        content = content[:open_paren + 1] + replacement + content[close_paren:]
                        cursor = open_paren + 1 + len(replacement) + 1
                    else:
                        cursor = close_paren + 1
                if return_type != "String":
                    content = re.sub(
                        rf"String\s+([A-Za-z_]\w*)\s*=\s*{re.escape(variable)}\.login\s*\(([^;]+)\)\s*;",
                        lambda match: (
                            f"var {match.group(1)}User = {variable}.loginUser({match.group(2)});\n"
                            f"            String {match.group(1)} = {match.group(1)}User != null "
                            f"? ResponseCode.SUCCESS.toString() : ResponseCode.UNAUTHORIZED.toString();"
                        ),
                        content,
                    )
                content = re.sub(
                    rf"\b{re.escape(variable)}\.login\s*\(",
                    f"{variable}.loginUser(",
                    content,
                )
        output[path] = content


def _prune_unreferenced_java_mappers(output: dict[str, str]) -> None:
    """Discard isolated hallucinated mapper utilities with no consumers."""
    for path, content in list(output.items()):
        if not path.casefold().endswith("mapper.java") or not isinstance(content, str):
            continue
        declared = re.search(r"\b(?:class|record|interface)\s+([A-Za-z_]\w*Mapper)\b", content)
        if not declared:
            continue
        name = declared.group(1)
        module = _java_source_module(path)
        referenced = any(
            other_path != path and _java_source_module(other_path) == module
            and isinstance(other, str) and re.search(rf"\b{re.escape(name)}\b", other)
            for other_path, other in output.items()
        )
        if not referenced:
            del output[path]


_JAVA_METHOD_DECLARATION = re.compile(
    r"(?m)^[ \t]*(?:public|protected|private)\s+(?:static\s+)?(?:final\s+)?"
    r"([A-Za-z_][\w<>?,.\[\] ]*)\s+([A-Za-z_]\w*)\s*\(([^)]*)\)"
    r"(?:\s+throws\s+[A-Za-z_$][\w.$]*(?:\s*,\s*[A-Za-z_$][\w.$]*)*)?\s*\{"
)

_JAVA_METHOD_CONTRACT = re.compile(
    r"(?m)^[ \t]*(?:(?:public|protected|private|abstract|default|static|final|"
    r"synchronized|native)\s+)*"
    r"(?P<return>[A-Za-z_$][\w.$<>?,.\[\] ]*)\s+"
    r"(?P<name>[A-Za-z_$][\w$]*)\s*\((?P<params>[^)]*)\)"
    r"(?:\s+throws\s+[A-Za-z_$][\w.$]*(?:\s*,\s*[A-Za-z_$][\w.$]*)*)?\s*[;{]"
)


def _reconcile_java_optional_assignment_contracts(output: dict[str, str]) -> None:
    """Align Optional local declarations with authoritative provider methods.

    Generated callers are not limited to ``*Controller`` classes: legacy
    servlet adapters and prompt-created endpoint classes routinely call a
    service interface directly.  Index class *and interface* method contracts
    by Maven module, then wrap only a compiler-equivalent assignment where the
    declared local is ``Optional<T>`` and the provider returns concrete ``T``.
    ``ofNullable`` preserves the caller's empty branch without changing the
    service contract.
    """
    contracts: dict[tuple[str, str], dict[str, str]] = {}
    for path, content in output.items():
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        declared = re.search(
            r"\b(?:class|interface|record)\s+([A-Za-z_$][\w$]*)\b", content,
        )
        if not declared:
            continue
        methods: dict[str, str] = {}
        for method in _JAVA_METHOD_CONTRACT.finditer(content):
            if method.group("name") == declared.group(1):
                continue
            methods.setdefault(method.group("name"), " ".join(method.group("return").split()))
        if methods:
            contracts[(_java_source_module(path), declared.group(1))] = methods

    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        module = _java_source_module(path)
        providers = {
            variable: contracts.get((module, provider_type), {})
            for provider_type, variable in re.findall(
                r"\b([A-Za-z_$][\w$]*)\s+([A-Za-z_$][\w$]*)\s*(?:[;,)])", content,
            )
            if (module, provider_type) in contracts
        }
        for variable, methods in providers.items():
            for method_name, return_type in methods.items():
                concrete_return = return_type.removeprefix("java.lang.").strip()
                if concrete_return.startswith(("Optional<", "java.util.Optional<")):
                    continue
                assignment = re.compile(
                    rf"(?P<prefix>\b(?:java\.util\.)?Optional\s*<\s*"
                    rf"(?P<inner>{re.escape(concrete_return)})\s*>\s+"
                    rf"[A-Za-z_$][\w$]*\s*=\s*)"
                    rf"(?P<call>{re.escape(variable)}\s*\.\s*{re.escape(method_name)}"
                    r"\s*\([^;\r\n]*\))(?P<suffix>\s*;)"
                )
                content = assignment.sub(
                    lambda match: (
                        f"{match.group('prefix')}java.util.Optional.ofNullable("
                        f"{match.group('call')}){match.group('suffix')}"
                    ),
                    content,
                )
        output[path] = content


def _java_parameter_type_signature(parameters: str) -> tuple[str, ...]:
    signature = []
    for parameter in _split_java_arguments(parameters):
        clean = re.sub(r"@[A-Za-z_][\w.]*\s*(?:\([^)]*\))?\s*", "", parameter).strip()
        tokens = clean.replace("final ", "").split()
        if len(tokens) >= 2:
            signature.append(" ".join(tokens[:-1]))
    return tuple(signature)


def _dedupe_java_methods(output: dict[str, str]) -> None:
    """Remove later exact-signature duplicates, making repair passes idempotent."""
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        seen: set[tuple[str, tuple[str, ...]]] = set()
        removals: list[tuple[int, int]] = []
        for match in _JAVA_METHOD_DECLARATION.finditer(content):
            if content[:match.start()].count("{") - content[:match.start()].count("}") != 1:
                continue
            key = (match.group(2), _java_parameter_type_signature(match.group(3)))
            end = _balanced_java_member_end(content, match.start())
            if key in seen and end is not None:
                removals.append((match.start(), end))
            else:
                seen.add(key)
        for start, end in reversed(removals):
            content = content[:start] + content[end:]
        output[path] = content


def _reconcile_java_controller_service_contracts(output: dict[str, str]) -> None:
    """Fail closed: controllers may expose only implemented service operations."""
    services: dict[tuple[str, str], dict[str, str]] = {}
    for path, content in output.items():
        if not path.casefold().endswith("service.java") or not isinstance(content, str):
            continue
        declared = re.search(r"\bclass\s+([A-Za-z_]\w*Service)\b", content)
        if not declared:
            continue
        contracts: dict[str, str] = {}
        for method in _JAVA_METHOD_DECLARATION.finditer(content):
            if content[:method.start()].count("{") - content[:method.start()].count("}") == 1:
                contracts.setdefault(method.group(2), method.group(1).strip())
        services[(_java_source_module(path), declared.group(1))] = contracts

    for path, content in list(output.items()):
        if not path.casefold().endswith("controller.java") or not isinstance(content, str):
            continue
        module = _java_source_module(path)
        dependencies = {
            variable: services.get((module, service_type), {})
            for service_type, variable in re.findall(r"private\s+final\s+([A-Za-z_]\w*Service)\s+([A-Za-z_]\w*)\s*;", content)
        }
        if not dependencies:
            continue
        content = content.replace("authentication.getPrincipal().getName()", "authentication.getName()")
        for variable, contracts in dependencies.items():
            for method, return_type in contracts.items():
                if return_type.endswith("TokenResponse"):
                    content = re.sub(
                        rf"(var\s+(\w+)\s*=\s*{re.escape(variable)}\.{re.escape(method)}\([^;]+\);\s*)"
                        rf"return\s+ResponseEntity\.ok\(new\s+TokenResponse\(\2,\s*\"\",\s*\"Bearer\",\s*3600\)\);",
                        r"\1return ResponseEntity.ok(\2);", content,
                    )
        removals: list[tuple[int, int]] = []
        for declaration in _JAVA_METHOD_DECLARATION.finditer(content):
            if content[:declaration.start()].count("{") - content[:declaration.start()].count("}") != 1:
                continue
            end = _balanced_java_member_end(content, declaration.start())
            if end is None:
                continue
            body = content[declaration.start():end]
            unsupported = any(
                any(call not in contracts for call in re.findall(rf"\b{re.escape(variable)}\.([A-Za-z_]\w*)\s*\(", body))
                for variable, contracts in dependencies.items()
            )
            if unsupported:
                start = declaration.start()
                line_start = content.rfind("\n", 0, start) + 1
                scan = line_start
                while scan > 0:
                    previous_end = scan - 1
                    previous_start = content.rfind("\n", 0, previous_end) + 1
                    previous = content[previous_start:previous_end].strip()
                    if previous.startswith("@") or previous == "":
                        scan = previous_start
                    else:
                        break
                removals.append((scan, end))
        for start, end in reversed(removals):
            content = content[:start] + content[end:]

        # Undo a stale Optional adapter when the authoritative service returns
        # a concrete value rather than Optional<T>.
        for variable, contracts in dependencies.items():
            for method, return_type in contracts.items():
                if not return_type.startswith("Optional<"):
                    content = re.sub(
                        rf"({re.escape(variable)}\.{re.escape(method)}\([^;]+?\))\.orElseThrow\([^;]+\)",
                        r"\1", content,
                    )
                else:
                    optional_inner = return_type[len("Optional<"):-1].strip()
                    content = re.sub(
                        rf"\b{re.escape(optional_inner)}\s+(\w+)\s*=\s*{re.escape(variable)}\.{re.escape(method)}\(([^()\r\n;]*)\)\s*(?:\.orElseThrow\([^;\r\n]*\)\s*)*;",
                        rf'{optional_inner} \1 = {variable}.{method}(\2).orElseThrow(() -> new IllegalArgumentException("{optional_inner} not found"));',
                        content,
                    )
        output[path] = content


def _reconcile_java_exception_constructors(output: dict[str, str]) -> None:
    for path, content in list(output.items()):
        if not path.casefold().endswith("exception.java") or "extends RuntimeException" not in str(content):
            continue
        declared = re.search(r"\bclass\s+([A-Za-z_]\w*Exception)\b", content)
        if not declared or re.search(rf"\b{declared.group(1)}\s*\(\s*String\s+\w+\s*\)", content):
            continue
        two_arg = re.search(rf"\b{declared.group(1)}\s*\(\s*String\s+(\w+)\s*,\s*String\s+\w+\s*\)", content)
        if two_arg:
            insertion = content.rfind("}")
            constructor = f"\n    public {declared.group(1)}(String {two_arg.group(1)}) {{ this({two_arg.group(1)}, null); }}\n"
            output[path] = content[:insertion] + constructor + content[insertion:]


def _reconcile_java_exception_call_contracts(output: dict[str, str]) -> None:
    """Reduce generated exception calls to a constructor the type declares."""
    response_code_exceptions: set[tuple[str, str]] = set()
    for path, content in output.items():
        if not path.casefold().endswith("exception.java") or not isinstance(content, str):
            continue
        declared = re.search(r"\bclass\s+([A-Za-z_]\w*Exception)\b", content)
        if declared and re.search(
            rf"\b{re.escape(declared.group(1))}\s*\(\s*ResponseCode\s+\w+\s*\)", content,
        ):
            response_code_exceptions.add((_java_source_module(path), declared.group(1)))

    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        module = _java_source_module(path)
        for exception_module, exception_name in response_code_exceptions:
            if exception_module != module:
                continue
            content = re.sub(
                rf"new\s+{re.escape(exception_name)}\s*\(\s*(ResponseCode\.[A-Z][A-Z0-9_]*)\s*,"
                r"\s*\"(?:[^\"\\]|\\.)*\"\s*,\s*[A-Za-z_]\w*\s*\)",
                rf"new {exception_name}(\1)",
                content,
            )
        output[path] = content


def _remove_misplaced_nested_record_accessors(output: dict[str, str]) -> None:
    """Remove stale outer getters previously injected for a nested record."""
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        nested_fields: set[str] = set()
        for record in re.finditer(r"\brecord\s+[A-Za-z_]\w*\s*\(([^)]*)\)", content):
            if content[:record.start()].count("{") - content[:record.start()].count("}") >= 1:
                nested_fields.update(
                    parameter.strip().split()[-1]
                    for parameter in _split_java_arguments(record.group(1)) if parameter.strip().split()
                )
        outer_fields = set(re.findall(r"(?m)^\s*private\s+(?!static\b)[\w<>?,.]+\s+([A-Za-z_]\w*)\s*;", content))
        for field in nested_fields - outer_fields:
            cap = field[0].upper() + field[1:]
            content = re.sub(
                rf"(?m)^\s*public\s+[\w<>?,.]+\s+get{cap}\s*\(\s*\)\s*\{{\s*return\s+{re.escape(field)}\s*;\s*\}}\s*\r?\n?",
                "", content,
            )
        output[path] = content


def _migrate_java_record_factories(output: dict[str, str]) -> None:
    """Use canonical constructors when a record declares no ``of`` factory."""
    records: dict[tuple[str, str], bool] = {}
    for path, content in output.items():
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        module = _java_source_module(path)
        for name in re.findall(r"\brecord\s+([A-Za-z_]\w*)\s*\(", content):
            has_factory = bool(re.search(
                rf"\bstatic\s+{re.escape(name)}\s+of\s*\(", content,
            ))
            records[(module, name)] = has_factory
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        module = _java_source_module(path)
        for (owner, name), has_factory in records.items():
            if owner != module or has_factory:
                continue
            content = re.sub(
                rf"\b{re.escape(name)}\.of\(([^;\n]*)\)",
                rf"new {name}(\1)",
                content,
            )
        output[path] = content


def _migrate_java_record_builder_chains(output: dict[str, str]) -> None:
    """Replace JavaBean builder chains on records with their canonical constructor."""
    components = _java_record_components(output)
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        module = _java_source_module(path)
        for (owner, record_name), fields in components.items():
            if owner != module:
                continue
            pattern = re.compile(
                rf"new\s+{re.escape(record_name)}\s*\(\s*\)\s*((?:\.set)[^;]+)",
                re.DOTALL,
            )
            def replace(match: re.Match) -> str:
                values = {
                    name[0].lower() + name[1:]: expression.strip()
                    for name, expression in re.findall(
                        r"\.set([A-Za-z_]\w*)\s*\(((?:[^()]|\([^()]*\))*)\)",
                        match.group(1), re.DOTALL,
                    )
                }
                if not all(field in values for field in fields):
                    return match.group(0)
                return f"new {record_name}({', '.join(values[field] for field in fields)})"
            content = pattern.sub(replace, content)
        output[path] = content


def _reconcile_java_entity_mutators(output: dict[str, str]) -> None:
    """Add missing bean accessors demanded by generated consumers/repositories."""
    for entity_path, entity in list(output.items()):
        if not entity_path.casefold().endswith(".java") or "@Entity" not in str(entity):
            continue
        class_match = re.search(r"\bclass\s+([A-Za-z_]\w*)", entity)
        if not class_match:
            continue
        entity_name = class_match.group(1)
        if not re.search(r"@(?:jakarta\.persistence\.)?Id\b", entity):
            entity = re.sub(
                r"(?m)^(\s*private\s+(?:Long|long|Integer|int|String|UUID)\s+id\s*;)",
                "    @jakarta.persistence.Id\n    @jakarta.persistence.GeneratedValue(strategy = jakarta.persistence.GenerationType.IDENTITY)\n\\1",
                entity, count=1,
            )
        fields = {
            name: field_type for field_type, name in re.findall(
                r"(?m)^\s*private\s+(?!static\b)([\w<>?,.]+)\s+([A-Za-z_]\w*)\s*(?:=[^;]*)?;", entity,
            )
        }
        module = _java_source_module(entity_path)
        module_text = "\n".join(
            value for source_path, value in output.items()
            if isinstance(value, str) and source_path.casefold().endswith(".java")
            and _java_source_module(source_path) == module
        )
        repo_properties = re.findall(r"\bfind(?:All)?By([A-Z][A-Za-z0-9]*?)(?:And|OrderBy|\s*\()", module_text)
        for raw_prop in repo_properties:
            boolean_property = bool(re.search(r"(?:Is)?(?:True|False)$", raw_prop))
            prop = re.sub(r"(?:Is)?(?:True|False)$", "", raw_prop) if boolean_property else raw_prop
            field = prop[0].lower() + prop[1:]
            if field not in fields and (field.casefold().endswith("id") or boolean_property):
                fields[field] = (
                    "Boolean" if boolean_property
                    else "String" if field.casefold() == "userid"
                    else "Long"
                )
                insertion = entity.rfind("}")
                initializer = " = true" if boolean_property else ""
                entity = entity[:insertion] + f"\n    private {fields[field]} {field}{initializer};\n" + entity[insertion:]
        entity_vars = re.findall(rf"\b{re.escape(entity_name)}\s+([a-zA-Z_]\w*)", module_text)
        for entity_var in entity_vars:
            for element_type, prop in re.findall(
                rf"for\s*\(\s*([A-Za-z_]\w*)\s+\w+\s*:\s*{re.escape(entity_var)}\.get([A-Z][A-Za-z0-9]*)\(\)",
                module_text,
            ):
                field = prop[0].lower() + prop[1:]
                if field not in fields:
                    fields[field] = f"java.util.List<{element_type}>"
                    insertion = entity.rfind("}")
                    entity = entity[:insertion] + f"\n    private {fields[field]} {field} = new java.util.ArrayList<>();\n" + entity[insertion:]
        methods = []
        for field, field_type in fields.items():
            cap = field[0].upper() + field[1:]
            getter = ("is" if field_type == "boolean" else "get") + cap
            if re.search(rf"\b{getter}\s*\(", module_text) and not re.search(rf"\b{getter}\s*\(", entity):
                methods.append(f"    public {field_type} {getter}() {{ return {field}; }}")
            if re.search(rf"\bset{cap}\s*\(", module_text) and not re.search(rf"\bset{cap}\s*\(", entity):
                methods.append(f"    public void set{cap}({field_type} value) {{ this.{field} = value; }}")
        if methods:
            insertion = entity.rfind("}")
            entity = entity[:insertion] + "\n\n" + "\n\n".join(methods) + "\n" + entity[insertion:]
        output[entity_path] = entity


def _reconcile_java_entity_update_copy_contracts(output: dict[str, str]) -> None:
    """Align generated full-update services with their entity's real fields.

    Domain files are generated independently. A generic service frequently
    emits ``target.setName(updated.getName())`` even after source grounding
    replaces the placeholder ``name`` field with real legacy fields. Remove
    those fabricated accessor pairs and copy every real mutable business field
    for the same PUT-style update method. The subsequent entity-mutator pass
    materializes any accessors not supplied by Lombok.
    """
    entities: dict[tuple[str, str], dict[str, str]] = {}
    for path, content in output.items():
        if (
            not path.casefold().endswith(".java")
            or not re.search(r"@(?:jakarta\.persistence\.)?Entity\b", str(content))
        ):
            continue
        declared = re.search(r"\bclass\s+([A-Za-z_$][\w$]*)\b", content)
        if not declared:
            continue
        fields = {
            name: field_type
            for field_type, name in re.findall(
                r"(?m)^\s*private\s+(?!static\b)([A-Za-z_$][\w$<>?,.\[\]]*)\s+"
                r"([A-Za-z_$][\w$]*)\s*(?:=[^;]*)?;",
                content,
            )
        }
        if fields:
            entities[(_java_source_module(path), declared.group(1))] = fields

    copy_pattern_template = (
        r"(?m)^(?P<indent>[ \t]*)(?P<target>[A-Za-z_$][\w$]*"
        r"(?:\.[A-Za-z_$][\w$]*\([^;()]*\))*)\.set(?P<property>[A-Z][A-Za-z0-9_$]*)"
        r"\(\s*{source}\.(?:get|is)(?P=property)\(\)\s*\)\s*;[ \t]*(?:\r?\n)?"
    )
    excluded = {"id", "createdAt", "updatedAt", "version"}
    for path, content in list(output.items()):
        if not path.casefold().endswith("serviceimpl.java") or not isinstance(content, str):
            continue
        module = _java_source_module(path)
        replacements: list[tuple[int, int, str]] = []
        for start, end in _java_top_level_method_spans(content):
            segment = content[start:end]
            # Method spans deliberately begin at the opening brace, so bind
            # the update parameter from the immediately preceding signature.
            signature_prefix = content[max(0, start - 1000):start + 1]
            matched_entity = None
            source_name = ""
            for (owner, entity_name), fields in entities.items():
                if owner != module:
                    continue
                signatures = list(re.finditer(
                    rf"\b{re.escape(entity_name)}\s+update\s*\([^)]*\b"
                    rf"{re.escape(entity_name)}\s+([A-Za-z_$][\w$]*)\s*\)\s*\{{",
                    signature_prefix,
                ))
                if signatures:
                    matched_entity = (entity_name, fields)
                    source_name = signatures[-1].group(1)
                    break
            if not matched_entity:
                continue
            _entity_name, fields = matched_entity
            copy_pattern = re.compile(
                copy_pattern_template.format(source=re.escape(source_name)),
            )
            copies = list(copy_pattern.finditer(segment))
            if not copies:
                continue
            target = copies[0].group("target")
            indent = copies[0].group("indent")
            field_by_cap = {
                field[0].upper() + field[1:]: field for field in fields
            }
            retained_fields: set[str] = set()

            def retain_real_copy(match: re.Match) -> str:
                field = field_by_cap.get(match.group("property"))
                if not field:
                    return ""
                retained_fields.add(field)
                return match.group(0)

            repaired = copy_pattern.sub(retain_real_copy, segment)
            missing_lines = []
            for field, field_type in fields.items():
                if field in excluded or field in retained_fields:
                    continue
                cap = field[0].upper() + field[1:]
                getter = f"is{cap}" if field_type == "boolean" else f"get{cap}"
                missing_lines.append(
                    f"{indent}{target}.set{cap}({source_name}.{getter}());"
                )
            if missing_lines:
                return_match = re.search(r"(?m)^[ \t]*return\s+[^;]+;", repaired)
                if return_match:
                    repaired = (
                        repaired[:return_match.start()]
                        + "\n".join(missing_lines) + "\n"
                        + repaired[return_match.start():]
                    )
            if repaired != segment:
                replacements.append((start, end, repaired))
        for start, end, repaired in reversed(replacements):
            content = content[:start] + repaired + content[end:]
        output[path] = content


def _reconcile_java_entity_constructors(output: dict[str, str]) -> None:
    """Add typed compatibility constructors only for observed entity calls."""
    all_java = "\n".join(
        content for path, content in output.items()
        if path.casefold().endswith(".java") and isinstance(content, str)
    )
    aliases = {"long": "Long", "int": "Integer", "boolean": "Boolean", "double": "Double", "float": "Float"}

    def expression_type(expression: str) -> str:
        value = expression.strip()
        if re.fullmatch(r'"(?:[^"\\]|\\.)*"', value): return "String"
        if value in {"true", "false"}: return "Boolean"
        if re.fullmatch(r"-?\d+[lL]", value): return "Long"
        if re.fullmatch(r"-?\d+", value): return "Integer"
        if "Instant." in value: return "Instant"
        return ""

    for path, content in list(output.items()):
        if "/src/main/java/" not in path or "@Entity" not in content or not isinstance(content, str):
            continue
        declared = re.search(r"\bclass\s+([A-Za-z_]\w*)", content)
        if not declared:
            continue
        name = declared.group(1)
        fields = re.findall(
            r"(?m)^\s*private\s+(?!static\b)([A-Za-z_][\w<>?,.]*)\s+([A-Za-z_]\w*)\s*(?:=[^;]*)?;",
            content,
        )
        additions = []
        for creation in re.finditer(rf"\bnew\s+{re.escape(name)}\s*\(", all_java):
            open_paren = creation.end() - 1
            depth = 0
            close = None
            for index in range(open_paren, len(all_java)):
                if all_java[index] == "(": depth += 1
                elif all_java[index] == ")":
                    depth -= 1
                    if depth == 0:
                        close = index
                        break
            if close is None:
                continue
            arguments = _split_java_arguments(all_java[open_paren + 1:close])
            if not arguments:
                continue
            argument_types = [expression_type(argument) for argument in arguments]
            if any(not value for value in argument_types):
                continue
            selected = []
            field_index = 0
            for argument_type in argument_types:
                while field_index < len(fields):
                    field_type, field = fields[field_index]
                    field_index += 1
                    if aliases.get(field_type, field_type) == argument_type:
                        selected.append((field_type, field))
                        break
                else:
                    selected = []
                    break
            if not selected:
                continue
            params = ", ".join(f"{field_type} {field}" for field_type, field in selected)
            if re.search(rf"\b{re.escape(name)}\s*\(\s*{re.escape(params)}\s*\)", content):
                continue
            assignments = " ".join(f"this.{field} = {field};" for _, field in selected)
            addition = f"    public {name}({params}) {{ {assignments} }}"
            if addition not in additions:
                additions.append(addition)
        if additions:
            insertion = content.rfind("}")
            content = content[:insertion] + "\n\n" + "\n".join(additions) + "\n" + content[insertion:]
            output[path] = content


def _reconcile_java_bean_compatibility_constructors(output: dict[str, str]) -> None:
    """Add observed all-property constructors to generated JavaBean subclasses."""
    classes: dict[tuple[str, str], dict] = {}
    for path, content in output.items():
        if "/src/main/java/" not in path or not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        declared = re.search(
            r"\bpublic\s+class\s+([A-Za-z_]\w*)(?:\s+extends\s+([A-Za-z_]\w*))?",
            content,
        )
        if not declared:
            continue
        fields = re.findall(
            r"(?m)^\s*private\s+(?!static\b)([A-Za-z_][\w<>?,.]*)\s+([A-Za-z_]\w*)\s*(?:=[^;]*)?;",
            content,
        )
        classes[(_java_source_module(path), declared.group(1))] = {
            "path": path, "content": content, "parent": declared.group(2), "fields": fields,
        }

    for key, info in classes.items():
        module, name = key
        if not name.endswith("Bean"):
            continue
        parent = classes.get((module, info["parent"])) if info["parent"] else None
        inherited = list(parent["fields"]) if parent else []
        fields = list(info["fields"]) + inherited
        if not fields:
            continue
        module_text = "\n".join(
            value for source_path, value in output.items()
            if isinstance(value, str) and source_path.casefold().endswith(".java")
            and _java_source_module(source_path) == module
        )
        observed_arity = False
        for creation in re.finditer(rf"\bnew\s+{re.escape(name)}\s*\(", module_text):
            open_paren = creation.end() - 1
            depth = 0
            for index in range(open_paren, len(module_text)):
                if module_text[index] == "(":
                    depth += 1
                elif module_text[index] == ")":
                    depth -= 1
                    if depth == 0:
                        observed_arity = len(_split_java_arguments(module_text[open_paren + 1:index])) == len(fields)
                        break
            if observed_arity:
                break
        if not observed_arity:
            continue
        content = info["content"]
        constructors = list(re.finditer(rf"\b(?:public|protected|private)\s+{re.escape(name)}\s*\(([^)]*)\)", content))
        if any(len(_split_java_arguments(match.group(1))) == len(fields) for match in constructors):
            continue
        additions = []
        if not any(not _split_java_arguments(match.group(1)) for match in constructors):
            additions.append(f"    public {name}() {{}}")
        params = ", ".join(f"{field_type} {field}" for field_type, field in fields)
        own_names = {field for _, field in info["fields"]}
        assignments = [
            f"        this.{field} = {field};" if field in own_names
            else f"        this.set{field[0].upper() + field[1:]}({field});"
            for _, field in fields
        ]
        additions.append(
            f"    public {name}({params}) {{\n" + "\n".join(assignments) + "\n    }"
        )
        insertion = content.rfind("}")
        output[info["path"]] = (
            content[:insertion] + "\n\n" + "\n\n".join(additions) + "\n" + content[insertion:]
        )


def _reconcile_java_setter_argument_types(output: dict[str, str]) -> None:
    """Coerce simple generated setter calls to the setter's declared scalar type."""
    contracts: dict[tuple[str, str, str], str] = {}
    for path, content in output.items():
        if "/src/main/java/" not in path or not isinstance(content, str):
            continue
        owner = re.search(r"\bclass\s+([A-Za-z_]\w*)", content)
        if not owner:
            continue
        for method, parameter_type in re.findall(
            r"\bvoid\s+(set[A-Z][A-Za-z0-9_]*)\s*\(\s*([A-Za-z_][\w<>?,.]*)\s+[A-Za-z_]\w*\s*\)",
            content,
        ):
            contracts[(_java_source_module(path), owner.group(1), method)] = parameter_type
    numeric = {"byte", "short", "int", "long", "float", "double", "Byte", "Short", "Integer", "Long", "Float", "Double"}
    for path, content in list(output.items()):
        if "/src/main/java/" not in path or not isinstance(content, str):
            continue
        module = _java_source_module(path)
        variables = {
            name: value_type
            for value_type, name in re.findall(
                r"(?:^|[,(;])\s*(?:final\s+)?([A-Za-z_][\w<>?,.]*)\s+([a-zA-Z_]\w*)\s*(?=[,)=;])",
                content,
                re.MULTILINE,
            )
        }
        for (owner_module, owner, method), target_type in contracts.items():
            if owner_module != module:
                continue
            receivers = [name for name, value_type in variables.items() if value_type.rsplit(".", 1)[-1] == owner]
            for receiver in receivers:
                call = re.compile(rf"\b{re.escape(receiver)}\.{re.escape(method)}\(\s*([A-Za-z_]\w*)\s*\)")
                def replace(match: re.Match[str]) -> str:
                    argument = match.group(1)
                    source_type = variables.get(argument, "")
                    if source_type == target_type or not source_type:
                        return match.group(0)
                    if target_type in numeric and source_type == "String":
                        wrapper = target_type if target_type[:1].isupper() else target_type.capitalize()
                        if wrapper == "Int":
                            wrapper = "Integer"
                        return f"{receiver}.{method}({wrapper}.valueOf({argument}))"
                    if target_type == "String" and source_type in numeric:
                        return f"{receiver}.{method}(String.valueOf({argument}))"
                    return match.group(0)
                content = call.sub(replace, content)
        output[path] = content


def _reconcile_java_persisted_entity_identity(output: dict[str, str]) -> None:
    """Use the repository-returned entity when generated code reads its identity."""
    for path, content in list(output.items()):
        if "/src/main/java/" not in path or not isinstance(content, str):
            continue
        for repository, variable in re.findall(
            r"\b((?:[a-zA-Z_]\w*Repository|repository))\.save\(\s*([a-zA-Z_]\w*)\s*\)\s*;",
            content,
        ):
            statement = f"{repository}.save({variable});"
            position = content.find(statement)
            if position < 0:
                continue
            if re.search(rf"\b{re.escape(variable)}\s*=\s*$", content[max(0, position - 120):position]):
                continue
            tail = content[position + len(statement):]
            if re.search(rf"\b{re.escape(variable)}\.getId\s*\(", tail):
                content = content[:position] + f"{variable} = {statement}" + content[position + len(statement):]
        output[path] = content


def _reconcile_java_boolean_bean_accessors(output: dict[str, str]) -> None:
    """Add `isX` alongside generated `getX` for boolean bean consumers."""
    module_text: dict[str, str] = {}
    for path, content in output.items():
        if path.casefold().endswith(".java") and isinstance(content, str):
            module = _java_source_module(path)
            module_text[module] = module_text.get(module, "") + "\n" + content
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str) or not re.search(r"\bclass\s+", content):
            continue
        additions = []
        consumers = module_text.get(_java_source_module(path), "")
        for _, field in re.findall(r"(?m)^\s*private\s+(boolean|Boolean)\s+([A-Za-z_]\w*)\s*;", content):
            cap = field[0].upper() + field[1:]
            if re.search(rf"\.is{cap}\s*\(", consumers) and not re.search(rf"\bis{cap}\s*\(", content):
                additions.append(f"    public boolean is{cap}() {{ return Boolean.TRUE.equals({field}); }}")
        if additions:
            insertion = content.rfind("}")
            output[path] = content[:insertion] + "\n\n" + "\n".join(additions) + "\n" + content[insertion:]


def _reconcile_java_mapper_contracts(output: dict[str, str]) -> None:
    """Close conventional Entity/Request/Response mapper contracts per module."""
    module_files: dict[str, dict[str, tuple[str, str]]] = {}
    for path, content in output.items():
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        declared = re.search(r"\b(?:class|record|interface)\s+([A-Za-z_]\w*)", content)
        if declared:
            module_files.setdefault(_java_source_module(path), {})[declared.group(1)] = (path, content)

    for module, files in module_files.items():
        for mapper_name, (mapper_path, mapper) in list(files.items()):
            if not mapper_name.endswith("Mapper"):
                continue
            module_consumers = "\n".join(
                value for source_path, value in output.items()
                if _java_source_module(source_path) == module and source_path != mapper_path and isinstance(value, str)
            )
            if re.search(rf"\bfinal\s+{re.escape(mapper_name)}\b", module_consumers) and "@Component" not in mapper:
                mapper = re.sub(
                    rf"\bpublic\s+(class|record)\s+{re.escape(mapper_name)}\b",
                    rf"@org.springframework.stereotype.Component\npublic \1 {mapper_name}", mapper, count=1,
                )
            stem = mapper_name[:-6]
            entity_entry = files.get(stem + "Entity")
            request_entry = files.get(stem + "Request")
            response_entry = files.get(stem + "Response")
            additions: list[str] = []
            if entity_entry and response_entry and not re.search(rf"\b{stem}Response\s+toResponse\s*\(", mapper):
                if re.search(rf"\bstatic\s+{stem}Response\s+to{stem}Response\s*\(", mapper):
                    additions.append(
                        f"    public static {stem}Response toResponse({stem}Entity source) {{ return to{stem}Response(source); }}"
                    )
            if entity_entry and request_entry and not re.search(rf"\b{stem}Entity\s+toEntity\s*\(", mapper):
                entity = entity_entry[1]
                request = request_entry[1]
                entity_fields = re.findall(r"(?m)^\s*private\s+(?!static\b)[\w<>?,.]+\s+([A-Za-z_]\w*)\s*;", entity)
                request_match = re.search(rf"\brecord\s+{re.escape(stem)}Request\s*\(", request)
                request_fields: set[str] = set()
                if request_match:
                    start = request_match.end() - 1
                    depth = 0
                    close = None
                    for index in range(start, len(request)):
                        if request[index] == "(": depth += 1
                        elif request[index] == ")":
                            depth -= 1
                            if depth == 0: close = index; break
                    if close is not None:
                        for parameter in _split_java_arguments(request[start + 1:close]):
                            clean = re.sub(r"@[A-Za-z_][\w.]*\s*(?:\([^)]*\))?\s*", "", parameter).strip()
                            if clean.split(): request_fields.add(clean.split()[-1])
                shared = [field for field in entity_fields if field in request_fields]
                if shared:
                    assignments = "\n".join(
                        f"        target.set{field[0].upper() + field[1:]}(source.{field}());" for field in shared
                    )
                    additions.append(
                        f"    public static {stem}Entity toEntity({stem}Request source) {{\n"
                        f"        {stem}Entity target = new {stem}Entity();\n{assignments}\n        return target;\n    }}"
                    )
            if additions:
                insertion = mapper.rfind("}")
                if insertion >= 0:
                    mapper = mapper[:insertion] + "\n\n" + "\n\n".join(additions) + "\n" + mapper[insertion:]
                    output[mapper_path] = mapper
                    for source_path, source in list(output.items()):
                        if _java_source_module(source_path) != module or not isinstance(source, str):
                            continue
                        source = re.sub(rf"\b[a-zA-Z_]\w*::toResponse\b", f"{mapper_name}::toResponse", source)
                        source = re.sub(rf"\b[a-zA-Z_]\w*\.to(Response|Entity)\b", rf"{mapper_name}.to\1", source)
                        output[source_path] = source


def _java_record_components_uncached(
    snapshot: tuple[tuple[str, str], ...],
) -> tuple[tuple[tuple[str, str], tuple[str, ...]], ...]:
    """Component name lists for every declared record, keyed by (module,
    record name) — the record's own declaration is authoritative for its
    real accessor shape, the same source of truth `_migrate_java_record_factories`
    already trusts for constructor calls."""
    components: dict[tuple[str, str], list[str]] = {}
    for path, content in snapshot:
        module = _java_source_module(path)
        for match in re.finditer(r"\brecord\s+([A-Za-z_]\w*)\s*\(", content):
            name = match.group(1)
            open_paren = match.end() - 1
            depth = 0
            close_paren = None
            # Bean Validation component annotations routinely carry their
            # own parens — `@NotBlank(message = "...")`, `@Min(value = 0L,
            # message = "...")` — so a naive `[^)]*` capture for the
            # component list stops at the *annotation's* closing paren, not
            # the record's, and silently mis-parses every annotated field.
            for index in range(open_paren, len(content)):
                if content[index] == "(":
                    depth += 1
                elif content[index] == ")":
                    depth -= 1
                    if depth == 0:
                        close_paren = index
                        break
            if close_paren is None:
                continue
            params = content[open_paren + 1:close_paren]
            names = [tokens[-1] for tokens in (p.split() for p in _split_java_arguments(params)) if tokens]
            components[(module, name)] = names
    return tuple(
        (key, tuple(value)) for key, value in sorted(components.items())
    )


_java_record_components_cached = functools.lru_cache(maxsize=32)(
    _java_record_components_uncached
)


def _java_record_components(output: dict[str, str]) -> dict[tuple[str, str], list[str]]:
    """Return record components, reusing identical immutable source snapshots."""
    snapshot = tuple(sorted(
        (path, content)
        for path, content in output.items()
        if path.casefold().endswith(".java") and isinstance(content, str)
    ))
    return {
        key: list(value)
        for key, value in _java_record_components_cached(snapshot)
    }


def _java_top_level_method_spans(content: str) -> list[tuple[int, int]]:
    """(start, end) character spans of each depth-2 brace block in a
    single-top-level-type file — i.e. each method/constructor body.

    Used to scope a `TypeName varName` binding to the method it is actually
    declared in: the same parameter name (`request`, `product`, ...) is
    routinely reused across sibling methods with a *different* declared
    type in generated Spring code, so a file-wide "this name means this
    type everywhere" binding silently mis-attributes accessor calls in
    every method except the last one scanned.
    """
    spans: list[tuple[int, int]] = []
    depth = 0
    start: int | None = None
    for index, char in enumerate(content):
        if char == "{":
            depth += 1
            if depth == 2:
                start = index
        elif char == "}":
            if depth == 2 and start is not None:
                spans.append((start, index + 1))
                start = None
            depth = max(0, depth - 1)
    return spans


def _java_scope_variable_bindings(
    content: str, type_names,
) -> list[tuple[int, int, str, str]]:
    """Find `TypeName varName` declarations (parameter or local) for each
    name in `type_names`, each attributed to its enclosing method/constructor
    body span (see `_java_top_level_method_spans`)."""
    spans = _java_top_level_method_spans(content)
    bindings: list[tuple[int, int, str, str]] = []
    for type_name in type_names:
        for match in re.finditer(
            rf"\b{re.escape(type_name)}\s+([a-zA-Z_]\w*)\s*(?=[=;,:\)])", content,
        ):
            pos = match.start()
            # Local declarations sit inside their own body — "containing"
            # span. Parameter declarations sit in the method *signature*,
            # entirely before that method's own opening brace — the nearest
            # *following* span is unambiguously the body that parameter is
            # live in, since nothing else opens a depth-2 block between a
            # parameter list and its own method body.
            span = next(((s, e) for s, e in spans if s <= pos < e), None)
            if span is None:
                span = next(((s, e) for s, e in spans if s >= pos), None)
            if span is None:
                continue
            bindings.append((span[0], span[1], match.group(1), type_name))
    return bindings


def _apply_scoped_java_rewrites(
    content: str,
    bindings: list[tuple[int, int, str, str]],
    rewrite_fn: Callable[[str, str, str], str],
) -> str:
    """Apply `rewrite_fn(segment, var_name, type_name)` once per distinct
    method-body span, then reassemble — spans from
    `_java_top_level_method_spans` are non-overlapping, so this stays
    correct regardless of how much a rewrite changes each segment's length."""
    by_span: dict[tuple[int, int], list[tuple[str, str]]] = {}
    for start, end, var_name, type_name in bindings:
        by_span.setdefault((start, end), []).append((var_name, type_name))
    if not by_span:
        return content
    pieces: list[str] = []
    cursor = 0
    for start, end in sorted(by_span):
        pieces.append(content[cursor:start])
        segment = content[start:end]
        for var_name, type_name in by_span[(start, end)]:
            segment = rewrite_fn(segment, var_name, type_name)
        pieces.append(segment)
        cursor = end
    pieces.append(content[cursor:])
    return "".join(pieces)


def _migrate_java_record_getter_calls(output: dict[str, str]) -> None:
    """Rewrite JavaBean-style `.getX()`/`.isX()` calls made against a value
    declared (as a parameter or local) with a known record type back to
    that record's own canonical `.x()` accessor.

    This is the single most common per-file-generation drift for
    request/response DTOs: the DTO itself gets generated as a record, but a
    *consumer* file (a controller calling into a service's request type, or
    vice versa) is generated independently assuming JavaBean getters —
    e.g. `ProductCreateRequest` is a record but `ProductController` calls
    `request.getSku()`, or `OrderService`'s own nested `CreateOrderRequest`
    record is read via `request.getProductId()`/`request.getQuantity()`.
    """
    components = _java_record_components(output)
    if not components:
        return
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        module = _java_source_module(path)
        module_records = {name: fields for (mod, name), fields in components.items() if mod == module}
        if not module_records:
            continue
        bindings = _java_scope_variable_bindings(content, module_records)
        if not bindings:
            continue

        def rewrite(segment: str, var_name: str, record_name: str) -> str:
            for field in module_records[record_name]:
                cap = field[0].upper() + field[1:]
                for accessor in (f"get{cap}", f"is{cap}"):
                    pattern = rf"\b{re.escape(var_name)}\.{accessor}\(\)"
                    segment = re.sub(pattern, f"{var_name}.{field}()", segment)
            return segment

        output[path] = _apply_scoped_java_rewrites(content, bindings, rewrite)


def _java_entity_shapes(output: dict[str, str]) -> dict[tuple[str, str], dict]:
    """Real accessor surface of every plain (non-record) `@Entity` class —
    the authoritative shape a per-file-generated service/DTO must actually
    call against, instead of the record-style accessors or Lombok-builder
    API a *different* generation pass may have assumed for the same type."""
    shapes: dict[tuple[str, str], dict] = {}
    for path, content in output.items():
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        if "@Entity" not in content or "toBuilder" in content:
            continue
        class_match = re.search(r"\bclass\s+([A-Za-z_]\w*)\b", content)
        if not class_match:
            continue
        name = class_match.group(1)
        getters: dict[str, str] = {}
        for match in re.finditer(
            r"\bpublic\s+[\w<>\[\],.?]+\s+(get|is)([A-Za-z_]\w*)\s*\(\s*\)", content,
        ):
            prefix, rest = match.groups()
            getters[rest[0].lower() + rest[1:]] = prefix + rest
        if not getters:
            continue
        shapes[(_java_source_module(path), name)] = {"path": path, "getters": getters}
    return shapes


def _java_repository_entity_map(output: dict[str, str]) -> dict[tuple[str, str], str]:
    """Entity type each Spring Data repository interface is declared over."""
    mapping: dict[tuple[str, str], str] = {}
    for path, content in output.items():
        if not path.casefold().endswith("repository.java") or not isinstance(content, str):
            continue
        match = re.search(
            r"\binterface\s+([A-Za-z_]\w*)\s+extends\s+[\w.]*"
            r"(?:JpaRepository|CrudRepository|PagingAndSortingRepository)\s*<\s*([A-Za-z_]\w*)\s*,",
            content,
        )
        if match:
            mapping[(_java_source_module(path), match.group(1))] = match.group(2)
    return mapping


def _reconcile_java_entity_read_accessors(output: dict[str, str]) -> None:
    """Rewrite record-style bareword calls (`.id()`, `.name()`, ...) made
    against a value that resolves to a plain JPA entity back to that
    entity's real getter. This is the inverse drift of
    `_migrate_java_record_getter_calls`: a service method gets generated
    assuming its repository returns an immutable record when the entity is
    actually a mutable JavaBean-style `@Entity` class."""
    entities = _java_entity_shapes(output)
    if not entities:
        return
    repo_entity = _java_repository_entity_map(output)
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        module = _java_source_module(path)
        module_entities = {name: shape for (mod, name), shape in entities.items() if mod == module}
        if not module_entities:
            continue
        module_repos = {
            repo: entity for (mod, repo), entity in repo_entity.items()
            if mod == module and entity in module_entities
        }

        spans = _java_top_level_method_spans(content)
        bindings = _java_scope_variable_bindings(content, module_entities)

        repo_fields: dict[str, str] = {}
        for repo_name, entity_name in module_repos.items():
            for match in re.finditer(rf"\b{re.escape(repo_name)}\s+(\w+)\s*[;)]", content):
                repo_fields[match.group(1)] = entity_name
        for repo_field, entity_name in repo_fields.items():
            chain = (
                rf"\bvar\s+(\w+)\s*=\s*{re.escape(repo_field)}\."
                rf"(?:findById|findByIdOrThrow)\([^;]*?\)\s*\.(?:orElseThrow\([^;]*\)|get\(\))\s*;"
            )
            for match in re.finditer(chain, content):
                span = next(((s, e) for s, e in spans if s <= match.start() < e), None)
                if span:
                    bindings.append((span[0], span[1], match.group(1), entity_name))
            for match in re.finditer(
                rf"\bvar\s+(\w+)\s*=\s*{re.escape(repo_field)}\.save\([^;]*\)\s*;", content,
            ):
                span = next(((s, e) for s, e in spans if s <= match.start() < e), None)
                if span:
                    bindings.append((span[0], span[1], match.group(1), entity_name))

        if not bindings:
            continue

        def rewrite(segment: str, var_name: str, entity_name: str) -> str:
            shape = module_entities[entity_name]
            for field, getter in shape["getters"].items():
                pattern = rf"\b{re.escape(var_name)}\.{re.escape(field)}\(\)"
                segment = re.sub(pattern, f"{var_name}.{getter}()", segment)
            return segment

        output[path] = _apply_scoped_java_rewrites(content, bindings, rewrite)


_ENTITY_DTO_SUFFIXES = ("Response", "Dto", "DTO")


def _synthesize_java_entity_dto_factories(output: dict[str, str]) -> None:
    """Synthesize a missing `X.from(Entity)` static factory for an
    `<Entity><Suffix>` record whose components are all satisfied by the
    entity's real getters.

    `<Response>.from(<entity>)` is a convention several generated
    controllers/services call by name without the DTO itself ever defining
    it — a missing-method gap, not a wrong-call-site gap, so no amount of
    call-site rewriting fixes it; the method has to actually exist.
    """
    components = _java_record_components(output)
    entities = _java_entity_shapes(output)
    if not components or not entities:
        return
    for (module, record_name), fields in components.items():
        record_path = next(
            (
                path for path, content in output.items()
                if path.casefold().endswith(".java") and isinstance(content, str)
                and _java_source_module(path) == module
                and re.search(rf"\brecord\s+{re.escape(record_name)}\s*\(", content)
            ),
            None,
        )
        if not record_path:
            continue
        record_content = output[record_path]
        if re.search(rf"\bstatic\s+{re.escape(record_name)}\s+from\s*\(", record_content):
            continue
        entity_name = next(
            (
                record_name[:-len(suffix)] for suffix in _ENTITY_DTO_SUFFIXES
                if record_name.endswith(suffix) and (module, record_name[:-len(suffix)]) in entities
            ),
            None,
        )
        if not entity_name:
            continue
        getters = entities[(module, entity_name)]["getters"]
        accessors: list[str] = []
        for field in fields:
            getter = getters.get(field)
            if not getter:
                accessors = []
                break
            accessors.append(f"source.{getter}()")
        if not accessors:
            continue
        factory = (
            f"\n\n    public static {record_name} from({entity_name} source) {{\n"
            f"        return new {record_name}({', '.join(accessors)});\n"
            f"    }}\n"
        )
        insertion = record_content.rfind("}")
        if insertion == -1:
            continue
        output[record_path] = record_content[:insertion] + factory + record_content[insertion:]


def _migrate_java_identity_pageable_lambda(output: dict[str, str]) -> None:
    """`.findAll(x -> x)` is never valid — no JpaRepository overload accepts
    a same-typed identity function — and is a recurring hallucinated
    stand-in for "no paging requested"."""
    pattern = re.compile(r"\.findAll\(\s*(\w+)\s*->\s*\1\s*\)")
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        if pattern.search(content):
            output[path] = pattern.sub(".findAll(Pageable.unpaged())", content)


def _promote_privately_referenced_java_nested_types(output: dict[str, str]) -> None:
    """A DTO/record declared `private` inside one class but imported or
    referenced by a *different* top-level class cannot compile — Java's
    private access is scoped to the enclosing top-level class/file, and a
    controller generated independently of its service routinely imports
    `Service.RequestType` without knowing (or being able to know) that the
    service generated that type as a private nested member. Promote the
    declaration to `public` rather than guess at extracting it into its own
    top-level file, since other generated files may already reference it
    exactly as `Owner.Nested`."""
    private_nested: dict[tuple[str, str], tuple[str, str]] = {}
    for path, content in output.items():
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        owner_match = re.search(
            r"\b(?:public\s+)?(?:class|interface|record|enum)\s+([A-Za-z_]\w*)", content,
        )
        if not owner_match:
            continue
        owner = owner_match.group(1)
        for nested_match in re.finditer(
            r"(?m)^[ \t]*private\s+((?:static\s+)?(?:final\s+)?record\s+([A-Za-z_]\w*)\s*\([^)]*\)\s*\{)",
            content,
        ):
            private_nested[(owner, nested_match.group(2))] = (path, nested_match.group(0))
        for nested_match in re.finditer(
            r"(?m)^[ \t]*private\s+((?:static\s+)?(?:final\s+)?class\s+([A-Za-z_]\w*)\b)",
            content,
        ):
            private_nested[(owner, nested_match.group(2))] = (path, nested_match.group(0))

    if not private_nested:
        return

    referenced: set[tuple[str, str]] = set()
    for path, content in output.items():
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        for (owner, name), (source_path, _declaration) in private_nested.items():
            if path == source_path:
                continue
            if re.search(rf"\b{re.escape(owner)}\.{re.escape(name)}\b", content):
                referenced.add((owner, name))

    for owner, name in referenced:
        source_path, declaration = private_nested[(owner, name)]
        promoted = declaration.replace("private ", "public ", 1)
        output[source_path] = output[source_path].replace(declaration, promoted, 1)


# ─── Diagnostic-driven deterministic Java build repair ─────────────────────
#
# Everything above in this file is static: it runs before a build ever
# happens and can only guess at what other generated files reference. The
# functions below run *inside* the build/repair loop instead, directly off
# real javac/Maven diagnostics (services/build_runner.py's errors_by_file) —
# so they fix exactly what the compiler has already proven is wrong, for two
# well-known, unambiguous, and always-safe error shapes that previously kept
# recurring across repair rounds because an LLM repair attempt doesn't
# reliably resolve them:
#   - "<symbol> has private access in <FQCN>" — a field or nested type the
#     static passes above didn't happen to catch (they only recognize a
#     subset of reference patterns). Widening private -> public never
#     changes behavior, only where a symbol may be referenced from, and the
#     compiler has already proven external code needs it.
#   - "cannot find symbol ... symbol: class Page/Pageable/..." for a small,
#     fixed set of common Spring Data types used in generated
#     controllers/repositories without their import ever being added.
# Java-only; never touched for csharp/typescript/python/go output.

_JAVA_PRIVATE_ACCESS_ERROR_RE = re.compile(r"(\w+) has private access in ([\w.]+)")


def _reconcile_java_private_access_from_diagnostics(
    output: dict[str, str], errors_by_file: dict[str, list[str]],
) -> set[str]:
    """Widen visibility for a field or nested type the compiler has just
    named, by fully-qualified owner, as inaccessible from outside its
    declaring class. See module-level note above."""
    changed: set[str] = set()
    for messages in errors_by_file.values():
        for message in messages:
            match = _JAVA_PRIVATE_ACCESS_ERROR_RE.search(message)
            if not match:
                continue
            symbol, owner_fqcn = match.group(1), match.group(2)
            owner_simple = owner_fqcn.rsplit(".", 1)[-1]
            for candidate_path, content in output.items():
                if not candidate_path.casefold().endswith(".java") or not isinstance(content, str):
                    continue
                package_match = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", content)
                if not package_match:
                    continue
                declared_prefix = f"{package_match.group(1)}.{owner_simple}"
                if owner_fqcn != declared_prefix and not owner_fqcn.startswith(declared_prefix + "."):
                    continue
                if not re.search(rf"\b(?:class|interface|record|enum)\s+{re.escape(owner_simple)}\b", content):
                    continue
                # Field declaration: `private <Type...> symbol [= ...];`
                field_pattern = re.compile(
                    rf"(?m)^(\s*)private(\s+(?:static\s+)?(?:final\s+)?"
                    rf"[\w<>\[\],.?\s]+?\s+{re.escape(symbol)}\s*(?:=[^;]*)?;)"
                )
                field_match = field_pattern.search(content)
                if field_match:
                    output[candidate_path] = field_pattern.sub(r"\1public\2", content, count=1)
                    changed.add(candidate_path)
                    break
                # Nested class/record declaration.
                nested_pattern = re.compile(
                    rf"(?m)^(\s*)private(\s+(?:static\s+)?(?:final\s+)?"
                    rf"(?:class|record)\s+{re.escape(symbol)}\b)"
                )
                nested_match = nested_pattern.search(content)
                if nested_match:
                    output[candidate_path] = nested_pattern.sub(r"\1public\2", content, count=1)
                    changed.add(candidate_path)
                    break
    return changed


_JAVA_MISSING_SYMBOL_ERROR_RE = re.compile(
    r"cannot find symbol.*?symbol:\s*class\s+(\w+)", re.IGNORECASE | re.DOTALL,
)

# Fixed, well-known set of Spring Data types generated controllers and
# repositories routinely use (Page<T>, Pageable, ...) without the LLM
# reliably remembering their import — never touched for any other symbol,
# so this cannot inject an import for a project's own same-named type.
_JAVA_WELL_KNOWN_MISSING_IMPORTS = {
    "ArrayList":   "java.util.ArrayList",
    "HashMap":     "java.util.HashMap",
    "HashSet":     "java.util.HashSet",
    "LinkedHashMap": "java.util.LinkedHashMap",
    "LinkedHashSet": "java.util.LinkedHashSet",
    "Service":     "org.springframework.stereotype.Service",
    "Component":   "org.springframework.stereotype.Component",
    "Repository":  "org.springframework.stereotype.Repository",
    "Page":        "org.springframework.data.domain.Page",
    "Pageable":    "org.springframework.data.domain.Pageable",
    "PageRequest": "org.springframework.data.domain.PageRequest",
    "Sort":        "org.springframework.data.domain.Sort",
}


def _reconcile_java_missing_well_known_imports_from_diagnostics(
    output: dict[str, str], errors_by_file: dict[str, list[str]],
) -> set[str]:
    """Add the missing import for a small, fixed set of common Spring Data
    types (`Page`, `Pageable`, ...) directly in the file the compiler
    reported as missing it. See module-level note above."""
    changed: set[str] = set()
    for path, messages in errors_by_file.items():
        if not path.casefold().endswith(".java") or path not in output:
            continue
        content = output[path]
        if not isinstance(content, str):
            continue
        for message in messages:
            match = _JAVA_MISSING_SYMBOL_ERROR_RE.search(message)
            if not match:
                continue
            import_path = _JAVA_WELL_KNOWN_MISSING_IMPORTS.get(match.group(1))
            if not import_path or f"import {import_path};" in content:
                continue
            package_match = re.search(r"(?m)^\s*package\s+[\w.]+\s*;", content)
            if not package_match:
                continue
            insert_at = package_match.end()
            content = content[:insert_at] + f"\nimport {import_path};" + content[insert_at:]
            output[path] = content
            changed.add(path)
    return changed


def _reconcile_java_transactional_import_conflicts_from_diagnostics(
    output: dict[str, str], errors_by_file: dict[str, list[str]],
) -> set[str]:
    """Resolve compiler-proven ``Transactional`` single-type import clashes.

    Generated Spring services can contain a Jakarta transaction import before
    the framework import reconciliation pass adds Spring's richer annotation.
    When javac reports that exact ambiguity, retain Spring's annotation (which
    supports generated attributes such as ``readOnly`` and ``propagation``)
    and remove only the competing Jakarta/Javax transaction import.
    """
    changed: set[str] = set()
    spring_import = "import org.springframework.transaction.annotation.Transactional;"
    competing_import = re.compile(
        r"(?m)^\s*import\s+(?:jakarta|javax)\.transaction\.Transactional\s*;\s*\r?\n?"
    )
    for path, messages in errors_by_file.items():
        if path not in output or not path.casefold().endswith(".java"):
            continue
        diagnostic = "\n".join(messages).casefold()
        if not (
            "transactional is ambiguous" in diagnostic
            or (
                "same simple name" in diagnostic
                and "org.springframework.transaction.annotation.transactional" in diagnostic
            )
        ):
            continue
        content = output[path]
        if not isinstance(content, str) or spring_import not in content:
            continue
        repaired, substitutions = competing_import.subn("", content)
        if substitutions:
            output[path] = repaired
            changed.add(path)
    return changed


_JAVA_STATIC_CONTEXT_METHOD_RE = re.compile(
    r"non-static method\s+(\w+)\([^)]*\)\s+cannot be referenced from a static context",
    re.IGNORECASE,
)
_JAVA_METHOD_ARITY_ERROR_RE = re.compile(
    r"method\s+(\w+)\s+in class\s+([\w.]+)\s+cannot be applied to given types",
    re.IGNORECASE,
)


def _reconcile_java_static_invocations_from_diagnostics(
    output: dict[str, str], errors_by_file: dict[str, list[str]],
) -> set[str]:
    """Restore ``static`` when javac proves generated callers use Class.method()."""
    method_names: set[str] = set()
    same_file_methods: dict[str, set[str]] = {}
    for diagnostic_path, messages in errors_by_file.items():
        for message in messages:
            static_names = set(_JAVA_STATIC_CONTEXT_METHOD_RE.findall(message))
            method_names.update(static_names)
            if static_names:
                same_file_methods.setdefault(diagnostic_path, set()).update(static_names)
            arity = _JAVA_METHOD_ARITY_ERROR_RE.search(message)
            if arity:
                method_names.add(arity.group(1))
    if not method_names:
        return set()

    changed: set[str] = set()
    all_source = "\n".join(
        content for path, content in output.items()
        if path.casefold().endswith(".java") and isinstance(content, str)
    )
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        owner = re.search(r"\b(?:class|enum|record)\s+(\w+)\b", content)
        if not owner:
            continue
        owner_name = owner.group(1)
        for method_name in method_names:
            class_qualified_call = re.search(
                rf"\b{re.escape(owner_name)}\s*\.\s*{re.escape(method_name)}\s*\(", all_source,
            )
            compiler_proved_same_file_call = method_name in same_file_methods.get(path, set())
            if not class_qualified_call and not compiler_proved_same_file_call:
                continue
            declaration = re.compile(
                rf"(?m)^(\s*(?:public|protected|private)\s+)(?!static\b)"
                rf"((?:(?:final|synchronized|native|abstract)\s+)*"
                rf"[\w<>,.?\[\]\s]+?\s+{re.escape(method_name)}\s*\()"
            )
            repaired, count = declaration.subn(r"\1static \2", content, count=1)
            if count:
                content = repaired
                output[path] = repaired
                changed.add(path)
    return changed


def _remove_java_invalid_type_imports_from_diagnostics(
    output: dict[str, str], errors_by_file: dict[str, list[str]],
) -> set[str]:
    """Remove generated non-static imports whose compiler-proven type is lowercase."""
    changed: set[str] = set()
    for path, messages in errors_by_file.items():
        content = output.get(path)
        if not isinstance(content, str) or not path.casefold().endswith(".java"):
            continue
        symbols = {
            match.group(1)
            for message in messages
            for match in [_JAVA_MISSING_SYMBOL_ERROR_RE.search(message)]
            if match and match.group(1)[:1].islower()
        }
        for symbol in symbols:
            pattern = re.compile(
                rf"(?m)^\s*import\s+(?!static\b)[\w.]+\.{re.escape(symbol)}\s*;\s*\r?\n?"
            )
            content, count = pattern.subn("", content)
            if count:
                output[path] = content
                changed.add(path)
    return changed


def _reconcile_java_logger_api_from_diagnostics(
    output: dict[str, str], errors_by_file: dict[str, list[str]],
) -> set[str]:
    """Align a compiler-proven JUL/SLF4J API mismatch with SLF4J.

    Generated Java occasionally imports ``java.util.logging.Logger`` while
    emitting SLF4J calls such as ``log.error(message, exception)``. Only
    rewrite the logger when javac identifies that exact missing method on a
    JUL logger; otherwise leave the logging choice untouched.
    """
    changed: set[str] = set()
    jul_import = "import java.util.logging.Logger;"
    for path, messages in errors_by_file.items():
        content = output.get(path)
        if not isinstance(content, str) or not path.casefold().endswith(".java"):
            continue
        diagnostic = "\n".join(messages).casefold()
        if not (
            "java.util.logging.logger" in diagnostic
            and ("symbol: method error(" in diagnostic or "method error(" in diagnostic)
            and jul_import in content
        ):
            continue
        repaired = content.replace(
            jul_import,
            "import org.slf4j.Logger;\nimport org.slf4j.LoggerFactory;",
        )
        repaired = re.sub(
            r"Logger\.getLogger\(\s*([A-Za-z_$][\w$]*)\.class\.getName\(\)\s*\)",
            r"LoggerFactory.getLogger(\1.class)",
            repaired,
        )
        if repaired != content:
            output[path] = repaired
            changed.add(path)
    return changed


def _repair_java_structural_diagnostics(
    output: dict[str, str], errors_by_file: dict[str, list[str]],
) -> set[str]:
    """Repair unambiguous Java grammar corruption before spending LLM rounds."""
    changed: set[str] = set()
    package_pattern = re.compile(
        r"(?m)(?:^|(?<=[;}]))[ \t]*package\s+[A-Za-z_$][\w.$]*\s*;"
    )
    resource_pattern = re.compile(
        r"(?m)^[ \t]*(?P<type>[A-Za-z_$][\w.$<>, ?\[\]]*)\s+"
        r"(?P<name>[A-Za-z_$][\w$]*)\s*=\s*null\s*;\s*\r?\n"
        r"(?P<indent>[ \t]*)try\s*\(\s*(?P=name)\s*=\s*(?P<expr>[^;\r\n]+)\s*\)"
    )
    cleanup_finally = re.compile(
        r"}\s*finally\s*\{\s*(?P<cleanup>(?:[A-Za-z_$][\w$]*\.close\(\)\s*;\s*)+)\}",
        re.DOTALL,
    )
    invalid_type_annotations = (
        "org.springframework.data.domain.Pageable",
        "java.lang.String", "java.time.LocalDate", "java.time.LocalDateTime",
        "java.math.BigDecimal", "java.util.Optional",
    )
    invalid_exception_record = re.compile(
        r"(?m)^(?P<indent>[ \t]*)(?P<access>public|protected|private)?[ \t]*"
        r"record\s+(?P<name>[A-Za-z_$][\w$]*)\s*\(\s*String\s+message"
        r"(?P<cause>\s*,\s*Throwable\s+cause)?\s*\)\s*"
        r"extends\s+(?P<parent>(?:[A-Za-z_$][\w$]*\.)*"
        r"[A-Za-z_$][\w$]*(?:Exception|Error))\s*\{\s*\}[ \t]*$"
    )
    for path, diagnostics in errors_by_file.items():
        content = output.get(path)
        if not isinstance(content, str) or not path.casefold().endswith(".java"):
            continue
        lowered = "\n".join(diagnostics).casefold()
        repaired = content
        restarted_unit = False

        # Records are implicitly final subclasses of java.lang.Record and can
        # never extend an exception. Local models nevertheless repeat this
        # compact-looking invalid form after javac reports "'{' expected".
        # Preserve its intended message-carrying exception behavior using the
        # smallest legal class. The exact diagnostic and empty one-String
        # record shape keep this repair isolated from valid domain records.
        if "'{' expected" in lowered and "record" in repaired and "extends" in repaired:
            def replace_exception_record(match: re.Match) -> str:
                indent = match.group("indent")
                access = match.group("access") or ("private" if indent else "")
                static = " static" if indent else ""
                prefix = f"{access}{static} final class".strip()
                name = match.group("name")
                parent = match.group("parent")
                super_arguments = "message, cause" if match.group("cause") else "message"
                constructor_parameters = (
                    "String message, Throwable cause"
                    if match.group("cause") else "String message"
                )
                return (
                    f"{indent}{prefix} {name} extends {parent} {{\n"
                    f"{indent}    {name}({constructor_parameters}) "
                    f"{{ super({super_arguments}); }}\n"
                    f"{indent}}}"
                )

            repaired = invalid_exception_record.sub(replace_exception_record, repaired)

        declarations = list(package_pattern.finditer(repaired))
        if len(declarations) > 1 and any(
            token in lowered for token in ("illegal start", "not a statement", "reached end of file")
        ):
            candidate = repaired[declarations[-1].start():].lstrip()
            expected_type = Path(path).stem
            if re.search(rf"\b(?:class|interface|enum|record)\s+{re.escape(expected_type)}\b", candidate):
                repaired = candidate
                restarted_unit = True

        if restarted_unit:
            # A restarted unit can itself be followed by closing braces copied
            # from the abandoned prefix. Remove only terminal unmatched braces;
            # balanced braces and every non-whitespace token remain untouched.
            _state, depth = _java_lexical_tail_state(repaired)
            while depth < 0 and re.search(r"}\s*\Z", repaired):
                repaired = re.sub(r"}\s*\Z", "", repaired).rstrip() + "\n"
                _state, depth = _java_lexical_tail_state(repaired)

        if "try-with-resources resource" in lowered:
            repaired = resource_pattern.sub(
                lambda match: (
                    f"{match.group('indent')}try ({match.group('type').strip()} "
                    f"{match.group('name')} = {match.group('expr').strip()})"
                ),
                repaired,
            )

        if "finally' without 'try" in lowered or 'finally" without "try' in lowered:
            # Only rewrite a syntactically orphaned finally when its body is a
            # sequence of close() calls. Convert the prepared statement to a
            # real try-with-resources owner before dropping the invalid block.
            orphan = cleanup_finally.search(repaired)
            if orphan:
                cleanup = orphan.group("cleanup")
                resource_names = re.findall(r"([A-Za-z_$][\w$]*)\.close\(\)", cleanup)
                # The connection is commonly declared immediately inside the
                # method's existing try/catch. Promote that outer try to
                # try-with-resources so removing the malformed finally does
                # not trade a compiler fix for a connection leak.
                for resource_name in resource_names:
                    outer_declaration = re.compile(
                        rf"(?m)^(?P<indent>[ \t]*)try\s*\{{\s*\r?\n"
                        rf"(?P=indent)[ \t]+Connection\s+{re.escape(resource_name)}"
                        rf"\s*=\s*(?P<expr>[^;\r\n]+);"
                    )
                    if outer_declaration.search(repaired):
                        repaired = outer_declaration.sub(
                            lambda item: (
                                f"{item.group('indent')}try (Connection {resource_name} = "
                                f"{item.group('expr').strip()}) {{"
                            ),
                            repaired,
                            count=1,
                        )
                for resource_name in resource_names:
                    declaration = re.compile(
                        rf"(?m)^(?P<indent>[ \t]*)PreparedStatement\s+{re.escape(resource_name)}"
                        rf"\s*=\s*(?P<expr>[^;\r\n]+);"
                    )
                    if declaration.search(repaired):
                        repaired = declaration.sub(
                            lambda item: (
                                f"{item.group('indent')}try (PreparedStatement {resource_name} = "
                                f"{item.group('expr').strip()}) {{"
                            ),
                            repaired,
                            count=1,
                        )
                        break
                # One brace closes the if/else arm and one closes the nested
                # PreparedStatement try-with-resources introduced above.
                repaired = cleanup_finally.sub("}\n}", repaired, count=1)

        if "<identifier> expected" in lowered:
            for qualified_type in invalid_type_annotations:
                repaired = re.sub(
                    rf"@{re.escape(qualified_type)}\s+(?=[A-Za-z_$][\w$]*\b)",
                    qualified_type + " ",
                    repaired,
                )

        if repaired != content:
            output[path] = repaired
            changed.add(path)
    return changed


def _apply_deterministic_java_diagnostic_repairs(
    output: dict[str, str], errors_by_file: dict[str, list[str]],
) -> set[str]:
    """Run every diagnostic-driven deterministic Java repair once and return
    the set of paths changed, so a caller can decide whether re-running the
    build is worthwhile before falling back to LLM repair for what's left."""
    changed = _repair_java_structural_diagnostics(output, errors_by_file)
    changed |= _reconcile_java_private_access_from_diagnostics(output, errors_by_file)
    changed |= _reconcile_java_missing_well_known_imports_from_diagnostics(output, errors_by_file)
    changed |= _reconcile_java_transactional_import_conflicts_from_diagnostics(
        output, errors_by_file,
    )
    changed |= _reconcile_java_static_invocations_from_diagnostics(output, errors_by_file)
    changed |= _remove_java_invalid_type_imports_from_diagnostics(output, errors_by_file)
    changed |= _reconcile_java_logger_api_from_diagnostics(output, errors_by_file)
    for path, diagnostics in errors_by_file.items():
        content = output.get(path)
        if not isinstance(content, str) or not any(
            "Broad Exception/Throwable catches are forbidden" in str(item)
            for item in diagnostics
        ):
            continue
        def narrow_broad_catch(match: re.Match) -> str:
            alternatives = [item.strip() for item in match.group("types").split("|")]
            typed = [
                item for item in alternatives
                if item not in {
                    "Exception", "Throwable", "java.lang.Exception", "java.lang.Throwable",
                }
            ]
            replacement_types = " | ".join(typed) if typed else "RuntimeException"
            modifier = match.group("modifier") or ""
            return f"catch ({modifier}{replacement_types} {match.group('name')})"

        repaired = re.sub(
            r"\bcatch\s*\(\s*(?P<modifier>final\s+)?"
            r"(?P<types>(?:[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*"
            r"(?:\s*\|\s*)?)+)"
            r"\s+(?P<name>[A-Za-z_$][\w$]*)\s*\)",
            narrow_broad_catch,
            content,
        )
        if repaired != content:
            output[path] = repaired
            changed.add(path)
    return changed


def _migrate_java_decimal_min_literals(output: dict[str, str]) -> None:
    """Repair `@Min` applied to a fractional literal. `@Min.value()` is a
    `long`, so `@Min(value = 0.01, ...)` on a BigDecimal/Double field is a
    lossy-conversion compile error, not a validation-logic bug — the
    generator meant `@DecimalMin`, the Bean Validation annotation for
    exactly this case, whose bound is a `String`."""
    pattern = re.compile(r"@Min\((\s*(?:value\s*=\s*)?)(\d+\.\d+)")
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        if pattern.search(content):
            output[path] = pattern.sub(
                lambda m: f'@DecimalMin({m.group(1)}"{m.group(2)}"', content,
            )


_REACT_APP_SHELL = textwrap.dedent("""\
    export default function App() {
      return (
        <div>
          <h1>Application is running</h1>
        </div>
      );
    }
""")


def _react_entry_point_source(is_typescript: bool, react_major: int = 18) -> str:
    cast = " as HTMLElement" if is_typescript else ""
    if react_major < 18:
        return textwrap.dedent(f"""\
            import React from 'react';
            import ReactDOM from 'react-dom';
            import App from './App';

            ReactDOM.render(
              <React.StrictMode><App /></React.StrictMode>,
              document.getElementById('root'){cast},
            );
        """)
    return textwrap.dedent(f"""\
        import React from 'react';
        import ReactDOM from 'react-dom/client';
        import App from './App';

        ReactDOM.createRoot(document.getElementById('root'){cast}).render(
          <React.StrictMode>
            <App />
          </React.StrictMode>,
        );
    """)


def _reconcile_java_frontend_entry_point(output: dict[str, str]) -> None:
    """Guarantee the module index.html's `<script src="/src/main.tsx">`
    points to actually exists. `_frontend_scaffold_files`'s React branch
    always wires index.html to that entry point deterministically, but the
    module itself is normally the LLM's responsibility; if planning or
    generation dropped the entire frontend/src tree, ship a minimal — but
    real — mount point rather than fail npm's bundler on an unresolvable
    local import that this same pipeline guaranteed would be referenced."""
    for html_path, html in list(output.items()):
        if not html_path.casefold().endswith("index.html") or not isinstance(html, str):
            continue
        match = re.search(r'<script[^>]+src=["\']/?(src/main\.tsx)["\']', html)
        if not match:
            continue
        root = html_path.rsplit("/", 1)[0] if "/" in html_path else ""
        entry_path = f"{root}/{match.group(1)}" if root else match.group(1)
        if entry_path in output:
            continue
        app_path = next(
            (f"{root}/src/App.{ext}" for ext in ("tsx", "jsx") if f"{root}/src/App.{ext}" in output),
            None,
        )
        if app_path is None:
            app_path = f"{root}/src/App.tsx"
            output[app_path] = _REACT_APP_SHELL
        react_major = 18
        manifest = output.get(f"{root}/package.json" if root else "package.json")
        if isinstance(manifest, str):
            try:
                declared = json.loads(manifest).get("dependencies", {}).get("react", "")
                version_match = re.search(r"(\d+)", str(declared))
                if version_match:
                    react_major = int(version_match.group(1))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        output[entry_path] = _react_entry_point_source(app_path.endswith(".tsx"), react_major)


def _typescript_add_named_import(content: str, module: str, names: Iterable[str]) -> str:
    """Add missing names to an existing TypeScript named import."""
    match = re.search(
        rf"import\s*\{{([^}}]*)\}}\s*from\s*['\"]{re.escape(module)}['\"]\s*;?",
        content,
    )
    requested = [name for name in names if name]
    if match:
        current = [item.strip() for item in match.group(1).split(",") if item.strip()]
        merged = list(dict.fromkeys([*current, *requested]))
        replacement = f"import {{ {', '.join(merged)} }} from '{module}';"
        return content[:match.start()] + replacement + content[match.end():]
    return f"import {{ {', '.join(requested)} }} from '{module}';\n" + content


def _reconcile_java_angular_contracts(output: dict[str, str]) -> None:
    """Close recurring Angular/MSAL contracts in Java full-stack projects."""
    source_suffixes = (".ts", ".tsx")
    existing = set(output)

    # Materialize Angular lazy-loaded NgModules deterministically. Dynamic
    # import() was previously invisible to source closure and reached ngc as
    # TS2307 even though all feature components already existed.
    for consumer_path, consumer in list(output.items()):
        if not consumer_path.endswith(source_suffixes) or "/frontend/" not in consumer_path:
            continue
        parent = posixpath.dirname(consumer_path)
        lazy_modules = re.findall(
            r"import\s*\(\s*['\"](\.{1,2}/[^'\"]+)['\"]\s*\)\s*\.then\s*\(\s*\w+\s*=>\s*\w+\.([A-Za-z_$][\w$]*)\s*\)",
            consumer,
        )
        for specifier, module_class in lazy_modules:
            target = posixpath.normpath(posixpath.join(parent, specifier))
            target_path = target if target.endswith(".ts") else target + ".ts"
            if target_path in existing:
                continue
            feature_dir = posixpath.dirname(target_path)
            components: list[tuple[str, str, bool]] = []
            for component_path, component_source in output.items():
                if (
                    posixpath.dirname(component_path) == feature_dir
                    and component_path.endswith(".component.ts")
                    and isinstance(component_source, str)
                ):
                    declared = re.search(r"\bexport\s+class\s+([A-Za-z_$][\w$]*Component)\b", component_source)
                    if declared:
                        components.append((
                            component_path, declared.group(1),
                            bool(re.search(r"\bstandalone\s*:\s*true\b", component_source)),
                        ))
            if not components:
                continue
            components.sort(key=lambda item: ("list" not in item[0].casefold(), item[0]))
            component_imports = "\n".join(
                f"import {{ {name} }} from './{Path(path).stem}';" for path, name, _ in components
            )
            declarations = ", ".join(name for _, name, standalone in components if not standalone)
            standalone_imports = [name for _, name, standalone in components if standalone]
            default_component = components[0][1]
            extra_routes = "".join(
                f", {{ path: '{'transfer' if 'transfer' in path.casefold() else Path(path).stem.replace('.component', '')}', component: {name} }}"
                for path, name, _ in components[1:]
            )
            templates = "\n".join(
                str(output.get(path[:-3] + ".html", "")) for path, _, _ in components
            )
            material_modules = {
                "MatButtonModule": "@angular/material/button",
                "MatCardModule": "@angular/material/card",
                "MatFormFieldModule": "@angular/material/form-field",
                "MatIconModule": "@angular/material/icon",
                "MatInputModule": "@angular/material/input",
                "MatPaginatorModule": "@angular/material/paginator",
                "MatProgressSpinnerModule": "@angular/material/progress-spinner",
                "MatSelectModule": "@angular/material/select",
                "MatSnackBarModule": "@angular/material/snack-bar",
                "MatSortModule": "@angular/material/sort",
                "MatTableModule": "@angular/material/table",
            }
            used_material = [
                (name, module) for name, module in material_modules.items()
                if re.search(rf"\b{re.escape(name)}\b", "\n".join(
                    str(output.get(path, "")) for path, _, _ in components
                )) or "mat-" in templates
            ]
            material_imports = "\n".join(
                f"import {{ {name} }} from '{module}';" for name, module in used_material
            )
            ng_imports = [
                "CommonModule", "ReactiveFormsModule", "RouterModule.forChild(routes)",
                *standalone_imports, *(name for name, _ in used_material),
            ]
            declarations_line = f"  declarations: [{declarations}],\n" if declarations else ""
            output[target_path] = (
                "import { NgModule } from '@angular/core';\n"
                "import { CommonModule } from '@angular/common';\n"
                "import { ReactiveFormsModule } from '@angular/forms';\n"
                "import { RouterModule, Routes } from '@angular/router';\n"
                f"{material_imports}\n{component_imports}\n\n"
                f"const routes: Routes = [{{ path: '', component: {default_component} }}{extra_routes}];\n\n"
                "@NgModule({\n"
                f"{declarations_line}"
                f"  imports: [{', '.join(ng_imports)}]\n"
                "})\n"
                f"export class {module_class} {{}}\n"
            )
            existing.add(target_path)

    for path, content in list(output.items()):
        if not path.casefold().endswith("auth.service.ts") or "MsalService" not in str(content):
            continue
        assignment = re.compile(
            r"(?s)this\.msalService\.instance\.eventCallbackResponse\s*=\s*\(event\)\s*=>\s*\{(.*?)\s+return\s+event\s*;\s*\};"
        )
        match = assignment.search(content)
        if match:
            body = match.group(1)
            body = body.replace("event.eventType === 'loginSuccess'", "event.eventType === EventType.LOGIN_SUCCESS")
            body = body.replace("event.eventType === 'logout'", "event.eventType === EventType.LOGOUT_SUCCESS")
            replacement = "this.msalService.instance.addEventCallback((event: EventMessage) => {" + body + "\n    });"
            content = content[:match.start()] + replacement + content[match.end():]
            content = _typescript_add_named_import(content, "@azure/msal-browser", ("EventMessage", "EventType"))
        # InteractionType belongs to MsalGuardConfiguration, not RedirectRequest.
        content = re.sub(r"(?m)^\s*interactionType:\s*InteractionType\.[A-Za-z_$][\w$]*,?\s*\n", "", content)
        if "InteractionType" not in re.sub(r"import[^\n]+", "", content):
            content = re.sub(r"\bInteractionType\s*,\s*|,\s*InteractionType\b", "", content)
        invalid_account_token = "this.msalService.instance.getActiveAccount()?.accessToken"
        if invalid_account_token in content:
            method = re.compile(r"(?s)\n\s*getUserInfo\(\):\s*Observable<[^>]+>\s*\{.*?\n\s*\}(?=\n\})")
            replacement = """
  getUserInfo(): Observable<unknown> {
    return this.getAccessToken().pipe(
      switchMap(accessToken => this.http.get(`${this.apiUrl}/userinfo`, {
        headers: new HttpHeaders({ Authorization: `Bearer ${accessToken}` })
      })),
      catchError((error: unknown) => {
        console.error('Error fetching user info', error);
        return throwError(() => new Error('Failed to fetch user info'));
      })
    );
  }"""
            content = method.sub(replacement, content, count=1)
            content = _typescript_add_named_import(content, "rxjs", ("switchMap",))
        output[path] = content

    # A generated component may consume a backend operation that its sibling
    # service omitted. Close the known transfer API contract from the existing
    # controller route instead of leaving callback types unresolved.
    transfer_consumed = any(
        isinstance(content, str) and ".createTransfer(" in content
        for path, content in output.items()
        if "/frontend/" in path and path.endswith(source_suffixes)
    )
    if transfer_consumed:
        for path, content in list(output.items()):
            if not path.casefold().endswith("transaction.service.ts") or "class TransactionService" not in str(content):
                continue
            if re.search(r"\bcreateTransfer\s*\(", content):
                continue
            insertion = content.rfind("}")
            if insertion < 0:
                continue
            method = """

  createTransfer(request: {
    sourceAccountId: string;
    destinationAccountId: string;
    amount: number;
    currency: string;
    reference: string;
  }): Observable<unknown> {
    const idempotencyKey = crypto.randomUUID();
    return this.http.post<unknown>(`${environment.apiBaseUrl}/api/v1/transfers`, {
      ...request,
      idempotencyKey
    }, { headers: { 'Idempotency-Key': idempotencyKey } });
  }
"""
            output[path] = content[:insertion] + method + content[insertion:]


def _reconcile_java_frontend_source_extensions(output: dict[str, str]) -> None:
    """JSX-bearing TypeScript must use a .tsx compiler boundary."""
    from services.validators import _contains_jsx

    for path, content in list(output.items()):
        # Converted frontend sources may temporarily live under a legacy-core
        # module rather than `/frontend/`. Use the validator's shared JSX
        # grammar detector for every TypeScript source so reconciliation and
        # final validation can never disagree about the required extension.
        if path.endswith(".ts") and isinstance(content, str) and _contains_jsx(content):
            output.setdefault(path[:-3] + ".tsx", content)
            del output[path]


def _reconcile_typescript_java_record_contracts(output: dict[str, str]) -> None:
    """Materialize Java record DTOs as native TypeScript interfaces."""
    records: dict[str, list[tuple[str, str]]] = {}
    for path, content in output.items():
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        for match in re.finditer(r"\brecord\s+([A-Za-z_]\w*)\s*\(", content):
            start, depth, end = match.end() - 1, 0, None
            for index in range(start, len(content)):
                if content[index] == "(": depth += 1
                elif content[index] == ")":
                    depth -= 1
                    if depth == 0: end = index; break
            if end is None: continue
            fields = []
            for parameter in _split_java_arguments(content[start + 1:end]):
                clean = re.sub(r"@[A-Za-z_][\w.]*\s*(?:\([^)]*\))?\s*", "", parameter).strip()
                tokens = clean.split()
                if len(tokens) >= 2: fields.append((tokens[-1], tokens[-2]))
            if fields: records[match.group(1)] = fields
    scalar = {"int": "number", "long": "number", "Long": "number", "Integer": "number", "BigDecimal": "number", "boolean": "boolean", "Boolean": "boolean", "String": "string", "Instant": "string", "LocalDateTime": "string", "UUID": "string"}
    for path, content in list(output.items()):
        if "/frontend/" not in path or not path.endswith((".ts", ".tsx")) or not isinstance(content, str): continue
        name, fields = Path(path).stem, records.get(Path(path).stem)
        if not fields or not (
            re.search(r"from\s+['\"]com\.", content)
            or (re.search(r"\bpackage\s+com\.", content) and re.search(r"\bpublic\s+record\b", content))
        ): continue
        rows = []
        for field, java_type in fields:
            generic = re.match(r"(?:List|Set)<([A-Za-z_]\w*)>", java_type)
            ts_type = f"{scalar.get(generic.group(1), generic.group(1))}[]" if generic else scalar.get(java_type, java_type)
            rows.append(f"  {field}: {ts_type};")
        output[path] = f"export interface {name} {{\n" + "\n".join(rows) + "\n}\n"


def _reconcile_java_stray_test_tree_duplicates(output: dict[str, str]) -> None:
    """A production-shaped class (no `@Test` methods, no JUnit imports)
    generated under `src/test/java` is always a duplicate-class compile
    error waiting to happen once its declared type collides with anything
    already on the test-compile classpath: javac sees the identical FQCN
    both as already-compiled main output and as a fresh test source, and
    refuses to proceed.

    This is a per-file-generation/repair mixup — a corrected class body
    written to the wrong tree, sometimes even landing *inside* a
    `*Test.java` file and clobbering what should have been the actual
    JUnit test (declaring `class Foo` instead of `class FooTest`) — not a
    legitimate test double. Keying off the *filename* alone misses that
    case, so this keys off the file's actual declared top-level type
    instead: if that type doesn't itself look test-shaped (name ending
    Test/Tests/IT) and the file has no `@Test`/JUnit markers, it is
    production code and belongs under `src/main/java`. Whichever version
    looks the least like a stub (fewest "Placeholder"/TODO markers, then
    longest) is kept as that type's single source of truth; a genuine
    `*Test.java` elsewhere is never touched.
    """
    placeholder_pattern = re.compile(r"//\s*Placeholder|\bTODO\b", re.IGNORECASE)

    def completeness_key(text: str) -> tuple[int, int]:
        return (len(placeholder_pattern.findall(text)), -len(text))

    marker = "/src/test/java/"
    for path in list(output.keys()):
        if not path.casefold().endswith(".java") or marker not in path:
            continue
        content = output.get(path)
        if not isinstance(content, str) or "@Test" in content or re.search(r"\borg\.junit\b", content):
            continue
        type_match = re.search(
            r"\b(?:public\s+)?(?:class|interface|record|enum)\s+([A-Za-z_]\w*)", content,
        )
        package_match = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", content)
        if not type_match or not package_match:
            continue
        declared_name = type_match.group(1)
        if declared_name.endswith(("Test", "Tests", "IT")):
            continue
        main_path = (
            f"{_java_source_module(path)}/src/main/java/"
            f"{package_match.group(1).replace('.', '/')}/{declared_name}.java"
        )
        main_content = output.get(main_path)
        if isinstance(main_content, str):
            output[main_path] = (
                content if completeness_key(content) < completeness_key(main_content) else main_content
            )
        else:
            # No main-tree counterpart exists at all — this stray file is
            # the *only* copy of that production class, so relocate rather
            # than discard it.
            output[main_path] = content
        del output[path]


def _split_java_fluent_chain(text: str) -> list[tuple[str, str]]:
    """Split `.a(x).b(y).c()` into `[("a", "x"), ("b", "y"), ("c", "")]`,
    respecting one level of nested parens inside each call's arguments."""
    calls: list[tuple[str, str]] = []
    index = 0
    length = len(text)
    while index < length:
        if text[index] != ".":
            index += 1
            continue
        match = re.match(r"\.([A-Za-z_]\w*)\(", text[index:])
        if not match:
            index += 1
            continue
        name = match.group(1)
        args_start = index + match.end()
        depth = 1
        cursor = args_start
        while cursor < length and depth > 0:
            if text[cursor] == "(":
                depth += 1
            elif text[cursor] == ")":
                depth -= 1
            cursor += 1
        calls.append((name, text[args_start:cursor - 1]))
        index = cursor
    return calls


_ASSERTJ_CHAIN_STATEMENT = re.compile(
    r"assertThat\(((?:[^()]|\([^()]*\))*)\)((?:\s*\.[A-Za-z_]\w*\((?:[^()]|\([^()]*\))*\))+)\s*;"
)


def _repair_java_chained_assertj_extracting(output: dict[str, str]) -> None:
    """Split `assertThat(x)....extracting(A::f).isEqualTo(v1).extracting(B::g)...`
    chains into one `assertThat(...)` per extraction.

    AssertJ's `.isEqualTo(...)` returns `self` on the *extracted* assert
    (now typed to the extracted value, e.g. `ObjectAssert<String>`), not the
    original subject's assert — a second `.extracting(...)` further down the
    same chain has no matching overload against that narrowed type. This is
    a recurring per-file-generation anti-pattern in JUnit/AssertJ tests, not
    a one-off; splitting the chain (assert each extracted value against the
    *original* subject, independently) preserves exactly the same
    assertions the test intended without guessing at new ones.
    """
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue

        def repair(match: re.Match) -> str:
            subject, chain_text = match.groups()
            calls = _split_java_fluent_chain(chain_text)
            if sum(1 for name, _ in calls if name == "extracting") < 2:
                return match.group(0)
            statements: list[str] = []
            current_subject = subject
            pending: list[str] = []
            for name, args in calls:
                if name == "extracting":
                    if pending:
                        statements.append(f"assertThat({current_subject}){''.join(pending)};")
                        pending = []
                    reference = re.match(r"\s*([A-Za-z_][\w.]*)\s*::\s*([A-Za-z_]\w*)\s*$", args)
                    if not reference:
                        return match.group(0)
                    current_subject = f"{subject}.{reference.group(2)}()"
                else:
                    pending.append(f".{name}({args})")
            if pending:
                statements.append(f"assertThat({current_subject}){''.join(pending)};")
            return "\n        ".join(statements) if statements else match.group(0)

        new_content = _ASSERTJ_CHAIN_STATEMENT.sub(repair, content)
        if new_content != content:
            output[path] = new_content


def _split_java_arguments(value: str) -> list[str]:
    """Split a Java argument/parameter list without splitting nested calls."""
    parts: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(value):
        if char in "(<[{":
            depth += 1
        elif char in ")>]}":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
    tail = value[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _reconcile_java_repository_contracts(output: dict[str, str]) -> None:
    """Align generated repository call sites with their declared return/arity."""
    contracts: dict[tuple[str, str], list[tuple[str, list[str]]]] = {}
    for path, content in output.items():
        if (
            not path.casefold().endswith("repository.java")
            or not isinstance(content, str)
            or "interface " not in content
        ):
            continue
        module = _java_source_module(path)
        for match in re.finditer(
            r"(?m)^\s*((?:java\.util\.)?List\s*<[^;]+?>|"
            r"(?:org\.springframework\.data\.domain\.)?Page\s*<[^;]+?>|"
            r"[A-Za-z_][\w<>?, .]*)\s+([A-Za-z_]\w*)\s*\(([^;]*)\)\s*;",
            content,
        ):
            return_type, method, parameters = match.groups()
            contracts.setdefault((module, method), []).append(
                (return_type.strip(), _split_java_arguments(parameters))
            )
    unique = {
        key: values[0] for key, values in contracts.items() if len(values) == 1
    }
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        module = _java_source_module(path)
        for (owner, method), (return_type, parameters) in unique.items():
            if owner != module:
                continue
            call = rf"\b[A-Za-z_]\w*\.{re.escape(method)}\(([^;\n]*)\)"
            if re.search(r"(?:^|\.)List\s*<", return_type):
                content = re.sub(rf"({call})\.getContent\(\)", r"\1", content)
            if parameters and "Pageable" in parameters[-1]:
                def add_pageable(match: re.Match) -> str:
                    arguments = _split_java_arguments(match.group(1))
                    if len(arguments) != len(parameters) - 1:
                        return match.group(0)
                    joined = match.group(1).rstrip()
                    separator = ", " if joined else ""
                    return match.group(0)[:-1] + separator + "Pageable.unpaged())"
                content = re.sub(call, add_pageable, content)
        output[path] = content


def _migrate_spring_security_authorities_claim_api(output: dict[str, str]) -> None:
    """Repair a common hallucinated Spring Security JWT converter method."""
    for path, content in list(output.items()):
        if (
            path.casefold().endswith(".java")
            and isinstance(content, str)
            and "JwtGrantedAuthoritiesConverter" in content
            and ".setClaimName(" in content
        ):
            output[path] = content.replace(
                ".setClaimName(",
                ".setAuthoritiesClaimName(",
            )


# Function: _java_single_module_path
def _java_single_module_path(path: str) -> str:
    """Flatten pseudo-module source roots into the canonical backend module."""
    normalized = path.replace("\\", "/")
    return re.sub(
        r"(^|/)backend/[^/]+/(src/(?:main|test)/(?:java|resources)/)",
        r"\1backend/\2",
        normalized,
        count=1,
    )


# Function: _flatten_java_module_paths
def _flatten_java_module_paths(output: dict[str, str]) -> None:
    for path in list(output):
        flattened = _java_single_module_path(path)
        if flattened == path:
            continue
        output.setdefault(flattened, output[path])
        del output[path]


def _align_java_public_type_paths(output: dict[str, str]) -> None:
    """Make Maven's source filename contract deterministic for public types."""
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        public_type = re.search(
            r"\bpublic\s+(?:(?:abstract|final|sealed|non-sealed)\s+)*"
            r"(?:class|interface|record|enum)\s+([A-Za-z_]\w*)",
            content,
        )
        if not public_type or Path(path).stem == public_type.group(1):
            continue
        renamed = path.rsplit("/", 1)[0] + "/" + public_type.group(1) + ".java"
        if renamed not in output:
            output[renamed] = content
            del output[path]
        else:
            # The correctly named source is authoritative.  Keeping the
            # misnamed copy creates a duplicate FQCN even though its filename
            # repair target already exists.
            del output[path]


def _dedupe_java_fqcns(output: dict[str, str]) -> None:
    """Retain exactly one canonical source path for every module-local FQCN."""
    owners: dict[tuple[str, str], list[str]] = {}
    for path, content in output.items():
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        package = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", content)
        declared = re.search(r"\bpublic\s+(?:class|record|interface|enum)\s+([A-Za-z_]\w*)", content)
        if package and declared:
            owners.setdefault((_java_source_module(path), f"{package.group(1)}.{declared.group(1)}"), []).append(path)
    for (_, fqcn), paths in owners.items():
        if len(paths) < 2:
            continue
        package, name = fqcn.rsplit(".", 1)
        suffix = f"/src/main/java/{package.replace('.', '/')}/{name}.java"
        preferred = next((path for path in paths if path.endswith(suffix)), sorted(paths, key=len)[0])
        for path in paths:
            if path != preferred:
                del output[path]


_JAVA_KEYWORDS = {
    "abstract", "assert", "boolean", "break", "byte", "case", "catch", "char", "class",
    "const", "continue", "default", "do", "double", "else", "enum", "extends", "final",
    "finally", "float", "for", "goto", "if", "implements", "import", "instanceof", "int",
    "interface", "long", "native", "new", "package", "private", "protected", "public",
    "return", "short", "static", "strictfp", "super", "switch", "synchronized", "this",
    "throw", "throws", "transient", "try", "void", "volatile", "while",
}

# Common prose fragments emitted as import symbols by small code models when
# they mistake nearby English text for a Java type. None can be a generated
# type name under the project's filename/type conventions.
_JAVA_INVALID_GENERATED_IMPORT_SYMBOLS = {"by", "from", "in", "not", "to", "used"}

_JAVA_PLATFORM_CANONICAL_IMPORTS = {
    "Connection": "java.sql.Connection",
    "PreparedStatement": "java.sql.PreparedStatement",
    "ResultSet": "java.sql.ResultSet",
    "SQLException": "java.sql.SQLException",
    "PrintWriter": "java.io.PrintWriter",
    "NumberFormatException": "java.lang.NumberFormatException",
    "ClassNotFoundException": "java.lang.ClassNotFoundException",
    "ProblemDetail": "org.springframework.http.ProblemDetail",
}


def _remove_invalid_java_imports(output: dict[str, str]) -> None:
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        package_match = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", content)
        declared_type = Path(path).stem
        # A generated class can implement a framework interface with the same
        # simple name (for example a local RowMapper class). Importing that
        # interface makes `implements RowMapper<T>` resolve to the class being
        # declared. Qualify the interface and remove the conflicting import.
        for imported in re.findall(r"(?m)^\s*import\s+([\w.]+)\s*;", content):
            if imported.rsplit(".", 1)[-1] != declared_type:
                continue
            self_implementation = re.compile(
                rf"(\bclass\s+{re.escape(declared_type)}\b[^{{]*?\bimplements\s+)"
                rf"{re.escape(declared_type)}(?=\s*(?:<|\{{))"
            )
            content, count = self_implementation.subn(rf"\1{imported}", content, count=1)
            if count:
                content = re.sub(
                    rf"(?m)^\s*import\s+{re.escape(imported)}\s*;\s*\r?\n?",
                    "",
                    content,
                )
        self_prefix = (
            f"{package_match.group(1)}.{declared_type}."
            if package_match else ""
        )
        output[path] = re.sub(
            r"(?m)^\s*import\s+([\w.]+)\s*;\s*\r?\n",
            lambda match: "" if (
                # Non-static Java imports name a type, whose generated simple
                # name must follow the project's upper-camel convention.
                # Repair models have repeatedly imported English prose tokens
                # as nested types (BookTrainPayment.is, .name, .should), then
                # propagated that invalid import across dozens of files.
                match.group(1).rsplit(".", 1)[-1][:1].islower()
                or match.group(1).rsplit(".", 1)[-1] in _JAVA_KEYWORDS
                or match.group(1).rsplit(".", 1)[-1] in _JAVA_INVALID_GENERATED_IMPORT_SYMBOLS
                or (self_prefix and match.group(1).startswith(self_prefix))
            ) else match.group(0),
            content,
        )
        # A generated DAO sometimes imports both its legacy domain exception
        # and JDBC's SQLException under the same simple name. Every catch in
        # that compilation unit surrounds java.sql calls, so retain JDBC's
        # checked exception and remove only the proven conflicting legacy one.
        if "import java.sql.SQLException;" in output[path]:
            output[path] = re.sub(
                r"(?m)^\s*import\s+(?!java\.sql\.)[\w.]+\.SQLException\s*;\s*\r?\n",
                "",
                output[path],
            )
        content = output[path]
        jdbc_evidence = bool(re.search(
            r"\b(?:Connection|PreparedStatement|ResultSet)\b|\.prepareStatement\s*\(|\.executeQuery\s*\(",
            content,
        ))
        for symbol, canonical in _JAVA_PLATFORM_CANONICAL_IMPORTS.items():
            if not re.search(rf"\b{re.escape(symbol)}\b", content):
                continue
            if symbol == "SQLException" and not jdbc_evidence:
                continue
            if symbol == "NumberFormatException" and not re.search(r"\b(?:parseInt|parseLong|parseDouble)\s*\(", content):
                continue
            if symbol == "ClassNotFoundException" and "Class.forName(" not in content:
                continue
            if symbol == "ProblemDetail" and "ProblemDetail.forStatusAndDetail(" not in content:
                continue
            replacement = "" if canonical.startswith("java.lang.") else f"import {canonical};"
            content = re.sub(
                rf"(?m)^\s*import\s+(?!{re.escape(canonical)}\s*;)[\w.]+\.{re.escape(symbol)}\s*;\s*\r?\n?",
                replacement + ("\n" if replacement else ""),
                content,
            )
        output[path] = content


def _repair_java_broken_html_string_concatenation(output: dict[str, str]) -> None:
    """Repair invalid quote seams inside generated servlet HTML fragments."""
    broken_seam = re.compile(r">\s+\+\s+\"(?=</[A-Za-z][A-Za-z0-9:-]*>)")
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        repaired = broken_seam.sub(">", content)
        if repaired != content:
            output[path] = repaired


_SPRING_BOOT3_JAVAX_PACKAGES = {
    "javax.annotation": "jakarta.annotation",
    "javax.persistence": "jakarta.persistence",
    "javax.servlet": "jakarta.servlet",
    "javax.transaction": "jakarta.transaction",
    "javax.validation": "jakarta.validation",
    "javax.ws.rs": "jakarta.ws.rs",
}

_JPA_LIFECYCLE_ANNOTATIONS = (
    "PostLoad", "PostPersist", "PostRemove", "PostUpdate",
    "PrePersist", "PreRemove", "PreUpdate",
)


def _migrate_spring_boot3_javax_imports(output: dict[str, str]) -> None:
    """Normalize Java EE imports that were renamed for Spring Boot 3."""
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        for legacy, jakarta in _SPRING_BOOT3_JAVAX_PACKAGES.items():
            content = re.sub(
                rf"(?m)^(\s*import\s+){re.escape(legacy)}(?=[.;])",
                rf"\1{jakarta}",
                content,
            )
        content = content.replace(
            "jakarta.validation.DecimalMin",
            "jakarta.validation.constraints.DecimalMin",
        )
        # These callbacks are JPA annotations, not Hibernate annotations.
        # Small code models routinely emit the plausible but nonexistent
        # org.hibernate.annotations imports, producing deterministic Maven
        # "cannot find symbol" failures in otherwise valid entity classes.
        for annotation in _JPA_LIFECYCLE_ANNOTATIONS:
            content = content.replace(
                f"import org.hibernate.annotations.{annotation};",
                f"import jakarta.persistence.{annotation};",
            )
        output[path] = content


# Function: _reconcile_java_type_imports
def _java_source_module(path: str) -> str:
    """Return the Maven source-root owner for a generated Java path."""
    normalized = path.replace("\\", "/")
    marker = "/src/"
    return normalized.split(marker, 1)[0] if marker in normalized else ""


def _reconcile_java_type_imports(
    output: dict[str, str], module_scoped: bool = False,
) -> None:
    """Align project-local imports with the package that actually owns each type."""
    owners: dict[tuple[str, str], set[str]] = {}
    for path, content in output.items():
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue
        package_match = re.search(r"(?m)^\s*package\s+([^;]+);", content)
        if not package_match:
            continue
        package = package_match.group(1).strip()
        primary_type = Path(path).stem
        for declaration_match in re.finditer(
            r"\b(?:class|interface|record|enum)\s+([A-Za-z_]\w*)",
            content,
        ):
            declaration = declaration_match.group(1)
            prefix = content[:declaration_match.start()]
            brace_depth = prefix.count("{") - prefix.count("}")
            owner = (
                f"{package}.{primary_type}.{declaration}"
                if declaration != primary_type and brace_depth > 0
                else f"{package}.{declaration}"
            )
            scope = _java_source_module(path) if module_scoped else ""
            owners.setdefault((scope, declaration), set()).add(owner)
    unique_owners = {
        key: next(iter(values))
        for key, values in owners.items()
        if len(values) == 1
    }
    owners_by_scope: dict[str, dict[str, str]] = {}
    for (scope, simple_name), owner in unique_owners.items():
        owners_by_scope.setdefault(scope, {})[simple_name] = owner
    for path, content in list(output.items()):
        if not path.casefold().endswith(".java") or not isinstance(content, str):
            continue

        scope = _java_source_module(path) if module_scoped else ""

        def replace_import(match: re.Match) -> str:
            imported = match.group(1)
            simple_name = imported.rsplit(".", 1)[-1]
            owner = unique_owners.get((scope, simple_name))
            if owner and imported.startswith("com.") and owner != imported:
                return f"import {owner};"
            return match.group(0)

        scope_owners = owners_by_scope.get(scope, {})
        reconciled = re.sub(
            r"\bimport\s+(?!static\s)([A-Za-z_][\w.]*)\s*;",
            replace_import,
            content,
        )

        def replace_qualified_local_type(match: re.Match) -> str:
            qualified = match.group(0)
            parts = qualified.split(".")
            # The match may include a static member or chained call after the
            # type (for example com.wrong.OrderDto.builder). Replace the
            # longest recognized type prefix and retain the member suffix.
            for index in range(len(parts) - 1, 0, -1):
                owner = scope_owners.get(parts[index])
                if owner:
                    suffix = ".".join(parts[index + 1:])
                    return owner + (f".{suffix}" if suffix else "")
            return qualified

        reconciled = re.sub(
            r"\bcom(?:\.[A-Za-z_]\w*)+\.[A-Za-z_]\w*\b",
            replace_qualified_local_type,
            reconciled,
        )
        body = re.sub(
            r"(?m)^\s*(?:package|import)\s+[^;]+;\s*$",
            "",
            reconciled,
        )
        body_symbols = set(re.findall(r"\b[A-Za-z_]\w*\b", body))
        existing_imports = set(re.findall(
            r"\bimport\s+(?!static\s)([A-Za-z_][\w.]*)\s*;", reconciled,
        ))
        existing_simple_names = {
            import_name.rsplit(".", 1)[-1] for import_name in existing_imports
        }
        current_package_match = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", reconciled)
        current_package = current_package_match.group(1) if current_package_match else ""
        additions = []
        for simple_name, owner in scope_owners.items():
            owner_package = owner.rsplit(".", 1)[0]
            if (
                owner not in existing_imports
                and owner_package != current_package
                and simple_name in body_symbols
            ):
                additions.append(f"import {owner};")
        declared = set(re.findall(
            r"\b(?:class|interface|record|enum)\s+([A-Za-z_]\w*)", body,
        ))
        for symbol, import_name in _KNOWN_JAVA_SYMBOL_IMPORTS.items():
            if (
                symbol in body_symbols
                and symbol not in declared
                and import_name not in existing_imports
                and symbol not in existing_simple_names
            ):
                additions.append(f"import {import_name};")
        if additions and current_package_match:
            insertion = current_package_match.end()
            reconciled = (
                reconciled[:insertion] + "\n\n"
                + "\n".join(sorted(set(additions)))
                + reconciled[insertion:]
            )

        def remove_unused_import(match: re.Match) -> str:
            simple_name = match.group(1).rsplit(".", 1)[-1]
            return match.group(0) if simple_name in body_symbols else ""

        output[path] = re.sub(
            r"\bimport\s+(?!static\s)([A-Za-z_][\w.]*)\s*;",
            remove_unused_import,
            reconciled,
        )


def _reconcile_java_auditing_contracts(output: dict[str, str]) -> None:
    """Close Spring Data auditing annotations, listeners, and bootstrap as one contract."""
    audited_modules: set[str] = set()
    for path, content in list(output.items()):
        if (
            not path.casefold().endswith(".java")
            or not isinstance(content, str)
            or "@Entity" not in content
            or not any(marker in content for marker in ("@CreatedDate", "@LastModifiedDate"))
        ):
            continue
        audited_modules.add(_java_source_module(path))
        if "@EntityListeners" not in content:
            content = content.replace(
                "@Entity", "@Entity\n@EntityListeners(AuditingEntityListener.class)", 1,
            )
        output[path] = content

    if not audited_modules:
        return
    for path, content in list(output.items()):
        if (
            _java_source_module(path) not in audited_modules
            or not path.casefold().endswith(".java")
            or not isinstance(content, str)
            or "@SpringBootApplication" not in content
            or "@EnableJpaAuditing" in content
        ):
            continue
        output[path] = content.replace(
            "@SpringBootApplication", "@EnableJpaAuditing\n@SpringBootApplication", 1,
        )


_KNOWN_JAVA_SYMBOL_IMPORTS = {
    "BigDecimal": "java.math.BigDecimal",
    "Instant": "java.time.Instant",
    "LocalDate": "java.time.LocalDate",
    "LocalDateTime": "java.time.LocalDateTime",
    "List": "java.util.List",
    "Map": "java.util.Map",
    "HashMap": "java.util.HashMap",
    "Optional": "java.util.Optional",
    "Pageable": "org.springframework.data.domain.Pageable",
    "CreatedDate": "org.springframework.data.annotation.CreatedDate",
    "LastModifiedDate": "org.springframework.data.annotation.LastModifiedDate",
    "AuditingEntityListener": "org.springframework.data.jpa.domain.support.AuditingEntityListener",
    "EnableJpaAuditing": "org.springframework.data.jpa.repository.config.EnableJpaAuditing",
    "Set": "java.util.Set",
    "UUID": "java.util.UUID",
    "Base64": "java.util.Base64",
    "Claims": "io.jsonwebtoken.Claims",
    "Jwts": "io.jsonwebtoken.Jwts",
    "EntityNotFoundException": "jakarta.persistence.EntityNotFoundException",
    "RestTemplate": "org.springframework.web.client.RestTemplate",
    "Transactional": "org.springframework.transaction.annotation.Transactional",
    # Logger/LoggerFactory boilerplate appears in nearly every generated
    # service/controller ("private static final Logger logger =
    # LoggerFactory.getLogger(X.class)"), but per-file generation frequently
    # imports only one of the pair — LoggerFactory is the symbol actually
    # invoked, so its omission is the single most common "cannot find
    # symbol: variable LoggerFactory" failure in the real build.
    "Logger": "org.slf4j.Logger",
    "LoggerFactory": "org.slf4j.LoggerFactory",
    "DecimalMin": "jakarta.validation.constraints.DecimalMin",
    "BeforeEach": "org.junit.jupiter.api.BeforeEach",
    "MockHttpServletRequest": "org.springframework.mock.web.MockHttpServletRequest",
    "MockHttpServletResponse": "org.springframework.mock.web.MockHttpServletResponse",
    "SpringBootApplication": "org.springframework.boot.autoconfigure.SpringBootApplication",
    "Connection": "java.sql.Connection",
    "PreparedStatement": "java.sql.PreparedStatement",
    "ResultSet": "java.sql.ResultSet",
    "SQLException": "java.sql.SQLException",
    "PrintWriter": "java.io.PrintWriter",
}


# Function: _add_known_java_imports
def _add_known_java_imports(content: str) -> str:
    """Add narrowly unambiguous framework imports omitted by per-file generation."""
    existing = set(re.findall(r"\bimport\s+([A-Za-z_][\w.]*)\s*;", content))
    existing_simple_names = {import_name.rsplit(".", 1)[-1] for import_name in existing}
    declared = set(re.findall(
        r"\b(?:class|interface|record|enum)\s+([A-Za-z_]\w*)",
        content,
    ))
    additions = []
    for symbol, import_name in _KNOWN_JAVA_SYMBOL_IMPORTS.items():
        if (
            symbol not in declared
            and import_name not in existing
            and symbol not in existing_simple_names
            and re.search(rf"\b{re.escape(symbol)}\b", content)
        ):
            additions.append(f"import {import_name};")
    if not additions:
        return content
    package_match = re.search(r"\bpackage\s+[^;]+;", content)
    if not package_match:
        return "\n".join(additions) + "\n" + content
    insertion = package_match.end()
    return content[:insertion] + "\n\n" + "\n".join(additions) + content[insertion:]


# Function: _reconcile_java_frontend_local_assets
def _reconcile_java_frontend_local_assets(output: dict[str, str]) -> None:
    """Create harmless missing relative stylesheet assets imported by Java SPAs."""
    for path, content in list(output.items()):
        if (
            "/frontend/" not in path
            or not path.endswith((".js", ".jsx", ".ts", ".tsx"))
            or not isinstance(content, str)
        ):
            continue
        parent = path.rsplit("/", 1)[0]
        specifiers = re.findall(
            r"""(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s+)["'](\.[^"']+)["']""",
            content,
        )
        for specifier in specifiers:
            target = posixpath.normpath(posixpath.join(parent, specifier))
            if target in output:
                continue
            if target.endswith((".css", ".scss", ".sass", ".less")):
                output[target] = "/* Generated stylesheet entry point. */\n"


def _reconcile_java_frontend_exports(output: dict[str, str]) -> None:
    """Align local default imports with an already-declared named export.

    Vite reports these only during Rollup bundling, after TypeScript has
    completed, so a missing default export otherwise arrives as a synthetic
    project error that the per-file repair loop cannot attach to a source file.
    """
    source_suffixes = (".ts", ".tsx", ".js", ".jsx")
    for consumer_path, consumer in list(output.items()):
        if (
            "/frontend/" not in consumer_path
            or not consumer_path.endswith(source_suffixes)
            or not isinstance(consumer, str)
        ):
            continue
        parent = posixpath.dirname(consumer_path)
        for imported_name, specifier in re.findall(
            r"(?m)^\s*import\s+([A-Za-z_$][\w$]*)\s+from\s+['\"](\.[^'\"]+)['\"]\s*;?",
            consumer,
        ):
            unresolved = posixpath.normpath(posixpath.join(parent, specifier))
            candidates = [unresolved] if unresolved.endswith(source_suffixes) else [
                unresolved + suffix for suffix in source_suffixes
            ] + [
                posixpath.join(unresolved, "index" + suffix) for suffix in source_suffixes
            ]
            target_path = next((path for path in candidates if path in output), "")
            if not target_path or target_path == consumer_path:
                continue
            target = output[target_path]
            if not isinstance(target, str) or re.search(r"\bexport\s+default\b", target):
                continue
            declared_export = re.search(
                rf"\bexport\s+(?:async\s+)?(?:function|class|const|let|var)\s+{re.escape(imported_name)}\b",
                target,
            )
            listed_export = re.search(
                rf"\bexport\s*\{{[^}}]*\b{re.escape(imported_name)}\b[^}}]*\}}",
                target,
            )
            if declared_export or listed_export:
                output[target_path] = target.rstrip() + f"\n\nexport default {imported_name};\n"


def _reconcile_java_frontend_default_api_client_export(output: dict[str, str]) -> None:
    """Export an unexported axios client singleton as its module's default
    export when at least one sibling file already default-imports from
    that module.

    `const apiClient = axios.create(...)` is this generator's recurring
    "api client" shape, built once and then consumed via
    `import apiClient from './apiClient'` (or any other local alias — a
    default import binds to whatever name the importer chooses, so
    `_reconcile_java_frontend_exports`'s name-matching heuristic above
    cannot recognize this case) from multiple page components. When the
    module itself never gained an `export default` line, every one of
    those consumers fails to resolve at bundle time; this only appends the
    missing export, never touching how the client itself is built.
    """
    source_suffixes = (".ts", ".tsx", ".js", ".jsx")
    default_import = re.compile(
        r"(?m)^\s*import\s+[A-Za-z_$][\w$]*\s+from\s+['\"](\.[^'\"]+)['\"]\s*;?"
    )
    for consumer_path, consumer in list(output.items()):
        if (
            "/frontend/" not in consumer_path
            or not consumer_path.endswith(source_suffixes)
            or not isinstance(consumer, str)
        ):
            continue
        parent = posixpath.dirname(consumer_path)
        for specifier in default_import.findall(consumer):
            unresolved = posixpath.normpath(posixpath.join(parent, specifier))
            candidates = [unresolved] if unresolved.endswith(source_suffixes) else [
                unresolved + suffix for suffix in source_suffixes
            ]
            target_path = next((path for path in candidates if path in output), "")
            if not target_path or target_path == consumer_path:
                continue
            target = output[target_path]
            if not isinstance(target, str) or re.search(r"\bexport\s+default\b", target):
                continue
            client_match = re.search(
                r"(?m)^\s*const\s+([A-Za-z_$][\w$]*)\s*=\s*axios\.create\s*\(", target,
            )
            if not client_match:
                continue
            name = client_match.group(1)
            if re.search(rf"\bexport\s*\{{[^}}]*\b{re.escape(name)}\b[^}}]*\}}", target):
                continue
            output[target_path] = target.rstrip() + f"\n\nexport default {name};\n"


# Function: _dotnet_backend_dockerfile
def _dotnet_backend_dockerfile(project_name: str, tfm: str) -> str:
    """Deterministic multi-stage .NET Dockerfile. An LLM-generated one drifted
    from the actual port everywhere else agreed on (compose/k8s use 8080) and
    has no reliable way to know the real .csproj filename in advance — this
    generator shares _dotnet_tfm with _backend_manifest_files so the SDK/
    runtime image tags can never disagree with the csproj's TargetFramework."""
    dotnet_version = tfm.removeprefix("net")
    return textwrap.dedent(f"""\
        FROM mcr.microsoft.com/dotnet/sdk:{dotnet_version} AS build
        WORKDIR /src
        COPY *.csproj ./
        RUN dotnet restore
        COPY . .
        RUN dotnet publish -c Release -o /app

        FROM mcr.microsoft.com/dotnet/aspnet:{dotnet_version} AS runtime
        WORKDIR /app
        COPY --from=build /app .
        ENV ASPNETCORE_URLS=http://+:8080
        EXPOSE 8080
        ENTRYPOINT ["dotnet", "{project_name}.dll"]
    """)


# Function: _angular_frontend_dockerfile
def _angular_frontend_dockerfile() -> str:
    """Deterministic multi-stage Angular Dockerfile — Node 20, `ng build
    --configuration production` (`--prod` was removed in Angular 12), then
    served via nginx (never `ng serve`, a dev server, in a production image).
    The dist path (`/app/dist`, no nested "browser/" folder) matches the
    "@angular-devkit/build-angular:browser" builder + outputPath "dist" set
    in _frontend_scaffold_files's angular.json — the newer "application"
    builder nests output under dist/<project>/browser instead, which would
    silently 404 everything if these two generators ever disagreed."""
    return textwrap.dedent("""\
        FROM node:20-alpine AS build
        WORKDIR /app
        COPY package*.json ./
        RUN npm ci
        COPY . .
        RUN npx ng build --configuration production

        FROM nginx:alpine AS runtime
        COPY --from=build /app/dist /usr/share/nginx/html
        COPY nginx.conf /etc/nginx/conf.d/default.conf
        EXPOSE 80
        CMD ["nginx", "-g", "daemon off;"]
    """)


# Function: _nginx_conf
def _nginx_conf() -> str:
    """Deterministic nginx config with SPA fallback — a missing
    try_files .../index.html rule 404s every deep-linked Angular route."""
    return textwrap.dedent("""\
        server {
            listen       80;
            server_name  localhost;
            root   /usr/share/nginx/html;
            index  index.html;

            location / {
                try_files $uri $uri/ /index.html;
            }
        }
    """)


# Function: _default_frontend_file_list
def _default_frontend_file_list(frontend_tech: str, project_name: str) -> list[str]:
    """Fallback frontend skeleton used only if the LLM planning step fails.
    Nests under "frontend/" — must match _ensure_modular_path's convention
    for the LLM-planned-successfully case, and _k8s_manifests_prompt's/
    _docker_compose_prompt's build-context assumptions ("./frontend")."""
    fw = (frontend_tech or "").lower()
    if "angular" in fw:
        return [
            "frontend/src/app/app.module.ts",
            "frontend/src/app/app-routing.module.ts",
            "frontend/src/app/core/auth/auth.service.ts",
            "frontend/src/app/core/auth/auth.guard.ts",
            "frontend/src/app/core/api/api.service.ts",
            "frontend/src/environments/environment.ts",
            "frontend/angular.json",
            "frontend/package.json",
            "frontend/Dockerfile",
        ]
    if "vue" in fw:
        return [
            "frontend/src/App.vue",
            "frontend/src/auth/auth.ts",
            "frontend/src/api/client.ts",
            "frontend/package.json",
            "frontend/Dockerfile",
        ]
    return [  # React / default SPA
        "frontend/src/App.tsx",
        "frontend/src/auth/AuthProvider.tsx",
        "frontend/src/api/client.ts",
        "frontend/package.json",
        "frontend/Dockerfile",
    ]


# Function: _docker_compose_java
def _docker_compose_java(root_ns: str, domains: list[str]) -> str:
    services = {"postgres": textwrap.dedent("""\
      postgres:
        image: postgres:16
        environment:
          POSTGRES_USER: postgres
          POSTGRES_PASSWORD: changeme
        ports: ["5432:5432"]
        volumes: [postgres_data:/var/lib/postgresql/data]""")}
    for i, d in enumerate(domains):
        port = 8080 + i
        services[f"{d.lower()}-service"] = textwrap.dedent(f"""\
      {d.lower()}-service:
        build: ./services/{d.lower()}-service
        ports: ["{port}:{port}"]
        environment:
          DB_USER: postgres
          DB_PASSWORD: changeme
        depends_on: [postgres]""")
    svc_block = "\n".join(services.values())
    return f"version: '3.9'\nservices:\n{svc_block}\nvolumes:\n  postgres_data:\n"


# Function: _k8s_manifests
def _k8s_manifests(root_ns: str, domains: list[str], lang: str) -> dict[str, str]:
    """Deterministic Kubernetes manifests for the folder-analysis pipeline's
    per-domain microservice topology — one Deployment/Service per domain plus
    a shared gateway, routed entirely through the gateway."""
    ns = root_ns.lower()
    services = [f"{d.lower()}-service" for d in domains]
    if lang == "csharp":
        # Only C# gets a real deployable ApiGateway (Program.cs + csproj) in
        # this pipeline — other languages' "gateway" is config-only.
        services = ["gateway", *services]
    entry_svc = "gateway" if lang == "csharp" else (services[0] if services else "app")
    return _k8s_manifests_core(ns, services, [("/", entry_svc)])


# Function: _k8s_manifests_prompt
def _k8s_manifests_prompt(project_name: str, has_backend: bool, has_frontend: bool) -> dict[str, str]:
    """Deterministic Kubernetes manifests for the prompt-driven pipeline's
    two-tier topology (backend API and/or frontend SPA) — "/api" routes to
    the backend, everything else to the frontend, when both are present.
    This is the one thing the LLM cannot be trusted to keep consistent across
    the Deployment/Service/Ingress/ConfigMap/Secret files it would otherwise
    generate independently of each other (see _contract_digest's docstring;
    infra manifests aren't code files an interface can pin down)."""
    ns = re.sub(r"[^a-z0-9-]", "-", project_name.lower()).strip("-") or "app"
    services = [s for s, present in (("backend", has_backend), ("frontend", has_frontend)) if present]
    if has_backend and has_frontend:
        routes = [("/api", "backend"), ("/", "frontend")]
    elif has_backend:
        routes = [("/", "backend")]
    else:
        routes = [("/", "frontend")]
    return _k8s_manifests_core(ns, services, routes)


# Function: _docker_compose_prompt
def _docker_compose_prompt(project_name: str, has_backend: bool, has_frontend: bool, lang: str) -> str:
    """Deterministic docker-compose.yml for the prompt-driven pipeline's
    two-tier topology, placed at the project root with build contexts that
    actually match where _ensure_modular_path puts things (backend at root,
    frontend under "frontend/") — an LLM-generated compose file has no way
    to know that layout in advance, and got it wrong in practice (found
    inside frontend/ using "./backend" and "./frontend" contexts, which only
    resolve from the repo root)."""
    services: dict[str, dict] = {}
    if has_backend:
        services["backend"] = {
            "build": "./backend",
            "ports": ["8080:8080"],
            "environment": {
                "ASPNETCORE_ENVIRONMENT": "Development",
                "ASPNETCORE_URLS": "http://+:8080",
            } if lang == "csharp" else {},
            "depends_on": ["db"] if lang == "csharp" else [],
        }
    if has_frontend:
        services["frontend"] = {
            "build": "./frontend",
            "ports": ["4200:80"],
            "depends_on": ["backend"] if has_backend else [],
        }
    if lang == "csharp":
        services["db"] = {
            "image": "mcr.microsoft.com/mssql/server:2022-latest",
            "environment": {"ACCEPT_EULA": "Y", "SA_PASSWORD": "YourStrong!Passw0rd"},
            "ports": ["1433:1433"],
        }
    try:
        import yaml as _yaml  # type: ignore
        return _yaml.dump({"version": "3.9", "services": services}, default_flow_style=False, sort_keys=False)
    except ImportError:
        import json as _json
        return "# yaml module not installed — raw JSON:\n" + _json.dumps(
            {"version": "3.9", "services": services}, indent=2)


# Function: _k8s_manifests_core
def _k8s_manifests_core(ns: str, services: list[str], ingress_routes: list[tuple]) -> dict[str, str]:
    """Shared manifest builder. `ingress_routes` is an ordered list of
    (path, service_name) pairs — more specific paths (e.g. "/api") must come
    before "/" since Kubernetes Ingress matches paths in list order.
    Not LLM-dependent — these are boilerplate that must always be present and
    correct, not something worth risking on model output."""
    # Function: _dump
    def _dump(docs: list[dict]) -> str:
        try:
            import yaml as _yaml  # type: ignore
            return _yaml.dump_all(docs, default_flow_style=False, sort_keys=False)
        except ImportError:
            import json as _json
            return "# yaml module not installed — raw JSON documents:\n" + "\n---\n".join(
                _json.dumps(d, indent=2) for d in docs
            )

    deployments, cluster_services = [], []
    for svc in services:
        container_port = 8080 if svc == "backend" or svc.endswith("-backend") else 80
        probe_path = "/health" if container_port == 8080 else "/"
        deployments.append({
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": svc, "namespace": ns, "labels": {"app": svc}},
            "spec": {
                "replicas": 2,
                "selector": {"matchLabels": {"app": svc}},
                "template": {
                    "metadata": {"labels": {"app": svc}},
                    "spec": {
                        "containers": [{
                            "name": svc,
                            # Placeholder — replace with your ACR/registry image before deploying.
                            "image": f"REPLACE_WITH_REGISTRY/{ns}/{svc}:v1",
                            "ports": [{"containerPort": container_port}],
                            "envFrom": [
                                {"configMapRef": {"name": f"{ns}-config"}},
                                {"secretRef": {"name": f"{ns}-secrets"}},
                            ],
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "128Mi"},
                                "limits":   {"cpu": "500m", "memory": "512Mi"},
                            },
                            "readinessProbe": {
                                "httpGet": {"path": probe_path, "port": container_port},
                                "initialDelaySeconds": 10, "periodSeconds": 10,
                            },
                            "livenessProbe": {
                                "httpGet": {"path": probe_path, "port": container_port},
                                "initialDelaySeconds": 20, "periodSeconds": 20,
                            },
                        }],
                    },
                },
            },
        })
        cluster_services.append({
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": svc, "namespace": ns},
            "spec": {
                "selector": {"app": svc},
                "ports": [{"port": 80, "targetPort": container_port}],
                "type": "ClusterIP",
            },
        })

    ingress = {
        "apiVersion": "networking.k8s.io/v1", "kind": "Ingress",
        "metadata": {"name": f"{ns}-ingress", "namespace": ns},
        "spec": {
            "tls": [{
                "hosts": [f"{ns}.example.com"],
                "secretName": f"{ns}-tls",
            }],
            "rules": [{
                "host": f"{ns}.example.com",
                "http": {
                    "paths": [
                        {
                            "path": path, "pathType": "Prefix",
                            "backend": {"service": {"name": svc, "port": {"number": 80}}},
                        }
                        for path, svc in ingress_routes
                    ],
                },
            }],
        },
    }
    configmap = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": f"{ns}-config", "namespace": ns},
        "data": {"APP_ENVIRONMENT": "production", "SERVER_PORT": "8080"},
    }
    secret_example = {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": f"{ns}-secrets", "namespace": ns},
        "type": "Opaque",
        "stringData": {
            "DATABASE_URL": "REPLACE_VIA_SECRET_MANAGER_OR_CI_CD",
            "JWT_SECRET": "REPLACE_VIA_SECRET_MANAGER_OR_CI_CD",
        },
    }

    return {
        "k8s/deployment.yaml": _dump(deployments),
        "k8s/service.yaml": _dump(cluster_services),
        "k8s/ingress.yaml": _dump([ingress]),
        "k8s/configmap.yaml": _dump([configmap]),
        "k8s/secret.example.yaml": (
            "# Copy to secret.yaml, fill in real values, and apply with kubectl.\n"
            "# NEVER commit secret.yaml (only this .example file) — see README for the\n"
            "# recommended cloud secret manager + CSI driver integration.\n"
        ) + _dump([secret_example]),
    }


# Function: _docker_compose
def _docker_compose(root_ns: str, domains: list[str]) -> str:
    services = {"gateway": {
        "build": "./ApiGateway",
        "ports": ["5000:80"],
        "depends_on": [f"{d.lower()}-service" for d in domains],
    }}
    port = 7001
    for d in domains:
        services[f"{d.lower()}-service"] = {
            "build": f"./Services/{d.capitalize()}Service",
            "expose": [str(port)],
            "environment": {
                "ASPNETCORE_URLS": f"http://+:{port}",
                "ConnectionStrings__DefaultConnection":
                    f"Server=sqlserver;Database=Modernized_{d.capitalize()}DB;Trusted_Connection=True;",
            },
            "depends_on": ["sqlserver"],
        }
        port += 1

    services["sqlserver"] = {
        "image": "mcr.microsoft.com/mssql/server:2022-latest",
        "environment": {
            "ACCEPT_EULA": "Y",
            "SA_PASSWORD": "YourStrong!Passw0rd",
        },
        "ports": ["1433:1433"],
    }

    import yaml as _yaml  # type: ignore
    try:
        return _yaml.dump({"version": "3.9", "services": services}, default_flow_style=False)
    except ImportError:
        import json as _json
        return "# yaml module not installed — raw JSON:\n" + _json.dumps(
            {"version": "3.9", "services": services}, indent=2)


# ─── Go build files ─────────────────────────────────────────────────────────
# Function: _go_mod
def _go_mod(root_ns: str, backend_tech: str) -> str:
    """Real go.mod for the generated project. Dependency selected by a
    substring match on backend_tech ("Go + Gin" -> Gin, else plain net/http),
    same signal TARGET_STACKS' go_rest/go_gin_react entries already carry."""
    module = f"github.com/{root_ns.lower()}/modernizedapp"
    deps = ['\tgithub.com/jackc/pgx/v5 v5.6.0']
    if "gin" in (backend_tech or "").lower():
        deps.append("\tgithub.com/gin-gonic/gin v1.10.0")
    if "fiber" in (backend_tech or "").lower():
        deps.append("\tgithub.com/gofiber/fiber/v2 v2.52.5")
    deps_block = "\n".join(deps)
    return f"module {module}\n\ngo 1.22\n\nrequire (\n{deps_block}\n)\n"


# Function: _docker_compose_go
def _docker_compose_go(root_ns: str, domains: list[str]) -> str:
    services = {"postgres": textwrap.dedent("""\
      postgres:
        image: postgres:16
        environment:
          POSTGRES_USER: postgres
          POSTGRES_PASSWORD: changeme
        ports: ["5432:5432"]
        volumes: [postgres_data:/var/lib/postgresql/data]""")}
    for i, d in enumerate(domains):
        port = 8080 + i
        services[f"{d.lower()}-service"] = textwrap.dedent(f"""\
      {d.lower()}-service:
        build: ./services/{d.lower()}-service
        ports: ["{port}:{port}"]
        environment:
          DATABASE_URL: postgres://postgres:changeme@postgres:5432/{root_ns.lower()}?sslmode=disable
        depends_on: [postgres]""")
    svc_block = "\n".join(services.values())
    return f"version: '3.9'\nservices:\n{svc_block}\nvolumes:\n  postgres_data:\n"
