"""Slot history: a permanent, queryable record of every availability check.

    db.py        connection + forward-only migrations
    schema.sql   the tables
    parse.py     banner text -> outcome + dates          (pure)
    registry.py  config/routes/*.json -> canonical combos, label resolution
    events.py    check-to-check transitions (opened/closed/moved)  (pure)
    store.py     the only writer; failure-isolated API for the bot
    logreader.py log files -> checks, for seeding history
    query.py     the numbers the dashboard asks for
    dashboard.py the agent-facing HTML page

The bot writes; the dashboard reads. See SLOT_ANALYTICS_TASKS.md for the plan.
"""
