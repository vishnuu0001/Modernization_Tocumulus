# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: Modernization — services/modernizer/scaffolds (typescript.py)
# Date: 2025-11-01
# ---------------------------------------------------------------------------
from __future__ import annotations

import logging
import re
import textwrap

logger = logging.getLogger(__name__)



# ─── TypeScript / JavaScript component generation ─────────────────────────────
# Function: _gen_ts_component
def _gen_ts_component(output: dict[str, str], root_ns: str, domain: str, target_stack: str):
    base = f"ModernizedApp/src/components/{domain}"
    fw = "React"
    if "angular" in target_stack.lower():
        fw = "Angular"
    elif "vue" in target_stack.lower():
        fw = "Vue"
    extension = "ts" if fw == "Angular" else "tsx"
    output[f"{base}/{domain}Page.{extension}"] = _ts_page_template(domain, fw)
    output[f"{base}/{domain}Service.ts"] = _ts_service(domain)


# Function: _ts_page_template
def _ts_page_template(domain: str, framework: str) -> str:
    if framework == "React":
        return textwrap.dedent(f"""\
            import React, {{ useState, useEffect }} from 'react';
            import {{ {domain}Service }} from './{domain}Service';

            interface {domain}Item {{
              id: number;
              name: string;
              isActive: boolean;
              createdAt: string;
            }}

            export const {domain}Page: React.FC = () => {{
              const [items, setItems] = useState<{domain}Item[]>([]);
              const [loading, setLoading] = useState(true);
              const [error, setError] = useState('');

              useEffect(() => {{
                {domain}Service.getAll()
                  .then(setItems)
                  .catch(e => setError(e.message))
                  .finally(() => setLoading(false));
              }}, []);

              if (loading) return <div className="loading">Loading {domain}...</div>;
              if (error)   return <div className="error">Error: {{error}}</div>;

              return (
                <div className="{domain.lower()}-page">
                  <h1>{domain}</h1>
                  <table>
                    <thead>
                      <tr><th>ID</th><th>Name</th><th>Active</th><th>Created</th></tr>
                    </thead>
                    <tbody>
                      {{items.map(item => (
                        <tr key={{item.id}}>
                          <td>{{item.id}}</td>
                          <td>{{item.name}}</td>
                          <td>{{item.isActive ? '✓' : '✗'}}</td>
                          <td>{{new Date(item.createdAt).toLocaleDateString()}}</td>
                        </tr>
                      ))}}
                    </tbody>
                  </table>
                </div>
              );
            }};

            export default {domain}Page;
        """)
    if framework == "Angular":
        selector = re.sub(r"[^a-z0-9]+", "-", domain.casefold()).strip("-") or "domain"
        return textwrap.dedent(f"""\
            import {{ CommonModule }} from '@angular/common';
            import {{ Component, OnInit }} from '@angular/core';
            import {{ {domain}Service }} from './{domain}Service';

            interface {domain}Item {{
              id: number;
              name: string;
              isActive: boolean;
              createdAt: string;
            }}

            @Component({{
              selector: 'app-{selector}-page',
              standalone: true,
              imports: [CommonModule],
              template: `
                <section aria-labelledby="{selector}-title">
                  <h1 id="{selector}-title">{domain}</h1>
                  <p *ngIf="loading" role="status">Loading {domain}…</p>
                  <div *ngIf="error" role="alert">
                    {{'{{'}} error {{'}}'}}
                    <button type="button" (click)="load()">Retry</button>
                  </div>
                  <table *ngIf="!loading && !error">
                    <thead><tr><th>ID</th><th>Name</th><th>Active</th><th>Created</th></tr></thead>
                    <tbody>
                      <tr *ngFor="let item of items; trackBy: trackById">
                        <td>{{'{{'}} item.id {{'}}'}}</td><td>{{'{{'}} item.name {{'}}'}}</td>
                        <td>{{'{{'}} item.isActive ? 'Yes' : 'No' {{'}}'}}</td>
                        <td>{{'{{'}} item.createdAt | date {{'}}'}}</td>
                      </tr>
                    </tbody>
                  </table>
                </section>
              `,
            }})
            export class {domain}PageComponent implements OnInit {{
              items: {domain}Item[] = [];
              loading = false;
              error = '';

              ngOnInit(): void {{ void this.load(); }}

              async load(): Promise<void> {{
                this.loading = true;
                this.error = '';
                try {{
                  this.items = await {domain}Service.getAll() as {domain}Item[];
                }} catch (error: unknown) {{
                  this.error = error instanceof Error ? error.message : 'Unable to load {domain}';
                }} finally {{
                  this.loading = false;
                }}
              }}

              trackById(_index: number, item: {domain}Item): number {{ return item.id; }}
            }}
        """)
    raise ValueError(f"Unsupported TypeScript UI framework: {framework}")


# Function: _ts_service
def _ts_service(domain: str) -> str:
    return textwrap.dedent(f"""\
        const API_BASE = import.meta.env.VITE_API_URL ?? '/api';

        export const {domain}Service = {{
          async getAll() {{
            const res = await fetch(`${{API_BASE}}/api/{domain.lower()}`);
            if (!res.ok) throw new Error(`HTTP ${{res.status}}`);
            return res.json();
          }},
          async getById(id: number) {{
            const res = await fetch(`${{API_BASE}}/api/{domain.lower()}/${{id}}`);
            if (!res.ok) throw new Error(`HTTP ${{res.status}}`);
            return res.json();
          }},
          async create(data: object) {{
            const res = await fetch(`${{API_BASE}}/api/{domain.lower()}`, {{
              method: 'POST',
              headers: {{ 'Content-Type': 'application/json' }},
              body: JSON.stringify(data),
            }});
            if (!res.ok) throw new Error(`HTTP ${{res.status}}`);
            return res.json();
          }},
          async delete(id: number) {{
            const res = await fetch(`${{API_BASE}}/api/{domain.lower()}/${{id}}`, {{ method: 'DELETE' }});
            if (!res.ok) throw new Error(`HTTP ${{res.status}}`);
          }},
        }};
    """)


# Function: _npm_root_package
def _npm_root_package(root_ns: str, target_stack: str) -> str:
    import json as _json
    framework = "react" if "react" in target_stack else \
                "angular" if "angular" in target_stack else "vue"
    deps: dict[str, str] = {}
    dev_deps: dict[str, str] = {
        "typescript": "^5.3.0",
        "vite": "^5.0.0",
        "@types/node": "^20.0.0",
    }
    if framework == "react":
        deps.update({"react": "^18.2.0", "react-dom": "^18.2.0", "react-router-dom": "^6.0.0"})
        dev_deps.update({"@vitejs/plugin-react": "^4.0.0", "@types/react": "^18.2.0"})
    elif framework == "angular":
        deps.update({"@angular/core": "^17.0.0", "@angular/common": "^17.0.0",
                     "rxjs": "^7.8.0", "zone.js": "^0.14.0"})
    else:  # vue
        deps.update({"vue": "^3.3.0", "vue-router": "^4.0.0", "pinia": "^2.0.0"})
        dev_deps["@vitejs/plugin-vue"] = "^4.0.0"
    return _json.dumps({
        "name": root_ns.lower().replace(" ", "-") + "-modernized",
        "version": "1.0.0",
        "scripts": {"dev": "vite", "build": "tsc && vite build", "preview": "vite preview"},
        "dependencies": deps,
        "devDependencies": dev_deps,
    }, indent=2)


# Function: _tsconfig
def _tsconfig() -> str:
    import json as _json
    return _json.dumps({
        "compilerOptions": {
            "target": "ES2022", "module": "ESNext", "moduleResolution": "bundler",
            "jsx": "react-jsx", "strict": True, "noUnusedLocals": True,
            "noUnusedParameters": True, "noFallthroughCasesInSwitch": True
        },
        "include": ["src"], "references": [{"path": "./tsconfig.node.json"}]
    }, indent=2)


# Function: _vite_config
def _vite_config() -> str:
    return textwrap.dedent("""\
        import { defineConfig } from 'vite'
        import react from '@vitejs/plugin-react'

        export default defineConfig({
          plugins: [react()],
          server: {
            proxy: {
              '/api': { target: 'http://localhost:8080', changeOrigin: true }
            }
          }
        })
    """)
