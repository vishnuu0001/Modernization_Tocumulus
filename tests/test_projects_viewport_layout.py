from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_governed_project_workspace_uses_viewport_bounded_panels():
    source = (ROOT / "frontend" / "src" / "pages" / "ProjectsPage.jsx").read_text(
        encoding="utf-8",
    )
    styles = (ROOT / "frontend" / "src" / "index.css").read_text(encoding="utf-8")

    assert "projects-viewport" in source
    assert "lg:h-full lg:min-h-0 lg:flex-col lg:overflow-hidden" in source
    assert "lg:grid-cols-[300px_minmax(0,1fr)]" in source
    assert "lg:overflow-y-auto lg:overscroll-contain" in source
    assert source.count("Next governed action") == 1
    assert "project-action-dock shrink-0" in source
    assert ".projects-viewport > .grid > section > .space-y-5" in styles
    assert "scrollbar-gutter: stable" in styles


def test_next_action_dock_is_outside_the_scrollable_project_content():
    source = (ROOT / "frontend" / "src" / "pages" / "ProjectsPage.jsx").read_text(
        encoding="utf-8",
    )
    assert (
        "          </div>\n"
        "          <div className=\"project-action-dock"
    ) in source
    assert "tab === 'Overview' && <div className=\"project-action-dock" not in source


def test_every_project_tab_uses_the_shared_viewport_and_action_dock():
    source = (ROOT / "frontend" / "src" / "pages" / "ProjectsPage.jsx").read_text(
        encoding="utf-8",
    )

    for tab in (
        "Overview",
        "Analysis",
        "Plan",
        "Contracts",
        "Compare",
        "Validation & Release",
        "History",
    ):
        assert f"tab === '{tab}'" in source

    assert source.count('className="project-action-dock') == 1


def test_plan_actions_are_rendered_only_in_the_fixed_action_dock():
    source = (ROOT / "frontend" / "src" / "pages" / "ProjectsPage.jsx").read_text(
        encoding="utf-8",
    )
    scrollable_content, action_dock = source.split(
        '<div className="project-action-dock',
        maxsplit=1,
    )

    for action in (
        "Save revision",
        "Confirm proposed decisions",
        "Mark review complete",
        "Approve and lock",
        "Validate contracts",
        "Approve Release",
        "Request Corrections & Re-run",
        "Reject Output",
        "Apply retention policy",
    ):
        assert action not in scrollable_content
        assert action in action_dock

    assert 'md:grid-cols-[1fr_1fr_auto]' not in scrollable_content
    assert '<FileDiff className="mr-2 inline h-4 w-4" />Compare' in action_dock


def test_architecture_and_generated_asset_empty_states_are_actionable():
    requirements_source = (
        ROOT / "frontend" / "src" / "pages" / "RequirementsDocumentationPage.jsx"
    ).read_text(encoding="utf-8")
    projects_source = (
        ROOT / "frontend" / "src" / "pages" / "ProjectsPage.jsx"
    ).read_text(encoding="utf-8")

    assert "analyzeProject(projectId, targetStack, customDescription)" in requirements_source
    assert "generateProjectPlan(projectId, targetStack, customDescription)" in requirements_source
    assert "Select target technology stack" in requirements_source
    assert "Custom technology stack" in requirements_source
    assert "ARCHITECTURE_STACK_FALLBACKS" in requirements_source
    assert "useState(ARCHITECTURE_STACK_FALLBACKS)" in requirements_source
    assert "verified defaults are ready" in requirements_source
    assert "setStackReloadKey(value => value + 1)" in requirements_source
    assert 'id="architecture-custom-stack"' in requirements_source
    assert "Generate architecture" in requirements_source
    assert "Open forward engineering" in requirements_source
    assert "flex w-full flex-wrap justify-center gap-3" in requirements_source
    assert "replaceAll('-', ' ')" in requirements_source
    assert "Database / Persistence Layer" in requirements_source
    assert "useSearchParams" in projects_source
    assert "searchParams.get('project')" in projects_source
