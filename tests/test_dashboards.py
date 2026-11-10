"""Offline unit and integration tests for live dashboards lifecycle, CSP, cadence, expiry, and edit tokens."""

from __future__ import annotations

import datetime as dt

import pytest

from askdash.dashboards import (
    DASHBOARD_CSP,
    GRACE_PERIOD_DAYS,
    IDLE_DAYS,
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    STATUS_REMOVED,
    VALIDITY_DAYS,
    DashboardAuthError,
    DashboardNotRefreshableError,
    DashboardNotRenewableError,
    DashboardStore,
    DashboardValidationError,
    PublishedDashboard,
    build_csp_headers,
    can_refresh_now,
    compute_new_expiry,
    days_remaining,
    due_for_refresh,
    expiry_actions,
    generate_edit_token,
    inject_dashboard_data,
    next_refresh_at,
    safe_json_for_script,
    verify_edit_token,
)
from askdash.executor import SQLiteExecutor
from askdash.sources import HttpFetchResult

SAMPLE_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>Sales Dashboard</title>
</head>
<body>
  <h1>Revenue Overview</h1>
  <script>console.log(window.ASKDASH_DATA);</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# CSP & HTML Injection Tests
# ---------------------------------------------------------------------------


def test_csp_policy_and_headers_enforce_connect_src_none():
    assert "connect-src 'none'" in DASHBOARD_CSP
    assert "frame-ancestors 'none'" in DASHBOARD_CSP
    assert "base-uri 'none'" in DASHBOARD_CSP

    headers = build_csp_headers()
    assert headers["Content-Security-Policy"] == DASHBOARD_CSP
    assert "connect-src 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_inject_dashboard_data_places_script_before_agent_scripts_and_escapes_xss():
    dataset = {
        "type": "sources",
        "sources": {
            "xss_attempt": [{"note": "</script><script>alert(1)</script><!--"}],
        },
        "collected_at": "2026-11-09T12:00:00+00:00",
    }
    meta = {"refreshable": True, "version": 1, "cooldown_minutes": 15}

    rendered = inject_dashboard_data(SAMPLE_HTML, dataset, meta)

    # CSP meta tag with connect-src 'none' is injected into <head>
    assert 'http-equiv="Content-Security-Policy"' in rendered
    assert "connect-src 'none'" in rendered

    # Injected data script comes right after <head> and BEFORE agent's <script>
    injected_pos = rendered.index("window.ASKDASH_DATA=")
    agent_script_pos = rendered.index("console.log(window.ASKDASH_DATA)")
    assert injected_pos < agent_script_pos
    assert "window.NEXDASH_DATA=window.ASKDASH_DATA" in rendered

    # Raw </script> inside JSON payload is escaped as \u003c/script\u003e
    assert "</script><script>alert(1)" not in rendered
    assert "\\u003c/script\\u003e" in safe_json_for_script("</script>")

    # Status footer bar is injected before </body>
    assert 'id="askdash-status-bar"' in rendered
    assert rendered.index('id="askdash-status-bar"') < rendered.rindex("</body>")


# ---------------------------------------------------------------------------
# Cadence & Pure Scheduling Tests
# ---------------------------------------------------------------------------


def test_next_refresh_at_and_cooldown():
    now = dt.datetime(2026, 11, 9, 10, 15, 0, tzinfo=dt.UTC)  # Monday 10:15 UTC (07:15 -03:00)

    assert next_refresh_at("never", now) == ""
    assert next_refresh_at("hourly", now) == "2026-11-09T11:00:00+00:00"

    # 06:30 BRT (-03:00) is 09:30 UTC; since it is already 10:15 UTC, next daily is tomorrow 09:30 UTC
    assert next_refresh_at("daily", now, "06:30", tz_offset_hours=-3) == "2026-11-10T09:30:00+00:00"

    # Weekly on Monday at 06:30 BRT -> next Monday (2026-11-16) at 09:30 UTC
    assert next_refresh_at("weekly", now, "06:30", tz_offset_hours=-3) == "2026-11-16T09:30:00+00:00"

    dash = PublishedDashboard(updated_at=now.isoformat())
    allowed, wait_s = can_refresh_now(dash, now + dt.timedelta(minutes=5))
    assert allowed is False
    assert wait_s == 600

    allowed_after, wait_after = can_refresh_now(dash, now + dt.timedelta(minutes=16))
    assert allowed_after is True
    assert wait_after == 0


# ---------------------------------------------------------------------------
# Expiry, Grace Period, and Renewal Tests
# ---------------------------------------------------------------------------


def test_expiry_actions_and_renewal_cap():
    now = dt.datetime(2026, 11, 9, 12, 0, 0, tzinfo=dt.UTC)

    # 10 days left -> no action
    d_active = PublishedDashboard(
        created_at=(now - dt.timedelta(days=20)).isoformat(),
        expires_at=(now + dt.timedelta(days=10)).isoformat(),
    )
    assert expiry_actions(d_active, now) == []
    assert days_remaining(d_active, now) == 10

    # 2.5 days left -> warn_d3
    d_d3 = PublishedDashboard(
        created_at=(now - dt.timedelta(days=27)).isoformat(),
        expires_at=(now + dt.timedelta(days=2, hours=12)).isoformat(),
    )
    assert expiry_actions(d_d3, now) == ["warn_d3"]

    # 12 hours left -> warn_d0 (suppresses warn_d3 even if d3 was never sent)
    d_d0 = PublishedDashboard(
        created_at=(now - dt.timedelta(days=29)).isoformat(),
        expires_at=(now + dt.timedelta(hours=12)).isoformat(),
    )
    assert expiry_actions(d_d0, now) == ["warn_d0"]

    # Past expiry -> warn_d0 + expire
    d_past = PublishedDashboard(
        created_at=(now - dt.timedelta(days=31)).isoformat(),
        expires_at=(now - dt.timedelta(hours=2)).isoformat(),
    )
    assert expiry_actions(d_past, now) == ["warn_d0", "expire"]
    assert days_remaining(d_past, now) == 0

    # Expired for 61 days -> remove
    d_remove = PublishedDashboard(
        status=STATUS_EXPIRED,
        expires_at=(now - dt.timedelta(days=GRACE_PERIOD_DAYS + 1)).isoformat(),
    )
    assert expiry_actions(d_remove, now) == ["remove"]

    # Permanent dashboard idle for 91 days -> warn_idle
    d_perm = PublishedDashboard(
        permanent=True,
        created_at=(now - dt.timedelta(days=IDLE_DAYS + 1)).isoformat(),
    )
    assert expiry_actions(d_perm, now) == ["warn_idle"]
    assert days_remaining(d_perm, now) is None

    # Renewal adds +30 days but is capped at now + 60 days
    first_renew = compute_new_expiry((now + dt.timedelta(days=20)).isoformat(), now)
    assert first_renew == now + dt.timedelta(days=50)
    second_renew = compute_new_expiry(first_renew.isoformat(), now)
    assert second_renew == now + dt.timedelta(days=60)


# ---------------------------------------------------------------------------
# Edit Token & Store End-to-End Lifecycle Tests
# ---------------------------------------------------------------------------


def test_edit_token_generation_and_verification():
    raw, token_hash = generate_edit_token()
    assert raw.startswith("ed_")
    assert raw != token_hash
    assert verify_edit_token(raw, token_hash) is True
    assert verify_edit_token(raw + "x", token_hash) is False
    assert verify_edit_token("not_ed_prefix", token_hash) is False


def test_live_dashboard_publish_render_refresh_failsafe_edit_and_renew():
    now = dt.datetime(2026, 11, 9, 12, 0, 0, tzinfo=dt.UTC)
    should_fail = False
    api_value = 100

    def fake_fetcher(method: str, url: str, headers: dict[str, str], body: object) -> HttpFetchResult:
        if should_fail:
            raise RuntimeError("Upstream connection failed with token secret_api_key_123")
        return HttpFetchResult(
            content=f'[{{"metric": {api_value}}}]'.encode(),
            content_type="application/json",
        )

    executor = SQLiteExecutor(":memory:")
    executor.execute_script(
        "CREATE TABLE kpis (name TEXT, val INTEGER); INSERT INTO kpis VALUES ('orders', 42);"
    )

    store = DashboardStore(
        secret_key="test-master-secret",
        resolver=lambda _: ["93.184.216.34"],
        http_fetcher=fake_fetcher,
        sql_executor=executor,
    )

    # 1. Publish live dashboard with HTTP + SQL + CSV sources
    published = store.publish_live(
        title="Executive KPIs",
        html=SAMPLE_HTML,
        owner_email="owner@example.com",
        cadence="hourly",
        secrets={"API_KEY": "secret_api_key_123"},
        sources=[
            {
                "name": "remote_api",
                "kind": "http",
                "url": "https://api.example.com/kpis",
                "headers": {"Authorization": "Bearer {{env.API_KEY}}"},
            },
            {
                "name": "db_kpis",
                "kind": "sql",
                "sql": "SELECT name, val FROM kpis WHERE name = :name",
                "params": {"name": "orders"},
            },
            {
                "name": "targets",
                "kind": "csv",
                "content": "quarter,goal\nQ4,200\n",
            },
        ],
        now=now,
    )

    dash_id = published["id"]
    access_token = published["access_token"]
    renewal_token = published["renewal_token"]
    edit_token = published["edit_token"]

    assert published["kind"] == "live"
    assert published["rows"] == {"remote_api": 1, "db_kpis": 1, "targets": 1}
    assert published["secret_names"] == ["API_KEY"]

    # Public dict never leaks raw tokens or secret values
    dash_obj = store.get(dash_id)
    assert dash_obj is not None
    pub_dict = dash_obj.to_public_dict(now=now)
    assert "access_token" not in pub_dict
    assert "renewal_token" not in pub_dict
    assert "edit_token" not in pub_dict
    assert "secret_api_key_123" not in str(pub_dict)

    # 2. Render by access token -> CSP connect-src 'none' + injected data
    rendered = store.render_by_access_token(access_token, now=now + dt.timedelta(minutes=1))
    assert rendered is not None
    assert "connect-src 'none'" in rendered.headers["Content-Security-Policy"]
    assert '"remote_api":[{"metric":100}]' in rendered.html
    assert '"db_kpis":[{"name":"orders","val":42}]' in rendered.html
    assert '"targets":[{"quarter":"Q4","goal":200}]' in rendered.html
    assert dash_obj.access_count == 1

    # 3. Manual refresh rate-limit cooldown check
    cooldown_res = store.refresh_dashboard(
        dash_id,
        enforce_cooldown=True,
        now=now + dt.timedelta(minutes=5),
    )
    assert cooldown_res["ok"] is False
    assert cooldown_res["rate_limited"] is True

    # 4. Fail-safe refresh: when upstream fails, previous good dataset stays intact and secret is masked
    should_fail = True
    failed_res = store.refresh_dashboard(dash_id, now=now + dt.timedelta(minutes=20))
    assert failed_res["ok"] is False
    assert failed_res["consecutive_failures"] == 1
    assert "secret_api_key_123" not in failed_res["error"]
    assert "****" in failed_res["error"]

    # Dashboard still renders the previous good data (metric: 100)
    rendered_after_fail = store.render_by_access_token(access_token, now=now + dt.timedelta(minutes=21))
    assert rendered_after_fail is not None
    assert '"remote_api":[{"metric":100}]' in rendered_after_fail.html
    assert "last refresh failed" in rendered_after_fail.html or "last_error" in rendered_after_fail.html

    # 5. Successful refresh updates dataset to version 2 and clears error
    should_fail = False
    api_value = 250
    ok_res = store.refresh_dashboard(dash_id, now=now + dt.timedelta(minutes=40))
    assert ok_res["ok"] is True
    assert ok_res["version"] == 2
    assert dash_obj.consecutive_failures == 0
    rendered_v2 = store.render_by_access_token(access_token, now=now + dt.timedelta(minutes=41))
    assert rendered_v2 is not None
    assert '"remote_api":[{"metric":250}]' in rendered_v2.html

    # 6. Edit dashboard using edit_token (wrong token rejected)
    with pytest.raises(DashboardAuthError):
        store.edit_dashboard("ed_invalid_token", renew=False, title="Hacked")

    api_value = 300
    edit_res = store.edit_dashboard(
        edit_token,
        renew=True,
        title="Executive KPIs v2",
        note="Updated Q4 targets",
        secrets={"API_KEY": "rotated_api_key_999"},
        cadence="daily",
        refresh_time="07:00",
        now=now + dt.timedelta(days=10),
    )
    assert edit_res["renewed"] is True
    assert edit_res["refresh"]["ok"] is True
    assert dash_obj.title == "Executive KPIs v2"
    assert dash_obj.cadence == "daily"
    assert dash_obj.version == 3

    # Rotate edit token -> old token stops working, new token works
    new_edit_token = store.rotate_edit_token(dash_id, now=now + dt.timedelta(days=10))
    with pytest.raises(DashboardAuthError):
        store.edit_dashboard(edit_token, renew=False, title="Should Fail")
    store.edit_dashboard(new_edit_token, renew=False, title="Executive KPIs v3", now=now + dt.timedelta(days=10))
    assert dash_obj.title == "Executive KPIs v3"

    # 7. Expiration and renewal lifecycle (renewed in step 6 to now + 60 days)
    expired_time = now + dt.timedelta(days=65)
    assert store.render_by_access_token(access_token, now=expired_time) is None
    exp_summary = store.process_expirations(now=expired_time)
    assert exp_summary["expired"] == 1
    assert dash_obj.status == STATUS_EXPIRED

    # Renewing via renewal_token restores active status and +30 days from expired_time
    renewed_info = store.renew_dashboard(renewal_token, now=expired_time)
    assert renewed_info["status"] == STATUS_ACTIVE
    assert renewed_info["days_remaining"] == VALIDITY_DAYS
    assert store.render_by_access_token(access_token, now=expired_time) is not None

    # After expiry + grace period, dashboard content is removed and cannot be renewed
    far_future = expired_time + dt.timedelta(days=VALIDITY_DAYS + 1)
    store.process_expirations(now=far_future)
    assert dash_obj.status == STATUS_EXPIRED
    after_grace = far_future + dt.timedelta(days=GRACE_PERIOD_DAYS + 1)
    rem_summary = store.process_expirations(now=after_grace)
    assert rem_summary["removed"] == 1
    assert dash_obj.status == STATUS_REMOVED

    with pytest.raises(DashboardNotRenewableError):
        store.renew_dashboard(renewal_token, now=after_grace)

    executor.close()


def test_snapshot_publish_and_convert_to_live_and_scheduled_refreshes():
    now = dt.datetime(2026, 11, 9, 12, 0, 0, tzinfo=dt.UTC)
    store = DashboardStore()

    snap = store.publish_snapshot(
        title="Static Report",
        html=SAMPLE_HTML,
        owner_email="analyst@example.com",
        now=now,
    )
    assert snap["kind"] == "snapshot"
    with pytest.raises(DashboardNotRefreshableError):
        store.refresh_dashboard(snap["id"], now=now)

    # Upgrade snapshot to live dashboard preserving tokens
    converted = store.convert_to_live(
        snap["id"],
        sources=[{"name": "inline_csv", "kind": "csv", "content": "item,count\nA,10\n"}],
        cadence="hourly",
        now=now,
    )
    assert converted["kind"] == "live"
    assert converted["rows"] == {"inline_csv": 1}

    dash = store.get(snap["id"])
    assert dash is not None
    assert dash.access_token == snap["access_token"]
    assert dash.renewal_token == snap["renewal_token"]

    # Check scheduled refresh job execution when due
    assert due_for_refresh([dash], now + dt.timedelta(minutes=30)) == []
    assert due_for_refresh([dash], now + dt.timedelta(hours=2)) == [dash]

    sched_summary = store.process_scheduled_refreshes(now=now + dt.timedelta(hours=2))
    assert sched_summary["due"] == 1
    assert sched_summary["refreshed"] == 1

    # Preview sources without persisting
    preview = store.preview_sources(
        [{"name": "inline_csv", "kind": "csv", "content": "a,b\n1,2\n3,4\n"}]
    )
    assert preview[0]["ok"] is True
    assert preview[0]["rows"] == 2

    # Validation errors on empty title/html/bad email
    with pytest.raises(DashboardValidationError):
        store.publish_snapshot(title="", html=SAMPLE_HTML, owner_email="a@b.com")
    with pytest.raises(DashboardValidationError):
        store.publish_snapshot(title="Valid", html="   ", owner_email="a@b.com")
    with pytest.raises(DashboardValidationError):
        store.publish_snapshot(title="Valid", html=SAMPLE_HTML, owner_email="invalid-email")
