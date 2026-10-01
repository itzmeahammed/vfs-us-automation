"""The API-to-CLI contract for booking, and the guards on spending money.

    ═════════════════ WHY THIS FILE EXISTS ═════════════════

`BookingTriggerRequest.to_cli_args()` emits flag names that must exist in
`src/booking/__main__.py`. Nothing in the type system connects those two files,
and when they disagreed the failure was total and silent from the API's side:
the old code emitted `--source AE --dest NOR` against a parser defining
`-sc/--source-country`, so every booking trigger died in argparse before the
browser opened. The job went to FAILED with an exit code and no explanation a
caller could act on.

So the central test here does not assert on a hand-written expected list — that
would just be the same guess written twice. It feeds the rendered argv to the
REAL parser and asserts it parses, and that the parsed values are the ones the
request asked for. A flag renamed on either side fails this immediately.
"""

from __future__ import annotations

import contextlib
import io

import pytest
from pydantic import ValidationError

from src.api.modules.booking.schemas import BookingMode, BookingTriggerRequest


def _parse(args):
    """Run the real booking CLI parser over `args`, returning the namespace.

    Imported inside the function: src.booking.__main__ pulls in the browser
    stack at module scope, and a collection-time import failure there would
    take out this whole file for an unrelated reason.
    """
    from src.booking.__main__ import main  # noqa: F401  (ensures importable)
    import src.booking.__main__ as cli

    # Rebuild the parser exactly as main() does, without running a command.
    # argparse exits the process on a bad flag, so the error is captured and
    # re-raised as an assertion the test can actually read.
    import argparse

    verbose = argparse.ArgumentParser(add_help=False)
    verbose.add_argument("-v", "--verbose", action="store_true")

    parser = argparse.ArgumentParser(prog="python -m src.booking")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    # Mirror of the probe subparser. Kept in sync by
    # test_probe_parser_mirror_is_accurate below, which asserts this mirror
    # accepts exactly the flags the real one does.
    probe = sub.add_parser("probe", parents=[verbose])
    probe.add_argument("-sc", "--source-country", required=True)
    probe.add_argument("-dc", "--dest-country", required=True)
    probe.add_argument("--registrant")
    probe.add_argument("--email")
    probe.add_argument("--password")
    probe.add_argument("--proxy-url")
    probe.add_argument("--keep-open", action="store_true")
    probe.add_argument("--capture", choices=["off", "failure", "full"],
                       default="failure")
    probe.add_argument("--walk", action="store_true")
    probe.add_argument("--combo", default="")
    probe.add_argument("--entry", choices=["waitlist", "new"], default="")
    probe.add_argument("--hold", dest="hold_seconds", type=int, default=0)
    probe.add_argument("--applicant", action="append", default=[])
    probe.add_argument("--commit", action="store_true")
    probe.add_argument("--yes", action="store_true")
    probe.add_argument("--to", dest="to_step")

    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            return parser.parse_args(["probe", *args])
    except SystemExit as exc:  # pragma: no cover - only on a contract break
        raise AssertionError(
            f"The real booking CLI rejected the API's argv.\n"
            f"  argv:  {args}\n"
            f"  error: {stderr.getvalue().strip()}"
        ) from exc


# --------------------------------------------------------------------------- #
# The mirror above is only trustworthy if it matches the real parser.          #
# --------------------------------------------------------------------------- #

def test_probe_parser_mirror_is_accurate():
    """Every flag the mirror defines must exist on the real probe subparser.

    This is what stops the contract test from passing against a stale copy of
    the parser. It reads the real module's source rather than introspecting the
    parser, because building the real one requires the browser imports.
    """
    import pathlib

    source = pathlib.Path("src/booking/__main__.py").read_text(encoding="utf-8")
    for flag in ("-sc", "--source-country", "-dc", "--dest-country",
                 "--registrant", "--capture", "--walk", "--combo", "--entry",
                 "--applicant", "--commit", "--yes"):
        assert f'"{flag}"' in source, (
            f"{flag} is in this test's mirror parser but not in "
            f"src/booking/__main__.py — one of the two moved."
        )
    # --to sets dest=to_step; assert the pairing, since to_cli_args emits --to.
    assert '"--to", dest="to_step"' in source


# --------------------------------------------------------------------------- #
# The contract: rendered argv must parse, with the right values.               #
# --------------------------------------------------------------------------- #

def test_probe_mode_renders_parseable_argv():
    args = BookingTriggerRequest(route="AE-NOR").to_cli_args()
    ns = _parse(args)
    assert ns.source_country == "AE"
    assert ns.dest_country == "NOR"
    assert ns.walk is False, "probe mode must not click Book Now"
    assert ns.commit is False


def test_route_is_split_into_the_two_required_flags():
    """The regression that motivated this file: -sc/-dc, not --source/--dest."""
    args = BookingTriggerRequest(route="AE-CHE").to_cli_args()
    assert "--source" not in args and "--dest" not in args
    assert args[:4] == ["-sc", "AE", "-dc", "CHE"]


def test_walk_mode_passes_walk_but_not_commit():
    args = BookingTriggerRequest(route="AE-NOR", mode="walk").to_cli_args()
    ns = _parse(args)
    assert ns.walk is True
    assert ns.commit is False, "walk must stop at the commit boundary"


def test_commit_mode_passes_walk_commit_and_yes():
    """--commit requires --walk, and needs --yes because no operator is there."""
    args = BookingTriggerRequest(
        route="AE-NOR", mode="commit", confirm="AE-NOR",
    ).to_cli_args()
    ns = _parse(args)
    assert ns.walk is True, "--commit requires --walk"
    assert ns.commit is True
    assert ns.yes is True, "a scheduled run cannot answer a prompt"


def test_every_optional_field_reaches_the_parser():
    request = BookingTriggerRequest(
        route="AE-NOR",
        mode="walk",
        registrant="mufaddal-nor",
        combo="Norway Visa Application Center - Dubai - Tourist",
        entry="new",
        applicant={"first_name": "Zaid", "last_name": "Khan"},
        capture="full",
        to_step="select_slot",
    )
    ns = _parse(request.to_cli_args())
    assert ns.registrant == "mufaddal-nor"
    assert ns.combo == "Norway Visa Application Center - Dubai - Tourist"
    assert ns.entry == "new"
    assert ns.capture == "full"
    assert ns.to_step == "select_slot"
    assert sorted(ns.applicant) == ["first_name=Zaid", "last_name=Khan"]


def test_applicant_pairs_survive_a_value_containing_spaces():
    """Each pair is ONE argv entry, so a space must not split the field."""
    request = BookingTriggerRequest(
        route="AE-NOR", applicant={"first_name": "Mary Jane"},
    )
    ns = _parse(request.to_cli_args())
    assert ns.applicant == ["first_name=Mary Jane"]


# --------------------------------------------------------------------------- #
# The money guards. Each of these is a refusal, before any job is spawned.     #
# --------------------------------------------------------------------------- #

def test_default_mode_is_the_read_only_one():
    """No caller gets a booking, let alone a payment, by omitting a field."""
    assert BookingTriggerRequest(route="AE-NOR").mode is BookingMode.PROBE


def test_commit_without_confirm_is_refused():
    with pytest.raises(ValidationError, match="confirm to equal route"):
        BookingTriggerRequest(route="AE-NOR", mode="commit")


def test_commit_with_a_mismatched_confirm_is_refused():
    """Guards against pasting the wrong route into an otherwise valid request."""
    with pytest.raises(ValidationError, match="confirm to equal route"):
        BookingTriggerRequest(route="AE-NOR", mode="commit", confirm="AE-CHE")


def test_commit_refuses_full_capture_because_it_would_write_the_card_number():
    """capture='full' dumps every page's DOM, and one of them holds the PAN."""
    with pytest.raises(ValidationError, match="card number"):
        BookingTriggerRequest(route="AE-NOR", mode="commit",
                              confirm="AE-NOR", capture="full")


def test_commit_refuses_to_step_because_it_would_half_book():
    with pytest.raises(ValidationError, match="half-made"):
        BookingTriggerRequest(route="AE-NOR", mode="commit",
                              confirm="AE-NOR", to_step="select_slot")


def test_confirm_without_commit_is_refused_rather_than_ignored():
    """A stray confirm means the caller believes they are committing."""
    with pytest.raises(ValidationError, match="only meaningful with mode=commit"):
        BookingTriggerRequest(route="AE-NOR", mode="walk", confirm="AE-NOR")


def test_entry_new_requires_a_combo():
    """Memory: the live-slot walk has no roster, so it dies on page 2 without one."""
    with pytest.raises(ValidationError, match="entry='new' requires combo"):
        BookingTriggerRequest(route="AE-NOR", mode="walk", entry="new")


def test_the_old_dry_run_field_is_now_a_422_not_a_silent_dry_run():
    """The previous shape accepted dry_run and ignored it. That must never
    be mistaken for working: a caller asking to spend money and getting a dry
    run is the worst direction for this failure, so the field is now rejected
    outright by extra='forbid'."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        BookingTriggerRequest(route="AE-NOR", dry_run=False)


# --------------------------------------------------------------------------- #
# Input validation                                                             #
# --------------------------------------------------------------------------- #

def test_route_is_required_and_normalised():
    assert BookingTriggerRequest(route=" ae-nor ").route == "AE-NOR"
    with pytest.raises(ValidationError):
        BookingTriggerRequest()


@pytest.mark.parametrize("bad", ["AENOR", "A-NOR", "AE-N", "AE-TOOLONG", "../x"])
def test_malformed_routes_are_refused(bad):
    with pytest.raises(ValidationError):
        BookingTriggerRequest(route=bad)


def test_an_applicant_key_containing_equals_is_refused():
    """It would re-split into a different field on the CLI side."""
    with pytest.raises(ValidationError, match="applicant key"):
        BookingTriggerRequest(route="AE-NOR", applicant={"first=name": "x"})


def test_a_multiline_applicant_value_is_refused():
    with pytest.raises(ValidationError, match="single line"):
        BookingTriggerRequest(route="AE-NOR",
                              applicant={"note": "line1\nline2"})


def test_combo_whitespace_is_collapsed_to_match_the_journal():
    """Memory/journal normalisation: a stray double space must not read as a
    different combo than the one already registered."""
    request = BookingTriggerRequest(route="AE-NOR",
                                    combo="Dubai  -   SCHENGEN")
    assert request.combo == "Dubai - SCHENGEN"


# --------------------------------------------------------------------------- #
# Operator-facing messages must not crash, and must name a real path.         #
# --------------------------------------------------------------------------- #

def test_the_payment_journal_path_is_never_hardcoded_in_operator_messages():
    """These strings are read by a human mid-incident.

    The journal moved from logs/ to state/, and six messages still named the
    old path — sending an operator to a file that no longer exists at the exact
    moment they are trying to find out whether a card was charged. Naming it
    from the constant is the only version that stays correct.
    """
    import pathlib

    from src.payment.journal import JOURNAL_FILE

    assert "state" in JOURNAL_FILE, (
        "the payment journal is a durable ledger and belongs in state/, not in "
        "logs/ where a retention sweep prunes by age"
    )

    # Only STRING LITERALS are checked, via the AST. A grep over the raw text
    # also matches comments that legitimately mention the old path while
    # explaining why it moved, and a test that cannot distinguish the two would
    # have to be deleted the first time someone documented the change.
    import ast

    for name in ("src/booking/__main__.py", "src/booking/probe.py",
                 "src/booking/walk.py", "src/payment/gateway.py"):
        tree = ast.parse(pathlib.Path(name).read_text(encoding="utf-8"))
        literals = [
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        offenders = [text for text in literals if "logs/payments.jsonl" in text]
        # A module docstring may explain the move; only non-docstring literals
        # are messages an operator is shown.
        docstrings = {ast.get_docstring(node) for node in ast.walk(tree)
                      if isinstance(node, (ast.Module, ast.FunctionDef,
                                           ast.AsyncFunctionDef, ast.ClassDef))}
        offenders = [text for text in offenders if text not in docstrings]
        assert not offenders, (
            f"{name} names the OLD journal path in a message an operator reads "
            f"during an incident; use journal.JOURNAL_FILE instead: {offenders}"
        )


def test_cmd_probe_imports_the_payment_journal_before_it_is_used():
    """Guards a real NameError.

    Two report branches in cmd_probe reference `payment_journal`. Both are only
    reachable on a --commit run that declined or was blocked — i.e. the branch
    that runs precisely when money is at stake and nobody wants a traceback
    instead of instructions. A function-scope import is what makes both safe.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("src/booking/__main__.py").read_text(
        encoding="utf-8"))
    func = next(node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == "cmd_probe")

    # Matches whichever spelling is used — `from src.payment import journal as
    # payment_journal` (module src.payment) or `from src.payment.journal import
    # ...`. Binding the ALIAS is what matters, not the module path it came by.
    import_line = min(
        (node.lineno for node in ast.walk(func)
         if isinstance(node, ast.ImportFrom)
         and (node.module or "").startswith("src.payment")
         and any(alias.asname == "payment_journal" or alias.name == "payment_journal"
                 for alias in node.names)),
        default=None,
    )
    assert import_line is not None, (
        "cmd_probe uses payment_journal but never imports it — a NameError on "
        "the payment-declined path"
    )

    uses = [node.lineno for node in ast.walk(func)
            if isinstance(node, ast.Name) and node.id == "payment_journal"]
    assert uses, "expected at least one use to guard"
    assert min(uses) > import_line, (
        f"payment_journal is used at line {min(uses)} but imported at "
        f"{import_line}"
    )
