# `bug-report-cli` — the agent/human CLI

`report-bug "title or first line"` from a terminal, a CI job, or a Claude-spawned tmux session.

## Install

```bash
pip install "bug-report-cli @ git+https://github.com/LeapAutomation/bug-report-widget.git#subdirectory=packages/cli"
```

## Configure

| Variable | Default | What it does |
|----------|---------|--------------|
| `BUG_REPORT_URL` | `http://127.0.0.1:8765/api/bugs` | Where to POST. |
| `BUG_REPORTER_EMAIL` | empty | Stamped on the report as the actor. |
| `BUG_ADDED_BY` | `agent` | Set to `cli` if a human is invoking. |

## Use

```bash
# Quickest form — title only
report-bug "ci suite hangs on apple-silicon runner"

# Title + details
report-bug -t "ci flake" -d "test_login fails 1/5 runs on staging"

# Pipe stdin
pytest -x 2>&1 | report-bug "test failure"

# Bundle a chat transcript (for agent-filed bugs from chat surfaces)
report-bug -t "tool returned bad json" -d "see transcript" \
    --transcript-file ~/.claude/last-10-turns.json

# Tag for triage
report-bug -t "perf regression" -d "p95 doubled after deploy abc1234" \
    --tag perf --tag critical
```

## Why exists

The widget covers the in-app path. The CLI covers everything else:

- **Agents** running outside a web context (a tmux-spawned Claude, a CI job, a server-side script) need a way to file bugs. They shell out to `report-bug`.
- **Humans** at a terminal benefit too — fewer context switches than opening the web app.

Every report through the CLI lands in the same review queue as web-filed bugs. `--added-by=agent` (the default for CLI) gets auto-tagged `agent-self-report` server-side so admins can spot agent-flagged issues.

## Tests

```bash
cd packages/cli
pip install -e ".[test]"
pytest
```
