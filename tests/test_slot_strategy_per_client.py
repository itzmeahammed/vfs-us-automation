"""The date window belongs to the CLIENT, not the country.

    ═══════════════ WHY THIS EXISTS ═══════════════

A sales agent sells one booking at a time. Two agents may be selling two
different things on the same route — "the soonest you can get me" and "the 10th
to the 20th of November, nothing else" — and both must work in the same run of
the same country.

Before this, `strategy` was read only from config/booking/<ROUTE>.json:
ONE SETTING FOR EVERY CLIENT that country ever books. Flipping it to serve one
client silently changed every other.

The dates were already per-client. The STRATEGY was not, which made the
per-client dates unusable without a global switch.
"""

from datetime import date

import pytest

from src.booking import walk
from src.waitlist.errors import WaitlistStepError

ROUTE_DEFAULT = {"strategy": "earliest"}


# --------------------------------------------------------------------------- #
# Precedence                                                                   #
# --------------------------------------------------------------------------- #

def test_a_window_on_the_record_selects_in_range_BY_ITSELF():
    """*** THE ONE THAT MATTERS. ***

    An agent who typed a date range has already said what they want. Requiring
    a SECOND flag means the failure mode is a record whose dates are silently
    ignored — and the booking lands on a date nobody agreed to, which is the
    worst outcome this code has.
    """
    values = {"date_from": "2026-11-10", "date_to": "2026-11-20"}

    assert walk.resolve_strategy(values, ROUTE_DEFAULT) == "in_range"


def test_NO_WINDOW_REFUSES_rather_than_falling_back():
    """*** A BOOKING IS NEVER MADE ON A GUESSED DATE. ***

    A missing window used to mean "earliest". That made two cases identical: a
    client who genuinely wanted the soonest slot, and a client whose agent
    MEANT to give a range and forgot. The first costs nothing; the second costs
    a real appointment on a date nobody agreed to, a real card charge, and a
    slot that is then gone — discovered afterwards, and irreversible.

    Refusing costs someone typing two fields, before a browser opens.
    """
    with pytest.raises(WaitlistStepError) as caught:
        walk.resolve_strategy({}, ROUTE_DEFAULT)

    message = str(caught.value)
    assert "date_from" in message and "date_to" in message, (
        "the message must name the fields a sales agent has to fill in")
    assert "slot_strategy" in message, (
        "it must also say how to ask for 'soonest' deliberately")


def test_the_route_strategy_alone_is_NOT_enough():
    """A route-level default cannot authorise a booking.

    It is one setting for every client that country ever books, so it can never
    express what one agent sold to one person. Kept in the config only so
    existing files parse.
    """
    with pytest.raises(WaitlistStepError):
        walk.resolve_strategy({}, {"strategy": "earliest"})


def test_an_explicit_client_strategy_beats_the_route():
    assert walk.resolve_strategy(
        {"slot_strategy": "latest"}, ROUTE_DEFAULT) == "latest"


def test_an_explicit_earliest_overrides_the_agents_own_window():
    """Deliberate asymmetry: dates imply the strategy, but an explicit
    'earliest' still wins. An agent who writes that has overridden their own
    window on purpose, and the window stays on the record as a note rather
    than having to be deleted."""
    values = {"slot_strategy": "earliest",
              "date_from": "2026-11-10", "date_to": "2026-11-20"}

    assert walk.resolve_strategy(values, ROUTE_DEFAULT) == "earliest"


def test_two_clients_on_one_route_get_different_strategies():
    """The whole point, stated as the scenario it came from.

    Both say what they want ON THEIR OWN RECORD, and neither can be served by
    a country-wide setting.
    """
    urgent = {"slot_strategy": "earliest"}
    scheduled = {"date_from": "2026-11-10", "date_to": "2026-11-20"}

    assert walk.resolve_strategy(urgent, ROUTE_DEFAULT) == "earliest"
    assert walk.resolve_strategy(scheduled, ROUTE_DEFAULT) == "in_range"


def test_the_refusal_happens_BEFORE_a_browser_starts():
    """The check must be offline, or it costs a login to discover.

    VFS blocks an account after roughly three logins in a short window, and
    that block outlives a 12-hour invitation. resolve_strategy runs deep in the
    walk — after login — so the probe asks the same question up front.
    """
    import inspect

    from src.booking import probe

    assert hasattr(probe, "_assert_bookable_window")

    source = inspect.getsource(probe.run_probe)
    gate = source.index("_assert_bookable_window")
    launch = source.index("bot = None")
    assert gate < launch, (
        "the date-window gate runs after the browser is launched, so a client "
        "with no dates still costs a login")


def test_the_gate_matches_the_real_slot_step_type():
    """A hard-coded type that drifts from the config makes the gate match
    NOTHING — indistinguishable from 'every client is fine'."""
    from src.utils.config_reader import initialize_config
    from src.booking import config as booking_config

    initialize_config()
    booking_config.clear_cache()
    steps = booking_config.steps_for("AE-NOR", "new")

    assert any(st.get("type") == walk.SLOT_STEP_TYPE for st in steps), (
        f"no step has type {walk.SLOT_STEP_TYPE!r}; the offline gate would "
        "silently pass every client")


def test_a_typo_in_slot_strategy_is_refused_by_name():
    with pytest.raises(WaitlistStepError) as caught:
        walk.resolve_strategy({"slot_strategy": "soonest"}, ROUTE_DEFAULT)

    message = str(caught.value)
    assert "soonest" in message
    assert "in_range" in message, "must list what IS valid"


def test_an_unknown_route_strategy_is_still_refused():
    with pytest.raises(WaitlistStepError):
        walk.resolve_strategy({}, {"strategy": "whenever"})


# --------------------------------------------------------------------------- #
# The window is validated OFFLINE                                              #
# --------------------------------------------------------------------------- #

TODAY = date(2026, 9, 29)


@pytest.mark.parametrize("values,expected_fragment", [
    ({"date_from": "10/11/2026", "date_to": "2026-11-20"}, "YYYY-MM-DD"),
    ({"date_from": "2026-11-10"}, "BOTH ends"),
    ({"date_to": "2026-11-20"}, "BOTH ends"),
    ({"date_from": "2026-11-20", "date_to": "2026-11-10"}, "backwards"),
    ({"date_from": "2020-01-01", "date_to": "2020-01-05"}, "in the past"),
])
def test_the_realistic_typing_mistakes_are_caught(values, expected_fragment):
    """These are what a sales agent actually gets wrong. Each must be named in
    plain language, because the person reading it is not a programmer."""
    problems = walk.check_window(values, {}, today=TODAY, max_months=6)

    assert problems, f"{values} was accepted"
    assert any(expected_fragment in p for p in problems), problems


def test_every_problem_is_reported_at_once():
    """A list, not the first failure: an operator fixing one and resubmitting
    to find the next is a worse experience and a slower one."""
    problems = walk.check_window(
        {"date_from": "nonsense", "date_to": "also-nonsense"},
        {}, today=TODAY, max_months=6)

    assert len(problems) == 2


def test_an_unparseable_end_does_not_ALSO_claim_a_missing_end():
    """Both ends WERE supplied; one could not be read. Telling the operator to
    add the missing end sends them looking for a field they already filled."""
    problems = walk.check_window(
        {"date_from": "10/11/2026", "date_to": "2026-11-20"},
        {}, today=TODAY, max_months=6)

    assert not any("BOTH ends" in p for p in problems), problems


def test_no_window_is_not_a_problem():
    """A client taking 'earliest' supplies no dates. That is ordinary."""
    assert walk.check_window({}, {}, today=TODAY, max_months=6) == []


def test_a_single_day_is_expressed_as_the_same_date_twice():
    values = {"date_from": "2026-11-14", "date_to": "2026-11-14"}

    assert walk.check_window(values, {}, today=TODAY, max_months=6) == []
    assert walk.resolve_strategy(values, ROUTE_DEFAULT) == "in_range"


# --------------------------------------------------------------------------- #
# Filtering                                                                    #
# --------------------------------------------------------------------------- #

def test_only_dates_inside_the_window_are_usable():
    offered = ["2026-10-05", "2026-11-09", "2026-11-14",
               "2026-11-18", "2026-12-01"]
    start, end = walk.date_window(
        {"date_from": "2026-11-10", "date_to": "2026-11-20"})

    assert walk.dates_in_window(offered, start, end) == ["2026-11-14",
                                                        "2026-11-18"]


def test_both_ends_are_inclusive():
    start, end = walk.date_window(
        {"date_from": "2026-11-10", "date_to": "2026-11-20"})

    assert walk.dates_in_window(["2026-11-10", "2026-11-20"], start, end) == [
        "2026-11-10", "2026-11-20"]


def test_the_offline_check_command_validates_client_windows():
    """A typo'd date must fail before a browser starts.

    walk.check_window existed but was called only from INSIDE the walk — after
    a login. VFS blocks an account after roughly three logins in a short
    window, so spending one to be told a date was typed with slashes is the
    most avoidable failure in this system.
    """
    import inspect

    from src.booking import __main__ as cli

    assert hasattr(cli, "_check_client_windows")
    source = inspect.getsource(cli.cmd_check)
    assert "_check_client_windows" in source, (
        "`python -m src.booking check` does not validate client date windows")


# --------------------------------------------------------------------------- #
# The API boundary                                                             #
# --------------------------------------------------------------------------- #
#
# date_from/date_to come from an API consumer — the sales agent's system —
# writing into config/registrants/<client>.json. Format mistakes must be
# refused where they are made, with a 400 the caller sees while still looking
# at what they typed. Every later place is worse: the offline check only if
# someone runs it, the pre-launch gate only when a booking is attempted, the
# walk only after a login.

def _record(**extra):
    base = {"route": "AE-NOR", "combos": ["x"],
            "first_name": "A", "last_name": "B"}
    base.update(extra)
    return base


@pytest.mark.parametrize("extra,fragment", [
    ({"date_from": "10/11/2026", "date_to": "2026-11-20"}, "YYYY-MM-DD"),
    ({"date_from": "20-11-2026", "date_to": "2026-11-20"}, "YYYY-MM-DD"),
    ({"date_from": "2026-13-01", "date_to": "2026-11-20"}, "real calendar"),
    ({"date_from": "2026-11-10"}, "BOTH ends"),
    ({"date_to": "2026-11-20"}, "BOTH ends"),
    ({"date_from": "2026-11-20", "date_to": "2026-11-10"}, "backwards"),
])
def test_a_bad_window_is_refused_when_the_record_is_WRITTEN(extra, fragment):
    from src.waitlist.errors import WaitlistConfigError
    from src.waitlist.registrant import _validate

    with pytest.raises(WaitlistConfigError) as caught:
        _validate("c1", _record(**extra))

    assert fragment in str(caught.value), caught.value


def test_an_ambiguous_date_is_never_guessed():
    """10/11/2026 is 10 November to some readers and 11 October to others.

    Accepting it and picking one reading books an appointment on a date the
    agent did not mean, which is irreversible and costs a charge.
    """
    from src.waitlist.errors import WaitlistConfigError
    from src.waitlist.registrant import _validate

    with pytest.raises(WaitlistConfigError):
        _validate("c1", _record(date_from="10/11/2026", date_to="11/11/2026"))


def test_a_valid_window_is_accepted():
    from src.waitlist.registrant import _validate

    _validate("c1", _record(date_from="2026-11-10", date_to="2026-11-20"))


def test_a_single_day_is_accepted():
    from src.waitlist.registrant import _validate

    _validate("c1", _record(date_from="2026-11-14", date_to="2026-11-14"))


def test_no_window_is_still_a_VALID_RECORD():
    """Validation is about SHAPE. A record with no dates is well-formed; it is
    simply not bookable yet, and resolve_strategy is what refuses the booking.
    Rejecting it here would stop an agent saving a client before the dates are
    agreed."""
    from src.waitlist.registrant import _validate

    _validate("c1", _record())


def test_the_api_accepts_the_date_fields():
    """The consumer writes these through the client API, so the request models
    must not strip them."""
    from src.api.schemas import ClientPatchRequest

    patch = ClientPatchRequest(**{"date_from": "2026-11-10",
                                  "date_to": "2026-11-20"})
    sent = patch.model_dump(exclude_unset=True)

    assert sent["date_from"] == "2026-11-10"
    assert sent["date_to"] == "2026-11-20"
