"""Owner routing: only sessions named `<owner>--<rest>` reach an owner webhook."""

import json
from datetime import datetime
from pathlib import Path

from herdr_webhook_notify import cli, events, filters, herdr, owners
from herdr_webhook_notify import config as config_mod

OWNER_CONFIG = """
[http]
retries = 0

[owners.cto]
url = "${CTO_WAKE_URL}"
header = "${CTO_WAKE_HEADER}"
"""


def base_url(server) -> str:
    return f"http://127.0.0.1:{server.server_port}"


def write_config(config_dir, text):
    (Path(config_dir) / "config.toml").write_text(text, encoding="utf-8")


def configure_cto(monkeypatch, config_dir, server, header="Authorization: Bearer wake-token"):
    monkeypatch.setenv("CTO_WAKE_URL", f"{base_url(server)}/cto")
    monkeypatch.setenv("CTO_WAKE_HEADER", header)
    write_config(config_dir, OWNER_CONFIG)


def status_event(status, pane_id="wD:p1", workspace_id="wD"):
    return {
        "type": "pane_agent_status_changed",
        "pane_id": pane_id,
        "workspace_id": workspace_id,
        "agent_status": status,
    }


def send_event(monkeypatch, event_name, data):
    pane_id = data.get("pane_id", "wD:p1")
    context = {
        "workspace_id": data.get("workspace_id", "wD"),
        "workspace_label": "owner-work",
        "tab_id": "wD:t1",
        "tab_label": "2",
        "focused_pane_id": pane_id,
        "focused_pane_agent": "omp",
        "focused_pane_cwd": "/tmp/work",
    }
    monkeypatch.setenv("HERDR_PLUGIN_EVENT", event_name)
    monkeypatch.setenv("HERDR_PLUGIN_EVENT_JSON", json.dumps({"event": event_name, "data": data}))
    monkeypatch.setenv("HERDR_PLUGIN_CONTEXT_JSON", json.dumps(context))
    return cli.main(["notify"])


def route_to(monkeypatch, name):
    monkeypatch.setattr(cli.herdr, "agent_name", lambda pane_id: name)


def test_prefixed_session_reaches_the_owner_webhook(isolated_env, webhook_server, monkeypatch):
    configure_cto(monkeypatch, isolated_env["config_dir"], webhook_server)
    route_to(monkeypatch, "cto--hwn")

    assert send_event(monkeypatch, "pane.agent_status_changed", status_event("done")) == 0

    assert len(webhook_server.requests) == 1
    request = webhook_server.requests[0]
    assert request["path"] == "/cto"
    assert request["headers"]["authorization"] == "Bearer wake-token"
    body = json.loads(request["body"])
    assert body["agent"] == "cto--hwn"
    assert body["owner"] == "cto"
    assert body["pane_id"] == "wD:p1"
    assert body["workspace_id"] == "wD"
    assert body["status"] == "done"
    assert body["event"] == "done"
    assert body["herdr_event"] == "pane.agent_status_changed"
    assert datetime.fromisoformat(body["time"]).tzinfo is not None


def test_session_without_owner_prefix_is_ignored(isolated_env, webhook_server, monkeypatch):
    configure_cto(monkeypatch, isolated_env["config_dir"], webhook_server)
    route_to(monkeypatch, "mobius-omp")

    send_event(monkeypatch, "pane.agent_status_changed", status_event("done"))
    assert webhook_server.requests == []


def test_session_with_unknown_owner_is_ignored(isolated_env, webhook_server, monkeypatch):
    configure_cto(monkeypatch, isolated_env["config_dir"], webhook_server)
    route_to(monkeypatch, "qa--suite")

    send_event(monkeypatch, "pane.agent_status_changed", status_event("done"))
    assert webhook_server.requests == []


def test_each_owner_receives_only_its_own_sessions(isolated_env, webhook_server, monkeypatch):
    text = f"""
[http]
retries = 0

[owners.cto]
url = "{base_url(webhook_server)}/cto"

[owners.qa]
url = "{base_url(webhook_server)}/qa"
header = "X-Owner: qa"

[owners.dev]
url = "{base_url(webhook_server)}/dev"
"""
    write_config(isolated_env["config_dir"], text)
    names = {"wD:p1": "qa--suite", "wD:p2": "cto--hwn"}
    monkeypatch.setattr(cli.herdr, "agent_name", lambda pane_id: names.get(pane_id, ""))

    send_event(monkeypatch, "pane.agent_status_changed", status_event("done", pane_id="wD:p1"))
    send_event(monkeypatch, "pane.agent_status_changed", status_event("done", pane_id="wD:p2"))

    paths = [request["path"] for request in webhook_server.requests]
    assert paths == ["/qa", "/cto"]
    assert json.loads(webhook_server.requests[0]["body"])["agent"] == "qa--suite"
    assert json.loads(webhook_server.requests[1]["body"])["agent"] == "cto--hwn"
    assert webhook_server.requests[0]["headers"]["x-owner"] == "qa"
    assert "x-owner" not in webhook_server.requests[1]["headers"]


def test_provider_delivery_still_runs_for_owner_sessions(isolated_env, webhook_server, monkeypatch):
    text = f"""
[http]
retries = 0

[providers.generic]
enabled = true
url = "{base_url(webhook_server)}/hook"

[owners.cto]
url = "{base_url(webhook_server)}/cto"
"""
    write_config(isolated_env["config_dir"], text)
    route_to(monkeypatch, "cto--hwn")

    send_event(monkeypatch, "pane.agent_status_changed", status_event("done"))
    assert sorted(request["path"] for request in webhook_server.requests) == ["/cto", "/hook"]


def test_pane_exit_uses_the_agent_name_captured_while_alive(
    isolated_env, webhook_server, monkeypatch
):
    configure_cto(monkeypatch, isolated_env["config_dir"], webhook_server)
    found = {"name": "cto--hwn"}
    monkeypatch.setattr(cli.herdr, "agent_name", lambda pane_id: found["name"])

    send_event(monkeypatch, "pane.agent_status_changed", status_event("working"))
    found["name"] = ""  # the pane is gone, so the agent list no longer has it
    send_event(monkeypatch, "pane.exited", {"type": "pane_exited", "pane_id": "wD:p1", "workspace_id": "wD"})

    assert len(webhook_server.requests) == 1
    body = json.loads(webhook_server.requests[0]["body"])
    assert body["agent"] == "cto--hwn"
    assert body["event"] == "exited"


def test_quiet_hours_are_off_by_default(isolated_env):
    write_config(isolated_env["config_dir"], OWNER_CONFIG)
    cfg = config_mod.load()

    assert cfg.notify["quiet_hours"] == []
    assert cfg.notify["quiet_ranges"] == []
    decision = events.Decision("done", "done", False)
    fields = {"workspace": "w1", "tab": "1", "agent": "omp", "duration_seconds": 30}
    allowed, _reason = filters.evaluate(cfg, decision, fields, {}, 0.0, datetime(2026, 1, 1, 3, 0))
    assert allowed


def test_owner_route_alone_is_enough_to_notify(isolated_env, webhook_server, monkeypatch):
    configure_cto(monkeypatch, isolated_env["config_dir"], webhook_server)
    assert not any(
        "no provider is enabled" in warning for warning in config_mod.load().warnings
    )
    route_to(monkeypatch, "cto--hwn")

    send_event(monkeypatch, "pane.agent_status_changed", status_event("done"))
    assert len(webhook_server.requests) == 1


def test_test_action_sends_an_owner_sample(isolated_env, webhook_server, monkeypatch, capsys):
    configure_cto(monkeypatch, isolated_env["config_dir"], webhook_server)

    assert cli.main(["test", "owner", "done"]) == 0
    output = capsys.readouterr().out
    assert "OK   owner:cto (HTTP 200)" in output
    body = json.loads(webhook_server.requests[0]["body"])
    assert body["agent"] == "cto--test"
    assert body["event"] == "done"


def test_test_action_rejects_an_unknown_owner(isolated_env, webhook_server, monkeypatch, capsys):
    configure_cto(monkeypatch, isolated_env["config_dir"], webhook_server)
    assert cli.main(["test", "owner:nope"]) == 2
    assert "Unknown owner 'nope'" in capsys.readouterr().out


def test_preview_prints_the_owner_request_without_sending(
    isolated_env, webhook_server, monkeypatch, capsys
):
    monkeypatch.setenv("CTO_WAKE_URL", f"{base_url(webhook_server)}/cto")
    monkeypatch.setenv("CTO_WAKE_HEADER", "Authorization: Bearer s3cret-value")
    write_config(isolated_env["config_dir"], OWNER_CONFIG)

    assert cli.main(["preview", "owner"]) == 0
    output = capsys.readouterr().out
    assert webhook_server.requests == []
    assert "owner:cto: POST" in output
    assert "Authorization: ***" in output
    assert "s3cret-value" not in output
    assert '"agent": "cto--test"' in output


def test_status_lists_owner_routes_with_masked_url_and_header(
    isolated_env, webhook_server, monkeypatch, capsys
):
    configure_cto(monkeypatch, isolated_env["config_dir"], webhook_server)
    assert cli.main(["status"]) == 0
    output = capsys.readouterr().out
    assert "owners:" in output
    assert "cto -> http://127.0.0.1:" in output and "***" in output
    assert "(header Authorization)" in output
    assert "wake-token" not in output


def test_status_survives_an_unresolved_header_variable(isolated_env, capsys):
    (Path(isolated_env["config_dir"]) / "config.toml").write_text(
        '[owners.cto]\nurl = "https://example.test/cto"\nheader = "${NOT_SET_ANYWHERE}"\n',
        encoding="utf-8",
    )
    assert cli.main(["status"]) == 0
    output = capsys.readouterr().out
    assert "header needs its ${VAR} set" in output


def test_agent_name_is_read_from_the_herdr_agent_list(monkeypatch):
    payload = {
        "result": {
            "agents": [
                {"pane_id": "wK:p1", "name": "cto--hwn"},
                {"pane_id": "wK:p2", "name": ""},
            ]
        }
    }
    monkeypatch.setattr(herdr, "cli_json", lambda args, fresh=False: payload)
    assert herdr.agent_name("wK:p1") == "cto--hwn"
    assert herdr.agent_name("wK:p2") == ""
    assert herdr.agent_name("wK:p9") == ""
    assert herdr.agent_name("") == ""


def test_owner_names_must_match_the_agent_prefix_format(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[owners.CTO]\nurl = "https://example.test/hook"\n', encoding="utf-8")
    try:
        config_mod.load(str(path))
    except config_mod.ConfigError as exc:
        assert "owner names must match" in str(exc)
    else:  # pragma: no cover - the assertion above is the expectation
        raise AssertionError("an invalid owner name must be rejected")


def test_owner_without_url_is_disabled_with_a_warning(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[owners.cto]\nheader = 'X-A: b'\n", encoding="utf-8")
    cfg = config_mod.load(str(path))
    assert cfg.owners == {}
    assert any("owners.cto" in warning and "disabled" in warning for warning in cfg.warnings)


def test_empty_env_value_disables_the_route_instead_of_breaking_the_plugin(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CTO_WAKE_URL", "")
    monkeypatch.setenv("CTO_WAKE_HEADER", "")
    path = tmp_path / "config.toml"
    path.write_text(
        '[providers.generic]\nenabled = true\nurl = "https://example.test/hook"\n'
        '[owners.cto]\nurl = "${CTO_WAKE_URL}"\nheader = "${CTO_WAKE_HEADER}"\n',
        encoding="utf-8",
    )
    cfg = config_mod.load(str(path))
    assert cfg.owners == {}
    assert cfg.enabled == ["generic"]


def test_owner_header_must_be_one_name_value_line(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[owners.cto]\nurl = "https://example.test/hook"\nheader = "Authorization Bearer x"\n',
        encoding="utf-8",
    )
    try:
        config_mod.load(str(path))
    except config_mod.ConfigError as exc:
        assert "owners.cto.header" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an invalid header must be rejected")


def test_owner_url_and_header_are_interpolated(tmp_path, monkeypatch):
    monkeypatch.setenv("OWNER_HOOK", "https://example.test/owner")
    monkeypatch.setenv("OWNER_AUTH", "X-Wake: abc")
    path = tmp_path / "config.toml"
    path.write_text(
        '[owners.cto]\nurl = "${OWNER_HOOK}"\nheader = "${OWNER_AUTH}"\n', encoding="utf-8"
    )
    cfg = config_mod.load(str(path))
    assert cfg.owners["cto"] == {"url": "https://example.test/owner", "header": "X-Wake: abc"}
    assert owners.parse_header(cfg.owners["cto"]["header"]) == ("X-Wake", "abc")


def test_unknown_owner_key_is_reported(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[owners.cto]\nurl = "https://example.test/hook"\nheadr = "X-A: b"\n', encoding="utf-8"
    )
    cfg = config_mod.load(str(path))
    joined = " | ".join(cfg.unknown_keys)
    assert "owners.cto.headr" in joined and "header" in joined


def test_owner_route_with_a_broken_url_reports_a_failure(isolated_env, capsys):
    text = '[owners.cto]\nurl = "notaurl"\n'
    write_config(isolated_env["config_dir"], text)

    assert cli.main(["test", "owner"]) == 1
    output = capsys.readouterr().out
    assert "FAIL owner:cto" in output
    assert "notaurl" in output
