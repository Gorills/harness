# ADR-0080: Global shared Servers inventory and projectless agent access

- Status: Accepted for implementation; installed-host activation remains separate
- Date: 2026-10-05
- Amends: ADR-0037, ADR-0045 and ADR-0069 (shared non-secret infrastructure metadata only)

## Context

Server research and maintenance must retain context across Projects and projectless Codex chats.
The project MCP endpoint admits only an explicit registered Workspace. Relaxing that check would
mix unrelated project permissions and would turn a missing header into global access. The private
vault contains human-only records and cannot become a model-visible server catalogue.

## Decision

`harnessd` owns a local OS-user Servers registry independent of Git/Workspace, stored in core
SQLite. Stable explicit server IDs and revision CAS protect create/update/archive/result writes;
creation uses expected revision 0. Optional many-to-many Project associations are references and
confer no access to project, Hidden SCM or vault state. Server events are append-only and paged.
Archiving retains metadata and history. Imported facts have source provenance and unknown live
freshness. A connection/service record is not automatically a verified physical host.

Schema v26 adds `servers`, `server_projects` and `server_events` without changing existing data.
Foreign keys preserve referential integrity; Project removal only removes links. Migrations run
through the existing transactional migration owner. Older runtimes refuse v26. Rollback uses an
existing verified Harness backup, not table deletion after accepted writes.

A separate official-SDK MCP catalogue at authenticated `/servers/mcp` provides `server_list`,
`server_get`, `server_save`, `server_archive`, `server_record`. It admits no Workspace root and
exposes only bounded shared metadata for the local user. `/mcp` keeps its five project tools and
fails closed on absent/unknown roots. Both endpoints retain bearer authentication, loopback Host
checks, body limits and daemon IPC ownership. Tool output and errors are bounded; unknown fields,
credential fields, private-key blocks and common credential assignments are rejected without
reflecting rejected payloads. This reduces accidental disclosure; arbitrary prose is not a reliable
secret classifier. Operators/agents must keep all secret values out of the shared registry.
Text must also be UTF-8 encodable: lone Unicode surrogates return bounded domain errors before
persistence and cannot escape the IPC worker as unexpected daemon failures.

Server tools never execute SSH or arbitrary remote commands. Local agents use separately available
execution tools and existing authorization. The human-only vault process remains unchanged and
has no server IPC/MCP bridge. SSH metadata contains alias/user/port/key reference, never key bytes.

The Russian `/servers/` dashboard uses the existing navigation, forms and progressive fetch
updates for create/edit/archive/result writes. Search/environment/archive filters retain their
URL context; validation/conflict responses preserve drafts. History pages are bounded.
An explicit refresh after a conflict preserves edited controls while adopting the latest hidden
CAS revision and stored values for untouched controls. Failed native submissions retain their
editable draft markers too. Initial event-kind selection has an explicit HTML selected default
so an untouched detail page remains clean for ordinary navigation.
Failed HTML refresh reports that it could not update, retains the current forms and drafts, and
keeps the refresh control available for retry. SSE connection events do not erase that error.
A retry after an uncertain POST remains a read and continues to ask the operator to check whether
the write was saved; it never replays the mutation automatically.

Index import previews an allowlist of explicit connection/domain metadata. Explicit `--server`
reads one source card and extracts recognized service tokens and operational absolute paths from
allowlisted sections, skipping sensitive lines. It never embeds raw card text or imports secrets,
env, dumps, commands or private backup procedures. Unknown/missing sections remain gaps. Stable
source identities deduplicate reruns without guessing physical-host merges. Reruns preserve
existing records; selected enrichment of an existing record requires explicit revision and merges
references without replacing operator edits. Original sources remain unchanged.

Server guidance preserves the operator's deployment scheme on every server. Application image tags
remain exactly as published by CI; the update path is `docker compose pull` then
`docker compose up -d`. Replacing a tag with a digest, substituting a build, or changing the
CI/update/automation/rollback scheme requires prior explicit operator approval for that deviation.
Read-only digest verification is allowed. Both global `server-operations` and project
`deployment-operations` carry this policy; it is agent guidance, not remote execution or a new
server metadata permission.

The canonical `server-operations` skill is global-only by default: no detected project-stack
selection. Explicit installed-runtime `harness servers setup --host codex` delivers an owned
`harness-servers` entry to user Codex config and one user skill at `$HOME/.agents/skills`. It
preserves unrelated config bytes, refuses unknown/edited collisions and uses private files.
Removal is the matching explicit `setup --remove` lifecycle. Global delivery is optional, so
its HTTP entry is not required for unrelated Codex startup. After machine restart, run
`harness servers start` if the daemon is absent. Fully restart Codex and open a fresh chat after
configuration changes. A static HTTP entry does not itself start a daemon or prove host discovery.
Setup and removal acquire persistent OS file locks at `$CODEX_HOME/.harness-servers.lock` and
`$HOME/.agents/skills/.harness-servers.lock` before reading snapshots, and hold both through success
or rollback. Lexically normalized lock paths have one consistent acquisition order. This covers
cooperating processes with shared skills but different Codex homes, or shared config/marker but
different supplied homes. The lock inode is never unlinked on release; unknown, linked or changed
lock files are refused. A bounded lock wait fails without changing config, skill or marker.
Lock files remain as coordination artifacts. POSIX uses `flock`; Windows uses an exclusive byte
lock with the existing private-path ACL checks. This is adapter-owned filesystem coordination,
not daemon business lifecycle or a new dependency.

Inside those locks, setup and removal snapshot the config, skill and ownership marker. File
replacements revalidate expected bytes and relevant skill-directory ownership after preparing,
flushing and fsyncing the temporary file, immediately before replacement. Deletions revalidate
immediately before unlink/rmdir. Rollback applies the same guards to bytes/deletions produced by
this invocation. Detected operator edits and changed directory ownership are preserved. A complete
rollback reports restored content; any detected concurrent change or failed restoration returns a
bounded manual reconciliation error. An arbitrary external writer that ignores these locks can
still change a target between the final check and replacement/deletion: this is a bounded recheck,
not an atomic filesystem compare-and-swap. The multi-file lifecycle is not crash-atomic.

## Alternatives and verification boundary

Rejected rootless project endpoint, private vault import and automatic remote execution. Deferred
physical-host autodetection and monitoring installation. Import favors safe partial metadata over
raw free-form migration. Official Codex docs checked 2026-10-05 specify user `config.toml`, static
HTTP headers and `$HOME/.agents/skills`; proprietary-host discovery remains real-host acceptance.
Synthetic tests cover migration preservation, CAS rollback, immutable/paged history, response
budgets, secret canaries, import ownership/idempotency, global file ownership, dashboard persistence
and origin checks, rootless HTTP MCP and unchanged root-required project contracts. No actual
server mutation, global user installation or host refresh is implied by these checks.


## Amendment: sites and preparation profiles (schema v27)

Servers metadata now contains bounded nested `sites` with stable explicit site IDs and a separate
`platform_profiles` collection with stable profile IDs. Sites represent documented/actual deployments;
profiles represent reusable preparation policy per environment/stack and are not executable authority.
A site's optional profile reference must exist in that same card. Empty versions/patterns and null
checked_at retain uncertainty. Dashboard exposes distinct Russian forms with existing CAS/refresh
semantics; per-site/profile form identity prevents one application's draft from replacing another.
Search includes site names, purposes and domains. The MCP catalogue remains exactly five tools and
publishes the complete nested contract in server_save's description.

V27 adds two daemon-owned import-ID ledgers without rewriting existing JSON/cards/events. The schema
barrier refuses older writers that would erase unknown nested metadata during full replacement.
Additive selected-card enrichment applies CAS to the whole card and records imported IDs transactionally;
reruns cannot replace operator changes or recreate operator-deleted imported IDs. Reviewed nonsecret
per-card manifests pin index/card hashes and source line anchors, retain omissions as unverified facts,
and never infer domain→application relations from free-form text. Imported profiles/facts stay unchecked.

Schema v28 adds a compatibility barrier without rewriting cards/events, refusing older full-card
writers. Durable metadata allows 256 sites/32 profiles and 512 KiB ASCII JSON, with base32 KiB and
individual child6 KiB. Disclosure is independent: server_get returns bounded site/profile pages or
exact selected records, plus base metadata/history. sites_page/profiles_page expose filtered total,
full_total, offset/truncated/next_offset. Read expected_revision pins a traversal; mismatch requires
starting over. Byte trimming retains a requested record/event and a progressing cursor. Ordinary
server_list searches all stored site names/domains, regardless of the current child page.

Existing public server_save accepts exactly one base metadata patch or child upsert/deletion. Base
patches preserve omitted fields and reject nested collections. Child merges run within BEGIN
IMMEDIATE after server revision CAS, preserving neighbors, Project links and import ledgers.
Creation may supply initial children within the request budget. Write responses are paged; clients
fetch the exact changed child. Dashboard pages/query select bounded editors and preserves the
existing browser beforeunload warning for dirty POST forms. Confirmed child deletion discards only
that child's draft; conflicts/uncertain writes and unrelated drafts retain their edit state.
Mutation responses preserve each unsent dirty form's baseline CAS, including responses from
neighboring editors and conflicts. Only the confirmed submitted form may advance its own CAS;
an explicit read-latest/refresh rebases other drafts for reconciliation. Dashboard-only pagination
clamps an empty nonzero child offset to the last valid bounded page while retaining other filters;
public MCP offset semantics remain unchanged. Filtered no-results differ from an empty inventory.

Domain response48 KiB, MCP wrapper52 KiB and public Servers HTTP/IPC64 KiB remain bounded; Projects
retain16 KiB. Administrative import/enrich-only local IPC supports1 MiB for atomic validated imports;
no model-facing tool exposes those operations. Receive framing bounds allocation to1 MiB, then
rejects ordinary decoded requests over their own smaller limit. Stored cards never enter event
history. Reviewed manifests may additionally pin bounded regular Markdown explicitly linked by the
selected source card inside its root. Symlinks, unlinked files, traversal/outside paths and arbitrary
source import are refused. Line anchors cite only selected/pinned sources; imported freshness stays
unknown. No new dependency, SSH execution or global remote provisioning is added.

## Amendment: server history scope

Agents classify the requested outcome and each action before execution/journaling. Project code,
SQL/GraphQL and business-data diagnosis, application DML/schema migrations and local tests belong
to the project Task even when inspected through SSH/Docker. Registry lookup provides connection
context; it does not justify copying those diagnostics into server events, facts or sites.
Explicit primary infrastructure audits record supported infrastructure findings and read-only/no-change
status. Actual OS/config/proxy/cert/access/directories/deployment-runtime/artifact/service/restart or
DB-engine maintenance records only the actual infrastructure changes, when and verification.
Mixed work states Code/Server portions up front, retains project details in the same project Task,
and records only the infrastructure portion in server history. No duplicate Task is created for
that portion, and projectless server history does not require a synthetic Workspace/Task.
Authorized platform preparation retains actual site/profile parameters and an infrastructure operation.
Metadata CRUD events remain registry revision events, not evidence of remote intervention.
Ambiguous goals require resolving scope before execution or append. This is skill/tool guidance,
not semantic text filtering: the five MCP tools and existing append-only event payloads are preserved.
No historical event is deleted/reclassified or moved automatically.

### Explicit operator history-clear exception

An explicit operator request may permanently clear all registry events of one stable server ID
through `harness servers clear-history ID --expected-revision N --confirm`. This is a local
operator CLI/IPC operation, never a sixth MCP tool or an implication of audit/lookup permissions.
The daemon validates confirmation, identity and positive revision, acquires BEGIN IMMEDIATE,
checks CAS, and deletes only that server's events. It bumps revision/updated_at without appending
a replacement event, preserving the complete card, sites/profiles, links, import ledgers and all
other servers. Missing/stale/invalid requests change nothing. No remote logs or project Tasks
are deleted, and no historical cleanup happens automatically.

Schema v28 remains unchanged. The daemon verifies the existing delete-protection trigger,
temporarily drops only that trigger, deletes selected events and restores its exact SQL inside
the same SQLite transaction. The writer lock serializes other writes; transactional DDL and
rollback preserve both events and the trigger on failure. No committed unguarded state exists.
Ordinary event updates/deletes stay prohibited before and after this operator exception.

The canonical global skill now requires selected site/profile context and retention of verified actual
preparation parameters, alongside the source's no-executor-artifacts rule. That rule excludes signatures,
unneeded README/backups/scripts and unsolicited notifications, while preserving legitimate backups,
service output and the normal operational audit trail. It never authorizes log deletion or concealment.

## Amendment: machine passport and categorized observations (schema v29)

Base metadata may contain a bounded `passport` for machine identity, OS/kernel, CPU model/logical
CPU count, total RAM/swap in integer MiB, documented disks/volumes and hosting. Unknown numeric
values stay null; swap 0 means absent. Text is bounded at256 UTF-8 bytes and storage at16 entries.
Passport source/checked_at apply to the whole block. Base patches omitting it preserve it; supplying
it replaces that block under the existing whole-server CAS. This does not introduce an extra write
operation or increase the base/request/response budgets. Transient load/free-space/service state
remains in `facts`, whose optional category has eight explicit topic values. Legacy facts without
category retain their exact stored shape.

The Dashboard shows a machine passport with a separate editor, followed by operational context and
observations grouped by source/date and topic. Unchecked evidence is explicitly collapsed, and
source text is disclosed once per snapshot. Known legacy prefixes only choose a presentation group;
they never populate passport fields, infer verification or rewrite stored prose. History remains
unchanged. Empty fields show missing information instead of guessed hardware.

V29 is a writer compatibility barrier: old validators/full-card writers reject newer databases
instead of losing unknown passport/category fields. Migration changes only the schema watermark,
preserving exact card/event bytes and revisions. Tests cover migration preservation/old-writer
refusal, CAS/validation rollback, child/history preservation, bounded disclosure, legacy grouping
without writes, and Dashboard form persistence/conflicts. Installed-runtime activation stays separate.
