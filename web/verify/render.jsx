import { renderToStaticMarkup } from "react-dom/server";
import SlotWall from "../SlotWall.jsx";
import payload from "../payload.sample.json" with { type: "json" };

function must(cond, msg) {
  console.log((cond ? "  OK   " : "  FAIL ") + msg);
  if (!cond) process.exitCode = 1;
}

const html = renderToStaticMarkup(<SlotWall payload={payload} />);
console.log("rendered", html.length, "chars\n");

const view = payload.views[0];
must(html.includes("Schengen Slot Board"), "title renders");
for (const v of payload.views) must(html.includes(v.name), `tab "${v.name}"`);
for (const k of view.kpis) must(html.includes(k.value), `KPI ${k.label} = ${k.value}`);
for (const c of view.cards) {
  must(html.includes(c.country), `card ${c.country}`);
  must(html.includes(c.status), `  status "${c.status}"`);
  if (c.has_date) must(html.includes(c.headline), `  date ${c.headline}`);
  else must(html.includes(c.empty_sub), `  empty "${c.empty_sub.slice(0, 32)}"`);
}
must(html.includes("All countries"), "table button present");
must(!html.includes("[object Object]"), "no object leaked into the markup");
must(!html.includes("undefined"), "no undefined leaked into the markup");
must(!html.includes("NaN"), "no NaN leaked into the markup");

// The empty state and the headline are mutually exclusive by design.
const bookable = view.cards.filter(c => c.has_date).length;
must(bookable > 0, `${bookable} bookable card(s) in the sample`);
