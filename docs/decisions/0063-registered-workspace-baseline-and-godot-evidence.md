# ADR-0063: Registered Git Workspaces get the quality baseline; Godot evidence is nested-safe

- **Status:** Accepted
- **Date:** 2026-09-06
- **Deciders:** Repository architecture baseline
- **Builds on:** [ADR-0042](0042-project-stack-skill-selection.md),
  [ADR-0045](0045-project-skill-scope-policy.md)
- **Amended by:** [ADR-0064](0064-opt-in-workspace-init-and-skill-include.md) Decision 4: non-Git
  directories are registerable via `harness init`; Dashboard skill scope includes `Included`.

## Context

Skill projection uses only the detected Workspace stack plus Project Auto/Excluded surface policy.
`software-project` was derived from indexed languages, software manifests, or specialized facets.
A newly registered empty Git worktree therefore projected nothing, even though Dashboard copy said
the quality baseline stays on and operators had no Include control (ADR-0045 is Auto/Excluded only).

Godot detection required an indexed file named `project.godot` for the `godot-project` facet.
GDScript (`.gd`) only contributed the `gdscript` language. Nested `godot/project.godot` already
matched by filename when indexed, but CMake/GDExtension trees also use `.gdextension` / `.tscn`
and `CMakeLists.txt`. Watcher directory sampling excluded `build/` by exact name only, so
`build-godot/` and `build-sanitize/` were walked and could delay index/skill convergence.

## Decision

1. `detect_workspace_stack` always adds `software-project` for a registered Workspace, including
   an empty index. The six core built-ins therefore project after `harness init` of a Git worktree
   or ordinary folder with no source yet. Specialized facets (`godot-project`, `backend-service`,
   and the rest of the ADR-0045 managed set) still require indexed evidence or Dashboard Include.
   Task `stack_hints` remain non-selectors.
2. `godot-project` is derived from indexed `project.godot` (any directory), `.gdextension`, `.gd`,
   `.tscn`, and `.gdshader`. `CMakeLists.txt` is a software manifest and therefore also baseline
   evidence when it is the only file.
3. Watcher metadata directory walks skip the existing exact names, plus directories whose names
   case-insensitively start with `build-` or `cmake-build-` and that look like CMake build trees
   (`CMakeCache.txt` or `CMakeFiles`). Index candidate selection continues to honor Git and
   `.harnessignore`; this only keeps change sampling off generated CMake trees.
4. Non-Git directories remain unregisterable through `harness scan`. Binding them is `harness init`
   ([ADR-0064](0064-opt-in-workspace-init-and-skill-include.md)). Dashboard skill scope is
   Auto/Included/Excluded.

## Consequences

- `harness init` of an empty Git worktree or ordinary folder delivers the quality baseline into
  `.agents/skills` before the first source file exists. Adding `project.godot` or GDScript/scenes
  still unlocks `godot-development`; Dashboard Include can do the same before those files exist.
- A README-only or empty registered Workspace is a software project for Skill purposes. Specialized
  FastAPI/Godot/Expo Skills still do not appear from Task hints alone.
- Nested Godot under `godot/` and CMake GDExtension layouts match without a root-level
  `project.godot`.
- ADR-0045 gains `Included` via [ADR-0064](0064-opt-in-workspace-init-and-skill-include.md).

## Verification

Automated tests must prove:

- an empty Git Workspace detects `software-project` and resolves the six core built-ins, not
  `godot-development`;
- nested `godot/project.godot` plus `.gdextension` / `.gd` detect `godot-project`;
- `.gd` without `project.godot` still detects `godot-project`;
- `CMakeLists.txt` alone is enough for `software-project`;
- watcher directory listing omits CMake `build-godot/` and `cmake-build-*` trees but still sees
  a non-CMake `build-scripts/` source directory;
- Task `stack_hints` still do not activate specialized Skills before their evidence exists.
