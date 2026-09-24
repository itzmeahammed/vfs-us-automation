import "./setup.js";
import { act } from "react";
import { createRoot } from "react-dom/client";
import SlotWall from "../SlotWall.jsx";
import payload from "../payload.sample.json";

function must(c, m) { console.log((c ? "  OK   " : "  FAIL ") + m); if (!c) process.exitCode = 1; }
const $btn = (t) => [...document.querySelectorAll("button")].find(b => b.textContent === t);
const click = (el) => act(() => { el.dispatchEvent(new MouseEvent("click", { bubbles: true })); });

async function main() {
  const root = createRoot(document.getElementById("root"));
  await act(() => { root.render(<SlotWall payload={payload} />); });

  must(!document.querySelector("table"), "table is closed to start with");
  const btn = $btn("All countries");
  must(!!btn, "found the All countries button");
  await click(btn);

  const table = document.querySelector("table");
  must(!!table, "table opened on click");
  const cols = table.querySelectorAll("thead th").length;
  const rows = [...table.querySelectorAll("tbody tr")];
  const perRow = rows.map(r => r.querySelectorAll("td").length);
  must(new Set(perRow).size === 1, "every row has the same number of cells");
  must(perRow[0] === cols, `columns ${cols} === cells per row ${perRow[0]}`);
  must(rows.length === payload.views[0].table.length,
       `${rows.length} rows === ${payload.views[0].table.length} countries`);

  const ranks = rows.map(r => Number(r.querySelector("td").textContent));
  must(JSON.stringify(ranks) === JSON.stringify(ranks.map((_, i) => i + 1)),
       "ranks run 1..n in order: " + ranks.join(","));

  await act(() => {
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  });
  must(!document.querySelector("table"), "Escape closes the table");

  await click($btn("Waitlist"));
  await click($btn("All countries"));
  const t2 = document.querySelector("table");
  const heads = [...t2.querySelectorAll("thead th")].map(h => h.textContent);
  const cells = [...t2.querySelectorAll("tbody tr")][0].querySelectorAll("td").length;
  must(heads.includes("Offered"), "waitlist tab shows its own 'Offered' column");
  must(cells === heads.length, `waitlist columns ${heads.length} === cells ${cells}`);
  console.log("\n  tourist headers : " + cols + " columns");
  console.log("  waitlist headers: " + heads.join(" | "));
}
main().then(() => process.exit(process.exitCode || 0),
          (e) => { console.error(e); process.exit(1); });
