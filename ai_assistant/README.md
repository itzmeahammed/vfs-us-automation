# ai_assistant

A read-only analytics assistant for a local dashboard: an "Ask AI" panel that
answers questions by calling **your project's own read-only tools** (stats,
logs, a SQLite database…) through OpenAI, then writes a short Markdown answer.

- **Portable** — standard library only; copy the folder, write one tools file, mount it.
- **Read-only** — tools only return data; the SQL tool is locked down at the database level.
- **Private** — secrets are stripped and e-mails pseudonymised before anything leaves the machine.
- **Bounded** — rate limit, daily token budget, timeouts, tool-round cap, output caps.

```
ai_assistant/
  config.py       AssistantConfig — every knob, frozen
  engine.py       Assistant.ask(): question -> tools -> streamed answer
  llm.py          OpenAI Chat Completions client (urllib, streaming, retries)
  tools.py        ToolRegistry: schema validation, timeouts, redaction, size caps
  sqlite_tool.py  register_sqlite_tool(): read-only SQL with the live schema
  redact.py       secret scrubbing + reversible e-mail pseudonyms
  guard.py        request guard (CSRF / DNS rebinding), rate limit, token ledger, audit log
  web.py          AssistantHTTP: mounts on any http.server handler
  static/         the widget (shadow-DOM, light/dark, mobile)
  tests/          pytest suite, fake model — no network, no cost
```

## Use it in another project

1. Copy `ai_assistant/` next to your server code.
2. Write a tools module (see `assistant_vfs.py` in this repo for a full example):

```python
from ai_assistant import (Assistant, AssistantConfig, AssistantHTTP,
                          ToolError, ToolRegistry, register_sqlite_tool)

reg = ToolRegistry()

@reg.tool(description="Orders per day for the last N days (oldest first).",
          parameters={"type": "object", "properties": {
              "days": {"type": "integer", "minimum": 1, "maximum": 90}}})
def get_orders_trend(days: int = 14):
    return my_app.orders_by_day(days)          # plain dicts/lists; READ-ONLY

register_sqlite_tool(reg, "data/app.db", about="orders.customer_id -> customers.id")

assistant = Assistant(AssistantConfig(
    api_key=os.environ["OPENAI_API_KEY"],
    model="gpt-4o-mini",
    title="Shop Assistant",
    system_context="An online shop. 'Order' = ..., statuses mean ...",
    suggestions=("How did sales do this week?",),
    audit_path="state/assistant_audit.jsonl",
    usage_path="state/assistant_usage.json",
), reg)
ai = AssistantHTTP(assistant)
```

3. Mount it on a **threaded** stdlib server and add the script tag to a page:

```python
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if ai.handle(self): return
        ...   # your routes; put ai.script_tag() before </body> on pages that want the panel
    def do_POST(self):
        if ai.handle(self): return     # BEFORE you read the body yourself
        ...

http.server.ThreadingHTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
```

Other frameworks (Flask, FastAPI…): serve `static/` yourself and stream
`assistant.ask(question, history)` events as one JSON object per line.

## Writing good tools

- **Return aggregates, not raw rows.** The model is good at explaining numbers and bad at
  adding up 2,000 of them. Pre-compute rates, totals and runways in Python or SQL.
- **Describe meaning in the description** — units, time zone, what a status means, and
  which tool to use for which kind of question. The model chooses tools from these.
- **Validate with the schema** (`minimum`, `maximum`, `enum`, `pattern`, `maxLength`);
  arguments are untrusted. Raise `ToolError("…")` for an expected miss — the model reads it
  and tries again.
- **Never return secrets.** Redaction is a safety net, not the plan. Use an allow-list
  when exposing configuration.
- **Never write.** Not files, not the database, not external APIs with side effects.

## Scope control — it only answers about YOUR system

Set `scope_description` (one or two sentences on what the assistant is for). Every
question then passes three layers, cheapest first (`scope.py`, `engine.py`):

```
question
 ├─ 1. local     greetings / "help" → canned reply with suggestions      0 tokens
 ├─ 2. gate      tiny classifier call, NO tool schemas sent: IN / OUT  ~260 tokens
 │               OUT → fixed refusal (nothing is generated)
 └─ 3. grounding the first reply is held back; if it read no data (no
                 tool call) it is discarded and replaced by the refusal.
                 Exception: a follow-up the gate cleared ("as a table", "which
                 of those?") may reuse the previous answer.
```

- A model that replies `OUT_OF_SCOPE` (its own instruction) is refused the same way.
- If the gate itself fails, the question falls through to grounding — never to a free answer.
- `off_topic_limit` refusals within `off_topic_window_s` pause the assistant for
  `off_topic_cooldown_s`, enforced by the server; paused questions cost nothing.
- `/assistant/meta` reports `usage_today.refused`; the audit log records the reason
  (`scope`, `model`, `ungrounded`, `local`).

## Security model

| Threat | Control |
|---|---|
| Another site in your browser POSTs to localhost (CSRF) | JSON-only POSTs (forces a CORS preflight that is never answered), Origin + Sec-Fetch-Site checks |
| DNS rebinding to 127.0.0.1 | Host header allow-list (`allowed_hosts`) |
| Other machines on the LAN | loopback-only by default (`loopback_only`) |
| Secrets reaching the provider | key-name + value-pattern redaction of every tool result, question and history turn |
| Personal data reaching the provider | e-mails replaced by per-request pseudonyms, restored only in the browser's copy |
| Prompt injection via logs/scraped text | system prompt marks tool output as data; tools are read-only, so injected instructions have nothing to act on |
| Model writing to the database | SQLite opened `mode=ro` + `query_only` + authorizer allow-list + single statement + deadline + row cap |
| Runaway cost | rate limit/min, daily token budget (persisted), concurrency cap, tool-round cap, output caps |
| Using it as a general chatbot | local small-talk replies, scope gate, grounding check, off-topic cooldown (see above) |
| Rendering attacks in answers | widget escapes all text before its Markdown subset adds tags; links limited to http(s), `noopener` |
| Key leakage | key only in the `Authorization` header; never in URLs, logs, errors or `repr()` |

The audit log records question (redacted), tools + arguments, tokens and latency per
request — not answers.

## Tests

```
python -m pytest ai_assistant/tests
```
