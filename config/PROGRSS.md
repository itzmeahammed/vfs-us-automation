# Country progress

Checked against `config/` and `state/` on 2026-10-15.

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
| Netherlands  | AE-NLD | Done         | Pending          | In progress     | Waitlist confirmed 2026-10-14 (NEU128929309594, AED 100.42). Booking config built 2026-10-15, 3 dropdowns confirmed. No slots available — portal falls back to waitlist flow when no slots, so remaining booking pages untested. Same Angular app as Sweden/Norway. |
| Czech        | AE-CZE | Armed        | Pending          | Pending         | Waitlist config enabled; 2 dry runs only (Abu Dhabi and Dubai Tourism). Inbox config copied from NLD, not yet checked against real Czech mail. No booking config. |
| Germany      | AE-DEU | Done         | Pending          | Pending         | Confirmed 2026-10-14 (DEU73214173427). Abu Dhabi only: the Dubai rows in `routes/AE-DEU.json` have the real centre name but are still `"disabled": true`. |
| Greece       | AE-GRC | In progress  | In progress      | N/A             | Waitlist config ready, all steps verified up to Confirm click. 403 geo-blocked at submit (proxy IP). Login needs OTP (text). Booking config (invite only) is disabled. |
| Italy        | AE-ITA | Done         | Pending          | Pending         | Waitlist confirmed 2026-10-14 (ITD128931344738, AED 38.30). Typed fields, no passport upload, no mobile OTP in waitlist flow. |
| France       | AE-FRA | N/A          | N/A              | In progress     | No waitlist on this portal. Booking config built 2026-10-15. All 3 dropdowns + all 11 Your Details fields confirmed (incl. France-specific: France-Visas Registration Number, Date Of Birth). Blocked by no slots in Abu Dhabi — remaining pages same as Sweden/Norway. `pre_submit_js` in config to force past no-slots gate. |
| Sweden       | AE-SWE | In progress  | Pending          | Done            | Full booking walk completed 2026-10-15. All pages captured: dashboard -> appointment_details -> your_details -> details_summary -> select_slot -> services -> review_pay -> payment. Config production-ready. 11 dates available, 8 time slots. |
| Hungary      | AE-HUN | In progress  | Pending          | Done            | Full booking walk completed 2026-10-15. All pages captured end-to-end (force-enabled past no-slots calendar). 10 fields on Your Details (no DOB). 10s countdown. Config production-ready. Waitlist: no waitlist offered for Dubai combo. |
| Luxembourg   | AE-LUX | In progress  | Pending          | Pending         | Waitlist scaffold added 2026-10-14 (disabled, unverified). Route still commented out in `vfs_urls.ini`; centre names unchecked. |
| Malta        | AE-MT  | In progress  | Pending          | Pending         | Waitlist scaffold added 2026-10-14 (disabled, unverified). Route still commented out in `vfs_urls.ini`; centre names unchecked. |

Machine switches (`config.ini [switches]`, overridable in `config.local.ini`): `waitlist`, `invite_booking`, `live_booking` = off; `test_booking` = on.
