/**
 * The slot board, as a React component.
 *
 * This file renders. It does not decide anything. Every figure, label, colour
 * role and the order of the rows arrive already settled from
 * `src/slots/wall.py::build_payload` — including which country outranks which,
 * whether a slot is bookable now, and the wording of the forecast. Re-deriving
 * any of that here would create a second implementation of rules that are
 * covered by the Python test suite, and the subtle ones drift silently: a
 * reading that went stale is not a bookable slot, and the board is ranked
 * before it is cut to six cards.
 *
 * So: no sorting, no date maths, no thresholds. The two exceptions are the wall
 * clock and the "updated Nm ago" counter, which have to tick in the browser
 * because they measure the present rather than the payload.
 *
 * Tailwind colours are written as arbitrary values (`bg-[#16263A]`) so the file
 * drops into an existing app without touching its Tailwind config. Promote them
 * to theme tokens if you would rather; nothing here depends on that.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

// The board's palette. One place, so a theme change is one edit.
const C = {
  bg: "#0F1B2A",
  panel: "#16263A",
  line: "#26384E",
  ink: "#EEF2F6",
  ink2: "#C9D3DE",
  muted: "#9AAABB",
  dim: "#5E7086",
  accent: "#6CB4FF",
};

// Colour role -> dot colour. The label always ships with the dot, so colour is
// never the only thing carrying the meaning.
const DOT = {
  open: "bg-[#3DD68C]",
  occ: "bg-[#F2B84B]",
  wait: "bg-[#F07A64]",
  none: "bg-[#5E7086]",
};

const EMPTY_TEXT = {
  open: "text-[#3DD68C]",
  occ: "text-[#F2B84B]",
  wait: "text-[#F07A64]",
  none: "text-[#5E7086]",
};

/* ===== the two things that must tick in the browser ===================== */

function useClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  return now;
}

/**
 * How long ago the payload was built. Counted here rather than baked in,
 * because a cached payload has to keep telling the truth as it ages — that is
 * the whole point of serving the last known board when the bot is asleep.
 */
function useFreshness(generatedIso) {
  const now = useClock();
  return useMemo(() => {
    const built = new Date(generatedIso);
    if (Number.isNaN(built.getTime())) return { text: "unknown", stale: true };
    const mins = Math.max(0, Math.round((now - built) / 60000));
    const text =
      mins < 1 ? "just now"
        : mins < 60 ? `${mins}m ago`
          : `${Math.floor(mins / 60)}h ago`;
    // Half an hour is about one bot cycle, so past that the board is behind.
    return { text, stale: mins > 30 };
  }, [generatedIso, Math.floor(now.getTime() / 30000)]);
}

/* ===== pieces =========================================================== */

function Kpi({ label, value }) {
  return (
    <div className="flex flex-col gap-1 min-w-0">
      <div className="text-xs sm:text-sm whitespace-nowrap" style={{ color: C.muted }}>
        {label}
      </div>
      <div className="text-xl sm:text-3xl font-semibold leading-none whitespace-nowrap truncate"
           style={{ color: C.ink }}>
        {value}
      </div>
    </div>
  );
}

function StatusChip({ status, kind }) {
  return (
    <span className="inline-flex items-center gap-2 text-sm" style={{ color: C.ink2 }}>
      <span className={`h-2.5 w-2.5 shrink-0 rounded-full ${DOT[kind] || DOT.none}`} />
      <span>{status}</span>
    </span>
  );
}

/**
 * The "next opening" strip. The payload decides whether there is one at all —
 * it is absent for a bookable card, and for one whose empty state already says
 * "Waitlist only" or "No slots".
 */
function ForecastLine({ forecast }) {
  if (!forecast) return null;
  const due = forecast.kind === "due";
  return (
    <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1 border-t pt-2 text-sm"
         style={{ borderColor: C.line }}>
      <span style={{ color: C.muted }}>{forecast.label}</span>
      <span className={due ? "font-semibold" : ""}
            style={{ color: due ? C.accent : C.dim }}>
        {forecast.value}
      </span>
      {forecast.note ? (
        <span className="text-xs" style={{ color: C.dim }}>{forecast.note}</span>
      ) : null}
    </div>
  );
}

function CountryCard({ card, lead }) {
  const stats = [...(card.stats_left || []), ...(card.stats_right || [])];
  return (
    <div
      className="flex min-w-0 flex-col gap-3 rounded-xl p-4 sm:p-5"
      style={{
        background: C.panel,
        boxShadow: lead ? `inset 0 0 0 2px ${C.accent}` : "none",
      }}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-baseline gap-2.5">
          <span className="text-lg font-semibold tabular-nums" style={{ color: C.dim }}>
            {card.rank}
          </span>
          <span className="truncate text-2xl font-bold sm:text-3xl" style={{ color: C.ink }}>
            {card.country}
          </span>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1.5">
          <span className="text-xl font-semibold leading-none tabular-nums"
                style={{ color: C.ink }}>
            {card.meter_value}
          </span>
          <div className="h-1 w-16 overflow-hidden rounded-full" style={{ background: C.line }}>
            <i className="block h-full rounded-full"
               style={{ width: `${Math.max(2, Math.min(100, card.meter))}%`, background: C.accent }} />
          </div>
        </div>
      </div>

      <div className="flex items-center gap-2">
        <StatusChip status={card.status} kind={card.kind} />
        {card.badge ? (
          <span className="rounded-full bg-[#1E3A2E] px-2.5 py-0.5 text-xs font-semibold text-[#7FE3AE]">
            {card.badge}
          </span>
        ) : null}
      </div>

      {/* The middle: a bookable date, or the empty state. Never both. */}
      <div className="flex min-h-[5.5rem] flex-1 flex-col justify-center gap-0.5">
        {card.has_date ? (
          <>
            <div className="text-xs" style={{ color: C.muted }}>{card.headline_label}</div>
            <div className="text-4xl font-bold leading-none sm:text-5xl" style={{ color: "#FFFFFF" }}>
              {card.headline}
            </div>
            <div className="truncate text-sm" style={{ color: C.ink2 }}>{card.centre}</div>
          </>
        ) : card.empty_title ? (
          <>
            <div className={`text-2xl font-semibold sm:text-3xl ${EMPTY_TEXT[card.kind] || EMPTY_TEXT.none}`}>
              {card.empty_title}
            </div>
            <div className="text-sm" style={{ color: C.muted }}>{card.empty_sub}</div>
          </>
        ) : (
          // No heading: the fact carries the card, so it takes the larger size.
          <div className="text-lg leading-snug" style={{ color: C.ink2 }}>{card.empty_sub}</div>
        )}
      </div>

      <ForecastLine forecast={card.forecast} />

      <div className="flex flex-wrap justify-between gap-x-4 gap-y-1 border-t pt-2.5 text-sm"
           style={{ borderColor: C.line, color: C.muted }}>
        {stats.map((s) => (
          <span key={s.label} className="whitespace-nowrap">
            {s.label}{" "}
            <b className="font-semibold" style={{ color: C.ink }}>{s.value}</b>
          </span>
        ))}
      </div>
    </div>
  );
}

function ChangeFeed({ view }) {
  return (
    <div className="flex min-h-0 flex-col gap-3 rounded-xl p-4 sm:p-5" style={{ background: C.panel }}>
      <div className="text-xl font-bold" style={{ color: C.ink }}>{view.feed_title}</div>
      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-auto">
        {view.feed.length ? (
          view.feed.map((f, i) => (
            <div key={`${f.title}-${i}`} className="flex items-start gap-2.5">
              <span className={`mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full ${DOT[f.kind] || DOT.none}`} />
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-semibold" style={{ color: C.ink }}>{f.title}</div>
                <div className="truncate text-xs" style={{ color: C.muted }}>{f.sub}</div>
              </div>
              <div className="shrink-0 text-xs" style={{ color: C.dim }}>{f.when}</div>
            </div>
          ))
        ) : (
          <div className="text-sm" style={{ color: C.dim }}>{view.feed_empty}</div>
        )}
      </div>
      <div className="rounded-lg p-3" style={{ background: C.bg }}>
        <div className="text-xs" style={{ color: C.muted }}>{view.note_label}</div>
        <div className="text-sm" style={{ color: C.ink2 }}>{view.note}</div>
      </div>
    </div>
  );
}

/**
 * Every country, not the six on the board.
 *
 * Stat columns are read off the rows rather than hard-coded, because the
 * waitlist tab counts different things (Offered, Real slots) and earns its own
 * headings that way without a branch here.
 */
function AllCountriesSheet({ view, onClose }) {
  const rows = view.table || view.cards || [];
  const statCols = useMemo(() => {
    const first = rows[0];
    if (!first) return [];
    return [...(first.stats_left || []), ...(first.stats_right || [])].map((s) => s.label);
  }, [rows]);

  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="absolute inset-0 z-20 flex flex-col gap-4 p-5 sm:p-8"
         role="dialog" aria-modal="true" aria-label="All countries"
         style={{ background: "rgba(15, 27, 42, 0.97)" }}>
      <div className="flex items-baseline justify-between gap-4">
        <div className="text-2xl font-bold sm:text-3xl" style={{ color: C.ink }}>
          All countries — {view.name}
        </div>
        <button type="button" onClick={onClose}
                className="shrink-0 rounded-full border px-4 py-1.5 text-sm"
                style={{ borderColor: C.line, color: C.ink2 }}>
          Close ✕
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr>
              {["#", "Country", "Status", "Next appointment", "Where", "Next opening"]
                .concat(statCols, ["Score"])
                .map((h, i) => (
                  <th key={h + i}
                      className={`sticky top-0 whitespace-nowrap border-b px-3 py-2 text-xs font-semibold ${
                        i === 0 || i > 5 ? "text-right" : "text-left"}`}
                      style={{ background: C.panel, color: C.muted, borderColor: C.line }}>
                    {h}
                  </th>
                ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((c) => {
              const stats = [...(c.stats_left || []), ...(c.stats_right || [])];
              return (
                <tr key={c.country} className="border-b" style={{ borderColor: "rgba(38,56,78,0.6)" }}>
                  <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums"
                      style={{ color: C.muted }}>{c.rank}</td>
                  <td className="whitespace-nowrap px-3 py-2 font-semibold" style={{ color: C.ink }}>
                    {c.country}
                    {c.badge ? (
                      <span className="ml-2 rounded-full bg-[#1E3A2E] px-2 py-0.5 text-xs font-semibold text-[#7FE3AE]">
                        {c.badge}
                      </span>
                    ) : null}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2">
                    <StatusChip status={c.status} kind={c.kind} />
                  </td>
                  <td className="whitespace-nowrap px-3 py-2">
                    {c.has_date
                      ? <span className="font-semibold" style={{ color: C.accent }}>{c.headline}</span>
                      : <span style={{ color: C.dim }}>—</span>}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2" style={{ color: C.ink2 }}>
                    {c.has_date ? (c.centre || "") : (c.empty_sub || "")}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2">
                    {c.forecast ? (
                      <>
                        <span className={c.forecast.kind === "due" ? "font-semibold" : ""}
                              style={{ color: c.forecast.kind === "due" ? C.accent : C.dim }}>
                          {c.forecast.value}
                        </span>
                        {c.forecast.note ? (
                          <span className="block text-xs" style={{ color: C.dim }}>
                            {c.forecast.note}
                          </span>
                        ) : null}
                      </>
                    ) : <span style={{ color: C.dim }}>—</span>}
                  </td>
                  {stats.map((s) => (
                    <td key={s.label}
                        className="whitespace-nowrap px-3 py-2 text-right tabular-nums"
                        style={{ color: C.ink2 }}>{s.value}</td>
                  ))}
                  <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums"
                      style={{ color: C.ink2 }}>{c.meter_value}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* ===== the board ======================================================== */

export default function SlotWall({
  payload,
  autoRotate = false,
  rotateSeconds,
  initialView,
  className = "",
}) {
  const views = payload?.views || [];
  const startIndex = Math.max(0, views.findIndex((v) => v.name === initialView));
  const [current, setCurrent] = useState(startIndex);
  const [sheetOpen, setSheetOpen] = useState(false);
  const clock = useClock();
  const fresh = useFreshness(payload?.generated_iso);

  // A view can vanish between payloads (a tab removed upstream); clamp rather
  // than render undefined.
  const index = Math.min(current, Math.max(0, views.length - 1));
  const view = views[index];

  // Rotation is off by default: a TV wants it, a page inside an app does not.
  // It also stops while the table is open — rotating the view out from under
  // someone reading it is the one thing this must not do.
  const period = (rotateSeconds ?? payload?.rotate_seconds ?? 20) * 1000;
  useEffect(() => {
    if (!autoRotate || sheetOpen || views.length < 2) return undefined;
    const id = setTimeout(() => setCurrent((i) => (i + 1) % views.length), period);
    return () => clearTimeout(id);
  }, [autoRotate, sheetOpen, views.length, period, index]);

  const pick = useCallback((i) => { setCurrent(i); setSheetOpen(false); }, []);

  if (!view) {
    return (
      <div className={`rounded-xl p-6 text-sm ${className}`}
           style={{ background: C.panel, color: C.muted }}>
        No board data yet.
      </div>
    );
  }

  return (
    <div className={`relative flex flex-col gap-5 rounded-2xl p-5 sm:p-7 ${className}`}
         style={{ background: C.bg, color: C.ink }}>

      <header className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex min-w-0 flex-col gap-2">
          <div className="text-2xl font-bold sm:text-3xl">Schengen Slot Board</div>
          <div className="flex flex-wrap gap-2" role="tablist" aria-label="View">
            {views.map((v, i) => (
              <button key={v.name} type="button" role="tab"
                      aria-selected={i === index} title={v.subtitle}
                      onClick={() => pick(i)}
                      className="rounded-full px-3 py-1 text-sm font-semibold transition-colors"
                      style={i === index
                        ? { background: C.accent, color: C.bg }
                        : { background: C.panel, color: C.muted }}>
                {v.name}
              </button>
            ))}
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-4 sm:gap-6">
          {view.kpis.map((k) => <Kpi key={k.label} {...k} />)}
        </div>

        <div className="flex flex-col items-end gap-1.5">
          <div className="text-2xl font-semibold leading-none tabular-nums sm:text-4xl">
            {clock.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })}
          </div>
          <div className="flex items-center gap-2 text-xs" style={{ color: C.muted }}>
            <span className={`h-2 w-2 rounded-full ${fresh.stale ? DOT.occ : DOT.open}`} />
            <span>updated {fresh.text}</span>
            <button type="button" onClick={() => setSheetOpen((o) => !o)}
                    aria-expanded={sheetOpen}
                    className="rounded-full border px-3 py-1 text-sm transition-colors hover:brightness-125"
                    style={{ borderColor: C.line, color: C.ink2 }}>
              All countries
            </button>
          </div>
        </div>
      </header>

      <div className="grid min-h-0 gap-4 lg:grid-cols-[1fr_20rem]">
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {view.cards.map((card, i) => (
            <CountryCard key={card.country} card={card} lead={i === 0} />
          ))}
        </div>
        <ChangeFeed view={view} />
      </div>

      {sheetOpen ? <AllCountriesSheet view={view} onClose={() => setSheetOpen(false)} /> : null}
    </div>
  );
}
