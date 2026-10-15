"""Payment: the gateway VFS hands off to after "Pay Online".

Separate from `src/booking/` on purpose. Booking is reversible and
country-specific; payment is irreversible and (probably) processor-specific,
which is a different axis. Keeping them apart means the booking code has no
path that can spend money, and the payment code has no reason to know what a
visa appointment is.

    card.py      the company card: the environment, then a guarded local file.
    gateway.py   drives the processor's page. The only irreversible code here.
    otp.py       types a 3-D Secure code into the BANK's screen — and is built
                 mostly out of guards against clicking the gateway's Pay
                 button a second time.

The code itself comes from `src/otp_relay/` (an SMS relay), which is kept
outside this package deliberately: "get me a one-time code" is not a payment
concern, and the waitlist has an OTP step that wants the same service.
"""
