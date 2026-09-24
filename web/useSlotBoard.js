/**
 * Fetching the slot board.
 *
 * Deliberately small and dependency-free so it drops into any app. If yours
 * already uses React Query or SWR, throw this away and use that instead — the
 * only things worth copying are the ETag handling and the rule about errors
 * below.
 *
 * THE RULE: a failed refresh must not blank the board. The payload carries its
 * own age on every card ("Seen 28m", "Open now" vs "Was open 6h ago"), so
 * showing the last good board next to an honest "updated 2h ago" is strictly
 * better than showing nothing. This matters more than it looks: the bot runs
 * 09:00-24:00 by design, so overnight there is no fresher board to get, and a
 * hook that clears its data on error would show an empty dashboard every
 * morning.
 */

import { useCallback, useEffect, useRef, useState } from "react";

const DEFAULT_INTERVAL_MS = 60_000;

export default function useSlotBoard(url, {
  intervalMs = DEFAULT_INTERVAL_MS,
  fetchOptions,
  enabled = true,
} = {}) {
  const [payload, setPayload] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  // Kept in refs so a 304 costs nothing and a re-render does not refetch.
  const etag = useRef(null);
  const abort = useRef(null);
  const options = useRef(fetchOptions);
  options.current = fetchOptions;

  const load = useCallback(async () => {
    if (!url) return;
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    try {
      const headers = { Accept: "application/json", ...(options.current?.headers || {}) };
      if (etag.current) headers["If-None-Match"] = etag.current;

      const res = await fetch(url, {
        ...options.current,
        headers,
        signal: controller.signal,
      });

      // Unchanged since last poll: keep what we have, and keep it current.
      if (res.status === 304) { setError(null); return; }
      if (!res.ok) throw new Error(`Slot board request failed: ${res.status}`);

      const tag = res.headers.get("ETag");
      if (tag) etag.current = tag;

      const body = await res.json();
      if (!body || !Array.isArray(body.views)) {
        throw new Error("Slot board response is not a board payload.");
      }
      setPayload(body);
      setError(null);
    } catch (err) {
      if (err.name === "AbortError") return;
      // The board we already have stays on screen; see THE RULE above.
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [url]);

  useEffect(() => {
    if (!enabled) return undefined;
    load();
    const id = setInterval(load, intervalMs);
    // A tab left open all night is the normal case for a dashboard, so refresh
    // when it comes back to the front rather than waiting out the interval.
    const onVisible = () => { if (document.visibilityState === "visible") load(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
      abort.current?.abort();
    };
  }, [enabled, intervalMs, load]);

  return { payload, error, loading, refresh: load };
}
