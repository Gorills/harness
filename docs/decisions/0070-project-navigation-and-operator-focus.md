# ADR-0070: Consistent project navigation and operator focus

- **Status:** Accepted for implementation
- **Date:** 2026-09-23
- **Amends:** ADR-0020, ADR-0065, ADR-0069

## Context

The project hub introduced project-local notes, but Workspace and Task pages did not expose
project sections. The project breadcrumb sometimes led to a Workspace instead of the Project.
Entering notes also lost the source Task and could choose another Workspace on return.
Management forms occupied the primary work area; narrow Task layouts placed decisions below
the entire timeline.

## Decision

1. Render one shared Project navigation in the dashboard shell for overview, Workspace Task
   lists, Task detail, notes and settings. The sections are Overview, Tasks, Notes/access and
   Settings. Breadcrumb labels always match their destination. A Workspace list provides a
   sibling-Workspace switcher when the Project has several folders. Existing Task and Workspace
   URLs and their domain meaning remain unchanged.
2. Add `/projects/{id}/settings/` as a presentation of the existing Project services. It owns
   visibility, skill policy, relocation entry points and Project deletion. It retains the
   Project SSE fingerprint and existing POST identity/revision/origin checks. Existing Project
   POST URLs remain supported. The old `#skill-scope` bookmark exposes a link to its new location.
   An unavailable Workspace keeps its relocation form as the immediate recovery action.
3. Notes links from Task/Workspace pages carry optional public `workspace` and `task` identities.
   The dashboard rejects unknown/duplicate fields, missing or mismatched identities, and foreign
   Project context before launching the vault. A Task requires its matching Workspace. The wrapper
   supplies a direct return link to that exact Task. These navigation identities are never sent
   to the private frame; its existing fragment contains only Project identity/name. The parent
   still runs no JavaScript/SSE on vault pages and never reads private records or drafts.
4. Place Task result, reported checks and operator decisions before history in reading order.
   State changes, deployment markers and facts use disclosures. Keep all mutation forms and
   authoritative revision tokens; this is presentation, not a new workflow/state model.
5. Dashboard navigation warns before leaving unsaved POST fields or an in-flight save. Search
   text does not trigger a navigation warning. Existing in-place refresh/recovery continues to
   preserve drafts. No new persistent browser store is introduced. Private vault drafts remain
   exclusively inside their own origin and retain their existing unload protection.
6. Missing routes and failed navigation show useful recovery links instead of blank pages.
   Global vault selection and Project section selection expose accurate current-state semantics.
   Tabs remain reachable while scrolling, wrap on narrow screens, and retain native link behavior.
7. On narrow vault screens, show the record list or the selected record/editor as separate
   views. The explicit return action preserves filters and asks before discarding a dirty
   editor; cancellation keeps the editor and its draft. Returning clears private detail fields.
   Wide screens retain the simultaneous list/detail layout. An empty dashboard prioritizes
   the existing project-registration instruction and access to the personal vault.

## Verification

HTTP regression tests traverse each section, two Workspaces of one Project, source Task return,
settings POST/303 and error recovery, legacy mutations, empty Projects, and invalid/cross-Project
vault parameters. Node tests execute the actual draft-navigation guard and private-origin UI.
Existing dashboard HTTP/SSE/CAS/history/disclosure tests remain required. Visual acceptance uses
synthetic data; real credentials and canonical user state are unnecessary. Responsive layout proof
is reported separately from native-browser viewport emulation when the host cannot resize it.
