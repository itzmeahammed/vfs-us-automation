"""Payment: the gateway VFS hands off to after "Pay Online".

Separate from `src/booking/` on purpose. Booking is reversible and
country-specific; payment is irreversible and (probably) processor-specific,
which is a different axis. Keeping them apart means the booking code has no
path that can spend money, and the payment code has no reason to know what a
visa appointment is.

    card.py      the company card, from the environment. Never persisted.
    gateway.py   drives the processor's page. The only irreversible code here.
"""
