"""Command dispatch: event hooks plus the init/test/status/toggle actions."""

from __future__ import annotations

import base64
import json
import os
import socket
import sys
import time
from datetime import datetime

from . import (
    __version__,
    events,
    filters,
    herdr,
    http,
    i18n,
    markup,
    owners,
    providers,
    render,
)
from . import (
    config as config_mod,
)
from . import (
    state as state_mod,
)
from .providers import base

USAGE = f"""herdr-webhook-notify {__version__}

Usage:
  python3 run.py notify            run as a Herdr event hook (default)
  python3 run.py test [provider] [kind]
                                   send a test notification
  python3 run.py preview [provider]
                                   print the request without sending it
  python3 run.py init [--force]    write a starter config.toml
  python3 run.py status            show the effective configuration
  python3 run.py toggle on|off     silence or resume notifications
"""


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def out(message: str = "") -> None:
    print(message, flush=True)


def first_str(*values) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def read_json_env(name: str) -> dict:
    raw = os.environ.get(name, "")
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        log(f"invalid {name}: {exc}")
        return {}
    return payload if isinstance(payload, dict) else {}


def main(argv=None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    command = args[0].lower() if args else ""
    if not command:
        action = os.environ.get("HERDR_PLUGIN_ACTION_ID", "")
        if action:
            command = action.rsplit(".", 1)[-1].lower()
        elif os.environ.get("HERDR_PLUGIN_EVENT"):
            command = "notify"
        else:
            command = "help"

    handlers = {
        "notify": cmd_notify,
        "test": cmd_test,
        "preview": cmd_preview,
        "init": cmd_init,
        "status": cmd_status,
        "toggle": cmd_toggle,
        "help": cmd_help,
        "--help": cmd_help,
        "-h": cmd_help,
        "version": cmd_version,
        "--version": cmd_version,
    }
    handler = handlers.get(command)
    if handler is None:
        log(f"unknown command: {command}")
        out(USAGE)
        return 2

    try:
        return handler(args[1:])
    except config_mod.ConfigError as exc:
        log(f"config error: {exc}")
        return 0 if command == "notify" else 1
    except Exception as exc:  # a broken hook must never break Herdr
        log(f"unexpected error: {exc}")
        return 0 if command == "notify" else 1


def cmd_help(_args) -> int:
    out(USAGE)
    return 0


def cmd_version(_args) -> int:
    out(__version__)
    return 0


# --------------------------------------------------------------------- notify


def collect_fields(pane_id: str, data: dict, context: dict, entry: dict, started_at, now: float) -> dict:
    info = herdr.pane(pane_id) if pane_id else {}
    # Context pane fields describe this pane only when the ids match.
    context_pane = context if first_str(context.get("focused_pane_id")) == pane_id else {}

    workspace_id = first_str(
        data.get("workspace_id"),
        context.get("workspace_id"),
        info.get("workspace_id"),
        entry.get("workspace_id"),
    )
    tab_id = first_str(context.get("tab_id"), info.get("tab_id"), entry.get("tab_id"))
    cwd = first_str(
        context_pane.get("focused_pane_cwd"),
        context.get("workspace_cwd"),
        info.get("foreground_cwd"),
        info.get("cwd"),
        entry.get("cwd"),
    )
    duration = None
    if isinstance(started_at, (int, float)) and now >= started_at:
        duration = now - started_at

    worktree = context.get("worktree")
    worktree = worktree if isinstance(worktree, dict) else {}

    return {
        "session": first_str(os.environ.get("HERDR_SESSION"), entry.get("session")) or "default",
        "workspace": first_str(
            context.get("workspace_label"),
            data.get("workspace_label"),
            herdr.workspace_label(workspace_id),
            workspace_id,
            entry.get("workspace"),
        ),
        "tab": first_str(
            context.get("tab_label"),
            herdr.tab_label(tab_id),
            tab_id,
            entry.get("tab"),
        ),
        "task": first_str(
            info.get("terminal_title_stripped"),
            info.get("terminal_title"),
            data.get("title"),
            entry.get("task"),
            pane_id,
        ),
        "agent": first_str(
            context_pane.get("focused_pane_agent"),
            data.get("display_agent"),
            data.get("agent"),
            info.get("agent"),
            entry.get("agent"),
        ),
        "pane_id": pane_id,
        "cwd": cwd,
        "branch": herdr.git_branch(cwd),
        "repo": first_str(worktree.get("repo_name")),
        "worktree": first_str(worktree.get("checkout_path")),
        "duration": render.format_duration(duration),
        "duration_seconds": duration,
        "host": socket.gethostname(),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "workspace_id": workspace_id,
        "tab_id": tab_id,
    }


def build_message(cfg, kind: str, status: str, fields: dict, language: str, timestamp: int, module):
    values = dict(fields)
    values["kind"] = kind
    values["status"] = status
    values["status_label"] = i18n.status_label(language, kind, cfg.messages)

    flavor = module.flavor(cfg.provider_options(module.NAME)) if hasattr(module, "flavor") else module.FLAVOR
    title_template = cfg.message.get("title") or i18n.translate(language, "title", cfg.messages)
    body_template = cfg.message.get("body") or i18n.translate(language, "body", cfg.messages)
    title = render.drop_empty_lines(markup.convert(render.render(title_template, values), flavor))
    body = render.drop_empty_lines(markup.convert(render.render(body_template, values), flavor))
    return base.Message(
        kind=kind,
        status=status,
        title=title,
        body=body,
        fields=values,
        timestamp=timestamp,
    )


def deliver(cfg, kind: str, status: str, fields: dict, timestamp: int, only=None, ignore_events=False):
    """Send the notification to every enabled provider. Returns result tuples."""
    results = []
    for name in cfg.enabled:
        if only and name not in only:
            continue
        options = cfg.provider_options(name)
        module = providers.get(name)
        timeout = float(options.get("timeout_seconds", cfg.http.get("timeout_seconds", 5)))
        retries = int(options.get("retries", cfg.http.get("retries", 1)))
        provider_events = options.get("events")
        if not ignore_events and provider_events and kind not in provider_events:
            results.append((name, True, None, "skipped: kind not enabled for this provider"))
            continue

        language = i18n.normalize(options.get("language") or cfg.language)
        message = build_message(cfg, kind, status, fields, language, timestamp, module)
        try:
            delivery = module.build(options, message)
        except Exception as exc:
            results.append((name, False, None, f"build failed: {exc}"))
            continue

        response = http.send(
            delivery.url,
            delivery.body,
            delivery.headers,
            timeout=timeout,
            retries=retries,
            method=delivery.method,
        )
        if response.ok:
            results.append((name, True, response.status, response.body[:200]))
        else:
            results.append((name, False, response.status, response.error or response.body))
    return results


def flush_pending(cfg, state) -> int:
    items = state.pending_items()
    if not items:
        return 0
    remaining = []
    sent = 0
    for item in items:
        attempts = int(item.get("attempts") or 0)
        if attempts >= state_mod.MAX_ATTEMPTS:
            log(f"dropping queued notification for {item.get('provider')}: too many attempts")
            continue
        try:
            body = base64.b64decode(item.get("body") or "")
        except (ValueError, TypeError):
            continue
        item_timeout = item.get("timeout_seconds")
        item_retries = item.get("retries")
        response = http.send(
            item.get("url", ""),
            body,
            item.get("headers") or {},
            timeout=float(
                item_timeout if item_timeout is not None else cfg.http.get("timeout_seconds", 5)
            ),
            retries=int(item_retries if item_retries is not None else cfg.http.get("retries", 1)),
            method=item.get("method") or "POST",
        )
        if response.ok:
            sent += 1
            continue
        item["attempts"] = attempts + 1
        item["last_error"] = response.error or response.body
        remaining.append(item)
    state.replace_pending(remaining)
    return sent


def settle_blocked(cfg, state, pane_id: str, decision, fields: dict, now: float):
    """Wait out an auto-approval before reporting a blocked pane.

    Agents that approve their own prompts go through ``blocked`` for a moment.
    Waiting gives them the chance to move on, in which case no notification is
    sent at all. Returns ``(state, now)`` to continue with, or ``None``
    when the pane stopped waiting for a human while we waited.
    """
    delay = float(cfg.notify.get("blocked_delay_seconds") or 0)
    if delay <= 0:
        return state, now

    log(f"pane {pane_id} is blocked, waiting {delay:g}s before notifying")
    time.sleep(delay)

    status = herdr.agent_status(pane_id)
    if status == "":
        log("skip notification: pane is gone, the exit event covers that")
        return None
    if status is not None and status != "blocked":
        log(f"skip notification: pane left blocked for '{status}' while we waited")
        return None

    # Other hooks may have written state while we slept, and the filters have to
    # judge the notification at the moment it is actually sent.
    now = time.time()
    state = state_mod.State()
    entry = state.pane(pane_id)
    allowed, reason = filters.evaluate(cfg, decision, fields, entry, now, datetime.now())
    if not allowed:
        log(f"skip notification: {reason}")
        return None
    return state, now


def cmd_notify(_args) -> int:
    state = state_mod.State()
    cfg = config_mod.load()

    for key in cfg.unknown_keys:
        log(f"config warning: unknown key {key}")
    if state.is_disabled():
        log("notifications are silenced, run 'toggle on' to resume")
        return 0
    if not cfg.enabled and not cfg.owners:
        for warning in cfg.warnings:
            log(warning)
        return 0

    # Anything that failed earlier gets another chance before new work.
    flushed = flush_pending(cfg, state)
    if flushed:
        log(f"flushed {flushed} queued notification(s)")

    event = read_json_env("HERDR_PLUGIN_EVENT_JSON")
    context = read_json_env("HERDR_PLUGIN_CONTEXT_JSON")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    event_name = first_str(
        os.environ.get("HERDR_PLUGIN_EVENT"),
        event.get("event") if isinstance(event.get("event"), str) else "",
    )

    pane_id = first_str(data.get("pane_id"), os.environ.get("HERDR_PANE_ID"))
    if not pane_id:
        log("event carries no pane id, nothing to do")
        return 0

    entry = state.pane(pane_id)
    previous_status = first_str(entry.get("status")).lower()
    decision = events.classify(event_name, data, previous_status)
    now = time.time()

    raw_status = first_str(data.get("agent_status")).lower()
    if events.status_event(event_name, data):
        started_at = entry.get("started_at")
        if raw_status == "working" and previous_status != "working":
            started_at = now
        state.update_pane(
            pane_id,
            status=raw_status,
            started_at=started_at,
            status_seen_at=now,
        )
    elif events.exit_event(event_name, data):
        state.forget_pane(pane_id)

    fields = collect_fields(pane_id, data, context, entry, entry.get("started_at"), now)
    # Owner routing needs the Herdr agent name, not the detected agent kind.
    # A pane that just exited is already gone from the agent list, so fall back
    # to the name captured while it was alive.
    owner_agent = ""
    if cfg.owners:
        owner_agent = herdr.agent_name(pane_id) or first_str(entry.get("agent_name"))
    if events.status_event(event_name, data):
        state.update_pane(
            pane_id,
            agent=fields["agent"],
            agent_name=owner_agent or None,
            task=fields["task"],
            workspace=fields["workspace"],
            workspace_id=fields["workspace_id"],
            tab=fields["tab"],
            tab_id=fields["tab_id"],
            cwd=fields["cwd"],
            session=fields["session"],
        )
    state.save()

    if decision is None:
        return 0

    allowed, reason = filters.evaluate(cfg, decision, fields, entry, now, datetime.now())
    if not allowed:
        log(f"skip notification: {reason}")
        return 0

    if decision.kind == "blocked":
        settled = settle_blocked(cfg, state, pane_id, decision, fields, now)
        if settled is None:
            return 0
        state, now = settled

    owner_results = []
    if cfg.owners:
        owner_results = owners.deliver(
            cfg,
            decision.kind,
            decision.status,
            fields,
            owner_agent,
            now,
            herdr_event=event_name,
        )
    sent = 0
    for owner_name, ok, status_code, detail in owner_results:
        if ok:
            sent += 1
            log(f"owner {owner_name}: delivered ({status_code})")
        else:
            log(f"owner {owner_name}: failed ({detail})")

    results = deliver(cfg, decision.kind, decision.status, fields, int(now))
    for name, ok, status_code, detail in results:
        if ok:
            sent += 1
            log(f"{name}: delivered ({status_code})")
        else:
            log(f"{name}: failed ({detail})")
            if detail.startswith("build failed"):
                continue
            options = cfg.provider_options(name)
            module = providers.get(name)
            try:
                message = build_message(
                    cfg,
                    decision.kind,
                    decision.status,
                    fields,
                    i18n.normalize(options.get("language") or cfg.language),
                    int(now),
                    module,
                )
                state.queue(
                    name,
                    module.build(options, message),
                    detail,
                    timeout=options.get("timeout_seconds", cfg.http.get("timeout_seconds", 5)),
                    retries=options.get("retries", cfg.http.get("retries", 1)),
                )
            except Exception:  # queueing is best effort
                pass

    if sent:
        state.mark_notified(pane_id, decision.kind, now)
    state.save()

    if not sent and results:
        log("no provider accepted the notification")
    return 0


# ----------------------------------------------------------------------- test


def sample_fields() -> dict:
    context = read_json_env("HERDR_PLUGIN_CONTEXT_JSON")
    return {
        "session": first_str(os.environ.get("HERDR_SESSION")) or "default",
        "workspace": first_str(context.get("workspace_label"), context.get("workspace_id")) or "my-workspace",
        "tab": first_str(context.get("tab_label"), context.get("tab_id")) or "1",
        "task": "herdr-webhook-notify test notification",
        "agent": first_str(context.get("focused_pane_agent")) or "codex",
        "pane_id": first_str(os.environ.get("HERDR_PANE_ID")) or "w1:p1",
        "cwd": os.getcwd(),
        "branch": herdr.git_branch(os.getcwd()),
        "duration": "12s",
        "duration_seconds": 12,
        "host": socket.gethostname(),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "workspace_id": first_str(context.get("workspace_id")),
        "tab_id": first_str(context.get("tab_id")),
    }


def resolve_target(cfg, target: str):
    """Map a `test`/`preview` positional onto (providers, owner, error).

    ``owner`` is ``"*"`` for every configured owner, a single owner name, or
    ``None`` when providers were selected instead.
    """
    if not target:
        return None, None, ""
    if target == "owner":
        if not cfg.owners:
            return None, None, "No owner route is configured, add an [owners.<name>] table"
        return None, "*", ""
    if target.startswith("owner:"):
        name = target.split(":", 1)[1]
        if name not in cfg.owners:
            available = ", ".join(sorted(cfg.owners)) or "none"
            return None, None, f"Unknown owner '{name}'. Available: {available}"
        return None, name, ""
    if providers.get(target) is None:
        return None, None, f"Unknown provider '{target}'. Available: {', '.join(providers.names())}"
    return {target}, None, ""


def owner_sample_agent(owner: str) -> str:
    return f"{owner}--test"


def cmd_test(args) -> int:
    cfg = config_mod.load()
    if not cfg.enabled and not cfg.owners:
        out("No provider is enabled yet.")
        out(f"Config file: {config_mod.resolve_path()}")
        out("Run 'init' to write a starter config, then set enabled = true.")
        return 1

    positional = [arg for arg in args if not arg.startswith("-")]
    only, only_owner, error = resolve_target(cfg, positional[0] if positional else "")
    if error:
        out(error)
        return 2
    kind = "test"
    if len(positional) > 1:
        kind = positional[1]
        allowed = config_mod.SUPPORTED_EVENTS + ("test",)
        if kind not in allowed:
            out(f"Unknown kind '{kind}'. Choose from {', '.join(allowed)}.")
            return 2

    fields = sample_fields()
    now = int(time.time())
    results = []
    if only_owner is None:
        results = deliver(cfg, kind, kind, fields, now, only=only, ignore_events=True)

    owner_results = []
    if only is None:
        for owner_name in sorted(cfg.owners):
            if only_owner not in (None, "*") and owner_name != only_owner:
                continue
            agent_name = owner_sample_agent(owner_name)
            owner_results += owners.deliver(
                cfg, kind, kind, fields, agent_name, now, "test", only=owner_name
            )

    failures = 0
    for name, ok, status_code, detail in results:
        if ok:
            out(f"OK   {name} (HTTP {status_code})")
        else:
            failures += 1
            out(f"FAIL {name}: {detail}")
    for owner_name, ok, status_code, detail in owner_results:
        if ok:
            out(f"OK   owner:{owner_name} (HTTP {status_code})")
        else:
            failures += 1
            out(f"FAIL owner:{owner_name}: {detail}")
    return 1 if failures else 0


# -------------------------------------------------------------------- preview


def cmd_preview(args) -> int:
    """Print the request each target would send, without sending anything."""
    cfg = config_mod.load()
    if not cfg.enabled and not cfg.owners:
        out("No provider is enabled yet.")
        out(f"Config file: {config_mod.resolve_path()}")
        out("Run 'init' to write a starter config, then set enabled = true.")
        return 1

    positional = [arg for arg in args if not arg.startswith("-")]
    targets = [resolve_target(cfg, target) for target in positional]
    errors = [error for _providers, _owner, error in targets if error]
    if errors:
        out(errors[0])
        return 2
    owner_selectors = [owner for _providers, owner, _error in targets if owner]
    only_owner = owner_selectors[0] if owner_selectors else None
    only = None
    if positional:
        only = set()
        for provider_names, _owner, _error in targets:
            only |= provider_names or set()

    fields = sample_fields()
    now = int(time.time())
    for name in cfg.enabled:
        if only is not None and name not in only:
            continue
        options = cfg.provider_options(name)
        module = providers.get(name)
        language = i18n.normalize(options.get("language") or cfg.language)
        message = build_message(cfg, "test", "test", fields, language, now, module)
        try:
            delivery = module.build(options, message)
        except Exception as exc:
            out(f"{name}: build failed: {exc}")
            continue
        timeout = float(options.get("timeout_seconds", cfg.http.get("timeout_seconds", 5)))
        retries = int(options.get("retries", cfg.http.get("retries", 1)))
        out(f"{name}: {delivery.method} {http.redact(delivery.url)}")
        out(f"  summary: {delivery.summary}")
        out(f"  timeout: {timeout:g}s   retries: {retries}")
        for key, value in sorted(http.redact_headers(delivery.headers).items()):
            out(f"  {key}: {value}")
        out("  body:")
        for line in delivery.body.decode("utf-8", "replace").splitlines():
            out(f"    {line}")
        out("")

    if not positional or owner_selectors:
        timeout = float(cfg.http.get("timeout_seconds", 5))
        retries = int(cfg.http.get("retries", 1))
        for owner_name in sorted(cfg.owners):
            if only_owner not in (None, "*") and owner_name != only_owner:
                continue
            options = cfg.owners[owner_name]
            agent_name = owner_sample_agent(owner_name)
            body = owners.payload("test", "test", fields, agent_name, owner_name, now, "test")
            try:
                delivery = owners.build(owner_name, options, body)
            except ValueError as exc:
                out(f"owner:{owner_name}: build failed: {exc}")
                continue
            out(f"owner:{owner_name}: {delivery.method} {http.redact(delivery.url)}")
            out(f"  summary: {delivery.summary} (agent {agent_name})")
            out(f"  timeout: {timeout:g}s   retries: {retries}")
            for key, value in sorted(http.redact_headers(delivery.headers).items()):
                out(f"  {key}: {value}")
            out("  body:")
            for line in delivery.body.decode("utf-8", "replace").splitlines():
                out(f"    {line}")
            out("")
    return 0


# ----------------------------------------------------------------------- init


def cmd_init(args) -> int:
    force = "--force" in args
    target = config_mod.resolve_path(for_write=True)
    example = config_mod.plugin_root() / "examples" / "config.toml"
    if target.exists() and not force:
        out(f"Config already exists: {target}")
        out("Re-run with --force to overwrite it.")
        return 1
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError as exc:
        out(f"Cannot write {target}: {exc}")
        return 1

    out(f"Wrote {target}")
    out("")
    out("Next steps:")
    out("  1. open the file and set enabled = true on the provider you use")
    out("  2. put secrets in an .env file next to it, e.g. FEISHU_WEBHOOK=...")
    out("  3. verify with: herdr plugin action invoke herdr-webhook-notify.test")
    return 0


# --------------------------------------------------------------------- status


def cmd_status(_args) -> int:
    cfg = config_mod.load()
    state = state_mod.State()
    path = cfg.path

    out(f"herdr-webhook-notify {__version__}")
    out(f"config      : {path if path else 'built-in defaults (no config.toml found)'}")
    out(f"state       : {state.path}")
    out(f"language    : {cfg.language} (available: {', '.join(i18n.available())})")
    out(f"silenced    : {state.is_disabled()}")
    notify = cfg.notify
    out(f"events      : {', '.join(notify['events'])}")
    out(f"focused     : {'notify' if notify['notify_when_focused'] else 'skip'} when you watch the pane")
    out(f"min turn    : {notify['min_turn_seconds']:g}s   cooldown: {notify['cooldown_seconds']:g}s")
    out(f"blocked wait: {notify['blocked_delay_seconds']:g}s")
    out(
        f"http        : timeout {cfg.http.get('timeout_seconds', 5):g}s"
        f"   retries {int(cfg.http.get('retries', 1))}"
    )
    if notify["include_workspaces"]:
        out(f"include ws  : {', '.join(notify['include_workspaces'])}")
    if notify["exclude_workspaces"]:
        out(f"exclude ws  : {', '.join(notify['exclude_workspaces'])}")
    if notify["include_tabs"]:
        out(f"include tab : {', '.join(notify['include_tabs'])}")
    if notify["exclude_tabs"]:
        out(f"exclude tab : {', '.join(notify['exclude_tabs'])}")
    if notify["include_agents"]:
        out(f"include ag  : {', '.join(notify['include_agents'])}")
    if notify["exclude_agents"]:
        out(f"exclude ag  : {', '.join(notify['exclude_agents'])}")
    hours = ", ".join(str(item) for item in notify["quiet_hours"]) or "none"
    exempt = ", ".join(str(item) for item in notify["quiet_hours_exempt"]) or "none"
    out(f"quiet hours : {hours} (exempt: {exempt})")
    overrides = [key for key in ("title", "body") if cfg.message.get(key)] + sorted(cfg.messages)
    out(f"message     : {'custom: ' + ', '.join(overrides) if overrides else 'built-in'}")

    out("")
    if cfg.enabled:
        out("providers:")
        for name in cfg.enabled:
            options = cfg.provider_options(name)
            module = providers.get(name)
            target = first_str(options.get("webhook_url"), options.get("url"), options.get("topic"), options.get("chat_id"))
            detail = http.redact(target) if target.startswith("http") else target
            language = i18n.normalize(options.get("language") or cfg.language)
            provider_events = options.get("events")
            scope = f" events=[{', '.join(provider_events)}]" if provider_events else ""
            out(f"  - {name} ({module.TITLE}) language={language}{scope} {detail}")
    else:
        out("providers: none enabled")

    if cfg.owners:
        out("")
        out("owners:")
        for name in sorted(cfg.owners):
            options = cfg.owners[name]
            try:
                header = owners.parse_header(options.get("header") or "")
            except ValueError:
                header = None
                header_note = "header needs its ${VAR} set"
            else:
                header_note = f"header {header[0]}" if header else "no header"
            out(f"  - {name} -> {http.redact(options['url'])} ({header_note})")
    else:
        out("owners: none")

    pending = state.pending_items()
    out("")
    out(f"queued      : {len(pending)}")
    for item in pending[-3:]:
        out(f"  - {item.get('provider')} attempts={item.get('attempts')} error={item.get('last_error')}")

    for warning in cfg.warnings:
        out(f"warning     : {warning}")
    return 0


# --------------------------------------------------------------------- toggle


def cmd_toggle(args) -> int:
    state = state_mod.State()
    action = args[0].lower() if args else ""
    if action in ("on", "resume", "enable"):
        state.set_disabled(False)
        state.save()
        out("notifications enabled")
        return 0
    if action in ("off", "mute", "disable"):
        state.set_disabled(True)
        state.save()
        out("notifications silenced (run 'toggle on' to resume)")
        return 0
    out(f"notifications are {'silenced' if state.is_disabled() else 'enabled'}")
    out("usage: toggle on|off")
    return 0
