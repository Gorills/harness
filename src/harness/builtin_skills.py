from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from harness.skills import (
    SKILL_FILE_NAME,
    SKILL_METADATA_FILE_NAME,
    SkillRegistryError,
    validate_skill_registry_trust,
)

_BUILTIN_MANIFEST_NAME: Final[str] = ".harness-builtin-skills.json"
_BUILTIN_MANIFEST_VERSION: Final[int] = 1
_BUILTIN_FILE_MODE: Final[int] = 0o600
_BUILTIN_DIR_MODE: Final[int] = 0o700
_YAML_LINE_BREAKS: Final[str] = "\n\r\u2028\u2029"


class BuiltinSkillError(RuntimeError):
    """Raised when the Harness-owned quality skill pack cannot be reconciled safely."""


class BuiltinSkillCollisionError(BuiltinSkillError):
    """Raised when a built-in id collides with unknown or user-modified registry content."""


def _yaml_quoted_scalar(value: str, *, field: str) -> str:
    """Serialize a built-in metadata scalar as a quoted YAML string (JSON syntax)."""
    if not value.strip():
        raise BuiltinSkillError(f"built-in skill {field} must be non-empty text")
    quoted = json.dumps(value, ensure_ascii=False)
    if any(character in quoted for character in _YAML_LINE_BREAKS):
        raise BuiltinSkillError(
            f"built-in skill {field} cannot be serialized as a single-line YAML scalar"
        )
    return quoted


@dataclass(frozen=True, slots=True)
class BuiltinSkill:
    skill_id: str
    description: str
    task_hints: tuple[str, ...]
    body: str
    applies_languages: tuple[str, ...] = ()
    applies_dependencies: tuple[str, ...] = ()
    applies_manifests: tuple[str, ...] = ()
    applies_facets: tuple[str, ...] = ()
    references: tuple[tuple[str, str], ...] = ()

    def files(self) -> dict[str, bytes]:
        name = _yaml_quoted_scalar(self.skill_id, field="name")
        description = _yaml_quoted_scalar(self.description, field="description")
        frontmatter = f"---\nname: {name}\ndescription: {description}\n---\n\n"
        metadata = [f"id: {_yaml_quoted_scalar(self.skill_id, field='id')}"]
        applies = (
            ("languages", self.applies_languages),
            ("dependencies", self.applies_dependencies),
            ("manifests", self.applies_manifests),
            ("facets", self.applies_facets),
        )
        if any(values for _, values in applies):
            metadata.append("applies:")
            for field, values in applies:
                if values:
                    metadata.append(f"  {field}:")
                    metadata.extend(
                        f"    - {_yaml_quoted_scalar(value, field=field)}" for value in values
                    )
        if self.task_hints:
            metadata.append("task_hints:")
            metadata.extend(
                f"  - {_yaml_quoted_scalar(hint, field='task_hints')}" for hint in self.task_hints
            )
        files = {
            SKILL_FILE_NAME: (frontmatter + self.body.strip() + "\n").encode(),
            SKILL_METADATA_FILE_NAME: ("\n".join(metadata) + "\n").encode(),
        }
        for name, body in self.references:
            relative = PurePosixPath(name)
            if (
                relative.is_absolute()
                or len(relative.parts) != 1
                or relative.name in {"", ".", ".."}
                or not relative.name.endswith(".md")
                or "\\" in name
                or "\x00" in name
            ):
                raise BuiltinSkillError(
                    f"built-in skill reference name is invalid: {self.skill_id}/{name}"
                )
            key = (PurePosixPath("references") / relative).as_posix()
            if key in files:
                raise BuiltinSkillError(
                    f"built-in skill reference name is duplicated: {self.skill_id}/{name}"
                )
            files[key] = (body.strip() + "\n").encode()
        return files


@dataclass(frozen=True, slots=True)
class BuiltinSkillSyncResult:
    installed: int
    updated: int
    unchanged: int
    adopted: int
    retired: int
    released: int
    skill_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Replacement:
    target: Path
    backup: Path | None


BUILTIN_SKILLS: Final[tuple[BuiltinSkill, ...]] = (
    BuiltinSkill(
        "testing-strategy",
        "Use when implementing or modifying software behavior; defines reproduction, regression coverage, focused verification, and repository-required completion checks.",
        (),
        """
# Testing strategy
- Reproduce failures or define a falsifiable acceptance check before changing behavior when practical.
- During iteration, run the smallest relevant unit/integration checks that can catch the change.
- Add the smallest regression coverage that protects the actual failure mode and important negative path; do not expand the test surface for unrelated behavior.
- Mock network/external systems at explicit boundaries; prefer real local domain/storage behavior where cheap.
- Before publication or merge, run every repository-mandated quality gate for the exact candidate. A targeted green test never substitutes for required CI.
- After repeated failed attempts, stop changing code, restate the evidence and current hypothesis, and inspect the boundary before trying another fix.
- Report only checks that actually ran; distinguish failed, not run, and environment-blocked verification.
- Do not duplicate facts Harness can derive from manifests or the Structural Index. Record durable
  Knowledge only for non-mechanical conventions future agents would otherwise rediscover: focused
  test commands, canonical task runner, local integration environment, docs locations, unsafe
  operations, migration workflow, and release practice. Verify them from repository evidence;
  prefer a few anchored operational facts over a broad generated project summary.
""",
        applies_facets=("software-project",),
    ),
    BuiltinSkill(
        "secure-by-design",
        "Use when work changes a trust boundary, sensitive data, authentication, authorization, exposed input, infrastructure, or delivery security.",
        (),
        """
# Secure by design
Security reduces likelihood and impact; it never makes a project impossible to compromise.

- For a new project, a new external interface, sensitive/regulated data, identity/payment/admin
  behavior, or a material trust-boundary change, read
  [security architecture](references/security-architecture.md) before choosing the implementation.
- Read only the references for surfaces the task actually crosses:
  [web and backend](references/web-backend.md),
  [browser frontend](references/browser-frontend.md),
  [mobile](references/mobile.md), and
  [infrastructure and supply chain](references/infrastructure-supply-chain.md).
- For every security-sensitive change and every new externally reachable project, define and execute
  the applicable evidence in [security verification](references/verification.md).
- Preserve repository authorization and scope. Security review does not authorize production access,
  credential use, active scanning of third parties, account changes, releases, or destructive tests.
- Prefer deny-by-default, least privilege, explicit trust boundaries, minimized sensitive data,
  maintained platform controls, and several independent mitigations for high-impact failures.
- Never invent cryptography, authentication protocols, token formats, parsers, or sandboxing when a
  maintained platform facility or reviewed library satisfies the requirement.
- Never weaken TLS, certificate validation, authorization, isolation, secret handling, or verification
  to make a test pass. If a required control cannot be implemented or verified, report the residual
  risk and stop at the affected boundary instead of describing the system as secure.
""",
        applies_facets=("software-project",),
        references=(
            (
                "security-architecture.md",
                """
# Security architecture

Use this baseline at project inception and whenever exposure, identities, privileged operations,
sensitive data, integrations, or deployment trust change. Align web controls to the current OWASP
ASVS, mobile controls to OWASP MASVS/MASTG, and lifecycle controls to NIST SSDF; standards are a
verification baseline, not a substitute for a system-specific threat model.

## Establish the security contract

- Classify the product's data and operations by confidentiality, integrity, availability, privacy,
  safety, fraud, and recovery impact. Minimize collection, privileges, retention, replicas, exports,
  logs, analytics, and backups before adding encryption around unnecessary data.
- Map actors, assets, entry points, data flows, trust zones, tenant boundaries, administrative planes,
  external providers, build/update paths, and recovery paths. Treat clients, networks, proxies,
  queues, files, imports, webhooks, plugins, support tools, and operators as explicit trust boundaries.
- Enumerate realistic misuse/abuse cases and attacker goals. Apply a repeatable method such as STRIDE,
  then rank by plausible impact and exposure; include chained failures and compromised dependencies,
  accounts, devices, CI, and administrators.
- Convert mitigations into testable security requirements with an owner and evidence. Record accepted
  residual risk and expiry/reevaluation triggers; do not mark a control complete because a framework
  usually enables it.

## Choose secure boundaries

- Authenticate every caller at the boundary that relies on identity, authorize every operation and
  object server-side, and default to no access. Separate user, service, support, and administrative
  identities; keep privileged interfaces isolated and strongly authenticated.
- Centralize policy decisions enough to stay consistent while enforcing them at every resource. Model
  tenant ownership in durable data and queries; never accept tenant/user/role authority from a client.
- Give services, databases, queues, object stores, CI jobs, and humans separate least-privilege
  identities. Prefer short-lived, audience-bound credentials and explicit egress over shared static
  credentials or broad network trust.
- Validate untrusted data at its first trusted boundary and preserve typed/canonical forms internally.
  Encode at the output/interpreter boundary. Apply independent size, count, depth, time, and resource
  limits before expensive parsing, decompression, image/PDF processing, regex, or cryptography.
- Use maintained, misuse-resistant cryptographic APIs. Define key generation, storage, access,
  rotation, revocation, backup, and destruction; keep encryption keys separate from encrypted data.
  Never use reversible encryption where password hashing is required.
- Design secure failure: bounded timeouts/retries, backpressure, quotas, idempotency, transaction and
  rollback behavior, non-sensitive errors, isolated blast radius, protected backups, and rehearsed
  recovery. Availability and abuse resistance are security properties.
- Make production defaults safe: debug/admin/test interfaces off, no default credentials, minimum
  exposure, strict transport, safe headers/policies, isolated environments, and startup failure for
  missing critical security configuration.

## Keep evidence durable

- Keep the threat model and requirements proportional and near the architecture they govern. Update
  existing ADRs/diagrams/runbooks instead of creating a parallel documentation system.
- Require review for new public ingress, privilege or identity changes, sensitive-data flows,
  cryptographic design, native code, parsers of hostile formats, sandbox escapes, build/signing
  changes, and controls with high-impact single points of failure.
- Revisit assumptions after incidents, dependency/runtime upgrades, new integrations, deployment
  topology changes, and meaningful changes in data sensitivity or attacker value.

Primary baselines:
- OWASP ASVS: https://owasp.org/www-project-application-security-verification-standard/
- OWASP MASVS/MASTG: https://mas.owasp.org/
- NIST SSDF SP 800-218: https://csrc.nist.gov/pubs/sp/800/218/final
""",
            ),
            (
                "web-backend.md",
                """
# Web, API, and backend security

- Use maintained framework security primitives and production settings, but verify their effective
  configuration. Patch supported runtime/framework versions promptly; isolate or remove unsupported
  legacy components rather than assuming a WAF compensates for them.
- Separate authentication from authorization. For every read/write/list/export/action, enforce
  subject, tenant, object, field, and state-transition permission server-side. Test horizontal and
  vertical privilege escalation, guessed identifiers, bulk APIs, alternate methods, and stale roles.
- Use phishing-resistant MFA/passkeys for high-value and administrative accounts where feasible.
  Passwords use a current memory-hard password KDF such as Argon2id, or a deliberately tuned
  supported alternative, plus breached-password defenses, safe recovery, generic responses,
  rotation only on compromise, and rate/automation controls that do not enable account lockout abuse.
- Sessions/tokens need bounded lifetime, rotation/revocation, secure issuance, audience/issuer checks,
  replay resistance where required, and invalidation on relevant account changes. Browser session
  cookies are Secure, HttpOnly, narrowly scoped, and use a SameSite policy compatible with the real
  OAuth/payment flow; never put bearer tokens in URLs or logs.
- Validate content type and a strict schema including length/range/count/depth. Parameterize queries;
  allowlist dynamic identifiers/operators. Use contextual escaping for HTML, JS, CSS, URLs, shells,
  templates, LDAP, and other interpreters. Do not pass untrusted text to `eval`, shells, or unsafe
  deserializers.
- Defend SSRF with destination allowlists and resolved-address checks at connection time; restrict
  schemes, redirects, DNS rebinding, credentials, metadata/private/link-local networks, and egress.
  Validate redirects and callback URLs against exact registered origins/paths.
- Treat uploads and archives as hostile: limit size/count/type after content inspection, randomize
  server names, store outside executable/web roots, prevent traversal and archive bombs, scan where
  appropriate, transform in isolation, and serve with safe type/disposition and authorization.
- Configure CORS for exact trusted origins/methods/headers/credentials. Protect cookie-authenticated
  state changes from CSRF; do not use CORS as authorization. Reject ambiguous duplicate headers,
  request smuggling conditions, unsupported methods, and unexpected encodings at a consistent edge.
- Bound REST/GraphQL/WebSocket/gRPC complexity, message size, subscriptions, pagination, fan-out,
  concurrency, and per-identity/resource rates. Authenticate connection establishment and recheck
  authorization as identity/resource state changes; validate every message, not just the handshake.
- Authenticate webhooks with a documented signature scheme over exact raw bytes, timestamp tolerance,
  replay protection, idempotency, and secret rotation. Do not trust source IP alone.
- Keep database constraints as the final integrity guard. Make money, quota, inventory, workflow,
  retries, idempotency, locking, and time-of-check/time-of-use behavior explicit under concurrency.
- Return non-sensitive errors and correct status codes; disable production stack traces and debug
  endpoints. Redact secrets, credentials, session material, sensitive payloads, and personal data from
  logs/traces; protect audit logs from tampering and unauthorized reads.
- Terminate only current secure TLS, preserve origin authentication behind proxies/CDNs, restrict
  forwarded-header trust, and set HSTS/security/cache headers appropriate to the content. Ensure
  private/authenticated responses cannot enter shared caches and secrets never enter URLs.
""",
            ),
            (
                "browser-frontend.md",
                """
# Browser frontend security

- Treat every value from APIs, URLs, storage, DOM, postMessage, files, CMS, Markdown, translations,
  analytics, and third-party scripts as untrusted. Prefer framework text binding and safe DOM APIs;
  avoid raw HTML sinks. If rich HTML is required, sanitize with a maintained context-appropriate
  library and test mutation/bypass cases.
- Deploy a restrictive Content Security Policy from HTTP headers, using nonces/hashes and strict
  script loading where the stack supports it. Keep it effective in report-only first when migrating,
  then enforce; do not add broad `unsafe-inline`, `unsafe-eval`, wildcard sources, or untrusted script
  domains to silence violations. Use Trusted Types where supported and useful.
- Keep long-lived bearer credentials out of localStorage/sessionStorage and client bundles. Prefer
  server-managed Secure/HttpOnly cookies for browser sessions, or deliberately bounded in-memory
  tokens where architecture requires them. Public frontend environment variables are public.
- Apply CSRF protection to cookie-authenticated state changes, exact CORS origins, origin checks where
  applicable, and safe SameSite settings. Prevent open redirects and login/OAuth mix-up; never trust
  client-side route guards or hidden controls as authorization.
- Validate postMessage origin and message schema, use exact target origins, sandbox untrusted frames,
  restrict embedding with CSP frame policies, and validate cross-window/native-bridge messages.
- For links and new windows, restrict schemes and destinations and prevent opener control. For
  downloads/uploads, preserve safe names/types and never render attacker-controlled active content
  under a trusted origin without isolation.
- Minimize third-party scripts, tags, ads, widgets, maps, fonts, and CDNs; each inherits page access.
  Pin/review dependencies, use integrity metadata for stable cross-origin assets where workable, and
  obtain consent before nonessential tracking. Never send credentials or sensitive data to telemetry.
- Do not expose source maps, debug endpoints, build metadata, internal API hosts, secrets, or detailed
  errors unintentionally. Remember that obfuscation and minification do not protect client secrets.
- Test DOM/reflected/stored XSS, CSRF, clickjacking, redirect, CORS, cache, upload/download, OAuth,
  third-party failure, and authorization-negative paths against the production build and effective
  response headers, not only component source.
""",
            ),
            (
                "mobile.md",
                """
# Mobile application security

Use OWASP MASVS/MASTG as the verification baseline for Android/iOS. React Native, Expo, Flutter,
WebView, and native modules do not make the client a trusted environment.

- Never embed API secrets, private keys, service credentials, signing credentials, privileged feature
  flags, or authorization policy in the app bundle, JavaScript bundle, resources, native constants,
  over-the-air update, or public build environment. Put privileged operations behind an authorized
  server boundary.
- Classify local data. Keep tokens/keys and small sensitive values in platform-backed secure storage
  (for Expo, `expo-secure-store` where its guarantees fit); AsyncStorage/preferences/plain SQLite,
  caches, persisted state, files, logs, crash reports, clipboard, screenshots, and backups are not
  secret stores. Minimize offline sensitive data and define logout/revocation cleanup.
- Use Authorization Code with PKCE and exact redirect registration for OAuth/OIDC. Prefer verified
  Universal Links/App Links over custom schemes; treat every deep link, intent, universal link,
  notification, QR code, shared file, and native bridge message as untrusted input. Never carry tokens
  or sensitive data in links.
- Enforce authentication and authorization on the server. Biometrics/device unlock may release a
  local credential but never proves server authorization by itself. Handle token expiry, refresh
  rotation/reuse, device loss, logout-all, account recovery, and clock skew deliberately.
- Require HTTPS with normal platform certificate validation and disable cleartext traffic. Certificate
  pinning is a risk-based option, not an automatic improvement: if used, define backup pins, expiry,
  rotation, outage recovery, and old-app compatibility before release.
- Request the minimum platform permissions at the moment of need, explain the user benefit, handle
  denial/revocation, and remove unused permissions/native capabilities. Restrict exported Android
  components, intent filters, iOS URL handlers, background modes, and inter-app data sharing.
- Harden WebViews: load only intended origins/content, disable unnecessary file/content access and
  debugging, restrict navigation, validate bridge messages, and never expose privileged native methods
  to untrusted web content.
- Protect release signing, store accounts, service-account/API keys, provisioning profiles, push
  credentials, and EAS/CI access with least privilege, MFA, audit, backup, and rotation. Separate dev,
  preview, and production application identifiers, endpoints, credentials, and update channels.
- Sign and verify releases/updates through the platform mechanism. Define Expo Updates runtime/version
  compatibility and rollback behavior; an OTA update must not bypass review, signing, staged rollout,
  or native compatibility gates.
- Treat root/jailbreak/emulator/debugger/obfuscation/tamper detection as defense-in-depth signals, not
  authorization boundaries. Avoid blocking legitimate users without a threat-driven requirement.
- Test release builds on supported real OS versions for storage extraction, backups, logs, screenshots,
  deep links, intents, WebViews, TLS, proxying, permissions, auth lifecycle, offline behavior, update
  rollback, and compromised-device assumptions.

Official platform guidance:
- React Native security: https://reactnative.dev/docs/security
- Expo authentication/storage: https://docs.expo.dev/develop/authentication/ and
  https://docs.expo.dev/develop/user-interface/store-data/
""",
            ),
            (
                "infrastructure-supply-chain.md",
                """
# Infrastructure and software supply-chain security

- Separate development, test, staging, and production accounts/projects/networks/data/secrets.
  Production is private by default: expose only required ingress through an authenticated, patched,
  rate-limited edge; block direct origin, database, cache, admin, metrics, and management access.
- Give workloads and operators individual least-privilege identities. Prefer short-lived workload
  identity/OIDC over static cloud keys; require MFA and audited elevation for humans. Restrict egress,
  cloud metadata access, service-to-service paths, and DNS according to actual dependencies.
- Store secrets in an approved secret manager or protected runtime mount, never source, images, build
  arguments, Terraform state outputs, CI logs/artifacts, chat, or shell history. Define access, rotation,
  revocation, compromise response, and bootstrap/root-secret custody.
- Harden hosts/images/services with supported versions, timely patches, minimal packages, non-root
  execution, read-only filesystems where viable, dropped capabilities, syscall/MAC isolation,
  resource/PID limits, and no privileged host/socket/device mounts without a documented requirement.
- At proxies/load balancers, validate upstream TLS, restrict trusted forwarded headers, normalize
  requests consistently, bound bodies/timeouts/connections, prevent bypass to origins, and keep admin
  APIs private. A CDN/WAF is defense in depth, not a replacement for application controls.
- Protect databases, object stores, queues, backups, and logs with private access, per-workload roles,
  encryption/key separation, retention, immutable/versioned recovery where warranted, and tested
  restore. Monitor privileged reads, policy changes, destructive actions, and unusual exports.
- Pin deliberate dependency/tool/action/image versions with a maintained update path. Review new
  publishers, install scripts, transitive/native code, licenses, maintenance, and compromise impact;
  remove unused dependencies. Use reproducible locked installs and trusted registries/mirrors.
- Isolate CI runners and untrusted pull requests from secrets and production. Minimize workflow token
  permissions, pin third-party actions by immutable identity, protect branches/environments/releases,
  require review for sensitive paths, and prevent build output from executing with stronger trust than
  its source.
- Produce provenance/SBOM and sign artifacts where the delivery environment can verify them. Scan
  source, secrets, dependencies, containers, and IaC with maintained tools, but triage findings against
  reachability/exposure and never treat a scanner as proof of security.
- Make deployment atomic or staged, health-gated, observable, and reversible. Protect migrations and
  one-shot jobs, preserve last-known-good artifacts/config, and rehearse credential revocation,
  containment, backup restore, and incident communication.
- Alert on authentication/authorization anomalies, privilege/config changes, secret use, deployment
  events, suspicious data access/export, integrity failures, and saturation/abuse signals. Redact
  sensitive data while retaining tamper-resistant evidence with bounded retention.
""",
            ),
            (
                "verification.md",
                """
# Security verification

- Derive checks from the threat model and security requirements. Map each applicable control to
  automated or manual evidence and record N/A with a reason; a generic checklist, code review, or
  green scanner alone is not sufficient.
- Add negative authorization tests across role, tenant, object, and action, including unauthenticated,
  disabled/revoked, stale-session, ownership-change, bulk/list/export, alternate-method, and concurrent
  state-transition cases. Assert denial and absence of sensitive side effects/disclosure.
- Test parser and boundary abuse: malformed types/encodings, duplicate fields/headers, extreme size,
  count/depth/compression, traversal, injection metacharacters, unsafe URLs/redirects, hostile files,
  timeouts, cancellation, replay, race, and resource exhaustion. Fuzz high-risk parsers/state machines
  when it adds meaningful coverage.
- Verify production-like effective configuration: TLS and origin routing, headers/CSP/CORS/cookies,
  debug/admin exposure, proxy trust, network policy, workload identity, database/object-store ACLs,
  secret sources, container privileges, backups, logging/redaction, and fail-closed startup.
- Run repository-approved static analysis, secret scanning, dependency/container/IaC analysis, and
  dynamic tests at explicit boundaries. Pin/configure tools, keep suppressions narrow with owners and
  expiry, fail on actionable severity according to policy, and review generated reports for false
  negatives caused by excluded paths or unsupported languages.
- For browser surfaces, verify XSS/CSRF/CORS/CSP/clickjacking/cache/OAuth/upload/redirect behavior in
  the production build. For APIs, verify ASVS-relevant controls and abuse/rate limits. For mobile,
  verify applicable MASVS controls with release binaries and MASTG techniques on both platforms.
- Review dependencies and build inputs for publisher/ownership changes, suspicious install scripts,
  unexpected lockfile deltas, vulnerable reachable components, unsigned/unverified artifacts, and CI
  permission expansion. Generate and retain the artifact/SBOM/provenance required by deployment.
- Keep testing authorized and bounded. Do not scan production or third-party systems, use real user
  data, attempt persistence, or exceed rate/cost limits without explicit scope and approval.
- Before release, require no known unmitigated critical/high-impact issue, documented residual risk,
  rotation/revocation and rollback paths, monitored security signals, responsible disclosure/contact,
  and an incident plan. Re-run affected controls against the exact release candidate.
""",
            ),
        ),
    ),
    BuiltinSkill(
        "container-infrastructure",
        "Use when changing Dockerfiles, Compose, container runtime configuration, images, or local/test/production container operations.",
        (),
        """
# Container infrastructure
- For a new Dockerized project or a material container redesign, read both
  [the project baseline](references/project-baseline.md) and
  [the image/runtime guide](references/image-runtime.md). Local development, automated tests,
  and production are separate acceptance environments, not postponed follow-ups.
- For a focused edit, read only the reference governing the affected boundary.
- Operate only on this project's declared containers, compose files, networks, and volumes.
- Reuse the repository task runner and existing service names before adding another entry point.
- Treat rendered configuration and observed runtime settings as evidence; a field ignored by the
  selected Compose/orchestrator runtime is not an effective limit or policy.
- Keep mutable state, especially uploads/media and databases, outside the container writable layer.
- Do not silently inspect, stop, prune, or mutate unrelated host containers or global Docker state.
- Verify the narrow service first, then the repository's required integration/smoke checks.
""",
        applies_manifests=(
            "dockerfile",
            "containerfile",
            "compose.yml",
            "compose.yaml",
            "docker-compose.yml",
            "docker-compose.yaml",
        ),
        applies_facets=("containerized",),
        references=(
            (
                "project-baseline.md",
                """
# Docker project baseline

## Configuration matrix

- Define an explicit shared baseline plus local-development, automated-test/CI, and production
  configuration. Compose overlays, profiles, or equivalent orchestrator configuration are all
  acceptable when the effective result is unambiguous and documented through the task runner.
- Local development may bind-mount source, enable hot reload/debugging, and expose convenience
  ports. Scope names, networks, and data to the project so parallel checkouts do not collide.
- Tests must start from an isolated deterministic state, wait for real readiness, run migrations
  and fixtures explicitly, propagate the test exit code, and never attach to production or a
  developer's durable volumes.
- Production uses immutable built images rather than source bind mounts, production-only secrets
  and settings, minimum host port exposure, and an explicit deploy/update/rollback path.
- Render and validate every environment's final configuration in CI or an equivalent gate; check
  required variables and fail startup rather than silently using unsafe defaults.

## Lifecycle, resources, and logs

- Give long-running production services a deliberate restart policy such as `unless-stopped`,
  `always`, or the orchestrator equivalent. Do not restart successful one-shot migration or batch
  jobs forever. A Docker healthcheck reports health but does not itself restart an unhealthy
  running process, so define the recovery mechanism separately.
- Add bounded healthchecks with realistic start period, interval, timeout, and retries. Do not
  confuse container start order with application readiness.
- Set effective CPU, memory, and PID limits for production services and appropriate reservations
  when the selected scheduler honors them. Add `ulimits` only for a measured runtime need. Verify
  the limits in the actual target runtime instead of assuming every Compose field is enforced.
- Configure bounded log retention per service/runtime (`local`, rotated `json-file`, or an
  external collector). Set size/file limits and keep application events on stdout/stderr; never
  allow default local logs to grow without an operational bound.
- Design dependency timeouts, bounded retries/backoff, graceful shutdown, and overload behavior so
  restart policies do not turn dependency failure into a hot restart loop.

## State, secrets, and networking

- Put uploads, generated media, database files, and other durable mutable data in named volumes,
  explicit host storage, or object storage. Define ownership, capacity, backup, restore, and
  migration behavior; prove data survives container recreation.
- Use ephemeral caches/tmpfs only for data that is safe to lose. Never let test cleanup target a
  production-named volume, and make any volume reset command explicit and recoverability-aware.
- Keep secrets out of images, build arguments, source, and logs. Use the deployment platform's
  mounted/runtime secret mechanism and separate non-secret configuration by environment.
- Use private service networks by default, publish only required ingress ports, and do not expose
  databases or caches to the host in production without a concrete operational requirement.
""",
            ),
            (
                "image-runtime.md",
                """
# Docker image and runtime quality

- Use a current trusted minimal base image, pin a deliberate version or digest, and retain an
  explicit update path so pinning does not freeze security fixes indefinitely.
- Prefer reusable multi-stage build/test/runtime stages. The runtime stage should contain only the
  application and runtime dependencies, not compilers, package-manager caches, test tools, or
  source files that are unnecessary at runtime.
- Copy lockfiles before application source where that improves cache reuse, install reproducibly,
  and keep a `.dockerignore` that excludes secrets, VCS data, local caches, dependencies, build
  output, and irrelevant media.
- Run as a non-root user; use deliberate UID/GID behavior when persistent host-mounted files are
  involved. Prefer a read-only root filesystem and dropped capabilities where the application and
  target runtime support them.
- Use exec-form entrypoints, correct signal forwarding (or a minimal init where needed), bounded
  shutdown, and one clear container concern. Keep migrations and administrative jobs explicit
  rather than hiding them in every replica's startup.
- Never bake credentials into layers. Do not print secrets in build output, healthchecks, command
  lines, crash reports, or image metadata.
- Build the production target and run its smoke/integration tests in CI. Use available image
  linting, vulnerability scanning, and SBOM/provenance checks without replacing repository gates.
- Verify architecture/platform targets where they differ, health/readiness behavior, restart after
  failure, bounded logs/resources, and persistence across container replacement.
""",
            ),
        ),
    ),
    BuiltinSkill(
        "observability",
        "Use when changing logs, metrics, traces, alerts, incident diagnostics, or runbook-facing observability behavior.",
        (),
        """
# Observability
- Prefer structured events with stable names and correlation/request identifiers.
- Never log secrets, credentials, raw tokens, or unnecessary personal data.
- Add metrics for user-impacting outcomes and saturation/error signals, not every internal variable.
- Keep metric labels/attributes bounded. Never use user IDs, emails, request IDs, raw URLs,
  arbitrary exception text, or other unbounded values as metric dimensions; use normalized
  route/error classes and put high-cardinality detail in logs/traces.
- Define units and histogram/bucket semantics deliberately and account for telemetry cost.
- Trace cross-boundary latency only where it helps diagnose real flows; preserve propagation across service calls.
- Alerts should correspond to actionable symptoms and link to a concise runbook or recovery path.
- During incidents, preserve evidence, form one hypothesis at a time, and verify recovery with user-visible health signals.
""",
        applies_dependencies=(
            "opentelemetry-api",
            "opentelemetry-sdk",
            "prometheus-client",
            "structlog",
            "sentry-sdk",
            "@opentelemetry/api",
            "pino",
            "winston",
        ),
    ),
    BuiltinSkill(
        "ci-release",
        "Use when changing CI pipelines, release automation, deployment gates, artifact publication, or rollback behavior.",
        (),
        """
# CI and release
- Treat the repository's existing CI contract as authoritative; extend it rather than replacing it with generic conventions.
- Preserve repository and organization workflow, review, and deployment policy rather than substituting generic defaults.
- Pin third-party Actions to a reviewed full-length commit SHA where repository policy supports it, and verify that SHA belongs to the expected upstream repository.
- Keep GITHUB_TOKEN and other workflow token permissions at the minimum the job needs.
- Never expose privileged secrets to untrusted fork pull-request code.
- Prefer short-lived OIDC credentials over static cloud keys where the platform supports it.
- Use protected environments and required approval for privileged release and production deploy jobs.
- Preserve artifact provenance and attestations when the repository already produces them.
- Keep dependency lockfiles current and use reproducible tool versions.
- PR checks should cover the affected behavior; required main/release gates remain mandatory even when focused local tests are green.
- Keep deployment credentials in the platform's secret mechanism and minimize permission scope.
- Make database migrations and irreversible operations explicit, ordered, and recoverable where possible.
- Document rollback/forward-fix behavior for release changes and verify the exact candidate being published.
""",
        applies_manifests=(".gitlab-ci.yml", "jenkinsfile", "azure-pipelines.yml"),
        applies_facets=("ci-pipeline",),
    ),
    BuiltinSkill(
        "public-frontend",
        "Use when changing any public or indexable web frontend, including search discoverability, document semantics, accessibility, and loading performance; exclude native-only apps.",
        (),
        """
# Public frontend
- Classify each affected route as public/indexable or deliberately private/non-indexable. Never
  expose private, staging, duplicate, or state-changing URLs through accidental SEO defaults.
- For every public route, SEO is part of implementation without a separate reminder: read and
  apply [Google/Yandex discoverability](references/search-discoverability.md).
- For every user-facing route or shared frontend primitive, read and apply
  [web quality](references/web-quality.md) to the affected behavior.
- Prefer server-rendered or statically generated indexable content when it improves reliable
  discovery and first load; do not force SSR/SSG onto authenticated application surfaces without
  a product or performance reason.
- Preserve the framework's established routing, metadata, rendering, and design-system patterns.
- Test rendered output and real responsive/keyboard behavior; visual polish is not evidence of
  semantics, indexing, accessibility, or loading performance.
""",
        applies_facets=("web-frontend",),
        references=(
            (
                "search-discoverability.md",
                """
# Google and Yandex discoverability

- Public indexable URLs must be anonymously crawlable, return the intended HTTP status, expose
  meaningful visible text and real crawlable links, and make required CSS/JavaScript/resources
  available to Googlebot and Yandex robots. Do not rely on user gestures or infinite scrolling as
  the only discovery path; provide linked pagination or equivalent URLs.
- Generate environment-correct `robots.txt`. Use it to control crawling and reference the sitemap,
  not as a substitute for `noindex` or authentication. Production rules must not inherit staging
  blocks; staging/private environments should combine access control with deliberate indexing
  policy. Do not block assets required to render public pages.
- Generate and serve a valid sitemap (or bounded sitemap index) from authoritative route/content
  data. Include absolute preferred HTTPS URLs that are indexable and return success; exclude
  redirects, errors, duplicates, private/filter/search/action URLs, and inaccurate `lastmod` data.
- Emit one stable preferred canonical URL per indexable page and keep canonical, redirects,
  internal links, sitemap entries, locale annotations, and host/protocol normalization consistent.
  Avoid canonical chains and accidental cross-environment/cross-locale canonicals.
- Give each indexable page a descriptive title, useful meta description, one clear content heading,
  semantic heading hierarchy, correct document language, and descriptive internal-link text.
  Localized alternates need valid reciprocal `hreflang` mapping where the product uses them.
- Add Schema.org structured data only for a supported type that matches visible authoritative page
  content. Prefer JSON-LD where the stack supports it, use stable entity URLs, and never fabricate
  ratings, availability, authorship, or other rich-result facts. Validate against both engines'
  current supported feature rules because support is not identical.
- Treat Open Graph/social-card metadata as sharing quality, not a replacement for search metadata.
  Keep titles, descriptions, images, and canonical page identity consistent.
- Preserve meaningful 301/308 redirects for durable moves, return real 404/410 responses for
  missing content, and avoid soft-404 pages, redirect loops, broken parameter normalization, and
  indexable state-changing URLs.
- Write useful human content for the page's actual intent; do not add hidden text, doorway pages,
  duplicated keyword variants, or promise ranking. Search optimization improves eligibility and
  comprehension, never guarantees placement.
- Verify the production-like rendered HTML, status codes, internal links, canonical/locale tags,
  robots rules, sitemap contents, and structured data. When account access exists, use Google
  Search Console and Yandex Webmaster for post-deploy inspection; their absence does not excuse
  local checks.
""",
            ),
            (
                "web-quality.md",
                """
# Frontend web quality

- Build mobile-first responsive layouts on the same preferred URLs where practical. Use semantic
  landmarks/elements, keyboard-operable controls, visible focus, associated names/labels, logical
  reading order, sufficient contrast, and reduced-motion behavior according to the project's
  accessibility target.
- Keep important content and primary actions useful before nonessential client JavaScript loads.
  Split by route/feature, remove unused dependencies, defer third parties, and avoid hydration or
  state duplication that adds no interaction value.
- Use Core Web Vitals as user-facing performance criteria for public templates: target 75th-percentile
  LCP at or below 2.5 s, INP at or below 200 ms, and CLS at or below 0.1 unless the product defines
  stricter budgets. When field data is unavailable, record reproducible lab evidence and budgets.
- Size images explicitly, provide appropriate `srcset`/`sizes` and modern formats, compress them,
  lazy-load below-the-fold media, and do not lazy-load the likely LCP image. Keep responsive media
  from causing layout shifts.
- Subset and self-host fonts when appropriate, preload only critical assets, use a deliberate
  `font-display`, and avoid blocking chains. Apply caching, compression, and CDN behavior according
  to content mutability; fingerprint immutable assets.
- Preserve correct loading, empty, error, offline, and slow-network states. Abort obsolete requests,
  avoid duplicate submissions, and do not trade accessibility or correctness for optimistic UI.
- Test representative mobile and desktop viewports, keyboard and screen-reader semantics for the
  changed path, production builds under throttling, and regressions in bundle/performance budgets.
  Prefer field telemetry for real-user conclusions and lab tools for reproducible diagnosis.
""",
            ),
        ),
    ),
    BuiltinSkill(
        "frontend-design",
        "Use when creating, changing, or reviewing any user-facing web or mobile interface; exclude backend-only and non-visual work.",
        (),
        """
# Frontend design
Apply this skill whenever the changed output includes a user-facing interface. Functional code is
not finished while its hierarchy, visual language, responsive behavior, or interaction states are
generic, inconsistent, or unverified.

## Make a design contract first
Inspect the existing screens, tokens, components, brand assets, copy, and platform conventions. If
they form a coherent system, extend it instead of silently rebranding the product. When direction
is missing, infer a defensible direction from the subject, audience, and job instead of falling back
to the model's favorite style.

Before implementation, settle this compact contract:

1. **User and job:** who is here, what they need, and the one primary action or outcome.
2. **Content hierarchy:** what must be noticed first, second, and only on demand.
3. **Visual direction:** one sentence tied to the subject's real world, with three character words
   and one explicitly rejected direction.
4. **Signature:** one memorable compositional, typographic, material, or interaction idea. Spend
   boldness here and keep the rest disciplined.
5. **System:** named color roles, type roles, spacing rhythm, shape/depth rule, content width,
   density, compact-layout behavior, and motion rule.

Do not show a long design essay unless the user asks. The contract exists to keep the implementation
coherent. Read [visual language](references/visual-language.md) for every design task, then read
exactly the applicable surface guide: [marketing and editorial sites](references/marketing-sites.md)
or [product and mobile interfaces](references/product-interfaces.md). A mixed product may need both.

## Build from hierarchy, not decoration
- Put real content and the primary task into the layout before polishing surfaces. Copy, images,
  data, and state are design material; generic filler produces a generic composition.
- Encode the contract as existing project tokens or a small semantic token layer. Derive component
  values from those roles instead of scattering arbitrary colors, radii, shadows, and spacing.
- Design wide and compact layouts together. Responsive behavior is a change in grouping, priority,
  navigation, and interaction where necessary, not merely smaller text and stacked columns.
- Prefer familiar controls and clear affordances. Originality belongs in visual voice and
  composition, not in making standard actions hard to recognize.
- Complete the real states: default, hover where available, focus-visible, active, selected,
  disabled, loading, empty, error, success, long content, and permission/offline states that the
  product can reach.
- Preserve repository architecture and the established design system. A design task does not
  authorize framework replacement, route churn, destructive rewrites, invented claims, or unrelated
  copy changes.

## Reject model defaults unless the brief earns them
Do not emit a purple/blue gradient hero, centered headline above three equal feature cards, card
inside card, glass panels, floating blurred orbs, universal pill shapes, identical rounded boxes,
decorative 01/02/03 labels, emoji as product icons, or glow on every important element merely
because they are easy defaults. Any one of these can be valid when it follows from the brand,
content, or interaction; without that reason, choose a structure specific to this subject.

Do not replace one fashion with another. Cream editorial pages, black pages with an acid accent,
brutalist grids, bento layouts, giant type, and excessive whitespace are also generic when selected
without a brief-specific reason. Do not fabricate testimonials, customer logos, ratings, usage
numbers, people, product screenshots, or photographic evidence. Use supplied/licensed assets,
clearly marked placeholders, or honest copy.

## Verify the rendered result
Before handoff, read and execute [visual review](references/visual-review.md). Inspect the rendered
interface at representative compact and wide sizes, fix the highest-impact problems as one batch,
and confirm once more. If rendering is unavailable, say that visual verification was not run; source
inspection alone is not proof of design quality.
""",
        applies_facets=("mobile-app", "web-frontend"),
        references=(
            (
                "visual-language.md",
                """
# Visual language

## Derive a direction from the subject
Start with the product rather than a style catalog. Name three concrete nouns from its world—tools,
materials, places, artifacts, behaviors, or cultural references—and translate them into visual
decisions. A direction such as "a field geologist's annotated specimen drawer: precise, tactile,
quiet" is actionable; "modern, clean, premium" is not.

State what the design must not become. This prevents drift while leaving room for judgment. Preserve
an established brand direction unless the user asked to change it.

## Define a small semantic system
- **Color:** name roles such as canvas, surface, strong text, muted text, border, accent, and semantic
  states. Prefer one primary accent and tinted neutrals. Color must communicate hierarchy or state;
  decoration alone is not a role. Check contrast in every state and theme.
- **Typography:** define display/title, body, label, and data/mono roles only when needed. Marketing
  surfaces may justify a distinctive display/body pairing; task-heavy product UI often works better
  with one well-tuned family. Use a deliberate scale, few weights, readable line height, and roughly
  45-75 characters for prose. Never choose a font only because models commonly do.
- **Spacing:** use a named rhythm instead of isolated values. A practical starting scale is
  4, 8, 12, 16, 24, 32, 48, 64, adjusted to the incumbent system. Related items sit closer than
  unrelated groups; section gaps must be visibly larger than component gaps.
- **Layout:** choose a content width, column logic, alignment anchors, and density. Break a grid only
  to reinforce hierarchy. Asymmetry without alignment looks accidental; perfect symmetry without a
  reason looks templated.
- **Shape and depth:** choose one radius vocabulary and one depth mechanism: borders, tonal layers,
  shadows, overlap, or a deliberate combination. Cards are for grouped or actionable units, not a
  default wrapper for every paragraph.
- **Imagery and icons:** use a coherent visual source, crop, aspect-ratio family, and icon family.
  Prefer real or purpose-built assets. Keep icon stroke, optical size, and alignment consistent;
  pair unfamiliar icons with labels.
- **Motion:** assign motion to orientation, feedback, state change, or one expressive signature.
  Most product transitions should feel immediate; marketing may use a longer orchestrated moment.
  Animate transform/opacity when practical, avoid scattered perpetual motion, and provide a useful
  reduced-motion result.

## Create hierarchy on purpose
Each screen needs one dominant element, a small supporting layer, and quiet detail. Achieve contrast
with scale, weight, space, placement, color, and content—not by making every element louder. A user
should understand the page purpose and next action from a blurred or squinted view.

Structural decoration must carry meaning. Use numbering only for real sequence, badges only for
status/category, dividers only for grouping, and labels only when they clarify a value. Remove any
ornament whose rationale would fit an unrelated product equally well.
""",
            ),
            (
                "marketing-sites.md",
                """
# Marketing and editorial sites

## Design the conversion argument
Give each page one commercial or editorial job. The first screen should make the audience, offer,
outcome, and next action understandable without slogans that could describe any competitor. The hero
is the page's thesis, not a mandatory centered headline block.

Order sections by the visitor's actual questions and objections. A useful sequence might establish
relevance, demonstrate the mechanism, prove the claim, handle risk, explain the offer, and close the
decision—but do not force every brief into hero → logo row → three features → testimonials → pricing
→ FAQ → CTA. Change rhythm, scale, density, and media according to the content.

## Make persuasion credible
- Use one primary CTA phrase consistently through the journey and a quieter secondary action only
  when it serves a different readiness level. Buttons state the outcome: "Start free trial" is
  clearer than "Get started."
- Put proof next to the claim it supports. Use real product evidence, demonstrations, sourced facts,
  customer material, policies, or concrete process detail. Never invent social proof.
- Make pricing, constraints, eligibility, delivery, cancellation, and form expectations clear before
  commitment. Conversion quality does not justify dark patterns, hidden costs, false urgency,
  preselected consent, or a visually suppressed alternative.
- Replace generic benefit stacks with specific situations, outcomes, and differentiators. Use strong
  headlines, short supporting prose, and scannable evidence rather than decorative micro-labels on
  every section.
- Use imagery when it communicates product, craft, people, place, or result. Do not add stock photos
  as mood filler or fake a product screenshot with meaningless rectangles.

## Build a distinctive page rhythm
Choose a macrostructure that fits the argument: demonstration-led, narrative scroll, editorial
index, comparison-led, case-study-led, catalog, manifesto, or another content-derived form. Let one
signature moment carry the personality. Alternate dense evidence with quieter comprehension space;
do not make every section the same height, alignment, and card grid.

Keep navigation proportional to page complexity. A short campaign may need only identity and one
action; a deep product site needs clear information architecture. The closing section should resolve
the page's argument and repeat the real next action, not merely add another gradient banner.
""",
            ),
            (
                "product-interfaces.md",
                """
# Product and mobile interfaces

## Optimize for the task
Product UI should disappear into the user's work. Start with the primary task, current state, and
next safe action. Keep navigation, terminology, save behavior, and control placement consistent with
the product's existing mental model. Do not trade recognition for novelty.

- Use standard controls for standard behaviors. Make the whole control target interactive, give it a
  visible label or accessible name, and keep destructive or irreversible actions visually distinct
  without making them the loudest element by default.
- Use progressive disclosure for advanced or infrequent choices. Density should follow frequency and
  expertise: dashboards and operations tools can be compact; onboarding and high-risk flows need more
  guidance and breathing room.
- Keep the primary action obvious but not repeated in every panel. Accent color denotes action,
  selection, or state rather than decorating large areas indiscriminately.
- Forms need persistent labels, help at the point of uncertainty, sensible grouping, forgiving input,
  inline validation, an error summary for long forms, and preservation of valid work after failure.
- Tables and data views need meaningful defaults, readable alignment, stable column semantics, units,
  sorting/filter state, overflow strategy, empty/loading/error states, and a compact-screen alternative
  based on user priority—not a blind stack of every cell.
- Empty states explain why the space is empty and offer the next relevant action. Loading preserves
  layout where possible. Errors say what happened, what remains safe, and how to recover.

## Treat mobile as a distinct composition
Respect platform navigation, safe areas, keyboards, system bars, back behavior, permissions, and
dynamic type. Place frequent actions within comfortable reach without obscuring content. Follow the
target platform's current accessibility guidance. For iOS/iPadOS, 44x44 pt is the normal default
control size. For Android, touch targets should normally provide at least 48x48 dp. Do not go below
the applicable platform/accessibility minimum or rely on tiny adjacent targets.

Mobile is not desktop squeezed into one column. Reorder by priority, collapse secondary controls,
replace hover-only behavior, choose deliberate sheet/dialog/navigation patterns, and preserve context
through interruption, rotation, backgrounding, offline use, and process restoration where relevant.

## Complete the interaction grammar
For each reusable control, specify default, hover when supported, focus, pressed/active, selected,
disabled, loading, success, and error behavior that applies. Use the same component for the same
meaning. Motion should confirm cause and effect in roughly 150-250 ms for ordinary product actions;
longer choreography must never delay task completion and must respect reduced-motion preferences.
""",
            ),
            (
                "visual-review.md",
                """
# Visual review

Do not review only the component you remember changing. Review the complete affected path and its
neighbors so local polish does not hide broken hierarchy or system drift.

## Render a bounded evidence set
1. Capture the actual target size plus at least one compact viewport around 390 px wide and one wide
   viewport around 1440 px when web responsiveness is in scope. For native UI, use representative
   supported devices and text scaling.
2. Include realistic content: longest expected title/label, empty and populated data, validation
   error, loading/disabled state, and localization expansion when relevant.
3. Inspect keyboard order and visible focus, pointer/touch targets, contrast, reduced motion, zoom or
   dynamic text, clipping, overflow, sticky/overlay collisions, image crop, and layout shift.
4. Fix all material findings in one batch, then capture one confirmation pass. Stop after the bounded
   confirmation unless a remaining defect is visible.

## Ask these questions against the screenshots
- Can a person identify the screen's purpose, current state, and primary action in five seconds?
- Does the eye land where the design contract said it should, or do equal cards, badges, borders,
  colors, and buttons compete at the same volume?
- Is the direction specific to this subject, or would the same structure, palette, copy, and
  decoration fit ten unrelated products?
- Is there exactly one controlled signature idea, and does it survive at compact size without
  hiding content or interaction?
- Are spacing, alignment, type roles, radii, shadows, icon style, and state colors visibly coherent?
- Is every visible element real, truthful, and useful? Remove filler copy, meaningless chips,
  decorative metrics, unsupported claims, fake logos, and redundant containers.
- Do compact layouts feel intentionally recomposed rather than shrunken or mechanically stacked?
- Are controls recognizable and complete across interaction, failure, and accessibility states?

Source review catches invalid tokens and component drift; screenshots catch visual truth. Require
both when the environment supports rendering. Record any platform, viewport, state, or assistive
behavior that could not be verified instead of silently claiming completion.
""",
            ),
        ),
    ),
    BuiltinSkill(
        "server-application",
        "Use when changing a backend service, HTTP API, worker, webhook, queue consumer, or server framework lifecycle.",
        (),
        """
# Server application
- Preserve the repository's domain, delivery, persistence, and integration boundaries; framework
  handlers/controllers should translate protocols rather than become the only home of business rules.
- For HTTP/API behavior, read [HTTP service contracts](references/http-service.md).
- For background workers, webhooks, queues, scheduled work, or third-party calls, read
  [jobs and integrations](references/jobs-integrations.md).
- Use the selected framework's lifecycle, validation, dependency/resource management, migrations,
  testing, and production-server conventions. Do not introduce a parallel framework abstraction.
- Keep authentication/authorization and other hostile-boundary decisions aligned with the projected
  secure-by-design skill; correctness tests do not substitute for negative security verification.
- Verify focused domain and adapter behavior, then run the actual server startup/health and affected
  integration/contract tests using the production-like configuration path.
""",
        applies_facets=("backend-service",),
        references=(
            (
                "http-service.md",
                """
# HTTP service contracts
- Define method, path, authentication, authorization, request and response schemas, status codes,
  content types, pagination/filter ordering, error shape, idempotency, caching, and versioning as one
  coherent contract. Preserve existing clients unless migration is explicitly in scope.
- Validate and normalize at the transport boundary, then pass typed domain values inward. Distinguish
  omitted, null, empty, zero, and false values; return stable bounded errors without internal details.
- Propagate request cancellation and deadlines through database and outbound calls. Bound request body,
  upload, page, batch, concurrency, and response size; do not leave framework or proxy defaults implicit.
- Make transaction scope and side-effect order explicit. Do not hold database transactions across slow
  network calls; use idempotency/outbox or a repository-established equivalent for cross-system effects.
- Generate OpenAPI/other schemas from authoritative code or validate generated code against the
  authoritative contract. Avoid undocumented response variants and framework-specific accidental APIs.
- Test serialization and validation failures, not-found/conflict/state transitions, cancellation,
  retries, concurrent requests, idempotency, and compatibility with actual consumers.
""",
            ),
            (
                "jobs-integrations.md",
                """
# Background jobs and integrations
- Give every job one owner, durable input contract, idempotency key/effect model, timeout, retry policy,
  backoff/jitter, maximum attempts, and terminal/dead-letter/manual-repair behavior.
- Enqueue only after the required durable state commits, using an outbox or the project's established
  atomic pattern where lost/duplicate delivery matters. Assume at-least-once delivery unless proven
  otherwise and make observable effects safe under repetition.
- Version job/event payloads and preserve rollout overlap. Keep messages bounded and pass identifiers
  rather than stale or sensitive object snapshots when the consumer can load authoritative state.
- For external calls, set connection and total timeouts, propagate cancellation, classify retryable
  failures narrowly, bound concurrency, and define circuit/backpressure behavior. Do not retry unsafe
  non-idempotent operations blindly.
- Verify webhook signatures/replay behavior, provider sandbox contracts, rate-limit handling, partial
  failures, duplicate/out-of-order messages, worker shutdown, poison messages, and recovery tooling.
""",
            ),
        ),
    ),
    BuiltinSkill(
        "mobile-application",
        "Use when changing an installed Android/iOS app, Expo or React Native code, native configuration, device behavior, or mobile delivery; exclude browser-only work.",
        (),
        """
# Mobile application
This skill is for installed Android/iOS applications. Do not apply browser SEO, DOM, or Core Web
Vitals guidance merely because Expo/React Native includes React, `react-dom`, or a web compatibility
dependency.

- For Expo or React Native work, read [Expo and React Native](references/expo-react-native.md).
- For native configuration, store delivery, signing, updates, or permissions, read
  [native delivery](references/native-delivery.md).
- Preserve the current navigation, state/data ownership, design system, native-module boundary, and
  supported OS/device matrix. Keep platform differences explicit instead of hiding them behind a
  lowest-common-denominator abstraction.
- Design loading, empty, error, retry, offline, reconnect, permission-denied, background/foreground,
  process-death, and interrupted-update states as product behavior.
- Treat touch target size, screen-reader labels/order, dynamic text, contrast, motion reduction,
  safe areas, keyboard avoidance, orientation, and locale as acceptance behavior.
- Profile release builds on representative real devices. Test affected Android and iOS paths, not
  only Expo Go, a browser target, emulator, or development JavaScript runtime.
""",
        applies_facets=("mobile-app",),
        references=(
            (
                "expo-react-native.md",
                """
# Expo and React Native engineering
- Derive Expo SDK, React Native, React, Node, package-manager, New Architecture, and EAS versions from
  manifests/lock/config. Use the matching official APIs and compatibility checks; do not upgrade one
  member of the compatibility set in isolation.
- Prefer Expo modules/config plugins when they satisfy the native requirement. A library requiring
  native code, permissions, entitlements, Gradle/Pod changes, or a development build must be evaluated
  and tested as a native integration, not assumed to work because TypeScript compiles.
- Keep render state minimal and stable. Avoid effect-derived duplicate state, unstable list keys,
  unbounded contexts, JS-thread blocking work, unnecessary bridge/native calls, and animation/layout
  work that cannot meet device frame budgets.
- Use virtualized lists correctly, size/cache images deliberately, clean up subscriptions/listeners,
  cancel obsolete requests, and make query/cache persistence compatible with logout, account switch,
  schema change, and offline conflicts.
- Keep navigation params serializable and validate deep-link/external inputs before resolving routes.
  Model protected-route UX without treating client navigation as authorization.
- Test with the repository's lint/type/unit tools plus focused component/integration tests. Verify a
  development build for native modules and release builds on supported Android/iOS devices for the
  changed lifecycle, notifications, audio/camera/location, background, or performance behavior.
""",
            ),
            (
                "native-delivery.md",
                """
# Native configuration and delivery
- Keep application IDs/bundle IDs, schemes, associated domains, entitlements, Android components,
  permissions, privacy manifests/descriptions, icons/splash assets, and environment endpoints explicit
  per development/preview/production variant.
- Request only capabilities the feature needs and handle denial/revocation. Keep Android exported
  components and intent filters narrow; keep iOS URL/document/background handlers deliberate.
- Protect signing credentials and store/service accounts outside the repository. Use least-privilege
  roles, MFA, audited CI/EAS access, recoverable key custody, and documented rotation/revocation.
- Version native binaries, runtime compatibility, database/cache migrations, and OTA updates together.
  Stage rollouts, monitor crashes/startup/API compatibility, retain rollback paths, and never ship an
  update to an incompatible native runtime.
- Verify clean native generation/build when configuration changes, store-quality release artifacts for
  both platforms, install/upgrade from the prior supported version, process death, offline startup,
  notification/deep-link cold starts, rollback, and privacy/permission declarations.
""",
            ),
        ),
    ),
    BuiltinSkill(
        "godot-development",
        "Use when changing any Godot project behavior, including gameplay, input, UI, localization, scenes, resources, rendering, performance, persistence, or export.",
        (),
        """
# Godot development
- First derive the Godot version, renderer, target platforms, GDScript/C# mix, autoloads, InputMap,
  Theme/localization setup, plugins, import/export settings, and project check commands. Existing
  project conventions and architecture win. Consult current official docs only for version-specific
  API, export, renderer, import, or plugin behavior the repository does not establish.
- For gameplay, player/controller behavior, controls, input, or rebinding, read
  [gameplay and input](references/gameplay-input.md).
- For menus, HUD, settings, Control layout, themes, accessibility, or localization, read
  [UI and localization](references/ui-localization.md).
- For resolution/stretch, assets, shaders, lighting, particles, rendering, or performance, read
  [rendering and performance](references/rendering-performance.md).
- Preserve scene/node/resource ownership and lifecycle. Keep signals typed and scoped, use
  lifecycle-safe connections, avoid fragile absolute node paths, and do not retain freed nodes or
  resources.
- Keep deterministic game rules separate from presentation, raw input, audio, persistence, and
  platform adapters where that seam helps the current architecture. Keep global autoload state small.
  Use `_physics_process` for fixed-step physics/simulation and `_process` for presentation/non-physics
  work where appropriate. Make pause/time scale, collisions, ordering, randomness, and save
  compatibility explicit rather than frame-rate dependent.
- Establish the applicable input, UI/localization, and rendering foundations before feature code can
  grow around accidental defaults. Implement the smallest coherent change, then review representative
  playable behavior and profile/render only the affected path. Do not add state machines, event buses,
  pools, or genre patterns without an actual project need.
- Do not hand-edit generated/imported artifacts or create unrelated scene/resource serialization churn.
  Preserve stable resource paths/UIDs and migrate serialized/exported fields or save data deliberately.
- Completion means the affected real project path works: input/device flow, UI/focus/localized layout,
  scene/pause/persistence transitions, resolution/rendering/performance, and target export/startup only
  as touched by the change. Run repository-required checks; do not create unrelated test surface.
""",
        applies_facets=("godot-project",),
        references=(
            (
                "gameplay-input.md",
                """
# Gameplay and input

## Build the control boundary
- Gameplay consumes semantic InputMap actions, not physical key/button codes. Physical events belong
  only at device capture/rebinding and presentation boundaries.
- Map keyboard/mouse and controller to the same gameplay actions. Keep device-specific prompts/glyphs
  outside gameplay logic so changing a binding or active device does not rewrite game rules.
- Treat analog input deliberately: deadzone, response curve/sensitivity, normalization, and clamping
  must match the action. Keep movement, camera, and control tuning in named configuration/resources
  rather than scattered literals.
- Define which context owns input when gameplay, pause menus, dialogs, rebinding, overlays, or cutscenes
  overlap. Paused/menu input must not leak into gameplay, and closing UI must not replay stale input.
- Where UI exists, make its keyboard/controller focus path complete enough that core menus/settings do
  not require a pointer.
- Rebinding changes action bindings without changing gameplay code. Persist bindings and control
  settings with safe defaults/reset behavior; device disconnect or switching must not corrupt them.
- If prompts switch to the active device, debounce noisy analog input and keep the switch presentational.

## Keep gameplay deterministic where it matters
- Separate deterministic rules from raw input and presentation when the current architecture supports
  that seam. Fixed-step physics/simulation uses the correct delta; presentation updates must not make
  outcomes depend on frame rate.
- Make pause/time scale, scene transitions, collision layers/masks, ordering, randomness/seed policy,
  and save compatibility explicit when affected.
- Prefer small components/resources and explicit ownership. Do not introduce a state machine, event
  bus, coyote time, pooling, or another genre pattern unless the actual mechanic or measured workload
  requires it.

## Verify the affected control path
- Exercise the devices and contexts the change touches: keyboard/mouse, controller, analog extremes and
  deadzone, rebind/reset, pause/menu/focus, and device disconnect/switch where supported.
- Check frame-rate variation only for mechanics that could become frame-dependent. Use the project's
  existing focused runtime/simulation checks; do not invent a parallel test harness.
""",
            ),
            (
                "ui-localization.md",
                """
# Godot UI and localization

## Build a reusable UI system
- Treat UI as a system, not independently styled Controls. Extend the existing Theme/design language;
  for greenfield UI establish a project-wide or deliberate subtree Theme and semantic type variations
  before screens multiply.
- Keep a compact set of semantic roles for typography, color, spacing, shape/depth, focus/selection,
  disabled/error/success states, and motion where used. Prefer Theme values, type variations, and
  reusable components over repeated local overrides.
- Build layout with Control/Container behavior, anchors, size flags, and intentional minimums. Design
  against the project's base design size, supported aspect ratios, and UI scale instead of one fixed
  viewport coordinate set.
- Every interactive state that can occur must be legible: default, focus, pressed/selected, disabled,
  error, and hover where a pointer exists. Keyboard/controller focus order is logical and visible;
  hover alone never carries required information.
- Keep fonts, icons, and control density coherent at target scale. Preserve an established design
  system instead of re-skinning one feature in isolation.

## Localize from the start
- Put user-visible copy into the project's translation pipeline as it is introduced. Use stable keys
  or the established source format and one authoritative translation workflow; do not scatter manual
  per-locale branches through scenes/scripts.
- Do not construct translatable sentences from concatenated fragments. Keep variables, plural/context
  needs, and word-order differences representable by the translation system.
- Layout must tolerate longer text, wrapping, changed word order, and missing/fallback translations;
  do not size controls only for the source language.
- Ensure the selected fonts/fallbacks cover glyphs for target locales. Persist locale when the product
  exposes it as a setting and use Godot's translation/locale facilities rather than per-label logic.
- Use pseudolocalization or equivalent expansion checks for meaningful UI work. Add RTL/CJK-specific
  handling only when target locales require it, but do not close the architecture against it.

## Verify the affected UI path
- Review representative menu/HUD/settings screens at supported aspect ratios and UI scales with
  expanded localized copy, focus navigation, and relevant disabled/error/selected states.
- Exercise locale switching/fallback only where supported by the product. Keep verification bounded to
  the changed UI path and repository-required checks.
""",
            ),
            (
                "rendering-performance.md",
                """
# Rendering and performance

## Establish the rendering contract early
- Derive target hardware/platforms, renderer, 2D/3D path, base design resolution/stretch policy,
  target frame rate/frame-time budget, quality range, and material memory constraints from product and
  repository evidence. Do not wait for late optimization to discover these constraints.
- Make resolution, aspect-ratio, stretch, render scale, and UI scaling behavior intentional and check
  representative targets early. Preserve pixel-art or high-DPI rules the project already establishes.
- Treat import settings as part of the asset pipeline: texture size/compression/filtering/mipmaps,
  mesh/animation/audio choices, and platform overrides should fit the targets. Never hand-edit generated
  import artifacts.
- Keep rendering features and quality tiers coherent. Expensive lighting, shadows, post-processing,
  viewports, shaders, and particles need a deliberate lower-end or disabled path when target hardware
  requires one.

## Keep hot paths bounded
- Avoid unbounded work in `_process`/`_physics_process`: repeated tree searches, transient allocation,
  excessive signals/physics queries, synchronous resource I/O, or repeated C#↔Godot crossings need a
  concrete reason on a hot path. Cache stable references and size work to the scene.
- Bound draw calls/overdraw, lights/shadows, particles, shader/viewport effects, animation, and physics
  according to the real scene and target. Load/preload/stream resources according to lifetime and hitch
  risk; do not add pooling until measured churn makes it the simplest fix.
- Design performance-sensitive ownership and work bounds proactively, but micro-optimize only from
  measurements. Profile representative gameplay with Godot's Profiler/Visual Profiler or the relevant
  platform/.NET profiler when engine tooling does not expose the bottleneck.
- Measure on target-like hardware/configuration, fix the highest material bottleneck, then measure again.
  An empty test scene or editor-only result is not representative evidence.

## Verify the affected render path
- Inspect the representative affected scene for frame/physics/render time, memory/loading hitches, and
  visual output at target resolution/quality as relevant.
- Exercise startup/transition/loading and target export/release behavior only when renderer, imports,
  shaders, particles, platform settings, or quality configuration changed.
""",
            ),
        ),
    ),
    BuiltinSkill(
        "deployment-operations",
        "Use when changing reverse proxies, system services, host deployment automation, rollout, or operational recovery.",
        (),
        """
# Deployment operations
- Read [Linux service and edge operations](references/linux-service-edge.md) for Nginx, systemd,
  Ansible, host-level deployment, or reverse-proxy changes.
- Treat rendered configuration and the actual target versions as evidence. Validate in an isolated or
  staging scope before touching a live host; this skill does not authorize production access.
- Keep environment inventory, service ownership, filesystem paths, users/groups, ports, certificates,
  secrets, data locations, health checks, and rollback artifacts explicit and project-scoped.
- Make automation idempotent and safely repeatable. Bound retries/timeouts, fail on partial deployment,
  and never hide destructive data/config replacement inside an ordinary restart.
- Define readiness, graceful drain/shutdown, deployment order, migration compatibility, rollback or
  forward repair, and post-deploy user-visible verification before changing the rollout path.
""",
        applies_facets=("deployment-ops",),
        references=(
            (
                "linux-service-edge.md",
                """
# Linux service and edge operations
- For systemd, use a dedicated least-privilege identity, explicit working/data/runtime directories,
  predictable environment/config loading, restart behavior matched to failure semantics, bounded start/
  stop timeouts, correct readiness type, journal identifiers, and hardening compatible with required I/O.
- Keep secrets out of unit files and command lines. Validate ownership/modes for executables, config,
  environment files, sockets, upload/data directories, logs, and deployment artifacts before restart.
- For Nginx/reverse proxies, preserve client identity only from trusted hops, set connection/header/body/
  upstream timeouts and size limits, validate upstream TLS where used, handle WebSocket/SSE buffering,
  and expose only intended routes. Keep admin/status/origin endpoints private.
- Terminate TLS with maintained protocols/ciphers and automated certificate renewal plus expiry alerts.
  Configure redirects/HSTS only after confirming all required subdomains and recovery behavior.
- In Ansible or equivalent automation, pin collections/roles, target an explicit inventory, use check/
  diff safely, protect vault material, avoid shell when a typed idempotent module exists, notify restarts
  only on change, and serialize stateful operations that cannot overlap.
- Validate syntax and rendered configuration (`nginx -t`, `systemd-analyze verify`, Ansible syntax/check
  mode where meaningful), then test failure/restart, health/readiness, logs, permissions, certificate
  renewal, rollback, and host reboot behavior in the closest safe environment.
""",
            ),
        ),
    ),
    BuiltinSkill(
        "project-architecture",
        "Use when creating or restructuring project boundaries, modules, or ownership, recording an ADR, or changing architecture for measured load, latency, capacity, or availability.",
        (),
        """
# Project architecture
Use this skill for a new project, a new subsystem, or a material boundary change.
- Read repository instructions, architecture/ADRs, manifests, neighboring modules, and deployment
  constraints before selecting a structure. Existing accepted boundaries outrank generic patterns.
- For greenfield work, begin from product use cases, data ownership, external contracts, expected
  scale/failure modes, and team/tooling constraints. Choose the simplest architecture that meets
  them; do not cargo-cult layers, microservices, repositories, buses, or dependency injection.
- Give each module one coherent responsibility and an explicit public API. Keep dependency
  direction, composition roots, configuration, side effects, and external adapters visible; avoid
  cycles and hidden cross-module state.
- Define error, validation, transaction, concurrency, timeout/cancellation, idempotency, and
  observability behavior at boundaries before scattering implementations across modules.
- Keep domain policy independent of delivery/storage/framework details where that separation pays
  for itself, but do not wrap stable libraries with empty abstractions.
- Design for test seams and replaceable external boundaries without duplicating production logic.
  Add boundary/architecture tests only for rules important enough to prevent recurring drift.
- When a change durably alters a public contract, data model, protocol, security model, or
  operational boundary, read [architecture decisions](references/architecture-decisions.md).
- When measured load, latency, throughput, capacity, or availability drives the change, read
  [scalability](references/scalability.md).
- Recheck the complete dependency graph and production operations after implementation; a tidy
  folder tree alone is not evidence of sound architecture.
""",
        applies_facets=("software-project",),
        references=(
            (
                "architecture-decisions.md",
                """
# Architecture decisions
Use this reference when a change alters a durable boundary, data model, protocol, security model, or
operational contract and the decision may need an ADR.
- Read existing architecture docs and ADRs before designing.
- Prefer the smallest design that preserves existing invariants.
- Record an ADR only for durable decisions, not routine implementation detail.
- State context, decision, alternatives rejected, consequences, migration/rollback implications, and
  verification boundary.
- Keep code, docs, and ADR terminology consistent. Do not invent behavior that authoritative
  evidence does not establish.
""",
            ),
            (
                "scalability.md",
                """
# Scalability architecture
- Start from measured workload, latency, throughput, durability, and failure requirements.
- Prefer the simplest architecture that meets the current envelope; do not add unused distributed
  machinery.
- Introduce caches, queues, replicas, sharding, or async pipelines only with an explicit bottleneck
  and invalidation/failure semantics.
- Define concurrency, idempotency, backpressure, retry, timeout, and overload behavior at external
  boundaries.
- Benchmark or load-test the relevant path and record assumptions that materially affect sizing.
""",
            ),
        ),
    ),
    BuiltinSkill(
        "language-engineering",
        "Use when changing source code in a supported language; apply the affected language's correctness, runtime, package, tooling, and compatibility conventions.",
        (),
        """
# Language engineering
Use the repository's selected language/runtime versions, package manager, lockfiles, formatter,
analyzers, test runner, and conventions. Do not replace working toolchains with personal defaults.

Read only the reference for each language crossed by the current change:
- [Python](references/python.md)
- [JavaScript and TypeScript](references/javascript-typescript.md)
- [Go](references/go.md)
- [Rust](references/rust.md)
- [Java and Kotlin](references/jvm.md)
- [C# and .NET](references/dotnet.md)
- [PHP](references/php.md)
- [Ruby](references/ruby.md)
- [C and C++](references/c-cpp.md)
- [GDScript](references/gdscript.md)
- [Shell](references/shell.md)
- [Swift](references/swift.md)
- [SQL](references/sql.md)

For polyglot boundaries, read every involved reference and verify serialization, error,
cancellation, time, numeric, and nullability semantics on both sides. Preserve public/package API
compatibility and run the repository's focused checks plus required gates.
""",
        applies_languages=(
            "c",
            "cpp",
            "csharp",
            "go",
            "gdscript",
            "java",
            "javascript",
            "kotlin",
            "php",
            "python",
            "ruby",
            "rust",
            "shell",
            "sql",
            "svelte",
            "swift",
            "typescript",
            "vue",
        ),
        applies_facets=("software-project",),
        references=(
            (
                "python.md",
                """
# Python engineering
- Derive the supported Python range and dependency workflow from `pyproject.toml`, lockfiles, CI,
  and deployment. Do not use syntax or stdlib behavior outside that range.
- Keep import direction and package boundaries explicit; avoid import-time I/O, mutable module
  globals, circular imports, and hidden application startup side effects.
- Add precise type annotations at changed public boundaries and keep the configured type checker
  green. Validate untyped external data at the edge instead of spreading `Any` or unchecked casts.
- Preserve sync/async boundaries. Never block an event loop with synchronous I/O; propagate
  cancellation/timeouts, close async resources, and do not create orphan background tasks.
- Use context managers for resource lifetime, intentional exception types/chaining, timezone-aware
  timestamps, and decimal/integer representations where binary float would violate domain rules.
- Keep dependency declarations and locks synchronized; separate runtime, development, and optional
  extras according to the repository's existing packaging model.
- Run the configured formatter/linter, type checker, unit tests, and affected integration tests.
  Test exception, cancellation, serialization, and concurrency paths changed by the work.
""",
            ),
            (
                "javascript-typescript.md",
                """
# JavaScript and TypeScript engineering
- Honor the declared Node/browser targets, package manager, lockfile, workspace layout, and
  ESM/CommonJS contract. Do not mix module systems or browser/server-only APIs accidentally.
- For TypeScript, preserve strictness and model boundary data as `unknown` until validated. Avoid
  widening public types, unsafe assertions, non-null assertions, and exported implementation types.
- Keep side effects and state ownership explicit. Avoid mutable singletons, import-time network I/O,
  accidental shared state between requests, and duplicated client/server business rules.
- Await or deliberately supervise every promise. Propagate errors, `AbortSignal` cancellation, and
  timeouts; prevent unhandled rejections, stale UI responses, and fire-and-forget work without an
  owner.
- Preserve package exports, tree-shaking, and runtime compatibility. Add dependencies only after
  checking platform capabilities and bundle/server cost; update the existing lockfile exactly.
- Use safe DOM/text APIs, validate external JSON and environment configuration, and distinguish
  absent, `null`, empty, and false values according to the domain contract.
- Run the configured formatter/linter, type checker, unit tests, production build, and affected
  browser/server integration tests. Check failure, cancellation, hydration, and serialization paths.
""",
            ),
            (
                "go.md",
                """
# Go engineering
- Honor the module's declared Go/toolchain version and existing package boundaries. Keep packages
  cohesive, avoid import cycles, and expose the smallest useful API.
- Accept `context.Context` at request/work boundaries, propagate cancellation/deadlines, and never
  store contexts in structs. Give every goroutine a clear owner, stop condition, and joined result.
- Handle every meaningful error; wrap with `%w` when callers need `errors.Is/As`, add operation
  context without leaking secrets, and do not use panic for expected failures.
- Make zero values, nil behavior, interface ownership, copying, and slice/map aliasing intentional.
  Close resources once and define concurrency protection next to the state it guards.
- Preserve wire/JSON/database field compatibility and distinguish omitted versus zero values where
  the contract needs it. Avoid nondeterministic map-order assumptions.
- Run `gofmt`, the repository's analyzer/vet/lint policy, `go test` for affected packages, and the
  race detector for changed concurrent code when the target supports it. Add fuzz/property tests
  for parsers and boundary-heavy logic when they materially improve coverage.
""",
            ),
            (
                "rust.md",
                """
# Rust engineering
- Honor `rust-toolchain`, MSRV, edition, Cargo features, workspace boundaries, and lockfile policy.
  Do not raise the compiler floor or alter default features accidentally.
- Model ownership and lifetimes directly before adding clones, reference counting, interior
  mutability, or broad locks. Keep public trait bounds and generic complexity proportional.
- Return meaningful `Result` errors at recoverable boundaries; reserve panic/`unwrap`/`expect` for
  proven invariants and tests. Preserve error sources without exposing sensitive data.
- Avoid `unsafe`; when it is required, minimize the block, document the exact invariants, and add
  tests/tools appropriate to memory, aliasing, and concurrency risk.
- Make cancellation, task ownership, blocking work, `Send`/`Sync`, lock ordering, and backpressure
  explicit in async/concurrent code. Do not hold blocking locks across `.await`.
- Preserve serde/wire/schema and semver compatibility for public crates. Test relevant feature
  combinations rather than only the default feature set.
- Run the pinned formatter, Clippy policy, unit/integration/doc tests, and affected feature/target
  checks; use Miri, sanitizers, or fuzzing when the changed risk warrants them.
""",
            ),
            (
                "jvm.md",
                """
# Java and Kotlin engineering
- Honor the declared JDK/Kotlin versions, Gradle/Maven wrapper, dependency locks/catalogs, module
  boundaries, compiler flags, and framework conventions. Do not bypass the repository wrapper.
- Preserve public binary/source compatibility where consumers require it. Keep nullability,
  generics, checked/unchecked exceptions, records/data classes, and Java/Kotlin interop explicit.
- Use structured resource lifetime (`try`-with-resources/`use`) and deliberate transaction scope.
  Do not leak executors, threads, streams, database handles, or coroutine scopes.
- Propagate interruption/cancellation and timeouts. Use structured concurrency/coroutine ownership
  where available; avoid blocking event-loop/dispatcher threads and unbounded executor queues.
- Keep dependency injection and framework annotations at composition/boundary layers; do not hide
  domain behavior in lifecycle callbacks, reflection, global statics, or magic configuration.
- Preserve serialization/database compatibility and make timezone, locale, decimal, and collection
  mutability choices explicit.
- Run wrapper-based formatting/static analysis, unit and affected integration tests, packaging, and
  compatibility checks. Test concurrency and transaction failure paths changed by the work.
""",
            ),
            (
                "dotnet.md",
                """
# C# and .NET engineering
- Honor the target frameworks, SDK pin (`global.json` when present), nullable context, analyzers,
  solution/project boundaries, central package management, and lock policy.
- Keep nullable reference types accurate; validate external options/input at startup or the boundary
  and avoid null-forgiving operators that merely silence an unproved state.
- Keep async flows async, propagate `CancellationToken`, use bounded timeouts, avoid sync-over-async,
  and observe background task failures through an owned hosted lifecycle.
- Dispose `IDisposable`/`IAsyncDisposable` resources correctly. Keep DI lifetimes compatible and do
  not capture scoped services in singletons or retain request state globally.
- Preserve public API, JSON, database, culture/timezone, decimal, and exception semantics. Use
  framework validation/authorization at the correct server-side boundary.
- Keep configuration and secrets outside compiled artifacts; use typed options and fail startup on
  missing unsafe production settings.
- Run the pinned formatter/analyzers, build with warnings policy, unit/affected integration tests,
  publish/trim/AOT checks when used by the project, and concurrency/cancellation failure tests.
""",
            ),
            (
                "php.md",
                """
# PHP engineering
- Honor the Composer platform PHP version, extensions, lockfile, autoloading, framework conventions,
  and configured coding/static-analysis standards.
- Preserve the project's strictness contract. Use precise parameter/return/property types and
  `strict_types=1` for new compatible modules when consistent; do not toggle it blindly in legacy
  files with coercion-dependent callers.
- Validate request, CLI, environment, and deserialized data at boundaries. Use parameterized data
  access, contextual output escaping, and server-side authorization.
- Keep service lifetime and mutable state safe for the actual runtime (request-per-process or
  long-lived workers). Reset per-request state and close/rollback resources on every failure path.
- Preserve public APIs, array/object shapes, serialization, database, timezone, and money semantics;
  avoid loose comparisons where type juggling can change domain behavior.
- Update Composer constraints and lockfile together. Run the repository formatter/style checks,
  static analyzer, unit/affected integration tests, and production autoload/container build.
""",
            ),
            (
                "ruby.md",
                """
# Ruby engineering
- Honor the declared Ruby version, Bundler lock/platforms, gem groups, framework conventions,
  autoloading mode, and configured formatter/linter/type tooling.
- Keep object mutation, callbacks, metaprogramming, monkey patches, and global/thread-local state
  explicit. Prefer the nearest established abstraction over hidden DSL behavior.
- Rescue only exceptions the boundary can handle; preserve causes and backtraces, ensure resources
  and transactions close/rollback, and do not use exceptions as ordinary control flow.
- Keep request/job state safe across threads/processes and retries. Make job idempotency, timeout,
  retry, transaction, and after-commit behavior explicit.
- Preserve method keyword arguments, hashes/JSON, database, timezone, decimal, and nil/false
  semantics across supported Ruby/framework versions.
- Update Gemfile and lockfile consistently. Run the repository style/static checks, unit and
  affected integration/system tests, eager-load/boot checks, and job failure/retry paths.
""",
            ),
            (
                "c-cpp.md",
                """
# C and C++ engineering
- Honor the selected language standard, compiler/platform matrix, build system, warning policy,
  ABI/export rules, and dependency lock/vendor strategy.
- Make ownership, lifetime, aliasing, nullability, bounds, integer width/sign, and initialization
  explicit. In C++, prefer RAII and project-standard value/smart-pointer types over manual lifetime.
- Check every allocation, syscall, parse, conversion, and partial I/O result. Preserve `errno` or
  error objects correctly and keep cleanup valid on all early-return paths.
- Avoid undefined behavior, data races, unsafe casts, unchecked arithmetic, and layout/alignment
  assumptions. Isolate unavoidable low-level operations behind small documented contracts.
- Preserve C ABI, binary layout, calling convention, exception boundary, serialization, and wire
  compatibility where external consumers or stored data require it.
- Run the configured formatter/static analysis, all affected build variants/tests, sanitizers for
  changed memory/concurrency code, and target/compiler compatibility checks. Add fuzzing for parsers
  and unsafe boundaries when warranted.
""",
            ),
            (
                "gdscript.md",
                """
# GDScript engineering
- Honor the project's Godot/GDScript version and typed-GDScript policy. Use only supported syntax,
  annotations, built-ins, and APIs; verify a version-specific API when repository evidence is not
  enough.
- Prefer static types on changed public or stateful boundaries when compatible with project style.
  Keep function signatures, typed arrays/dictionaries, enums, `StringName`/`NodePath`, and nullable
  states precise enough to make contracts visible without forcing noisy types where inference is clear.
- Treat exported properties, signal signatures, resource fields, and serialized scene-facing names as
  compatibility surfaces. Rename or change them deliberately and migrate affected scenes/resources or
  saved data when required.
- Keep signal/callable argument and return contracts explicit. Prefer typed signals, callables, enums,
  and resources over stringly dispatch when they fit the existing design.
- Make `await`, timer, and callback lifetime explicit: a continuation may resume after scene/state
  changes, so revalidate external/node state and keep ownership clear before mutating it.
- Prefer ordinary readable GDScript over clever dynamic property access or reflection. Keep collection
  and allocation choices proportional in frequent language-level code; engine hot-path, scene, input,
  rendering, and export policy belongs to `godot-development`.
- Run the project's parser/static-warning and focused script/unit checks plus required repository gates.
  Scene/input/render/export verification is governed by `godot-development`.
""",
            ),
            (
                "shell.md",
                """
# Shell engineering
- Honor the selected shell and platform; do not assume Bash features in POSIX `sh` scripts or GNU
  command behavior on unsupported targets. Prefer the repository's existing script entry points.
- Quote expansions, use arrays for argument lists in Bash, preserve whitespace/newlines, and avoid
  constructing commands with `eval`. Treat filenames, environment, subprocess output, and CLI input
  as untrusted data.
- Make strict-mode choices deliberately: `set -e` has contextual exceptions, pipelines need explicit
  status handling, and cleanup requires reliable traps. Do not claim a command ran merely because a
  surrounding pipeline succeeded.
- Validate destructive targets as explicit paths before mutation; avoid unresolved variables, broad
  globs, recursive operations on roots, and cleanup that can escape a task-owned temporary directory.
- Propagate exit status and signals, bound waits/retries, avoid leaking secrets through tracing or
  process arguments, and use atomic writes/locks where concurrent scripts share state.
- Run the configured formatter/linter (for example shfmt/ShellCheck), syntax checks for supported
  shells, and subprocess tests covering spaces, empty values, failures, interruption, and cleanup.
""",
            ),
            (
                "swift.md",
                """
# Swift engineering
- Honor the Swift/tools and platform deployment versions, package/project boundaries, dependency
  resolution files, compiler settings, and established Apple/server framework conventions.
- Model optionals and throwing APIs deliberately; avoid force unwrap/cast and `try!` outside proven
  invariants. Preserve value/reference semantics and copy-on-write expectations.
- Use structured concurrency, propagate cancellation, keep UI work on the correct actor, and make
  `Sendable`, actor isolation, detached tasks, and continuation completion explicit.
- Avoid retain cycles in closures/delegates and define resource lifetime for files, streams,
  observations, and tasks. Do not hide mutable global state behind singletons.
- Preserve Codable/wire/storage schemas, public API availability, timezone/locale, Decimal, and
  platform behavior across the supported matrix.
- Run the configured formatter/linter, package/project builds, unit/UI/integration tests for affected
  platforms, strict-concurrency diagnostics, and release-build checks.
""",
            ),
            (
                "sql.md",
                """
# SQL engineering
- Treat the database engine/version, schema, constraints, collations, timezone, isolation level,
  and migration tool as part of the language contract; do not write generic SQL that only happens
  to pass on a different engine.
- Parameterize values and allowlist any dynamic identifiers/operators. Preserve null, empty,
  decimal, timestamp, Unicode, and boolean semantics across the application boundary.
- Make transaction boundaries and concurrent outcomes explicit. Use constraints as the final
  integrity guard and handle uniqueness, deadlock, serialization, timeout, and retry behavior.
- Select only needed columns, avoid accidental per-row queries, and inspect representative query
  plans before adding indexes or rewriting performance-sensitive SQL. Include write/amplification
  and maintenance costs in index decisions.
- Design migrations for the real data volume and deployment overlap: expand, backfill/verify, switch,
  then contract where compatibility requires it. Bound locks and define rollback or forward repair.
- Test against the actual engine locally/CI where practical, including constraints, migrations from
  supported prior state, rollback/repair, concurrent writes, and representative query plans.
""",
            ),
        ),
    ),
    BuiltinSkill(
        "data-integrity",
        "Use when changing a database, schema, migration, ORM, transaction, query, or other durable-state behavior where data integrity matters.",
        (),
        """
# Data integrity
- Read the authoritative schema/migrations, ORM mappings, read/write paths, transaction boundaries,
  retention/backup policy, and supported deployment overlap before changing durable state.
- Put invariant enforcement at the lowest reliable layer: database constraints for durable facts,
  application validation for contextual feedback, and tests for both. Never rely on a prior read as
  the sole protection against concurrent writes.
- Define transaction isolation, lock ordering, idempotency, retry, timeout, and partial-failure
  behavior. Retry only errors known to be safe and rerun the whole transaction unit.
- Make migrations compatible with real table size and old/new application versions. Prefer staged
  expand/backfill/verify/switch/contract changes, bounded batches, resumability, and observable
  progress over one blocking irreversible step.
- Preserve data on failure. Define rollback when safe and forward repair when rollback would lose
  accepted writes; test backup/restore or snapshot recovery for changes that alter recovery needs.
- Base indexes and query changes on representative plans/workload. Account for write cost,
  selectivity, locking, storage growth, and stale-statistics behavior.
- Prevent test/dev/prod database and volume confusion. Destructive resets require exact targets and
  explicit authorization; fixtures and tests must never depend on production data.
- Verify constraints, supported-version migrations, failure/retry/concurrency paths, and application
  compatibility against the actual database engine when practical.
""",
        applies_languages=("sql",),
        applies_dependencies=(
            "@prisma/client",
            "alembic",
            "django",
            "drizzle-orm",
            "pg",
            "prisma",
            "psycopg",
            "psycopg2",
            "sequelize",
            "sqlalchemy",
            "typeorm",
            "github-com/golang-migrate/migrate/v4",
            "github-com/jackc/pgx/v5",
            "gorm-io/gorm",
            "laravel/framework",
            "mongodb",
            "mongoose",
            "mysqlclient",
            "redis",
        ),
        applies_facets=("database-backed",),
    ),
    BuiltinSkill(
        "complex-change-planning",
        "Use when planning a cross-boundary or migration-ordered change or independently reviewing a risky diff; exclude ordinary single-module bugfixes and routine test-only work.",
        (),
        """
# Complex change planning
Use Harness Task state as the source of truth. Do not create a second epic/status system unless the
repository already requires one.
- Map affected contracts, callers, callees, persistence, tests, and operational edges before editing.
- Split work into the smallest dependency-ordered slices that each leave the repository coherent.
- Identify blast radius, migration/rollback concerns, and explicit acceptance evidence for risky
  boundaries.
- Keep one current implementation slice active; checkpoint progress instead of duplicating status in
  ad-hoc files.
- Before implementing a risky or underspecified change, read
  [specification audit](references/specification-audit.md).
- Established-behavior and compatibility work uses the project-wide `legacy-preservation` Skill.
- Before publication, read [independent review](references/independent-review.md).
""",
        applies_facets=("software-project",),
        references=(
            (
                "specification-audit.md",
                """
# Specification audit
Before implementation, independently test the requested behavior against existing contracts.
- Identify the authoritative spec/ADR/API/schema and invariants the change must preserve.
- List material ambiguities, contradictions, missing failure behavior, migration concerns, and
  acceptance criteria.
- Resolve what can be proven from repository evidence; do not invent missing product decisions.
- If a gap can cause incompatible implementations or irreversible damage, stop implementation at that
  boundary and surface the blocker.
- Keep the audit concise: only findings that can change implementation or verification belong in the
  result.
""",
            ),
            (
                "independent-review.md",
                """
# Independent review
Review the finished change as if you did not implement it.
- Re-read the governing contract and inspect the complete diff plus nearby callers/callees.
- Look for stale-write races, unsafe defaults, ownership/collision mistakes, migration/recovery
  gaps, disclosure leaks, and tests that only prove the happy path.
- Classify findings by materiality. Fix correctness/safety/contract issues; do not churn code for
  taste.
- Re-run checks affected by any fix and the repository-required publication gate.
- Report verified evidence separately from assumptions, not-run checks, and real blockers.
""",
            ),
        ),
    ),
    BuiltinSkill(
        "legacy-preservation",
        "Use when changing established behavior, compatibility, or an existing runtime path; exclude greenfield-only design and an explicitly authorized rewrite.",
        (),
        """
# Legacy preservation
Treat the existing system as an evidence-bearing contract, including awkward behavior that users or
integrations may rely on.
- Before editing, read project instructions, architecture/ADRs, manifests, the target code, nearby
  callers/callees, tests, configuration, migrations, and relevant history. Trace the real runtime
  path; do not infer architecture from filenames or preferred modern patterns.
- Identify the local dependency direction, ownership boundaries, naming/error conventions,
  persistence and serialization formats, public APIs/CLI output, deployment topology, and supported
  runtime versions. Preserve them unless the task explicitly authorizes a migration.
- Capture current behavior with focused characterization, contract, or golden tests at the safest
  seam before changing poorly understood behavior. Include important failure and compatibility
  paths, not only the desired happy path.
- Make the smallest coherent change inside the nearest existing abstraction. Do not introduce a
  new framework, architectural style, duplicate service/repository layer, or parallel configuration
  mechanism in one corner merely because it would suit a greenfield design.
- Separate behavior changes from cleanup where practical. Do not opportunistically rename, move,
  reformat, upgrade dependencies, or "fix" historical quirks without evidence and authorization.
- When replacement is necessary, use an explicit adapter or migration seam, preserve backward
  compatibility for the required rollout window, and define data/config/API rollback or forward
  repair. Never assume old and new versions deploy atomically.
- Delete old paths only after proving callers, stored data, jobs, integrations, and rollback needs
  no longer require them. Keep deprecation observable and time-bounded.
- Run the repository's focused compatibility tests and required gates, then review the full diff
  specifically for architectural drift and unintended surface changes.
""",
        applies_facets=("software-project",),
    ),
)


def sync_builtin_skills(registry_root: Path) -> BuiltinSkillSyncResult:
    _prepare_registry(registry_root)
    manifest_path = registry_root / _BUILTIN_MANIFEST_NAME
    owned = _load_manifest(manifest_path)
    desired = {skill.skill_id: _tree_sha256(skill.files()) for skill in BUILTIN_SKILLS}
    stale_ids = tuple(sorted(set(owned) - desired.keys()))
    replacements = []
    installed = updated = unchanged = adopted = retired = released = 0
    try:
        for skill in BUILTIN_SKILLS:
            target = registry_root / skill.skill_id
            files = skill.files()
            wanted = desired[skill.skill_id]
            current = _directory_sha256(target) if _path_exists(target) else None
            recorded = owned.get(skill.skill_id)
            if current == wanted:
                unchanged += 1
                adopted += int(recorded != wanted)
                owned[skill.skill_id] = wanted
                continue
            if current is not None and recorded != current:
                raise BuiltinSkillCollisionError(
                    f"built-in skill collides with user-owned or modified content: {skill.skill_id}"
                )
            replacements.append(_materialize_replacement(registry_root, target, files))
            owned[skill.skill_id] = wanted
            if current is None:
                installed += 1
            else:
                updated += 1
        for skill_id in stale_ids:
            target = registry_root / skill_id
            recorded = owned[skill_id]
            if not _path_exists(target):
                del owned[skill_id]
                continue
            current = _directory_sha256(target)
            del owned[skill_id]
            if current == recorded:
                replacements.append(_retire_owned_directory(registry_root, target))
                retired += 1
            else:
                released += 1
        _write_manifest(manifest_path, owned)
    except Exception:
        rollback_error = _rollback_replacements(replacements)
        if rollback_error is not None:
            message = "built-in skill sync failed and prior registry state could not be restored"
            detail = str(rollback_error)
            if "preserved at" in detail:
                message = f"{message}; {detail}"
            raise BuiltinSkillError(message) from rollback_error
        raise
    _finalize_replacements(replacements)
    return BuiltinSkillSyncResult(
        installed,
        updated,
        unchanged,
        adopted,
        retired,
        released,
        tuple(s.skill_id for s in BUILTIN_SKILLS),
    )


def _prepare_registry(root: Path) -> None:
    try:
        root.parent.mkdir(parents=True, exist_ok=True, mode=_BUILTIN_DIR_MODE)
        pm = root.parent.lstat()
        if stat.S_ISLNK(pm.st_mode) or not stat.S_ISDIR(pm.st_mode):
            raise BuiltinSkillError("skill registry parent must be a real directory")
        if hasattr(os, "geteuid") and pm.st_uid != os.geteuid():
            raise BuiltinSkillError("skill registry parent must be owned by the current user")
        if stat.S_IMODE(pm.st_mode) & 0o022:
            raise BuiltinSkillError(
                "skill registry parent must not have group or other write access"
            )
        root.mkdir(exist_ok=True, mode=_BUILTIN_DIR_MODE)
    except OSError as exc:
        raise BuiltinSkillError("skill registry cannot be prepared") from exc
    try:
        validate_skill_registry_trust(root)
    except FileNotFoundError as exc:
        raise BuiltinSkillError("skill registry cannot be prepared") from exc
    except SkillRegistryError as exc:
        raise BuiltinSkillError(str(exc)) from exc


def _load_manifest(path: Path) -> dict[str, str]:
    try:
        m = path.lstat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise BuiltinSkillError("built-in skill manifest cannot be inspected") from exc
    if stat.S_ISLNK(m.st_mode) or not stat.S_ISREG(m.st_mode):
        raise BuiltinSkillError("built-in skill manifest must be a real file")
    try:
        payload = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuiltinSkillError("built-in skill manifest is invalid") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"version", "skills"}
        or payload["version"] != 1
        or not isinstance(payload["skills"], dict)
    ):
        raise BuiltinSkillError("built-in skill manifest has unsupported version or shape")
    result = {}
    for skill_id, digest in payload["skills"].items():
        if not isinstance(skill_id, str) or not isinstance(digest, str) or len(digest) != 64:
            raise BuiltinSkillError("built-in skill manifest contains invalid ownership data")
        result[skill_id] = digest
    return result


def _write_manifest(path: Path, owned: dict[str, str]) -> None:
    payload = (
        json.dumps(
            {"version": 1, "skills": dict(sorted(owned.items()))},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    fd = -1
    temporary = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        os.fchmod(fd, _BUILTIN_FILE_MODE)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise BuiltinSkillError("built-in skill manifest could not be persisted") from exc


def _retire_owned_directory(root: Path, target: Path) -> _Replacement:
    _require_real_skill_directory(target)
    backup = Path(tempfile.mkdtemp(prefix=f".{target.name}.builtin-backup-", dir=root))
    backup.rmdir()
    os.replace(target, backup)
    return _Replacement(target, backup)


def _materialize_replacement(root: Path, target: Path, files: dict[str, bytes]) -> _Replacement:
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.builtin-stage-", dir=root))
    os.chmod(stage, _BUILTIN_DIR_MODE)
    try:
        for name, payload in files.items():
            p = stage / name
            p.parent.mkdir(parents=True, exist_ok=True, mode=_BUILTIN_DIR_MODE)
            p.write_bytes(payload)
            os.chmod(p, _BUILTIN_FILE_MODE)
        backup = None
        if _path_exists(target):
            _require_real_skill_directory(target)
            backup = Path(tempfile.mkdtemp(prefix=f".{target.name}.builtin-backup-", dir=root))
            backup.rmdir()
            os.replace(target, backup)
        try:
            os.replace(stage, target)
        except Exception:
            if backup is not None and not _path_exists(target):
                os.replace(backup, target)
            raise
        return _Replacement(target, backup)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _rollback_replacements(items: Sequence[_Replacement]) -> Exception | None:
    first_error: Exception | None = None
    for item in reversed(items):
        try:
            _restore_replacement(item)
        except (OSError, BuiltinSkillError) as exc:
            if first_error is None:
                first_error = exc
    return first_error


def _restore_replacement(item: _Replacement) -> None:
    backup = item.backup
    try:
        if _path_exists(item.target):
            _remove_path(item.target)
        if backup is not None and _path_exists(backup):
            os.replace(backup, item.target)
    except (OSError, BuiltinSkillError) as exc:
        if backup is None:
            raise
        raise BuiltinSkillError(
            f"built-in skill registry entry could not be restored; preserved at {backup}"
        ) from exc


def _finalize_replacements(items: Sequence[_Replacement]) -> None:
    for item in items:
        if item.backup is not None and _path_exists(item.backup):
            shutil.rmtree(item.backup, ignore_errors=True)


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise BuiltinSkillError(f"skill registry entry cannot be inspected: {path.name}") from exc
    return True


def _remove_path(path: Path) -> None:
    m = path.lstat()
    shutil.rmtree(path) if stat.S_ISDIR(m.st_mode) and not stat.S_ISLNK(
        m.st_mode
    ) else path.unlink()


def _require_real_skill_directory(path: Path) -> None:
    try:
        m = path.lstat()
    except OSError as exc:
        raise BuiltinSkillError(f"skill directory cannot be inspected: {path.name}") from exc
    if stat.S_ISLNK(m.st_mode) or not stat.S_ISDIR(m.st_mode):
        raise BuiltinSkillCollisionError(f"skill registry entry is unsafe: {path.name}")


def _directory_sha256(path: Path) -> str:
    _require_real_skill_directory(path)
    files: dict[str, bytes] = {}

    def raise_walk_error(error: OSError) -> None:
        raise error

    try:
        for root, directories, filenames in os.walk(
            path,
            topdown=True,
            onerror=raise_walk_error,
            followlinks=False,
        ):
            root_path = Path(root)
            for name in sorted(directories):
                child = root_path / name
                m = child.lstat()
                if stat.S_ISLNK(m.st_mode) or not stat.S_ISDIR(m.st_mode):
                    raise BuiltinSkillCollisionError(
                        f"built-in skill contains unexpected directory content: {path.name}"
                    )
            if root_path != path and not directories and not filenames:
                raise BuiltinSkillCollisionError(
                    f"built-in skill contains an empty directory: {path.name}"
                )
            for name in sorted(filenames):
                child = root_path / name
                m = child.lstat()
                if stat.S_ISLNK(m.st_mode) or not stat.S_ISREG(m.st_mode):
                    raise BuiltinSkillCollisionError(
                        f"built-in skill contains unexpected non-file content: {path.name}"
                    )
                files[child.relative_to(path).as_posix()] = child.read_bytes()
    except OSError as exc:
        raise BuiltinSkillError(f"skill directory cannot be listed: {path.name}") from exc
    return _tree_sha256(files)


def _tree_sha256(files: dict[str, bytes]) -> str:
    d = hashlib.sha256()
    for name, payload in sorted(files.items()):
        e = name.encode()
        d.update(len(e).to_bytes(4, "big"))
        d.update(e)
        d.update(len(payload).to_bytes(8, "big"))
        d.update(payload)
    return d.hexdigest()
