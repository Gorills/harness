# ADR-0075: Preserve existing Codex per-tool approval preferences

- **Status:** Accepted for implementation
- **Date:** 2026-10-01
- **Amends:** ADR-0030, ADR-0037

## Context

The authorized live dashboard upgrade was blocked during Codex preflight: an otherwise
Harness-owned project config had an existing `mcp_servers.harness.tools.project_status.approval_mode`
preference. The strict owned-shape check treated the supported Codex setting as unknown content.
The installed package updated, but the old daemon continued serving the old dashboard.

The [official Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference),
checked on 2026-10-01, documents `mcp_servers.<id>.tools.<tool>.approval_mode` with values
`auto`, `prompt`, `writes`, and `approve`. Harness must preserve the operator's existing choice;
installation must not create or change an approval preference.

## Decision

- Recognize only a `tools` table whose named entries each contain exactly the documented
  `approval_mode` string with a supported value. Other content or malformed preferences remain
  a preflight collision. Core URL, headers, bootstrap, ownership and tracked-file checks stay exact.
- An already-current config with these preferences remains byte-unchanged. When an owned config
  needs its integration fields updated, carry every existing preference into the generated TOML,
  with quoted tool keys and no default approval entries.
- Manually adopted config remains user-owned and is not rewritten.
- Reconciliation and removal have different shape checks. Cleanup refuses to delete an owned
  config carrying user approval preferences, before removing any companion instruction or marker.
  The operator must resolve these preferences before uninstalling that integration.
- Preserve existing filesystem compare-and-replace checks and private file permissions.
  This amendment does not add automatic tool approval, widen access or change host policy.

## Verification

Test all four modes, unchanged-file identity, capability refresh, Hidden/Normal transitions,
quoted Unicode keys, subsequent current-state detection, refusal of unknown fields/values and
non-deletion of the config and companion files during cleanup. Retain the complete adapter and
installation lifecycle suites. Re-run the authorized Codex global installation and verify the
daemon code fingerprint and dashboard presentation through the installed runtime.
