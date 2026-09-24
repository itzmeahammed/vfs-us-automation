/**
 * The slot board payload, as `src/slots/wall.py::build_payload` produces it.
 *
 * Every string here is already formatted for display — "Sep 24", "28m", "+1d",
 * "Open now". The React component does no arithmetic on them and must not start
 * to: the ranking, bookability, forecast and wording rules live in Python where
 * they are covered by the test suite. Two implementations would drift, and the
 * subtle rules (a stale reading is not a bookable slot; sorting happens before
 * the board is cut to six) are exactly the ones that drift silently.
 *
 * Types are provided for TypeScript hosts. The component itself is plain JSX so
 * it drops into a JavaScript app unchanged.
 */

/** Colour role. Always shipped alongside its label, never used as the only cue. */
export type Kind = "open" | "occ" | "wait" | "none";

/** `due` = something to act on (accent). `quiet` = we are declining to guess. */
export type ForecastKind = "due" | "quiet";

export interface Stat {
  label: string;
  value: string;
}

/**
 * The "next opening" strip. Present only when it says something the card does
 * not already say — absent when the slot is bookable now, or when the card's
 * own empty state already reads "Waitlist only" or "No slots".
 */
export interface ForecastLine {
  label: string;
  value: string;
  note: string;
  kind: ForecastKind;
}

export interface Card {
  /** Position across the WHOLE board, 1-based. Stable between cards and table. */
  rank: number;
  country: string;
  /** Live state: "Open now", "Was open 6h ago", "Waitlist only", "Not checked". */
  status: string;
  kind: Kind;
  /** "New slot" / "5d sooner", set only within the last few minutes. */
  badge: string;
  /** Score 0-100 as a number (bar width) and as a string (label). */
  meter: number;
  meter_value: string;

  /** True only when a slot is bookable NOW: fresh reading, date not passed. */
  has_date: boolean;
  headline_label?: string;
  /** The appointment date, e.g. "Sep 24". Meaningful only when has_date. */
  headline?: string;
  /** Where that appointment is, e.g. "Abu Dhabi, Short Stay - Tourist". */
  centre?: string;

  /** Shown when has_date is false. An empty title means the sub carries alone. */
  empty_title: string;
  empty_sub: string;

  forecast: ForecastLine | null;
  stats_left: Stat[];
  stats_right: Stat[];

  /** Internal sort key. Rows arrive sorted; the component must not re-sort. */
  order?: [number, string, number];
}

export interface FeedItem {
  title: string;
  sub: string;
  kind: Kind;
  when: string;
}

export interface View {
  /** "Tourist" | "Business" | "Waitlist" — the tab label. */
  name: string;
  subtitle: string;
  kpis: Stat[];
  /** The six the board shows. A slice of `table`, already ranked. */
  cards: Card[];
  /** EVERY country, same order and same ranks. Behind "All countries". */
  table: Card[];
  feed: FeedItem[];
  feed_title: string;
  feed_empty: string;
  note_label: string;
  note: string;
}

export interface SlotBoardPayload {
  /** ISO 8601, UTC. What the freshness indicator counts from. */
  generated_iso: string;
  /** The same moment, pre-formatted for display. */
  generated: string;
  rotate_seconds: number;
  views: View[];
  /**
   * Contract version. Bumped when the shape changes in a way that would break
   * a renderer. Absent on payloads built before versioning.
   */
  schema?: number;
}

export interface SlotWallProps {
  payload: SlotBoardPayload;
  /** Cycle the tabs automatically. Off by default: a TV wants it, a page does not. */
  autoRotate?: boolean;
  rotateSeconds?: number;
  /** Start on this tab name, e.g. "Business". Defaults to the first view. */
  initialView?: string;
  className?: string;
}

declare const SlotWall: (props: SlotWallProps) => JSX.Element;
export default SlotWall;
