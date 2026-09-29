# Archive

Nothing here is deleted — it is moved out of the repo root so the five
top-level documents stay findable. Everything is still in git history under its
old path; `git log --follow docs/archive/<file>` shows it.

The live documents are:

| File | What it answers |
|---|---|
| [README.md](../../README.md) | What is this, and what are the three subsystems |
| [ARCHITECTURE.md](../../ARCHITECTURE.md) | How it works end to end |
| [COMMANDS.md](../../COMMANDS.md) | What do I type |
| [RUNBOOK.md](../../RUNBOOK.md) | It broke / I am about to spend money |
| [TASKS.md](../../TASKS.md) | What is next |

## What each archived file was for

### Still useful — read on demand

| File | Read it when |
|---|---|
| [MEMORY.md](MEMORY.md) | Before re-deriving anything. Settled facts and decisions. |
| [GLOSSARY.md](GLOSSARY.md) | You need the four safety switches and how they combine. |
| [SYSTEM_GUIDE.md](SYSTEM_GUIDE.md) | You want a guided tour following one client end to end. |
| [PHASES.md](PHASES.md) | You want the waitlist-to-booking pipeline on one page. |
| [BOOKING_DESIGN.md](BOOKING_DESIGN.md) | You are changing the booking half and need to know why it is shaped this way. |
| [addClient_Steps.md](addClient_Steps.md) | You are registering a new client by hand. |
| [keepInMind.md](keepInMind.md) | You are operating the inbox watcher. |
| [EC2_COMMANDS.md](EC2_COMMANDS.md) | You are on the Linux/EC2 box. Superseded in part by COMMANDS.md. |
| [MANUAL_TEST_RUNBOOK.md](MANUAL_TEST_RUNBOOK.md) | Verifying a flow by hand. Largely folded into RUNBOOK.md. |
| [API_TUNNEL_SETUP.md](API_TUNNEL_SETUP.md) | Exposing the local API through ngrok. |

### Historical — task lists, mostly done

| File | Status |
|---|---|
| [TASKS_WAITLIST.md](WAITLIST_TASKS.md) / [WAITLIST_AUTOMATION_TASKS.md](WAITLIST_AUTOMATION_TASKS.md) | Mostly complete; open items migrated to TASKS.md. |
| [DOCUMENT_STORAGE_TASKS.md](DOCUMENT_STORAGE_TASKS.md) | Built; §7 and §9 open. |
| [OTP_RELAY_TASKS.md](OTP_RELAY_TASKS.md) | Not started. Blocks AE-ITA. |
| [SLOT_ANALYTICS_TASKS.md](SLOT_ANALYTICS_TASKS.md) | Partially done. |
| [API_IMPLEMENTATION_GUIDE.md](API_IMPLEMENTATION_GUIDE.md) / [API_REFERENCE.md](API_REFERENCE.md) | Written before the API existed; the code is now the truth. Generate the schema with `VFSAPI_ENABLE_DOCS=1` and read `/docs`. |
| [HANDOVER.md](HANDOVER.md) / [HANDOFF_NEXT_SESSION.md](HANDOFF_NEXT_SESSION.md) | Point-in-time session notes. Superseded by TASKS.md. |
