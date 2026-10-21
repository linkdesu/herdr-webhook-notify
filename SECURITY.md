# Security

## Reporting

Please report suspected vulnerabilities privately through GitHub's
"Report a vulnerability" flow on this repository rather than a public issue.

## What this plugin does

Like every Herdr plugin, it is ordinary code running with your user privileges.
It reads Herdr events, may call the Herdr CLI, and sends HTTP requests to the
endpoints you configure. Review `herdr-plugin.toml` and the Python sources
before linking or installing it.

## Secret handling

- Put webhook URLs and tokens in a `.env` next to `config.toml`, or in the
  process environment; both are read at runtime. See "Known issues" below
  before you assume a token never lands on disk.
- Prefer read-only, scope-limited tokens, for example an ntfy token that can
  only publish to one topic.
- A webhook URL is a credential: anyone holding it can post to that channel.
  Rotate it if it leaks.
- Logs, `preview` and `status` print a configured URL as scheme and host only,
  for example `https://api.telegram.org/***`: the whole path and query are
  dropped, and credential headers show `***`. `preview` still prints the
  message body it would send, so treat its output as notification content
  rather than a log line.
- Assume anything a plugin can read could end up in a crash report you paste
  elsewhere.

## Network behaviour

The plugin only connects to the URLs in your configuration. It does not phone
home, collect telemetry, or read agent transcripts; it reads pane metadata
(title, cwd, workspace, tab, agent kind) through the local Herdr socket.

Redirects are never followed. A 3xx answer is reported as a delivery failure
whose detail holds the status code and the masked `Location`, so a redirect
cannot carry the request headers, `Authorization` included, to another host.

## Known issues

- **A failed delivery can copy credentials into `state.json`.** When a request
  fails and is queued for a retry, the queue entry holds the resolved URL and
  headers, so a Telegram bot token or an `Authorization` header may be written
  to `state.json` in the plugin state directory. That file is created with the
  default umask (commonly `0644`) and is not encrypted, so other local users can
  read it, and the temporary `state.json.tmp` swap happens in the same
  directory. Keep the directory private, keep the file out of backups and
  screenshots, and rotate a webhook URL if it leaks.
