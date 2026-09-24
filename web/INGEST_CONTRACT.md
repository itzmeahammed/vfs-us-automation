# `POST /api/tv/ingest/board` — the contract

Written for: the backend engineer building the receiving endpoint on
travnooker.com.

The slot checker POSTs the whole board here after every run cycle. You store the
latest one and serve it to your own React page. Nothing ever calls back into the
checker's machine.

## The request

```
POST https://www.travnooker.com/api/tv/ingest/board
Content-Type: application/json
X-API-Key: <the same key as /api/tv/ingest/announce>
User-Agent: vfs-slot-checker-board/1
```

```jsonc
{
  "kind": "slot_board",          // always this, on this endpoint
  "source": "vfs-slot-checker",
  "schema": 1,                   // see "Versioning"
  "generated_iso": "2026-09-23T18:16:40+04:00",
  "board": { /* ~26 KB — the whole payload the page renders */ }
}
```

**Body size ~26 KB. Frequency ~30/day** (once per run cycle, inside the checker's
09:00–24:00 window). Nothing arrives overnight — that is expected, not a fault.

## What to do with it

Store `board` as a single JSON blob, keyed by nothing — there is one board.
Overwrite what is there. Serve it back unchanged.

```
POST  /api/tv/ingest/board   →  latest_board = body.board
GET   /api/slots/board       →  latest_board          (your page calls this)
```

The React component in `web/SlotWall.jsx` renders `board` directly. It does not
transform it, and neither should you: reordering, reformatting or recomputing
anything in it will disagree with the rules the checker applies.

## Three things that matter

**1. Do not deduplicate.** `/ingest/announce` suppresses a repeated title for
five minutes, which is right for an alert. A board is a snapshot — every POST
must replace the stored one, even if it looks identical.

**2. Do not reject an unchanged board.** Between quiet cycles the board may be
near-identical apart from timestamps. That is still the current board.

**3. Serve it even when it is old.** If the checker has been asleep since
midnight, serve the midnight board. It is not misleading: `generated_iso` and
every card carry their own age (`Seen 28m`, `Open now` vs `Was open 6h ago`), and
the component shows "updated 7h ago" from them. An empty dashboard every morning
would be worse than an honest stale one.

## Response

Return `200`. A body is optional — an empty `200` counts as accepted.

To signal a problem, return `200` with `{"ok": false, "error": "..."}`, or any
4xx/5xx. Either is logged on the checker's side and the run continues; the next
cycle retries naturally. There is no retry queue, deliberately.

## Serving it to the browser

Add an `ETag` on your `GET` — the hook in `web/useSlotBoard.js` sends
`If-None-Match` and a `304` costs nothing. The page polls every 30–60s while
cycles land every ~30 minutes, so almost every poll should be a `304`.

```
ETag: "<hash of the stored blob>"
Cache-Control: no-cache
```

## Versioning

`schema` is `1`. It is bumped when `board`'s shape changes in a way that would
break a renderer.

**Reject a schema you do not know**, and keep serving the last one you do:

```js
if (body.schema !== 1) return reject("unknown board schema");
```

Failing loudly at ingest beats a page full of blanks that nobody traces back to a
deploy on a different machine.

## Auth

Same `X-API-Key` as the announcements. The checker reads `[tv_board] api_key`
from its config and falls back to `[tv_announce] api_key`, so in practice it is
one key for both.

The key belongs to the checker, not the browser. Your `GET /api/slots/board`
should be authorised however the rest of your app authorises a page — this key
must never reach the frontend.

## Turning it on

The checker ships with this **off**. Once your endpoint is live, set in
`config/config.local.ini`:

```ini
[tv_board]
enabled = true
url = https://www.travnooker.com/api/tv/ingest/board
```

No restart needed beyond the next run. To send one immediately without waiting
for a cycle:

```bash
python -c "from src.utils.config_reader import initialize_config; \
initialize_config(); from src.slots import publish; print(publish.push_quietly())"
```

## A sample body

`web/payload.sample.json` is a real `board` object. Wrap it in the envelope above
to get exactly what will arrive.
