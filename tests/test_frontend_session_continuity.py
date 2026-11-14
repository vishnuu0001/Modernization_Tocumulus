from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
def test_app_has_no_idle_session_timeout():
    app_source = (ROOT / "frontend" / "src" / "App.jsx").read_text(encoding="utf-8")
    client_source = (ROOT / "frontend" / "src" / "api" / "client.js").read_text(encoding="utf-8")

    assert "30 * 60 * 1000" not in app_source
    assert "logout({ force: false })" not in app_source
    assert "ACTIVE_WORK_SESSION_EVENT" not in app_source
    assert "notifyActiveWork" not in client_source
    assert "logout({ force: true })" in app_source
    assert "if (data?.logged_out)" in client_source
