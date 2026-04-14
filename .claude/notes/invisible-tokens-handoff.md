# Invisible-Tokens Investigation — Handoff

Status as of 2026-04-13. Use this doc to rebuild context after a compaction.

## Origin

Chris saved `Claude_Code_may_be_burning_your_limits_with_invisible_tokens_-_Efficienist.md` (plus two Reddit/X posts with the same claim) in the repo root. Article claim:

- Starting Claude Code **v2.1.100**, every request carries ~20,000 extra tokens injected **server-side**
- v2.1.98 billed 49,726 input tokens for a prompt; v2.1.100 billed 69,922 for the same prompt
- v2.1.100 request was **smaller in bytes sent from client** but billed more — ruling out client-side growth
- The extra tokens don't appear in `/context` and aren't in Anthropic's changelog
- Community workaround: pin to v2.1.98 via `npx claude-code@2.1.98`

Chris asked "can we use what we have in ccproxy-api to investigate?" — the proxy sits between Claude Code and Anthropic and logs billed `tokens_input` + client `user_agent`, so yes.

## Plan file

`.claude/plans/purring-splashing-nebula.md` — original plan from plan-mode. Skim for full method; it's still the reference for how to run the experiment.

## Experiment setup

Isolated under `/tmp/ccproxy-invisible-tokens/` so nothing touches `~/.config/ccproxy/`:

- Config: `/tmp/ccproxy-invisible-tokens/config.toml`
- Access log (structured JSON lines): `/tmp/ccproxy-invisible-tokens/access.log`
- Provider log: `/tmp/ccproxy-invisible-tokens/provider.log`
- Per-request traces: `/tmp/ccproxy-invisible-tokens/traces/*.json`
- DuckDB analytics: `/tmp/ccproxy-invisible-tokens/analytics.duckdb` (reset it to re-run cleanly)
- Server stdout: `/tmp/ccproxy-invisible-tokens/server.log` (overwritten each run)
- Analyze helper: `/tmp/ccproxy-invisible-tokens/analyze.py` (parses a pair of trace bodies, prints sizes + tool counts)

Throwaway test dirs (avoid CLAUDE.md auto-injection): `/tmp/tok-test-98/`, `/tmp/tok-test-latest/`.

### How to run the experiment

```bash
# 0. Make sure ccproxy is auth'd with Claude (one-time):
uv run --project /Users/chris/dev/github/chrisvaillancourt/ccproxy-api ccproxy auth login claude-api

# 1. Start the proxy with the experiment config. With the fixes in this branch
#    you no longer need CONFIG_FILE env; -c alone is enough.
uv run ccproxy serve -c /tmp/ccproxy-invisible-tokens/config.toml
#    (run in a separate shell or with run_in_background from Claude Code)

# 2. Pin an old Claude Code against the proxy from a clean dir:
cd /tmp/tok-test-98
ANTHROPIC_BASE_URL=http://127.0.0.1:8001/claude ANTHROPIC_API_KEY=dummy \
  pnpm dlx @anthropic-ai/claude-code@2.1.98 -p "reply with only the word pong"

# 3. Repeat with current:
cd /tmp/tok-test-latest
ANTHROPIC_BASE_URL=http://127.0.0.1:8001/claude ANTHROPIC_API_KEY=dummy \
  pnpm dlx @anthropic-ai/claude-code@2.1.105 -p "reply with only the word pong"

# 4. Inspect raw SSE (billed tokens live here, not in DuckDB yet — see "Known gap"):
ls /tmp/ccproxy-invisible-tokens/traces/*streaming_response.json
jq '.response_text' /tmp/ccproxy-invisible-tokens/traces/<id>_*_access_log_streaming_response.json
```

### Pnpm, not npm

Chris corrected me early on: use `pnpm dlx`, not `npx`. Always.

## Finding so far

| | v2.1.98 | v2.1.105 | Δ |
|---|---|---|---|
| Client → proxy body | 73,257 B | 76,331 B | +3,074 B |
| Proxy → upstream body | 74,126 B | 77,279 B | +3,153 B |
| `input_tokens` | 3 | 3 | 0 |
| `cache_creation_input_tokens` | 18,734 | 19,593 | **+859** |
| `cache_read_input_tokens` | 0 | 0 | 0 |

Full explanation:
- v2.1.105 adds a new tool `ScheduleWakeup` — **+3,268 chars** in `tools[]`
- `Agent` tool description grew **+195 chars**
- System prompt actually shrank **−332 chars**
- Net +3,153 upstream body bytes → +859 billed tokens (~3.6 B/token, typical)

**The article's "smaller body, more tokens, server-side injection" claim does not reproduce in this data.** Delta is fully client-side, fully visible, fully explained.

Caveats: n=1 per version; proxy transforms the body before forwarding so some of the billed content is our own system-prompt injection — but that's identical between runs so the delta is clean. Article compared v2.1.98 vs v2.1.100 specifically; I tested v2.1.98 vs v2.1.105. Retest with v2.1.100 if paranoid.

## Committed fixes (branch `fix/plugin-wiring-and-adaptive-thinking`)

All pushed to `origin/fix/plugin-wiring-and-adaptive-thinking` (which is `chrisvaillancourt/ccproxy-api`). Not yet opened as a PR.

| Commit | What | Files |
|---|---|---|
| `69b1b52` fix(llms): accept adaptive thinking type in Claude Code requests | Added `ThinkingConfigAdaptive` to `ThinkingConfig` union. Without this every 2.1.x request 422'd at `body -> thinking: Input tag 'adaptive'…` | `ccproxy/llms/models/anthropic.py`, `tests/unit/llms/test_thinking_config.py` (new, 6 tests) |
| `f221821` fix(cli): propagate --config path to uvicorn app factory | `_run_local_server` sets `os.environ["CONFIG_FILE"]` before `uvicorn.run`. Without this the factory's `Settings.from_config()` hit `find_toml_config_file()` and loaded the default `~/.config/ccproxy/config.toml`, dropping every plugin override from `-c` | `ccproxy/cli/commands/serve.py` |
| `514373e` fix(plugins): expose plugin registry in context during initialize_all | `PluginRegistry.initialize_all` registers `self` into `ServiceContainer`; `create_context` resolves it from there. Unblocks every plugin that uses `context.plugin_registry` (duckdb_storage, analytics, access_log, claude_api pricing). Root cause: `interfaces.py:165` set it to `None` and nothing populated it | `ccproxy/core/plugins/factories.py`, `ccproxy/core/plugins/interfaces.py` |
| `b22c63b` fix(access_log): look up analytics ingest via PluginRegistry | access_log was querying `ServiceContainer` by class; analytics registers by string into `PluginRegistry`. Switched to `plugin_registry.get_service("analytics_ingest")` + added `dependencies=["analytics"]` for init ordering. DuckDB `access_logs` table now created and per-request rows ingested | `ccproxy/plugins/access_log/plugin.py` |

Test suite: 573 passing, +6 new. Run with `uv run --group test pytest tests/unit/`.

## Known gap — not fixed

**`tokens_input = 0` in the access log / DuckDB for streaming claude_api requests.** Billed tokens are captured in Anthropic's SSE `message_start.usage` (visible in `/tmp/ccproxy-invisible-tokens/traces/<id>_*_access_log_streaming_response.json` under `response_text`) but never propagated into ccproxy's `usage_metrics` dict.

Root cause:

- `ccproxy/plugins/claude_api/hooks.py:68` — `ClaudeAPIStreamingMetricsHook.__call__` filters: `if context.provider != "claude_api": return`
- `ccproxy/streaming/deferred.py:295,369` — sets `provider = request_context.metadata.get("service_type", "unknown")` when emitting `PROVIDER_STREAM_CHUNK`
- Nobody sets `metadata["service_type"] = "claude_api"` for claude_api requests. Only `ccproxy/plugins/claude_sdk/adapter.py:290` does it, and only for `"claude_sdk"`.
- Result: stream chunks carry `provider="unknown"`, the claude_api hook short-circuits, `usage_metrics` stays `{}`, everything downstream (access log, DuckDB, cost calculation, prometheus metrics) reads 0.

Fix likely goes in whichever layer decides a request is claude_api-bound — probably `ccproxy/plugins/claude_api/adapter.py` around `process_provider_request`, setting `metadata["service_type"] = "claude_api"` on the request context before the streaming handler runs. Needs tracing the request_context lifecycle first.

Workaround: parse `response_text` from the trace JSON directly (what this investigation did).

## Other known issues (noted but not fixed)

- **Codex plugin startup error** — `using_fallback_codex_data` + a Python traceback for codex on every `ccproxy serve` start. Unrelated to this work; harmless without codex credentials configured.
- **duckdb read lock** — can't query `/tmp/ccproxy-invisible-tokens/analytics.duckdb` while the proxy is running. Stop proxy first, or set `read_only=True` on the connection.

## Repo state

- Branch `fix/plugin-wiring-and-adaptive-thinking` on `origin` (= `chrisvaillancourt/ccproxy-api`), pushed, no PR
- `upstream` = `CaddyGlow/ccproxy-api`; fork is already configured
- `main` is clean — all fixes on the feature branch
- Untracked: this file, `.claude/plans/purring-splashing-nebula.md`, and the three article `.md` files in the repo root (research input, not for commit)

## Possible next steps

1. **Fix the service_type gap** so claude_api streaming requests populate `tokens_input` in the access log / DuckDB. See "Known gap" for the exact root cause.
2. **Repeat the experiment** to reduce n=1 variance. Cache TTL is 5 min, so either wait or use fresh session IDs. Also worth testing **v2.1.100 specifically** (the article's exact comparison point) in case the +20K was introduced and fixed between 100 and 105.
3. **Open a PR upstream** to `CaddyGlow/ccproxy-api` for the four fixes (or at least the thinking-adaptive one — that's the most user-visible). Maybe bundle with a companion fix for the service_type gap.
4. **Analyze real traffic** — now that the pipeline works, point your day-to-day Claude Code at the proxy for a week and run the retrospective query from the plan file (`.claude/plans/purring-splashing-nebula.md` Step 2).

## Key anchors for quick navigation

| Thing | Path |
|---|---|
| Original plan | `.claude/plans/purring-splashing-nebula.md` |
| Experiment config | `/tmp/ccproxy-invisible-tokens/config.toml` |
| Analysis helper | `/tmp/ccproxy-invisible-tokens/analyze.py` |
| Trace files | `/tmp/ccproxy-invisible-tokens/traces/*streaming_response.json` |
| Access logs schema | `ccproxy/plugins/analytics/models.py:11` |
| Adapter transforms | `ccproxy/plugins/claude_api/adapter.py` (strips temperature, injects system prompt, overwrites `anthropic-beta`) |
| Streaming metrics hook | `ccproxy/plugins/claude_api/hooks.py:68` (the `provider != "claude_api"` filter — known gap) |
| Chunk emit site | `ccproxy/streaming/deferred.py:295, 369` (where `provider = service_type` is set) |
| PluginRegistry registration | `ccproxy/core/plugins/factories.py:456, 476` (register_service, get_service) |
| ServiceContainer | `ccproxy/services/container.py:100-123` (register_service, get_service — class-keyed) |
