# Invisible Token Investigation Plan

## Context

The article `Claude_Code_may_be_burning_your_limits_with_invisible_tokens_-_Efficienist.md` claims:

- Starting Claude Code **v2.1.100**, every request carries ~20,000 extra tokens injected server-side
- Same prompt: v2.1.98 billed 49,726 input tokens; v2.1.100 billed 69,922
- The v2.1.100 request was **smaller in bytes sent from the client**, ruling out client-side growth
- The inflation does not appear in `/context` and is not in Anthropic's changelog
- Community workaround: pin to v2.1.98 via `npx claude-code@2.1.98`

**Why ccproxy-api is the right tool for this:** it already records the exact signals needed — `tokens_input` as billed by Anthropic, alongside the client's `user_agent` (which contains the Claude Code version `claude-cli/X.Y.Z`). No code changes are required for a retrospective or controlled comparison.

**Outcome of this plan:** a concrete answer to whether our usage shows the claimed ~20K/request delta between Claude Code versions, plus a reusable query we can re-run over time.

## What ccproxy-api already captures

All from the `access_logs` DuckDB table (schema at `ccproxy/plugins/analytics/models.py:11-57`):

| Field | Why it matters |
|---|---|
| `tokens_input` | Billed input tokens from Anthropic's `usage.input_tokens` — the metric in question |
| `cache_read_tokens` / `cache_write_tokens` | Must control for these — a bigger cache-read block inflates `tokens_input` without being "invisible" |
| `user_agent` | Contains `claude-cli/X.Y.Z` — the only way to segment by Claude Code version |
| `model` / `timestamp` / `status_code` | Filter to successful Claude messages |
| `streaming` / `duration_ms` | Sanity/context |

**Extra data available per request (not in DuckDB):**
- Raw streaming response text incl. `message_start.usage`: `/tmp/ccproxy/traces/{request_id}_*_streaming_response.json` (when `request_tracer.json_logs_enabled=true`)
- Outgoing upstream `body_size` in bytes: `/tmp/ccproxy/access.log` (post-adapter transformation; see caveat below)

**Key code references:**
- user_agent capture: `ccproxy/plugins/access_log/hook.py:143-148`
- streaming usage extraction: `ccproxy/plugins/access_log/hook.py:441-482`
- trace file writer: `ccproxy/plugins/request_tracer/hook.py:351-415`
- adapter body transformation (the caveat): `ccproxy/plugins/claude_api/adapter.py`

**Caveat on `body_size`:** the logged value is measured *after* `adapter.py` rewrites the body (strips `temperature`, injects system prompts, normalizes `cache_control`, overwrites `anthropic-beta`). The article's "fewer bytes sent from client" claim was made against the *original* Claude Code body — which the proxy does not log as a separate size. For this investigation, we care about the billed input-token delta, so this caveat does not block us; it only means we cannot independently corroborate the "smaller on the wire" finding. We can still diff raw request bodies captured by the core `HTTPTracerHook` if needed (Step 4).

## Investigation steps

### Step 1 — Confirm the proxy has data

Before running queries, check:

1. Is `ccproxy serve` running, and does Claude Code have `ANTHROPIC_BASE_URL` pointed at it? (`http://localhost:8000/claude`, per `config.example.toml`)
2. Is the `access_log` plugin enabled with analytics ingest enabled in `config.toml`?
3. Where is the DuckDB file? (configured under `[analytics]` — default path in `config.example.toml`)
4. Does the table have rows spanning both "before v2.1.100" and "on/after v2.1.100"? Quick check:
   ```sql
   SELECT
     regexp_extract(user_agent, 'claude-cli/([0-9.]+)', 1) AS cc_version,
     COUNT(*) AS n,
     MIN(timestamp) AS first_seen,
     MAX(timestamp) AS last_seen
   FROM access_logs
   WHERE user_agent LIKE 'claude-cli/%'
   GROUP BY cc_version
   ORDER BY cc_version;
   ```

If we have ≥100 requests on each side of the v2.1.100 boundary, skip to Step 2. Otherwise, go straight to Step 3 (controlled experiment).

### Step 2 — Retrospective analysis (historical data)

Run this aggregate query against `access_logs`. It reports billed `tokens_input` per Claude Code version, with cache tokens broken out so we don't confuse a bigger cache-read block with true bill inflation.

```sql
WITH tagged AS (
  SELECT
    regexp_extract(user_agent, 'claude-cli/([0-9.]+)', 1) AS cc_version,
    tokens_input,
    cache_read_tokens,
    cache_write_tokens,
    tokens_input - cache_read_tokens - cache_write_tokens AS non_cache_input,
    model
  FROM access_logs
  WHERE status_code = 200
    AND path LIKE '%/messages'
    AND user_agent LIKE 'claude-cli/%'
    AND tokens_input > 0
)
SELECT
  cc_version,
  COUNT(*) AS requests,
  ROUND(AVG(tokens_input))     AS avg_input,
  ROUND(MEDIAN(tokens_input))  AS p50_input,
  ROUND(QUANTILE_CONT(tokens_input, 0.9)) AS p90_input,
  ROUND(AVG(non_cache_input))  AS avg_non_cache_input,
  ROUND(AVG(cache_read_tokens)) AS avg_cache_read,
  ROUND(AVG(cache_write_tokens)) AS avg_cache_write
FROM tagged
GROUP BY cc_version
HAVING COUNT(*) >= 20
ORDER BY cc_version;
```

**How to read it:**
- **Primary signal:** a jump in `avg_non_cache_input` (or `p50_input`) at the v2.1.100 boundary that is *not* explained by a corresponding rise in `avg_cache_read` points to the claimed injection.
- **Confound:** same project, same session → heavy cache reuse. If `avg_cache_read` is also much higher post-v2.1.100, the proxy may be billing the same underlying content in a new column rather than truly adding tokens; it is not "invisible" in that case.
- **Target delta:** the article claims ~20K per request. Anything in the >5K range is worth taking seriously; <1K is noise.

If the retrospective is inconclusive (session mix differs across versions, tool use changed, etc.), proceed to Step 3.

### Step 3 — Controlled experiment

Goal: remove session/project variance so the only difference between runs is the Claude Code version.

1. Start proxy with tracing on:
   ```
   ccproxy serve  # with request_tracer.json_logs_enabled = true and access_log.enabled = true
   ```
2. Create a throwaway directory with no `CLAUDE.md`, no `.claude/`, no git history → minimizes CC's auto-injected context.
3. Run the v2.1.98 side:
   ```
   export ANTHROPIC_BASE_URL=http://localhost:8000/claude
   npx claude-code@2.1.98
   ```
   - Start a fresh session, send a minimal deterministic prompt (e.g. `reply with only the word pong`).
   - Exit.
   - Capture the `request_id` from the access log, note the `tokens_input` and `cache_read_tokens`.
4. Same directory, same prompt, latest version:
   ```
   npx claude-code@latest
   ```
   - Repeat. Capture `tokens_input` and `cache_read_tokens`.
5. Compare the **first** request of each session (cache pollution grows over a session).

**Interpretation:**
- Same prompt, fresh session, `cache_read_tokens = 0` on both sides → difference in `tokens_input` is the injection.
- Repeat 2–3 times per version to rule out variance from dynamic content (time/date headers, etc.).

### Step 4 — Body-diff for a deeper answer (optional)

If Step 2 or 3 shows a delta, diff the upstream request bodies to see *what* is growing:

- Raw streaming response files: `/tmp/ccproxy/traces/{request_id}_*_streaming_response.json` — contains `usage_metrics` breakdown.
- Upstream request body is captured by the core `HTTPTracerHook` (per the note at `ccproxy/plugins/request_tracer/hook.py:128`); locate its output path from config and diff the two request JSONs with `jq -S . | diff` after sorting keys.

Things to look for in the diff:
- New system-prompt chunks / "memory" or "summary" blocks
- Additional tool schemas in `tools[]`
- New `cache_control` markers that move content between cache tiers
- Anthropic-injected fields in `metadata` or similar

Reminder: bodies are *post*-`adapter.py` transformation, so some differences will be ours (e.g., `anthropic-beta` overwrite). Ignore those; focus on `system`, `messages`, `tools`.

### Step 5 — Decide and (optionally) automate

- **If delta is real (~20K and not cache-attributable):** short-term, pin Claude Code to v2.1.98 via npx. Medium-term, track Anthropic's response.
- **If delta is explainable (cache shift, tool schema rev, etc.):** document the finding; no action.
- **Either way:** the Step 2 query is reusable. Consider wiring it into the analytics `/logs` endpoints or a small CLI subcommand as a follow-on — but that is out of scope for this investigation.

## Files relevant to this investigation (read-only)

| Path | Purpose |
|---|---|
| `ccproxy/plugins/analytics/models.py` | `access_logs` schema |
| `ccproxy/plugins/access_log/hook.py` | Where `user_agent` and `tokens_input` are captured |
| `ccproxy/plugins/request_tracer/hook.py` | Per-request JSON trace writer |
| `ccproxy/plugins/claude_api/streaming_metrics.py` | SSE `usage` parser |
| `ccproxy/plugins/claude_api/adapter.py` | Body transformation (the `body_size` caveat source) |
| `config.example.toml` | Config keys to enable everything above |

## Verification

1. Run the Step 1 query and confirm we have coverage across versions *or* commit to Step 3.
2. Produce a table (Step 2) or pair of numbers (Step 3) showing `tokens_input` by `cc_version`.
3. Sanity-check by controlling for `cache_read_tokens`.
4. If anomalous, open a trace JSON pair and diff request bodies to see what's new.
5. Result: a one-paragraph answer — "yes, we see +X tokens on v≥2.1.100, driven by Y" or "no measurable impact in our data."

No source-code changes are needed to reach a verdict. Any code work (e.g., adding a `/analytics/by-version` endpoint) would be a follow-on, driven by what Step 2/3 shows.
