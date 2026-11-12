# ADR 0082: Separate machine tunnels and a privileged traffic guard

Date: 2026-11-09. Status: implemented core; native platform/provider acceptance remains required.

## Decision

`/tunnels/` is a global human workspace, separate from Servers. Its schema32 profiles,
operations, runtime observations and minute aggregates do not reference `servers`,
write server history or expose model execution tools. The dashboard owns requested
state using revisions and one pending machine operation. A root/SYSTEM service owns
private endpoint credentials, the last accepted activation intent, engine processes
and kernel policy. The existing private vault remains independent.

An inactive profile is ready only when every configured endpoint has acknowledged
preparation. A separate `setup_incomplete` observation retains partial/uncertain
server ownership without claiming readiness. It allows preparation retry while
blocking local deletion and endpoint changes until acknowledged remote removal;
fresh-SSH removal recovery remains available for incomplete profiles.

The restricted maintenance account has a root-owned shared home parent with mode
0711: sshd can traverse to the account's public authorized_keys without listing
other profile homes or writing the parent. Account identity is checked before
changing these directory modes. This is covered by the isolated real-useradd
fixture and an actual post-bootstrap maintenance request on the selected VPS.

Activation arms the OS guard and persists reboot intent before launching a TUN.
Linux uses an owned nft table and a dedicated unprivileged engine UID; root applications
receive no exemption. A synchronous systemd oneshot restores the guard before
`network-pre.target`, without loading an engine or contacting the Internet. A damaged
intent blocks traffic with no runtime UID exemption. Windows uses a dedicated WFP
sublayer with soft permits, persistent ALE/runtime filters and separate boot transport
filters. Filters do not clear another provider's block. Windows children join a
kill-on-close job while suspended. The native Windows adapter has not been executed
on Windows in this implementation environment.

Linux explicitly installs both IPv4 and IPv6 halves of the default TUN route.
The engine's early policy lookup suppresses a plain `/0`; without these `/1`
routes an existing VPN's main-table routes can capture both real and FakeIP
destinations. The owned guard then drops ordinary applications while the
engine-UID SOCKS probe still succeeds. Outbound sockets bind to the standalone
underlay to avoid recursion. The earlier decision to inherit an existing VPN
underlay was incorrect: its carrier runs as a different UID and its firewall can
block our own DNS and outbound traffic. Before initial activation, Linux rejects
VPN underlays and reachable Amnezia output drop/reject chains without changing
the user's current connection or guard intent. Unreferenced stale chains do not
block activation. The service never rewrites another application's policy.

The root service configures only its own systemd-resolved link after TUN startup;
the unprivileged engine cannot reliably perform this operation. Resolver errors
are fatal when resolved is active. Plain resolv.conf installations retain native
TUN DNS interception. On Linux, every successful SOCKS health result additionally
requires system DNS and verified HTTPS from the desktop owner's UID, without
proxy environment variables, returning the same exit IP. This makes routing,
resolver and guard failures reject the connected state instead of hiding behind
the engine UID exemption. Windows retains its separate native acceptance scope.

The service accepts authenticated, typed local operations, never shell text, raw
engine configurations or executable paths. Linux verifies peer UID in both directions.
Windows uses an owner-scoped named pipe, challenge authentication and verification
that the server process runs as SYSTEM. The installer copies an isolated interpreter
and a small owned package, protects its tree and verifies its complete expected file
set and hashes before executing it with `-I -S`. It never runs the checkout/venv as
the long-lived root service. Source checkout installation is refused independently
of environment variables.

## Transport scope and dated component admission

The admitted default is REALITY/VLESS Vision with Chrome uTLS and XUDP on TCP443,
plus Hysteria2/Salamander on UDP443. Hysteria verifies a unique reserved DNS name
against its exact SSH-delivered self-signed certificate; no `insecure` verification
mode is generated. TCP can also use Hysteria when the TCP443 path is unavailable.
UDP falls back from Hysteria to REALITY/XUDP. A healthy path stays selected; two
failed checks and a120second candidate cooldown limit oscillation. Switching paths
serves new flows and can break existing sessions; it is not seamless session migration.

The originally proposed NaiveProxy HTTPS front is optional and **disabled**. The
current upstream154.0.8037.49-5 native candidate lacks the HTTPS CONNECT pool fix for
CVE-2026-110161. Embedded Cronet in stock sing-box1.14.4 predates the Naive CA fix.
The optional HAProxy/ACME compilers are retained but no native Naive, Cronet, HAProxy,
Certbot or apt package is admitted by the shipped catalog. Accordingly the default
does not claim browser/Cronet camouflage, genuine public IP certificates, ACME renewal
or protection against every censorship system.

The product catalog admits only the deterministic source recipe in
`src/harness/tunnel_recipe/engine-source-build-recipe.json` (SHA256
`ce39e010371f9cad21e6fc19757fb3f1355316dac2c46aff719877205414983b`). It pins
sing-box source commit`c005f35300ce76427dc285481ec5695f5b179cd2`, Go1.26.10,
the full412module graph, dependency checksums, and six exact uTLS replacement files.
The official Go TLS fixes for GO-2026-4340, GO-2026-4870 and GO-2026-6090 are adapted
to the old uTLS fork, including its alternate UClient path. This is a maintained
bounded patch obligation, not a blanket claim about the upstream engine.

The scoped component audit found no remaining reported advisory in the generated
core scope: both binary scans and the resolved graph returned zero finding records
against the Go database modified2026-11-05. Seven targeted TLS regressions passed;
the existing uTLS suite retained the same historical failures, so it is not described
as fully passing. The pinned builds produced the following admitted hashes; a fresh run of the
production source recipe reproduced the Linux binary:

| Target | SHA256 |
| --- | --- |
| Linux amd64 | `38409dae02011960174bfb72674a23e123f016fe79685caae2f2066c6596595d` |
| Windows amd64 | `88b88aab0cbc5c6ee6a7e2ae3112f64b889e6598bb326a577096794253cf3f89` |

The `1.14.4-harness.2` recipe additionally fixes FakeIP crash durability without
changing dependencies or the stored format. Cache metadata reads retain the
committed allocation cursor; each new domain/address mapping and both-family
cursor state commit in one bbolt transaction before DNS returns the address.
Persistence failure returns an error and restores the in-memory cursor. This
prevents address loss/reuse after SIGKILL, including repeated crashes after a
read-only restart. Go storage/failure tests and the race detector passed; actual
IPv4/IPv6 engine-crash regressions cover preserved numeric application addresses
and current profile policy. Both updated binaries returned zero finding records
in a fresh Go vulnerability-database scan on2026-11-09 (database modified2026-11-05).
The source recipe includes all four changed engine files and both Go tests.

The automatic installer builds without a system Go installation and rejects a
different output. Windows also builds the Linux amd64 server engine. Other CPU
architectures are not admitted. Embedded Wintun DLLs match the official signed
0.14.1 archive byte-for-byte; native driver loading/signature policy remains unverified.

Primary references: [sing-box release](https://github.com/SagerNet/sing-box/releases/tag/v1.14.4),
[Naive release](https://github.com/klzgrad/naiveproxy/releases/tag/v154.0.8037.49-5),
[official TLS fix4340](https://go-review.googlesource.com/c/go/+/724120),
[TLS fix4870](https://go-review.googlesource.com/c/go/+/763767),
[TLS fix6090](https://go-review.googlesource.com/c/go/+/804261),
[WFP arbitration](https://learn.microsoft.com/en-us/windows/win32/fwp/filter-arbitration),
[supported layer conditions](https://learn.microsoft.com/en-us/windows/win32/fwp/filtering-conditions-available-at-each-filtering-layer),
[WFP filter flags](https://learn.microsoft.com/en-us/windows/win32/api/fwpmtypes/ns-fwpmtypes-fwpm_filter0),
[Wintun archive](https://www.wintun.net/builds/wintun-0.14.1.zip).

## Routing, health and privacy

Linux resolves the existing route to the selected VPS for the dedicated engine UID
after stopping the previous engine. REALITY/Hysteria dialers bind to that interface
instead of forcing the physical default interface; an existing VPN remains a usable
underlay. An unresolved route or the owned TUN itself is refused. Direct/LAN rules
retain their existing policy and other providers' firewall rules remain effective.

IPv4/IPv6 TUN routing handles all supported traffic. Unsupported protocols cannot
escape via a direct fallback. Domain/subdomain and IP/CIDR exceptions select the
engine's direct outbound. Fake IP answers preserve domain identity for routing;
actual domain resolution uses authenticated HTTPS through the selected tunnel or,
for a direct exception, Quad9 HTTPS through the engine. Connected private LAN bypass
is visible, optional and restricted to physical link routes. DHCP/public/default
routes cannot manufacture a direct global whitelist. LAN changes trigger recompilation
under an armed guard.
Linux IPv6 kernel connected routes qualify without an explicit `scope` field when
gateway-free and private/link-local; static routes still require link scope. After
direct domain rules, a resolve action uses tunnel DNS before matching real CIDR/LAN
addresses restored from FakeIP. Numeric IP exceptions retain their normal behavior.

The FakeIP allocator and reverse mappings are retained in one private cache for the
installation's authorized owner and fixed address pools. Browser DNS entries carry no
profile identity and can outlive an engine replacement; separate profile allocators
could lose or alias those entries. All profiles therefore reuse the same mapping while
the current profile's domain/CIDR policy and selected transports are evaluated normally.
The cache is not a connection proof and cannot authorize a candidate or direct fallback.

On Linux the cache lives in a separate persistent sibling of the root-only state tree
(`/var/lib/harness-tunnel-cache` by default). Its parent and identity marker are root-owned;
the parent is0750 root:runtime_gid and its precreated `maps.db` is0600 runtime_uid:runtime_gid.
The engine can update the file but cannot unlink or replace it, traverse the0700 credential
tree, or write the marker. Existing wrong-owner, writable-parent, linked or permissive files
are refused rather than repaired. The marker binds state-root/owner/runtime identity and
address pools. Systemd admits only this additional directory through `ReadWritePaths`.
First installation refuses a cache-path collision and rolls back only the directory it
created. Existing native installations need the cache resource and updated unit as well
as the Python modules; a module-only update is insufficient.

Windows keeps the cache below the existing private state root with a protected SYSTEM/admin
ACL, without a desktop-user reader grant. Native Windows acceptance remains outstanding.
The cache contains DNS domain-to-address mappings and allocator metadata, not SSH/transport
credentials, visited URLs, request bodies or current profile policy. It stays outside the
shared SQLite, public DTO/SSE, metrics and logs. It survives normal transport replacement,
disconnect/reconnect, profile removal and service restart; removing one profile must not
invalidate another profile's cached browser addresses. It is retained until explicit native
component/cache removal, rather than automatically purged during active use. Graceful engine
shutdown persists allocator metadata. SIGKILL, power loss and upstream recovery of damaged
cache metadata are not claimed as lossless; Linux denies the engine's file-unlink reset,
but this does not guarantee recovery of every corrupt or incompletely saved mapping.

Initial and changed candidates must pass HTTPS egress, DNS, a256KiB download and
a256KiB upload. UDP uses a transaction-checked STUN exchange. A small request alone
does not approve a new candidate. Steady monitoring uses small checks every15seconds;
explicit checks and suspected/changed paths can incur bulk traffic.
Health exchanges use a scope-owned executor capped at five workers. Identity, DNS
and UDP overlap; a verified HTTPS external IP precedes parallel full256KiB download
and upload. Every worker joins before return, exceptions or an engine replacement;
required TCP/DNS/bulk proof and TLS validation are unchanged. Identity fallback
latency is timed per successful URL and denotes HTTPS response time, not ICMP/RTT.
An in-memory, endpoint-bound hint prioritizes the last fully verified endpoint and
TCP/UDP combination for up to300seconds, including a verified UDP fallback. It
never authorizes a connection without full revalidation, is capped at32profiles,
and is invalidated by prepare/remove, changed endpoints or a failed hinted path.
A degraded TCP-only success creates no UDP hint; any observed UDP failure clears
the existing hint so it cannot suppress a fresh UDP attempt on reconnect.
Small health checks do not extend the hint TTL. Healthy active fallback paths stay
selected; expiry permits a fresh default UDP attempt on the next candidate search.
No hint or monotonic timestamp enters persisted state or a public DTO. TCP can remain
connected with a visible UDP degradation. Metrics retain only aggregate byte/speed,
latency, external IP, errors and bounded events/operations, with minute totals pruned
after24hours. No destination/site history or original SSH passwords are stored in
shared SQLite, browser draft storage, SSE or logs.
The dedicated diagnostic inbound routes TCP/UDP to the selected candidates before
any user or LAN exceptions. DNS proof sends a wire-format DoH query to the same
selected upstream through this inbound and validates the transaction, question and
complete real A answer; local FakeIP responses cannot approve a candidate. Pending
notices reconcile to operation results and stream recovery without replacing editor
drafts, focus or unrelated validation errors.
The human UI guides first use through server settings, transient SSH preparation
and explicit connection. The stage resumes from the durable profile and actual
prepared/runtime observation, without inventing operation substeps or connection
proof. First-use advanced settings are folded away; existing profile settings are
preserved when edited. Monitoring and contextual removal/recovery are separate.
Prepared endpoint ownership still requires acknowledged server removal before edits;
no UI stage weakens that backend rule. Both SSH forms prevent document submission
and send only typed transient credential fields through the existing API.
Expected DoH HTTP/protocol failures, including truncated chunked responses, count
as failed DNS proof. They update the observed health flags and enter the normal
reconnect/candidate-failure path without terminating the monitoring worker.
Metrics HTTP/protocol or typed-response failures also remain recoverable. Missing
counters clear the current speed fields and set a bounded unavailable message,
while retaining the last aggregate totals and the independent successful data-path
health. Recovery clears that message and averages the next delta over the elapsed
time since the last good sample; an unavailable counter alone causes no bulk probe.
Failed data-path health, blocked/reconnecting protection state and managed-generation
retirement invalidate both current speeds and their interval baseline, while
retaining cumulative totals/offsets. The first new verified sample only establishes
a baseline; speeds remain absent until a valid same-generation pair exists. A zero
from that pair is a measured zero, rather than a substitute for unavailable speed.
The daemon applies the same unavailable-current-rate policy when its service IPC
observation is missing: startup reconciliation, failed status polling and command
completion without a fresh status remove speed fields while preserving byte/minute
aggregates, profile revisions/settings and connection/protection intent. Missing
IPC is not proof that the guard or data path failed. A fresh service observation
restores its actually reported rates; no zero is substituted during the gap.
New connection events also use the observed protection state: reconnecting or
blocked observations affirm retained protection/blocked traffic only when protection
is active. Unknown protection is explicitly unconfirmed; inactive or unavailable
protection is stated as such. This changes event wording only, leaves existing
history intact and does not infer native guard behavior from missing IPC.

The loopback Clash API credential is fresh for each managed engine launch and exists
only in that runtime generation and its protected generated configuration. It is
never a profile/restore credential; old profile credentials are discarded on load
and persistence. The current generation is retired before stopping any process.
Metrics requests require the same live managed process/generation before and after
the HTTP exchange; dead/changed-generation replies are discarded and trigger a new
connection check. Thus a credential captured by a replacement loopback listener
after process death cannot authorize the next engine's metadata/control API.
This mitigation relies on the exact admitted engine creating its API listener once
per process lifecycle; a future in-process listener replacement contract requires
separate admission. No native two-UID or Windows execution proof is inferred from
the loopback fixtures.

## Bootstrap and evidence limits

SSH bootstrap supports Ubuntu/Debian amd64 with systemd, existing Python3/OpenSSH,
root or existing sudo, and free TCP/UDP443. Initial host trust uses a private known_hosts
with OpenSSH accept-new; changed host keys fail. Original passwords stay transient.
The maintenance key admits only a fixed root-owned status/restart/remove helper, with
no forwarding/PTY or arbitrary command. Partial intent is durable before remote
writes and can be resumed with fresh initial credentials. Owned files/units have
rollback on ordinary errors/signals; a remote SIGKILL/power loss is recovered by
repeating the owned bootstrap, not claimed as a fully atomic remote transaction.
Rollback also removes only newly created empty directories and temporary publication
files. First local installation preflights all service/state collisions and rolls
back new units/state/services on failure; the elevated copier rolls back its own new
runtime tree so the ordinary setup path can retry. Existing installations are refused.
Existing listeners, unrelated units and firewall policy are preserved. Active UFW
receives labeled443rules; arbitrary nft/firewalld/provider firewalls are not rewritten.
Linux account ownership uses exact colon-free GECOS markers accepted by `useradd`:
`harness-tunnel-runtime-v1` locally and `harness-tunnel-<profile>` for VPS maintenance.
Creation and existing-account checks use the same marker; a foreign account is
neither adopted nor replaced. The local installer rolls back only accounts it created.
Endpoint removal journals `uncertain` before each remote call and `removed` only
after its acknowledgement in the private applied state. Retries skip acknowledged
endpoints across service restarts. A partial/lost reply retains the profile and blocks
prepare/connect until removal completes. The human can submit fresh transient SSH
credentials with the typed remove operation; the fixed helper validates owner markers
and exact resource identities or confirms absence, rather than treating failed
authentication as evidence. This repair uses no saved original password and accepts
no caller-supplied program or raw configuration.
Removing maintenance admissions leaves an unchanged `authorized_keys` untouched.
When owned lines exist, the helper filters bytes and atomically publishes a
same-directory temporary file with the original mode/owner; failed writes or
replacement preserve the original keys. Owned directory cleanup retains its marker
through recursive content deletion and restores its exact bytes/mode/owner if the
final directory removal raises an ordinary I/O error. The same fresh-SSH helper can
then resume; genuinely unowned directories remain refused. This recovery does not
claim atomic removal across SIGKILL, power loss or a second failure during repair.

Verified here: domain/CAS/HTTP/SSE/lifecycle/migration/CLI checks, independent local TLS
256KiB upload/download, actual nft IPv4/IPv6 drop and private LAN exceptions in a
disposable namespace, a separate nonroot UID permit namespace, automatic source build
and generated config checks. The component audit additionally exchanged byte-identical
512KiB TCP and1024byte UDP over both REALITY/Vision/XUDP and HY/Salamander in isolated
local fixtures. Those initial checkout fixtures lacked a TUN device and simultaneous
twoUID mapping; they did not constitute full native acceptance.

Subsequent operator-authorized Linux acceptance on2026-11-09 used separate network
and mount namespaces, private D-Bus/systemd-resolved (UID991), engine UID995 and desktop
UID1000. A slirp underlay with Amnezia-shaped main-table `/1` routes reproduced the
old SOCKS/application discrepancy. The corrected runtime passed system DNS/FakeIP,
ordinary TLS/HTTP (ChatGPT200, OpenAI API401), real/fake destination routes through
TUN, full bulk and UDP checks, engine SIGKILL with retained nft protection, automatic
recovery, cached FakeIP reuse and Hysteria2. Foreign-link DNS was unchanged before
and after. The four verified modules were then installed into the existing idle
root service with snapshot verification and rollback; the main Czech connection
remained active. This does not establish native Windows, reboot, arbitrary network
change, provider firewall or censorship acceptance. Crucially, slirp used the host's
Amnezia path outside the tested namespace; that run did not establish a standalone
connection. A subsequent user failure invalidated the broader readiness claim.

Standalone acceptance on the same date used a temporary macvlan on the physical
Ethernet adapter, a distinct DHCP lease and private net/mount/PID namespaces. Its
traffic bypassed the host's Amnezia routes and firewall while the host retained
the Czech connection. REALITY did not pass that path's health check; the existing
adaptive selection chose Hysteria2. System DNS, desktop-owner HTTPS (ChatGPT200,
OpenAI API401), real/FakeIP routes, bidirectional bulk, UDP, SIGKILL protection and
automatic recovery all passed on that direct path. The standalone preflight
modules were installed only after this acceptance. This evidence establishes
the tested direct Hysteria2 path, not unrestricted reachability of every transport.
