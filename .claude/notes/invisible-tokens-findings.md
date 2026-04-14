# Invisible Token Injection — Complete Findings

Date: 2026-04-14. Self-contained reference for any agent picking up this work.

## The Issue

Anthropic's API server injects additional hidden tokens into Claude Code requests based on the `User-Agent` header string. Starting with `claude-cli/2.1.100`, the server injects ~10K more tokens per request (empty project) or ~20K+ (real project with CLAUDE.md/MCP). These tokens are billed as `cache_creation_input_tokens`, occupy the model's context window, and are invisible to users.

**Workaround:** Set `ANTHROPIC_CUSTOM_HEADERS=User-Agent: claude-cli/2.1.98 (external, cli)` in Claude Code settings or shell environment.

## Background

Community articles reported CC v2.1.100+ silently bills ~20K extra tokens per request. Claims:

- v2.1.98 billed 49,726 input tokens; v2.1.100 billed 69,922 for the same prompt
- v2.1.100 request was smaller in bytes but billed more, ruling out client-side growth
- The extra tokens don't appear in `/context` and aren't in Anthropic's changelog
- GitHub issue: anthropics/claude-code#46917 (open, labeled "bug", "has repro", "area:cost")

Source material (markdown exports saved in repo root, not for commit):

- [Claude Code may be burning your limits with invisible tokens](https://efficienist.com/claude-code-may-be-burning-your-limits-with-invisible-tokens-you-cant-see-or-audit/)
- [Why Claude Code Max burns limits 40% faster with 20K less usable context. Proxy evidence inside. : r/ClaudeAI](https://www.reddit.com/r/ClaudeAI/comments/1sj8o9l/why_claude_code_max_burns_limits_40_faster_with/)
- [Usage limits hit me out of the blue! Found a 20K phantom token bug + cache issues. Evidence and fix inside. : r/ClaudeCode](https://www.reddit.com/r/ClaudeCode/comments/1sj10ou/usage_limits_hit_me_out_of_the_blue_found_a_20k/)

## Investigation

### Phase 1: ccproxy-based experiment (2026-04-13)

**Goal:** Run pinned CC v2.1.98 and v2.1.105 through ccproxy, capture full request/response, compare billed tokens.

**Setup:** Isolated under `/tmp/ccproxy-invisible-tokens/` with custom config, access log, provider log, per-request trace files, and DuckDB analytics. Throwaway test dirs at `/tmp/tok-test-98/` and `/tmp/tok-test-latest/`. Full setup details in original plan at `.claude/plans/purring-splashing-nebula.md`.

**Four bugs fixed to make the experiment work** (branch `fix/plugin-wiring-and-adaptive-thinking`, all pushed to origin, no PR yet):

| Commit | Fix | Files |
|---|---|---|
| `69b1b52` | Accept `adaptive` thinking type in CC requests. Without this, every CC 2.1.x request 422'd: `body -> thinking: Input tag 'adaptive'…` | `ccproxy/llms/models/anthropic.py`, `tests/unit/llms/test_thinking_config.py` (new) |
| `f221821` | Propagate `--config` path to uvicorn app factory. Without this, `-c /path/to/config.toml` was silently ignored — factory loaded `~/.config/ccproxy/config.toml` instead | `ccproxy/cli/commands/serve.py` |
| `514373e` | Expose plugin registry in context during `initialize_all`. `interfaces.py:165` set it to `None` and nothing populated it, breaking every plugin that uses `context.plugin_registry` | `ccproxy/core/plugins/factories.py`, `ccproxy/core/plugins/interfaces.py` |
| `b22c63b` | Access log: look up analytics ingest via PluginRegistry instead of ServiceContainer (class-keyed vs string-keyed mismatch) | `ccproxy/plugins/access_log/plugin.py` |

Test suite: 573 passing, +6 new. Run: `uv run --group test pytest tests/unit/`

**Phase 1 result:**

| | v2.1.98 | v2.1.105 | Delta |
|---|---|---|---|
| Client -> proxy body | 73,257 B | 76,331 B | +3,074 B |
| Proxy -> upstream body | 74,126 B | 77,279 B | +3,153 B |
| `cache_creation_input_tokens` | 18,734 | 19,593 | **+859** |

The +859 tokens were fully explained by client-side `tools[]` growth: new `ScheduleWakeup` tool (+3,268 chars), `Agent` description (+195 chars), system prompt shrank (-332 chars). Net +3,153 upstream bytes = +859 tokens at ~3.6 B/token.

**Phase 1 conclusion: Did not reproduce the article's +20K claim.** But something was wrong — see Phase 2.

### Phase 2: Diagnosing why ccproxy didn't reproduce (2026-04-14)

Inspected the trace file (`8fda393d`) headers and discovered ccproxy **rewrites the User-Agent** before forwarding to Anthropic:

```
Client  -> ccproxy:   user-agent: claude-cli/2.1.98 (external, cli)
ccproxy -> Anthropic:  user-agent: claude-cli/2.1.105 (external, cli)
```

**Root cause:** `ClaudeAPIDetectionService` at `ccproxy/plugins/claude_api/detection_service.py:71-97`:

1. At startup, runs `claude --version` against the system-installed CC binary (v2.1.105)
2. Probes that binary to capture its HTTP headers, caches them in `_cached_data`
3. `get_detected_headers()` (line 125) returns those cached headers
4. Adapter at `claude_api/adapter.py:94-106` merges cached headers onto every outgoing request
5. `ignores_header` (line 37-43) excludes `host`, `content-length`, `authorization`, `x-api-key` — but **not** `user-agent`
6. Result: every request to Anthropic carries v2.1.105's UA regardless of which CC version the client actually is

The `anthropic-beta` header is also rewritten — ccproxy adds `oauth-2025-04-20` and `context-1m-2025-08-07` flags not present in the client's original request.

**Phase 2 conclusion: The Phase 1 experiment was invalid.** Both test runs sent Anthropic identical UA headers. Any UA-keyed server-side injection was the same for both, so the +859 delta measured only client-side payload growth.

### Phase 3: Direct test bypassing ccproxy (2026-04-14)

Tested the UA hypothesis directly, no proxy involved. Used the system CC binary (2.1.105) with `ANTHROPIC_CUSTOM_HEADERS` to forge different UA strings per the workaround suggested by `@fabifont` in anthropics/claude-code#46917.

**Method:**

```bash
mkdir -p /tmp/tok-ua-test && cd /tmp/tok-ua-test

# Default UA (2.1.105):
claude --print --output-format json --no-session-persistence \
  "reply with only the word pong" > run-A-default-ua.json

# Forged UA (2.1.98):
ANTHROPIC_CUSTOM_HEADERS='User-Agent: claude-cli/2.1.98 (external, cli)' \
  claude --print --output-format json --no-session-persistence \
  "reply with only the word pong" > run-B-forged-ua98.json

# More UAs (sweep):
for v in 2.1.91 2.1.99 2.1.100 2.1.101 2.1.104; do
  ANTHROPIC_CUSTOM_HEADERS="User-Agent: claude-cli/${v} (external, cli)" \
    claude --print --output-format json --no-session-persistence \
    "reply with only the word pong" > "run-ua-${v}.json"
done
```

All runs: same binary (2.1.105), same account, same empty dir, same prompt. Only the UA string varies. Cold cache confirmed by `cache_read_input_tokens=0` on first run per UA group.

**Results:**

| UA forged | cache_creation | cache_read | Total injected | Notes |
|---|---|---|---|---|
| `claude-cli/2.1.91` | 0 | 17,505 | **17,505** | Cache hit from 2.1.98 run — identical content |
| `claude-cli/2.1.98` | 17,505 | 0 | **17,505** | Pre-bug baseline (cold) |
| `claude-cli/2.1.99` | 0 | 17,505 | **17,505** | Cache hit from 2.1.98 run — identical content |
| `claude-cli/2.1.100` | 24,215 | 0 | **24,215** | Bug onset: +6,710 from baseline (cold, unique key) |
| `claude-cli/2.1.101` | 9,841 | 17,684 | **27,525** | Grew further: +10,020 from baseline |
| `claude-cli/2.1.104` | 9,841 | 17,684 | **27,525** | Same as 2.1.101 |
| `claude-cli/2.1.105` | 27,525 | 0 | **27,525** | Default, cold (first run of the session) |

**Key findings:**

1. **Server-side injection is real and UA-keyed.** Same binary, same prompt, same account, same dir — only the UA string varies. Token counts differ by up to +10,020 (empty project).

2. **The breakpoint is exactly v2.1.100**, matching the article's claim. All pre-2.1.100 UAs produce identical 17,505-token injection. v2.1.100 jumps to 24,215. v2.1.101+ settles at 27,525.

3. **Cache key evidence confirms distinct injection content per UA group.** UAs 2.1.91/98/99 all hit a shared cache entry (read=17,505), proving the server injects byte-identical content for all three. UAs 2.1.101/104/105 share a different cache prefix (read=17,684 — 179 tokens more than old group), proving their injected content differs. UA 2.1.100 has its own unique cache key — it's transitional.

4. **Magnitude scales with project context.** My +10K is in an empty directory. The article's +20K was with a real project. A GitHub comment from `@saulo-silva` on #46917 reports v2.1.104 at ~96,539 `cache_creation` with their project context — that's +94% over v2.1.98.

5. **The workaround works.** Setting `ANTHROPIC_CUSTOM_HEADERS='User-Agent: claude-cli/2.1.98 (external, cli)'` causes the server to use the smaller pre-2.1.100 injection, saving ~10K tokens/request (empty project) or ~20K+ (loaded project).

**Phase 3 raw data preserved at:** `/tmp/tok-ua-test/run-*.json`

## Known Issues (not fixed)

### 1. ccproxy: `tokens_input=0` for streaming claude_api requests

Billed tokens are in Anthropic's SSE `message_start.usage` but never reach ccproxy's `usage_metrics`.

**Root cause chain:**

- `ccproxy/plugins/claude_api/hooks.py:68` — `ClaudeAPIStreamingMetricsHook.__call__` filters: `if context.provider != "claude_api": return`
- `ccproxy/streaming/deferred.py:295,369` — sets `provider = request_context.metadata.get("service_type", "unknown")`
- Nobody sets `metadata["service_type"] = "claude_api"` for claude_api requests. Only `claude_sdk/adapter.py:290` does it for `"claude_sdk"`
- Stream chunks arrive with `provider="unknown"`, the hook short-circuits, `usage_metrics` stays `{}`

**Fix:** Set `metadata["service_type"] = "claude_api"` in `ccproxy/plugins/claude_api/adapter.py` around `process_provider_request`.

### 2. ccproxy: User-Agent rewrite masks per-request client identity

`ClaudeAPIDetectionService` caches the system CC's UA and overwrites client UA on every request. This is intentional (lets non-CC clients appear as CC to Anthropic's API) but means:

- Per-request UA from actual CC clients is lost
- ccproxy can't test UA-based hypotheses without modification
- If using UA-pinning workaround, the detection_service needs a config override

**Relevant code:** `detection_service.py:37-43` (ignores_header), `detection_service.py:71-97` (startup detection), `adapter.py:83-106` (header merge).

### 3. ccproxy: Codex plugin startup error

`using_fallback_codex_data` traceback on every `ccproxy serve`. Harmless without codex credentials. Unrelated to this investigation.

### 4. ccproxy: DuckDB read lock

Can't query analytics.duckdb while proxy is running. Stop proxy first, or connect with `read_only=True`.

## Workaround Configuration

### For Claude Code directly (no proxy)

Add to `~/.claude/settings.json`:

```json
{
  "env": {
    "ANTHROPIC_CUSTOM_HEADERS": "User-Agent: claude-cli/2.1.98 (external, cli)"
  }
}
```

Or export in `~/.zshrc`:

```bash
export ANTHROPIC_CUSTOM_HEADERS='User-Agent: claude-cli/2.1.98 (external, cli)'
```

**Caveats:**

- Anthropic may fix the server-side inflation, making this unnecessary — check periodically
- The server may gate new features on UA version — pinning to 2.1.98 could silently block server-side improvements targeted at newer versions
- If Anthropic adds strict UA validation, this could cause request rejections
- Monitor anthropics/claude-code#46917 for official acknowledgment or fix

### For ccproxy

Not yet implemented. Proposed: add a `user_agent_override` config option to the detection service or adapter so all proxied traffic benefits without client-side changes.

## Updates (2026-04-14, later in same session)

### Independent verification on GitHub #46917

`@Adrian-Mteam` posted a controlled test across v2.1.98/100/104/107 + UA spoof using a custom Node.js SSE proxy on a real project (~66K rules, ~17K memory, 76 skills, 11 MCP servers):

| Version | Content-Length | cache_creation | Total | Delta vs 2.1.98 |
|---|---|---|---|---|
| v2.1.98 | 176,128 B | 40,950 | 50,511 | baseline |
| v2.1.100 | 174,943 B (-1,185 B) | 57,659 | 70,847 | **+40.8%** |
| v2.1.107 | 180,028 B (+3,900 B) | 58,249 | 72,719 | **+44.0%** |
| v2.1.107 + UA spoof | 180,028 B (same) | 41,095 | 51,618 | +2.2% (noise) |

Their data shows **17,154 fewer tokens** just from spoofing UA to 2.1.98, with identical payload. Confirms the server injects ~65 KB of hidden content (~17K tokens) for v2.1.100+ UAs.

No official Anthropic response on the issue as of 2026-04-14 21:34 UTC.

### Critical changelog finding: cache TTL regression affecting this setup

CC v2.1.108 changelog entry: "subscribers who set `DISABLE_TELEMETRY` falling back to 5-minute prompt cache TTL instead of 1 hour" — now fixed.

**This directly affects Chris's setup.** `~/.claude/settings.json` has `CLAUDE_CODE_ENABLE_TELEMETRY=0`, which likely triggers the same bug. On v2.1.105, this means:

- All prompt cache entries use 5-minute TTL instead of 1-hour
- After 5 minutes of inactivity, the next request pays full `cache_creation` price instead of cheap `cache_read`
- Combined with the +10K phantom injection, this is a double penalty: larger cache × more frequent cache misses

v2.1.108 adds two new env vars: `ENABLE_PROMPT_CACHING_1H` and `FORCE_PROMPT_CACHING_5M` for explicit control. Upgrading to v2.1.108 fixes the TTL regression while the UA workaround (already applied in settings.json) handles the phantom injection.

**Recommended action:** Upgrade CC from v2.1.105 to v2.1.108. The UA workaround in settings.json already pins the UA to 2.1.98 regardless of binary version, so both fixes stack.

### v2.1.108 resolves the UA-based injection (2026-04-14)

After upgrading to v2.1.108, reran the UA comparison from a fresh `/tmp/tok-ua-108-test/`:

| UA | cache_creation | cache_read | Total |
|---|---|---|---|
| `claude-cli/2.1.108` | 20,210 | 0 | **20,210** |
| `claude-cli/2.1.98` | 7,295 | 12,915 | **20,210** |

Totals are now **identical regardless of UA**. The +10,020 server-side penalty for post-2.1.100 UAs is gone. The +2,705 increase over v2.1.105's 2.1.98-UA baseline (17,505 → 20,210) is normal client-side growth (new tools/descriptions between v2.1.105 and v2.1.108). The partial cache hit (12,915 read) shows content layout differs slightly between UAs, but billing impact is the same.

**UA workaround removed from `~/.claude/settings.json`.** No longer needed on v2.1.108. `ENABLE_PROMPT_CACHING_1H=1` kept as belt-and-suspenders for the TTL fix.

## Repo State

- Branch `fix/plugin-wiring-and-adaptive-thinking` on `origin` (`chrisvaillancourt/ccproxy-api`), pushed, no PR
- `upstream` = `CaddyGlow/ccproxy-api`; fork already configured
- `main` is clean — all fixes on the feature branch
- Untracked: this file, `invisible-tokens-handoff.md`, `.claude/plans/purring-splashing-nebula.md`, three article `.md` files in repo root (research input, not for commit)

## Possible Next Steps

1. **Apply the workaround** in `~/.claude/settings.json` for immediate token savings
2. **Add UA-pinning config to ccproxy** — a `user_agent_override` setting so all traffic through ccproxy benefits
3. **Fix the `service_type` gap** so ccproxy can track token usage in access log / DuckDB
4. **Open a PR upstream** (`CaddyGlow/ccproxy-api`) for the four Phase 1 fixes, potentially bundled with UA-pinning and service_type fix
5. **Monitor** anthropics/claude-code#46917 for official response; remove workaround if Anthropic addresses it

## File References

| Thing | Path |
|---|---|
| This doc | `.claude/notes/invisible-tokens-findings.md` |
| Original handoff (Phase 1 context) | `.claude/notes/invisible-tokens-handoff.md` |
| Original plan | `.claude/plans/purring-splashing-nebula.md` |
| Phase 3 test data | `/tmp/tok-ua-test/run-*.json` |
| Phase 1 trace data | `/tmp/ccproxy-invisible-tokens/traces/` |
| Detection service (UA rewrite) | `ccproxy/plugins/claude_api/detection_service.py:37-43, 71-97, 125` |
| Adapter header merge | `ccproxy/plugins/claude_api/adapter.py:83-106` |
| Streaming metrics hook (known gap) | `ccproxy/plugins/claude_api/hooks.py:68` |
| Chunk emit site | `ccproxy/streaming/deferred.py:295, 369` |
| GitHub issue | `anthropics/claude-code#46917` |
