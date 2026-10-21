# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `[owners.<name>]` routes: one generic webhook per owner, hit only by sessions
  whose Herdr agent name is `<owner>--<rest>` (owner matching
  `[a-z][a-z0-9]*`), for example `cto--hwn`. The agent name is read from the
  Herdr CLI by pane id. Sessions without the prefix, and sessions whose prefix
  has no table, are never sent by this path; enabled providers keep working
  unchanged. `url` and `header` may reference `.env` values as `${VAR}`, and the
  POST body carries `agent`, `owner`, `pane_id`, `workspace_id`, `status`,
  `event`, `herdr_event` and `time`.
- `test owner` / `test owner:name` and `preview owner` exercise owner routes;
  `status` lists them.
- `{repo}` and `{worktree}` placeholders, filled from the worktree Herdr
  reports for the workspace.
- `docs/events.md`, covering hook payloads, invocation context and their limits.
- `status` now prints the configured quiet hours and tab include/exclude lists.
- `notify.blocked_delay_seconds` (default 10) waits before reporting a blocked
  pane and re-checks it first, so agents that approve their own prompts no
  longer trigger a notification. `0` restores the immediate behaviour.
- `status` also reports `blocked_delay_seconds`, the HTTP timeout and retries,
  per-provider event filters, and whether custom message templates are in use.
- The READMEs document where `state.json` lives when Herdr runs the plugin.
- `preview` prints the request each provider would build, with the URL and any
  credential headers masked, without sending it.
- `test` takes an optional kind (`test feishu blocked`) so one message shape can
  be checked without touching the rest.
- Providers accept `timeout_seconds` and `retries` to override `[http]` for a
  single destination, and queued notifications keep those values on replay.
- Unknown config keys are reported with a suggested spelling, in `status` and in
  the hook log, instead of being ignored.

### Changed

- `preview`, `status` and the logs print a webhook URL as scheme and host only
  (`https://api.telegram.org/***`). Redacting just the last path segment kept
  the Telegram bot token, which sits in the second-to-last one.

### Fixed

- Webhook requests no longer follow redirects. urllib used to re-send the
  request, `Authorization` header included, to whatever address a 3xx pointed
  at, and downgrade a POST to a GET on 301/302/303. A 3xx is now a failure and
  its `detail` carries the status code and the masked `Location`.
- One failing destination no longer aborts the whole notification. `http.send`
  returns a failed result for invalid header values and invalid URLs instead of
  raising (`urllib` raises `ValueError`), `deliver` and owner routing isolate
  each target, and ntfy strips CR/LF from the `Title` header it builds from the
  terminal title.
- `providers.generic.method` is honoured. It was validated but ignored, so
  `PUT` and `PATCH` were sent as `POST`. Queued notifications keep their method
  when they are replayed.
- A generic `body` template may contain JSON braces again. Every placeholder was
  passed through `str.format`, so the documented
  `body = '{"text": "{title}"}'` example failed with "Invalid format specifier".
- `quiet_hours` is normalized at load time, so a single string value is handled
  the same way as a list.
- README no longer claims `cooldown_seconds` merges a completion with a pane
  exit. Those are different kinds and are sent separately.

## [0.1.0] - 2026-09-20

### Added

- Herdr plugin manifest with `pane.agent_status_changed`, `pane.exited` and
  `pane.closed` event hooks.
- Completion detection that also covers `working|blocked -> idle`, which is how
  Herdr reports a turn that finishes while you watch the pane.
- Providers: feishu, dingtalk, wecom, slack, discord, teams, google_chat,
  telegram, ntfy, generic, plus `lark` for the international Feishu tenant.
- Configurable scope: event kinds, focused-pane policy, workspace/tab/agent
  include and exclude globs, quiet hours, minimum turn duration, cooldown.
- Bilingual messages (English default, `zh-CN`) with per-provider language
  overrides, custom templates and single-string overrides.
- Actions: init, test, status, mute, resume.
- Bounded retries plus a persisted queue for failed deliveries.
- 70 tests, ruff linting, CI matrix over Linux/macOS/Windows and Python 3.9/3.12.

[Unreleased]: https://github.com/zgxme/herdr-webhook-notify/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/zgxme/herdr-webhook-notify/releases/tag/v0.1.0
