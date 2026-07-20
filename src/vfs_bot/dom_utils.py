"""Small, generic Playwright DOM helpers shared by more than one flow module."""

import logging

from src.vfs_bot.errors import RetryableError


def fill_field(page, locator, value: str) -> None:
    """
    Sets an input's value robustly, immune to overlays and Xvfb hangs.

    Tries Playwright fill() first (bounded timeout). If that's blocked (an
    overlay intercepting actionability), falls back to setting the value via
    JS and dispatching the 'input'/'change' events Angular listens for, so
    the form model updates even without a real focus/click.
    """
    try:
        locator.fill(value, timeout=4000)
        return
    except Exception as e:
        logging.debug(f"fill() blocked ({e}); using JS value-set fallback.")
    try:
        locator.evaluate(
            """(el, val) => {
                el.value = val;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('blur', { bubbles: true }));
            }""",
            value,
        )
    except Exception as e:
        raise RetryableError(f"Could not fill a login field: {e}") from e
