# Passport Document Storage — Findings & Plan

**Status:** ✅ IMPLEMENTED (`src/waitlist/documents.py`, 37 tests).
**Blocks:** `AE-ITA` waitlist (its "Your Details" step uploads a passport bio
page instead of typing fields).

---

## The question

Italy's portal does not ask for typed applicant details. It asks for the
**passport bio page**, then OCRs the details out of it. So the bot needs a file
per client. Where should that file live?

## The answer, up front

For 10–50 clients and one operator: **local filesystem, outside the git repo,
with full-disk encryption and a hard deletion rule.** No S3, no application-level
encryption — not yet.

The single highest-value control is **not encryption, it is DELETION.** A
regulator cares far more that the scan was deleted once the registration
succeeded than that it was AES-encrypted while held. Deletion is also the
cheapest thing to build here, because the journal already tells us exactly when
a document stops being needed.

## Why the risk changed

Today `config/registrants/*.json` holds a passport *number* and DOB. A bio-page
scan adds the photograph, the MRZ, the signature and the issuing data — enough
for full identity takeover, and arguably biometric data once a face is processed
for identification. Same folder, materially larger blast radius.

---

## Checked already

- [x] **Repo is NOT OneDrive-synced.** `Documents` resolves to
      `C:\Users\Universal\Documents`, not the OneDrive path — so the existing
      registrant JSONs are not being replicated to Microsoft's cloud. This was
      worth checking: Windows 11 frequently redirects `Documents` into OneDrive
      by default, which would have silently synced client PII.
- [x] **No images have ever been committed** (`git log --diff-filter=A` over all
      branches finds no `.png/.jpg/.pdf`).
- [x] **`screenshots/` is gitignored** — but see the screenshot problem below.

## Verify before implementing

- [ ] **BitLocker on the C: volume.** Needs an elevated shell:
      `manage-bde -status C:`. Many Windows 11 Pro installs have BitLocker
      available but not switched on. This is the control doing the real work in
      the recommended setup, so it matters that it is actually enabled.
- [ ] **EC2 root/EBS volume encrypted.** EBS is **not** encrypted by default and
      cannot be enabled in place — an unencrypted volume must be snapshotted,
      copied with encryption, and reattached. Also switch on *EBS encryption by
      default* for the region so it never recurs.

---

## Plan

### 1. ✅ A `documents` module — one owner of document paths

```
src/waitlist/documents.py     path_for(registrant_id, kind) -> Path
                              store(...) / delete_for(...) / purge_older_than(...)
```

Everything else goes through it. This is the seam that makes every later tier a
contained change instead of a refactor — the same discipline `registrant.py` and
`journal.py` already follow. The thing to avoid is `open(path)` calls scattered
about with paths built inline from client names.

**Layout** — outside the repo, one directory per client, fixed internal filenames:

```
<documents_root>/<registrant_id>/passport_bio.<ext>
```

Per-client directories make erasure one `rmtree`. Fixed internal names mean a
client's real name never lands in a filename that could surface in a log, a
stack trace or a Telegram error.

**Default root** — outside the git tree on both platforms, configurable:

```ini
[waitlist]
; Where client documents live. MUST be outside the repo: config/registrants/ is
; inside the git working tree and its .gitignore rules cover *.json only, so an
; image dropped there would be committed.
documents_root =
; blank -> %LOCALAPPDATA%\vfs-bot\documents on Windows
;          /var/lib/vfs-bot/documents on Linux
```

### 2. ✅ Validate on the way in

- Extension allowlist (`.png`, `.jpg`, `.jpeg`, `.pdf`) **and magic-byte check**
  — extension alone is not a content check
- Enforce VFS's 2MB cap before Playwright sees the file, so a bad file fails
  with our error rather than a portal rejection
- Reject absolute/traversing paths from the client JSON

### 3. ✅ Delete on success — wired into the journal

The journal already records the moment a document stops being needed. When a row
flips to `success`, delete that client's document. Small change, no new
subsystem.

- [ ] `journal.update_status()` → on `success`, `documents.delete_for(client)`
- [ ] Never delete on `pending` or `unknown` — those may need a retry, and a
      human may still be reconciling them

### 4. ✅ Backstop retention sweep

A crashed run must not leave a passport scan on disk forever.

```
python -m src.waitlist purge --older-than 30d
```

Run it at the start of every invocation. This IS the retention policy, and it is
what makes "we delete after use" a true statement rather than an intention.

### 5. ✅ Deletion is `os.remove()` — deliberately

**Do not write a multi-pass overwrite.** NIST SP 800-88r2 explicitly does not
recommend overwriting on SSDs: wear-levelling, over-provisioning and remapped
blocks mean the passes never reach all physical cells, so "very little
confidentiality protection is achieved". NIST's answer for SSDs and virtual
storage is **cryptographic erase** — which full-disk encryption already provides,
and which EBS does natively on volume deletion.

`os.remove()` + FDE is the correct and honest implementation. A `shred` loop
would be cargo cult that also wears the disk.

### 6. ✅ Git backstop

`.gitignore` is a weak control: it only stops *untracked* files, and does nothing
once something has been committed once.

- [ ] Documents live outside the repo (then git cannot see them at all)
- [ ] Plus a **pre-commit hook** rejecting staged `*.png|jpg|jpeg|pdf` under the
      repo. Advisory and bypassable with `--no-verify`, which is the right weight
      for a single operator.
- [ ] Belt and braces: add those extensions to `.gitignore` too

### 7. Two things specific to this codebase

- [ ] **Screenshots capture the passport.** `redaction.py`'s own docstring
      already admits it "does not touch screenshots, which can obviously show a
      filled-in form". With a bio page on screen, a debug screenshot now contains
      the document itself. Suppress screenshots on the upload step, and treat
      `screenshots/` as PII-bearing with the same retention rule.
- [ ] **Register document paths with `redaction`** — a path containing a client
      id in a stack trace would otherwise reach Telegram.

### 8. ✅ Permissions — and honest about the platform

- `os.chmod(0o600)` on files, `0o700` on directories. Real on EC2.
- **On Windows `os.chmod` is effectively a no-op for ACLs** — POSIX bits do not
  map. Equivalence there needs `icacls`. A per-user directory under
  `%LOCALAPPDATA%` is already outside other non-admin users' reach, which is
  adequate here. Say so in a comment rather than calling `chmod` and assuming.

### 9. Paperwork — cheapest compliance win available

- [ ] A one-page consent + retention note per client: what is held, why, for how
      long, and how to withdraw
- [ ] A README paragraph stating the same

GDPR requires the retention period to be **justified and documented**, not to hit
a fixed number. The justification here is short and strong: *needed only to
complete the upload; deleted on confirmed submission, and in all cases within N
days.* UAE PDPL (relevant — the clients are Dubai-based) makes consent the
default legal basis and requires telling people how to withdraw it.

---

## Why not encryption at rest (yet)

Full-disk encryption protects against **physical theft and leaked snapshots**.
Application-level encryption would protect against a compromised operator machine
— *except* that an unattended bot must be able to decrypt without a human, so the
key lives on the same machine, so an attacker with code execution gets both.

That is real operational complexity (lose the key, lose the data) for a threat it
does not actually stop. Classic security theatre. FDE is the control that matches
the realistic threat model.

## Why not S3 (yet)

S3 is right when a second operator needs access, when an audit trail of who
viewed what is required, when clients upload documents themselves, or when
documents are needed on a different machine. **None of those apply.** Adding S3
now creates a *second* persistent copy, a credential to leak and a bucket to
misconfigure, while removing nothing.

The one genuinely attractive S3 feature is **lifecycle expiration** — retention
as code rather than as a cron job we have to remember. That alone is not worth
the move today; step 4 covers it.

---

## Upgrade path (preserved by step 1)

Because every read/write goes through `documents.py` with an id → path mapping,
each tier is a swap behind that seam:

**Tier 2 — second operator, or an audit requirement**
S3 + SSE-KMS with a customer-managed key + Bucket Keys. The KMS key policy
becomes a second, independent authorization layer: `s3:GetObject` alone is not
enough without `kms:Decrypt`. Lifecycle rules replace the purge sweep.
*Trap:* **do not enable versioning.** `DeleteObject` on a versioned bucket writes
a delete marker and keeps the prior version — an erasure that does not erase.
Same reason: no Object Lock. WORM and right-to-erasure are directly opposed.

**Tier 3 — untrusted host / stronger threat model**
Envelope encryption: AES-256-GCM, a per-file DEK wrapped by a KEK in AWS KMS.
Decrypt to a buffer and hand the buffer to Playwright — `set_input_files()`
accepts `{"name", "mimeType", "buffer"}`, so plaintext never touches disk. This
also gives **crypto-erase**: destroy the DEK and the document is gone regardless
of backups, a stronger guarantee than `os.remove()` and NIST's preferred method.

---

## Sources

[OWASP File Upload](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html) ·
[OWASP Cryptographic Storage](https://cheatsheetseries.owasp.org/cheatsheets/Cryptographic_Storage_Cheat_Sheet.html) ·
[NIST SP 800-88r2](https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-88r2.pdf) ·
[AWS S3 security best practices](https://docs.aws.amazon.com/AmazonS3/latest/userguide/security-best-practices.html) ·
[AWS encryption best practices for S3](https://docs.aws.amazon.com/prescriptive-guidance/latest/encryption-best-practices/s3.html) ·
[AWS EC2 data protection](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/data-protection.html) ·
[ICO storage limitation](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/data-protection-principles/a-guide-to-the-data-protection-principles/storage-limitation/) ·
[UAE PDPL overview](https://securiti.ai/uae-personal-data-protection-law/)
