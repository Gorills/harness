# ADR-0069: Project hub and an independent human-only encrypted vault

- **Status:** Accepted for implementation; real installation acceptance is separate
- **Date:** 2026-09-23
- **Amends:** ADR-0019, ADR-0040; the original specification remains unchanged

## Context

The operator needs a project home containing tasks, personal notes, server inventory,
passwords and SSH material. The operator explicitly requires that personal information never
become Harness agent context, and selected a single local user with backups in an existing
operator-chosen directory (external disk or externally synchronized folder).

The existing shared SQLite/MCP/search model is unsuitable for these private records. Merely
adding a `private` flag, an excluded table, or an ignore pattern would not create the requested
boundary. The existing Harness backup format remains responsible for Harness business state.

## Decision

1. `/` becomes a project-card hub, retaining global Task search/history. Sidebar Project links
   open `/projects/{id}/`; Tasks keep their existing domain model, URLs, revision CAS and review
   workflow. The project screen offers Tasks and Notes/access navigation. Project settings
   remain on their established route. `/vault/all/` works without any registered Project.
2. `python -m harness.vault` is a **separate subprocess**, started lazily by a launch-only bridge
   in the dashboard. The bridge passes a storage directory and the dashboard origin and receives
   only the child HTTP origin on stdout. It never receives records, passwords or unlock tokens.
   The private modules have no imports of Harness storage/registry/retrieval/task/MCP services.
   No vault method is added to IPC, MCP, Search, Knowledge, status, SSE, or core SQLite.
3. The process owns an independent `projects.kdbx` using the maintained, pinned PyKeePass library:
   KDBX4, AES-256 and Argon2 (64 MiB, 14 iterations, two lanes in the selected library template).
   All fields, titles, project-name snapshots and backup settings are encrypted together. A
   master password of at least 12 characters is required for creation and rotation. Keys and
   decrypted records exist only in the vault process and its browser origin while unlocked;
   no plaintext database, temporary export or cookie is used. On explicit operator opt-in,
   the master password may persist only in the Linux desktop Secret Service. No file-based
   keyring fallback is permitted. The private browser origin uses sessionStorage for its
   short-lived bearer and an explicit-lock flag, never for passwords or record content.
4. The canonical store is the sibling `harness-vault` directory under the selected XDG state
   root, not `harness.db` or a registered repository. Isolated development uses its isolated
   XDG root. Explicit test databases use `harness-vault` beside that database. Directories/files
   are current-user-only (0700/0600); symlink paths and unknown permissive directories fail closed.
   A process-lifetime flock serializes owners. Project removal never deletes private records.
5. The child binds an ephemeral loopback port and enforces exact Host, exact Origin, JSON
   content type and a separate random in-memory bearer for every private operation. There is
   no CORS allowance and the Harness MCP bearer grants no vault access. Static UI/setup/status
   are public to the local user. Unlock rotates the one active session. A 15-minute idle expiry,
   explicit lock or child exit releases decrypted state; browser lock clears its forms/list.
   Navigation clears rendered private data but preserves the short-lived session token.
   A normal child exit preserves the quick-entry preference; explicit/idle lock pauses automatic
   entry durably until an explicit unlock. A local sessionStorage flag also prevents a fast
   reload racing a pending lock request from automatically opening the vault.
   Only the exact dashboard origin may embed the child. The parent permits only that child
   frame origin. No postMessage exchange or parent DOM access exists; parent realtime is
   disabled on the vault wrapper so it cannot replace a private draft. Other dashboard pages
   keep their existing same-origin CSP and progressive forms. The vault requires JavaScript.
6. Saving requires a full encrypted snapshot in the selected folder's private
   `harness-vault-backups` subdirectory **before** atomic live-file replacement. Each copy gets
   a unique timestamped name, 0600 permissions, file/directory fsync and readback comparison.
   Existing snapshots are never overwritten or automatically pruned. Missing/full/unwritable
   backup destinations reject the write. Failed in-memory edits lock the store; the last good
   live version remains authoritative. Encrypted-file SHA-256 revisions prevent stale browser
   writes and detect out-of-process file replacement.
7. Restore validates/decrypts the selected snapshot before mutation and preserves the previous
   live bytes as a separate backup. Recovery also works when the live file is corrupt or the
   Harness registry was lost. Records retain their project names and can be viewed across all
   Projects or reassigned to a current Project. Recovery uses the backup's master password;
   there is no password reset/backdoor. Rotation produces a new-password snapshot but historical
   copies retain their old passwords. Copies can be opened in standard KDBX4 clients independent
   of Harness; arbitrary foreign KeePass database import is not the v1 restoration contract.
8. Requests/files/fields/counts and imported KDF parameters are bounded. Imports run only in the
   vault process with a 512 MiB address-space limit and disabled core dumps. The sequential HTTP
   owner serializes mutation and applies socket timeouts; failed unlocks are rate-limited. No
   access/request-body/exception-payload logging is enabled. UI text is DOM-bound; passwords are
   masked and SSH keys are disclosed explicitly. Clipboard copying is explicit and the OS
   clipboard remains outside the encrypted store.

### Desktop keychain and usability amendment (operator feedback, 2026-09-23)

The operator explicitly chose automatic entry through the OS keychain after restart. The vault
uses pinned SecretStorage against the default Linux Secret Service collection, with attributes
scoped to this product and a hash of the exact vault root. It never enumerates unrelated secrets.
A helper subprocess bounds desktop prompts to 45 seconds, passes secrets only through stdin/stdout,
and suppresses provider diagnostics. Only a successful write/readback enables the non-secret
0700-root/0600 preference marker. Missing/locked/cancelled providers retain manual entry; no
plaintext fallback is allowed. Existing private data remains readable if remembering fails.

Authenticated settings can enable or delete the stored credential. Password rotation/recovery
first remove it, so stale stored passwords cannot silently reopen a newly restored database.
The operator reenables quick entry afterward. If deletion fails, automatic entry remains paused
and the operation reports failure. Quick entry requires the same strict private-origin POST
contract as manual entry and never accepts the Harness model bearer as vault authorization.
The keychain credential is not part of KDBX backups and does not move to another machine.

The interface uses a list/detail layout, masked read-only cards with explicit copy/reveal,
a separate editor and a native modal for entry/backup settings. Parent navigation remains
isolated from private content. Real desktop-provider acceptance is separate from deterministic
OS-boundary doubles, subprocess HTTP checks and browser navigation/session tests.

### Explicit no-password mode amendment (operator request, 2026-09-23)

The operator explicitly requested a mode with no password and no desktop-keychain requirement,
accepting file-level exposure comparable to personal text documents. This supersedes mandatory
master-password protection and idle locking **only for that selected mode**; separate ownership,
origin enforcement, negative model disclosure, revision checks and backup-before-write remain.

The mode uses the same bounded KDBX4 format with an empty password. There is no hidden sidecar
key or OS-keychain dependency. The encoding is not a confidentiality promise: anyone with the
file can open it with an empty password. Empty-password backups are independently portable.
The KDBX itself determines the mode, avoiding inconsistent two-file mode/key migrations. Startup
attempts empty-password opening once; protected/damaged files retain manual entry/recovery.
The public private-origin open action verifies the actual file with the empty password; it
cannot turn a protected file into an open file. Record operations still require the separate
browser bearer, strict Host/Origin and JSON contract. No new MCP method or core field is added.

New UI setup defaults to no password, with an explicit protected option. Switching an existing
vault requires its current unlocked session and revision; a pre-transition snapshot and the new
full snapshot precede atomic replacement. Failure leaves the last good live file. Existing
backups keep their original passwords or lack of password. Restoration accepts empty-password
copies without credentials. Returning to password mode uses the existing password-setting path;
it does not retroactively protect earlier open snapshots. Enabling protection rotates the bearer,
invalidating tokens previously obtainable without a password.

No-password entry disables client/server idle locks and hides lock/keychain controls. An old
OS-keychain enrollment is paused locally and is never consulted by automatic open; OS availability
cannot block this conversion. Its old credential is not silently deleted from an unavailable
provider and can be removed independently. Protected-mode keychain behavior remains opt-in.

Evidence covers portable empty-password recovery, protected-file bypass rejection, missing backup
storage, stale/unauthorized mode transitions, corruption, return to password mode, no keychain
calls, no idle expiry, real subprocess restart/Origin checks, production JS startup and MCP
negative disclosure in both modes. Browser acceptance uses synthetic records only.

## Threat and recovery boundary

Password-protected mode protects data at rest; both modes preserve accidental model-disclosure
prevention, cross-origin browser read/mutation checks,
stale edits, and loss/corruption when a usable external snapshot exists. It does **not** isolate
against a compromised OS account, an agent with unrestricted same-user shell/browser/debugger
access, an unlocked browser extension, swap/hibernation or a compromised installed application.
Python memory cannot promise cryptographic zeroization. Use OS disk encryption and a separate
OS identity/device if that stronger boundary is required. CSP/ports/prompts are not an OS sandbox.

The selected directory must really be external or synchronized; saving a file cannot prove
remote synchronization. A backup on the failed disk does not survive that disk's loss. The
operator manages retention/capacity and keeps the relevant master passwords separately.

Private snapshots cover all private records, including notes and project-name associations.
They do not include Tasks/Knowledge or repository contents. `harness backup`/`harness restore`
continue covering Harness SQLite state under ADR-0036; repositories use their own Git/backups.
No combined plaintext archive or vault-to-core backup dependency is introduced.

## Alternatives and rollback

- Rejected private tables in core SQLite: unacceptable shared retrieval/backup ownership.
- Rejected a custom encryption format: KDBX4 has independent recovery clients and reviewed primitives.
- Rejected shared browser origin/token: an ordinary dashboard script would inherit secret access.
- Deferred team/multi-device synchronization and direct SSH/S3 backup transports: not requested.

There is no core schema migration. Rollback removes the UI/child launcher without modifying the
KDBX file or snapshots. Keep a copy before future format changes. Do not migrate private records
into core SQLite, even during rollback. Existing Harness installation/uninstallation does not
gain ownership of the sibling private directory.

## Required evidence

Domain round trips cover every field, cross-machine restore, wrong password/corruption rejection,
corrupt-live recovery, password rotation, stale revisions, absent backup storage, disk failure,
private permissions and symlink/collision refusal. Real subprocess HTTP tests cover Host/Origin,
missing/wrong/stale bearer, process restart and embedding headers. A real MCP stdio test checks
that private canaries have no search/context/SQLite exposure while the vault is unlocked.
Dashboard regression tests preserve Task behavior and verify new Project navigation. Browser
acceptance uses only synthetic records; real user unlock/password setup is operator-owned.
