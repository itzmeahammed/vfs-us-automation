"""`python -m src.otp_relay` — prove the SMS relay works before a payment does.

    ═══════════════════ RUN `devices` FIRST. ALWAYS. ═══════════════════

The bank texts the number registered on the card. If that SIM is not in a
Relayion device, no amount of correct code makes the 3-D Secure auto-fill
work, and the failure looks exactly like "no SMS arrived" at the worst
possible moment — mid-payment, with a booked appointment unpaid.

    devices     what the relay can see, and whether it is online
    watch       tail inbound SMS. Codes are MASKED; bodies are never printed
    wait        block for one fresh code, like the payment path does
    parse       run the extractor over a body you paste in — no network

`parse` is the one to reach for when a live run logged "held no 6-digit code":
paste the SMS, see what the extractor makes of it, and if it refuses, the fix
is a line in config/payment/<PROCESSOR>.json rather than a code change.
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from typing import List

from src.otp_relay import extract, relayion
from src.otp_relay.errors import OtpNotConfigured, OtpRelayError
from src.otp_relay.inbox import OtpInbox


def _mask(code: str) -> str:
    """A code is a secret for the three minutes that matter. Shape only."""
    if not code:
        return "none"
    return f"{code[0]}{'*' * (len(code) - 2)}{code[-1]} ({len(code)} digits)"


def cmd_devices(args) -> int:
    print()
    print("=" * 68)
    print("RELAYION DEVICES")
    print("=" * 68)
    try:
        found = relayion.devices()
    except OtpNotConfigured as exc:
        print(f"NOT CONFIGURED: {exc}")
        return 1
    except OtpRelayError as exc:
        print(f"UNAVAILABLE: {exc}")
        return 1

    if not found:
        print("No devices. The relay has nothing to receive SMS with, so the")
        print("3-D Secure code cannot be read and a human must type it.")
        return 1

    live = 0
    for device in found:
        online = relayion.is_online(device)
        state = "ONLINE" if online else "OFFLINE"
        live += 1 if online else 0
        print(f"  {device.get('name') or device.get('id') or '?':24s} {state}")
        if device.get("lastSeenAt"):
            print(f"      last seen: {device['lastSeenAt']}")
        if device.get("isPaired") is False:
            print("      NOT PAIRED — finish setup in the Relayion app")

        # THE NUMBERS ARE THE POINT OF THIS COMMAND. "A device is online" is
        # not the question; "is the card's registered number in the relay" is,
        # and that is answered by this list and nothing else.
        for sim in device.get("sims") or []:
            label = f" ({sim['label']})" if sim.get("label") else ""
            default = " [default]" if sim.get("isDefault") else ""
            print(f"      SIM {sim.get('slotIndex')}: "
                  f"{sim.get('phoneNumber') or 'no number reported'}"
                  f"{label}{default}")

    print()
    print("*** THE BANK TEXTS THE NUMBER REGISTERED ON THE CARD. ***")
    print("If that number is not a SIM listed above, the code will never")
    print("reach this relay, however healthy the device looks.")
    if not live:
        # A NON-ZERO EXIT MATTERS HERE. This command exists to be run before a
        # payment, and "a device exists but none is reachable" is a failure of
        # exactly the thing being checked — reporting success would make the
        # check worse than useless.
        print()
        print(f"NO DEVICE IS ONLINE ({len(found)} known). The relay cannot")
        print("receive an SMS right now, so a 3-D Secure code will have to be")
        print("typed by hand. Wake the phone, confirm it has signal, and")
        print("check the Relayion app is running and allowed to run in the")
        print("background.")
        return 1
    return 0


def _print_messages(messages: List, digits: int) -> None:
    for message in messages:
        when = (message.received_at.isoformat() if message.received_at
                else "no timestamp")
        code = extract.find_code(message.body, digits)
        print(f"  {when}  from {message.sender or '?':16s} "
              f"code={_mask(code) if code else 'NOT FOUND'}  "
              f"({len(message.body)} chars, id {message.id})")


def cmd_watch(args) -> int:
    """Tail the inbox. Bodies are never printed — a body IS the code."""
    since = datetime.now(timezone.utc) - timedelta(minutes=args.minutes)
    print()
    print(f"Inbound SMS since {since.isoformat()} "
          f"(bodies hidden, codes masked)")
    print("-" * 68)
    try:
        messages = relayion.list_inbound(since=since, sender=args.sender)
    except OtpNotConfigured as exc:
        print(f"NOT CONFIGURED: {exc}")
        return 1
    except OtpRelayError as exc:
        print(f"UNAVAILABLE: {exc}")
        return 1

    if not messages:
        print("  nothing")
        print()
        print("If a code WAS texted in this window, the SIM is not in the")
        print("relay — run `devices`.")
        return 1

    _print_messages(messages, args.digits)
    print()
    senders = relayion.senders_of(messages)
    if senders:
        print("Senders seen: " + ", ".join(senders))
        print("Put the bank's one in `otp_senders` in the processor config to")
        print("stop an unrelated SMS being considered at all.")
    return 0


def cmd_wait(args) -> int:
    """Block for one fresh code, exactly as the payment path waits."""
    inbox = OtpInbox(digits=args.digits, senders=[args.sender]
                     if args.sender else ())
    inbox.arm()
    print()
    print(f"Armed. Waiting up to {args.timeout}s for a {args.digits}-digit "
          "code.")
    print("Only a message received from NOW counts — trigger the SMS after")
    print(f"this line appears ({inbox.grace_s}s of clock grace applies).")
    print("-" * 68)

    import time

    deadline = time.monotonic() + args.timeout
    while True:
        code = inbox.poll()
        if code:
            print(f"  GOT IT: {_mask(code)}")
            print()
            print("  Masked on purpose. The payment path types it without")
            print("  printing it anywhere.")
            return 0
        if time.monotonic() >= deadline:
            errors = f" ({inbox.errors} relay errors)" if inbox.errors else ""
            print(f"  TIMED OUT after {args.timeout}s{errors}")
            return 1
        time.sleep(1)


def cmd_parse(args) -> int:
    """Run the extractor over a body, offline. No network, no key needed."""
    body = args.body or sys.stdin.read()
    code = extract.find_code(body, args.digits)
    print()
    if code:
        print(f"  code: {_mask(code)}")
        print(f"  (full value withheld; {args.digits}-digit run accepted)")
        return 0
    print("  NO CODE FOUND.")
    print()
    print("  The extractor refuses rather than guesses — see the module")
    print("  docstring. If this body really does carry a code, the fix is")
    print("  usually `otp_digits` or `otp_code_words` in the processor")
    print("  config, not a change to extract.py.")
    return 1


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.otp_relay",
        description="Read one-time codes from the Relayion SMS relay.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("devices", help="what the relay can see (RUN THIS FIRST)")

    watch = sub.add_parser("watch", help="recent inbound SMS, codes masked")
    watch.add_argument("--minutes", type=int, default=30)
    watch.add_argument("--sender", default="")
    watch.add_argument("--digits", type=int, default=6)

    wait = sub.add_parser("wait", help="block for one fresh code")
    wait.add_argument("--timeout", type=int, default=180)
    wait.add_argument("--sender", default="")
    wait.add_argument("--digits", type=int, default=6)

    parse = sub.add_parser("parse", help="test the extractor on an SMS body")
    parse.add_argument("body", nargs="?", default="")
    parse.add_argument("--digits", type=int, default=6)

    args = parser.parse_args(argv)
    return {"devices": cmd_devices, "watch": cmd_watch,
            "wait": cmd_wait, "parse": cmd_parse}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
