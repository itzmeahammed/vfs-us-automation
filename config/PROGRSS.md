# Country progress

Checked against `config/` and `state/` on 2026-10-14.

- **Waitlist reg**: register a client on the VFS waitlist (`config/waitlist/<ROUTE>.json`).
- **Waitlist booking**: book a client after VFS emails "Slots available" (`config/booking/<ROUTE>.json`, invite entry, plus `config/inbox/<ROUTE>.json`).
- **Full automation**: book and pay for an open slot unattended (`config/booking/<ROUTE>.json`, live entry).

Status values:

- **Done**: proven with a real run.
- **Armed**: the config is enabled, but no real run has finished yet.
- **In progress**: a config exists but is disabled or incomplete.
- **Pending**: nothing has been built yet.
- **N/A**: not how this country works.

| Country      | Route  | Waitlist reg | Waitlist booking | Full automation | Notes |
|--------------|--------|--------------|------------------|-----------------|-------|
| Norway       | AE-NOR | In progress  | N/A              | Done            | Live slots. Booking config armed 2026-09-28; payment reached the CyberSource gateway 2026-09-29. Waitlist scaffold added 2026-10-14 (disabled); live booking still takes priority. |
| Switzerland  | AE-CHE | Done         | In progress      | N/A             | Real registrations: SWDB79923880977, SWDB80350743950, SWDB82370725773. Booking config is mapped only up to /che/services and is disabled; the pages after it are blocked until an account is invited. |
| Netherlands  | AE-NLD | Armed        | Pending          | Pending         | Waitlist config enabled, but no registration (real or dry run) in the journal yet. Inbox matcher built from a real invitation. No booking config. |
| Czech        | AE-CZE | Armed        | Pending          | Pending         | Waitlist config enabled; 2 dry runs only (Abu Dhabi and Dubai Tourism). The `_comment_enabled` in the config is stale (it says OFF). `docs/Cze.txt` captures the Your Details countdown (3 s) and the Summary page with Continue. Inbox config copied from NLD, not yet checked against real Czech mail. No booking config. |
| Germany      | AE-DEU | Done         | Pending          | Pending         | Confirmed 2026-10-14 (DEU73214173427). Abu Dhabi only: the Dubai rows in `routes/AE-DEU.json` have the real centre name but are still `"disabled": true`. |
| Greece       | AE-GRC | In progress  | In progress      | N/A             | Waitlist scaffold added 2026-10-14 (disabled, unverified). Login needs OTP (text). Booking config (invite only) is disabled; only the dashboard step is grounded. Inbox matcher built from a real invitation (valid 36 h). Third combo label reads "Italy Visa Application Center ,Dubai". |
| Italy        | AE-ITA | In progress  | Pending          | Pending         | Waitlist pages mapped but disabled: blocked on the OTP sent to the mobile number before review-pay. Inbox matcher built from real mail. |
| Sweden       | AE-SWE | In progress  | Pending          | Pending         | Waitlist scaffold added 2026-10-14 (disabled, unverified). |
| Hungary      | AE-HUN | In progress  | Pending          | Pending         | Waitlist scaffold added 2026-10-14 (disabled, unverified). Centre names are bare "Abu Dhabi"/"Dubai", unchecked. |
| Luxembourg   | AE-LUX | In progress  | Pending          | Pending         | Waitlist scaffold added 2026-10-14 (disabled, unverified). Route still commented out in `vfs_urls.ini`; centre names unchecked. |
| Malta        | AE-MT  | In progress  | Pending          | Pending         | Waitlist scaffold added 2026-10-14 (disabled, unverified). Route still commented out in `vfs_urls.ini`; centre names unchecked. |
| France       | AE-FRA | N/A          | N/A              | Pending         | No waitlist on this portal. `config/waitlist/AE-FRA.json` is a leftover disabled scaffold. |

Machine switches (`config.ini [switches]`, overridable in `config.local.ini`): `waitlist`, `invite_booking`, `live_booking` = off; `test_booking` = on.
