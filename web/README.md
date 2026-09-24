# The slot board as a React component

Written for: the engineer dropping this into the travnooker web app.

`SlotWall.jsx` renders the same board as `reports/slot_wall.html`, from the same
payload. Copy `SlotWall.jsx` (and `useSlotBoard.js` if you want the fetching)
into your app. React 18 or 19, Tailwind, no other dependencies.

```jsx
import SlotWall from "./SlotWall";
import useSlotBoard from "./useSlotBoard";

export default function SlotsPage() {
  const { payload, error, loading } = useSlotBoard("/api/slots/board");
  if (loading && !payload) return <p>Loading the board…</p>;
  return (
    <>
      {error && payload ? <p>Showing the last board — refresh failed.</p> : null}
      {payload ? <SlotWall payload={payload} /> : null}
    </>
  );
}
```

## The one rule

**This component renders. It does not decide anything.**

Every string in the payload is already formatted — `"Sep 24"`, `"28m"`, `"+1d"`,
`"Open now"` — and the rows arrive in their final order with their final ranks.
The ranking, the bookability rule, the forecast and the wording all live in
Python (`src/slots/`), covered by the test suite.

Do not re-derive any of it in JavaScript. Two implementations drift, and the
rules that drift are the subtle ones:

- a reading that has gone stale is **not** a bookable slot, so a big date must
  not appear for it (we shipped `Next appointment Sep 03` off a 56-day-old
  reading before this rule existed);
- the board is ranked across **all** countries *before* it is cut to six cards,
  or a country with a live slot gets dropped for a waitlist-only one that scored
  higher on history;
- a slot seen six hours ago is `Was open`, not `Open now`, and not a prediction.

If a number looks wrong, fix `src/slots/`, not this file.

## Props

| Prop | Default | What it does |
|---|---|---|
| `payload` | — | Required. The board payload, shape in `types.d.ts`. |
| `autoRotate` | `false` | Cycle the tabs. A TV wants this; a page inside an app does not. |
| `rotateSeconds` | payload's value | Only with `autoRotate`. |
| `initialView` | first view | Start on `"Business"`, say. |
| `className` | `""` | Passed to the outer element. |

Rotation stops while the "All countries" table is open — moving the view out
from under someone reading it is the one thing it must not do.

## What differs from the HTML wall

Two things are deliberately gone, because they are TV-screen concerns that fight
a host layout:

- the fixed 1920×1080 canvas and its scale transform — the component is
  responsive instead (1 / 2 / 3 columns);
- auto-rotation, now off by default (`autoRotate` brings it back for a TV).

Everything else is the same board: the same six cards, the same live status
chip, the same "next opening" strip, the same All-countries table.

## Colours

Tailwind arbitrary values (`bg-[#16263A]`) so it drops in without touching your
`tailwind.config`. The palette is the `C` object at the top of `SlotWall.jsx` —
one edit to re-theme. Promote them to theme tokens if you prefer; nothing
depends on that.

Colour is never the only cue: every dot ships with its label.

## Fetching

`useSlotBoard.js` is a small dependency-free hook. If you already use React
Query or SWR, use that instead — but keep these two behaviours:

1. **Send `If-None-Match`.** The payload is ~26 KB and mostly unchanged between
   polls; the endpoint returns `304` and costs nothing.
2. **A failed refresh must not blank the board.** Keep the last good payload on
   screen. The board carries its own age on every card, so a stale board tells
   the truth rather than lying. This matters because the bot runs 09:00–24:00 by
   design — overnight there is no fresher board to fetch, and a hook that clears
   its data on error shows an empty dashboard every morning.

Poll every 30–60s. Cycles land every ~30 minutes, so a minute of lag is
invisible.

## Verifying a change

`web/verify/` renders the component against the real sample payload, in Node.
It is a throwaway harness, not part of the app.

```bash
cd web
npm install        # react, react-dom, jsdom, esbuild — dev only, gitignored
npm run verify
```

It checks that every card, KPI and tab reaches the markup, that nothing leaks
`undefined` / `NaN` / `[object Object]`, and — in a real jsdom click-through —
that the All-countries table opens, that its header count matches its cell
count on every row, that ranks run 1..n, that Escape closes it, and that the
waitlist tab picks up its own `Offered` / `Real slots` columns.

That last one is worth keeping: the column-alignment bug it catches is easy to
reintroduce and invisible until someone opens the table.

## Regenerating the sample payload

`payload.sample.json` is real output, useful as a fixture and for Storybook:

```bash
python -c "import sqlite3, json; from src.slots import wall; \
c = sqlite3.connect('state/slots.db'); c.row_factory = sqlite3.Row; \
json.dump(wall.build_payload(c, days=7), open('web/payload.sample.json','w'), indent=2)"
```
