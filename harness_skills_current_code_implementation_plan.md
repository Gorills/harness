# Harness Skills: план упрощения по актуальному `main`

**Репозиторий:** `Gorills/harness`  
**Зафиксированный baseline:** `9042e3abbf2b303de48e307491c2284868bf868a`  
**Дата анализа:** 2026-09-01  
**Статус:** implementation plan, код репозитория этим документом не изменён

---

# 0. Цель

Нужен простой и устойчивый результат:

> Harness определяет реальный стек Workspace, кладёт в проект небольшой набор качественных Skills, а Codex/Cursor нативно выбирают нужный Skill по текущей задаче.

Целевая цепочка:

```text
indexed project files
        ↓
detect_workspace_stack()
        ↓
resolve project-relevant Skills
        ↓
stable .agents/skills/
        ↓
Codex / Cursor
        ↓
native implicit skill selection by description
```

Task lifecycle не участвует в выборе файлов Skills.

Это не новый subsystem. Наоборот, это упрощение уже существующего.

---

# 1. Что подтверждено в актуальном коде

## 1.1. `main`

На момент анализа `main` указывает на:

```text
9042e3abbf2b303de48e307491c2284868bf868a
```

Перед началом реализации агент обязан проверить HEAD ещё раз. Если HEAD изменился, он сначала перечитывает затронутые файлы и адаптирует план к фактическому коду, а не механически применяет этот документ.

Источник:

- [main branch](https://github.com/Gorills/harness/tree/main)

---

## 1.2. Полезный project-level resolver уже существует

Файл:

- [`src/harness/skills.py`](https://github.com/Gorills/harness/blob/9042e3abbf2b303de48e307491c2284868bf868a/src/harness/skills.py)

`detect_workspace_stack()` уже получает данные из Structural Index и детерминированно определяет:

```text
languages
dependencies
manifests
facets
```

Текущая реализация уже умеет читать и классифицировать, среди прочего:

- `package.json`;
- `pyproject.toml`;
- requirements;
- `go.mod`;
- `Cargo.toml`;
- `composer.json`;
- `pubspec.yaml`;
- `Gemfile` / `Gemfile.lock`;
- Maven/Gradle;
- `.csproj`;
- Docker/container surfaces;
- CI surfaces;
- web/mobile/backend/database facets.

**Вывод:** новый stack detector не нужен.

Не создавать:

```text
task path classifier
touched-file classifier
current-file selector
second stack engine
```

Сначала использовать уже существующий `DetectedProjectStack`.

---

## 1.3. Лишняя сложность находится в Task-aware слое resolver-а

Сейчас `resolve_workspace_skills()` делает примерно следующее:

```text
detect_workspace_stack()
        +
get_relevant_task()
        +
get_task_stack_hints()
        ↓
resolve_skills(...)
```

А `resolve_skills()`:

1. сопоставляет Skill с `task_hints`;
2. ставит Task match выше stack evidence;
3. если хотя бы один Skill распознал Task hints, удаляет остальные stack-only Skills.

То есть для polyglot Workspace текущая логика может делать:

```text
project stack = FastAPI + Expo
Task hints = expo

stable project candidates:
  server
  mobile
  testing
  security

after Task narrowing:
  mobile
```

Это именно тот динамический слой, который нужно убрать.

---

# 2. Почему Task-level skill narrowing больше не нужен

Это подтверждается не только архитектурной логикой, но и текущими host contracts.

## Codex

Текущая документация Codex говорит:

- Codex сначала получает name/description Skills;
- полный `SKILL.md` загружается после выбора Skill;
- implicit invocation происходит, когда задача совпадает с `description`;
- `description` должен иметь ясный scope и trigger words;
- большое количество Skills ограничивается model-visible budget.

Источник:

- https://developers.openai.com/codex/skills/

То есть Codex уже имеет собственный task relevance mechanism.

## Cursor

Текущая документация Cursor говорит:

- Cursor автоматически обнаруживает project Skills;
- Agent получает доступные Skills;
- Agent сам решает, когда Skill релевантен по context;
- `description` используется для определения relevance;
- nested/path scoping существует нативно, если когда-нибудь реально понадобится.

Источник:

- https://prod.cursor.com/docs/skills

## Следствие

Harness должен решать:

```text
Какие Skills относятся к этому проекту?
```

Host должен решать:

```text
Какой из этих Skills нужен прямо сейчас?
```

Не нужно дважды решать одну и ту же задачу.

---

# 3. Что в текущей реализации НУЖНО СОХРАНИТЬ

Следующие части не являются проблемой и не должны переписываться без отдельного доказанного дефекта.

## 3.1. Stack detection

Сохранить:

```text
detect_workspace_stack()
DetectedProjectStack
dependency parsing
manifest parsing
facet classification
```

Файл:

- [`src/harness/skills.py`](https://github.com/Gorills/harness/blob/9042e3abbf2b303de48e307491c2284868bf868a/src/harness/skills.py)

---

## 3.2. Bounded resolver

Сохранить:

```text
SkillResolutionPolicy
DEFAULT_MAX_VISIBLE_SKILLS = 12
explicit_include / explicit_exclude
deterministic ordering
match reasons
```

Лимит 12 уже является разумным защитным budget.

Не изобретать новый dynamic budget.

---

## 3.3. Projection safety

Сохранить без redesign:

```text
validate_skill_projection_compatibility()
plan_skill_projection()
inspect_skill_projection()
apply_skill_projection()
```

Также сохранить:

- duplicate-free host visibility;
- Harness ownership markers;
- refusal on user-owned/tracked collisions;
- rollback;
- Git `info/exclude`;
- linked-worktree safety.

Это полезная инфраструктура. Она не имеет отношения к проблеме Task narrowing.

---

## 3.4. Host adapters

Сохранить текущую общую projection в:

```text
.agents/skills/
```

для Codex + Cursor.

Файл:

- [`src/harness/host_adapters.py`](https://github.com/Gorills/harness/blob/9042e3abbf2b303de48e307491c2284868bf868a/src/harness/host_adapters.py)

Не добавлять сейчас:

```text
nested monorepo projection
Cursor paths frontmatter
per-package generated skill roots
```

Host-specific path scoping рассматривается только после воспроизводимого native-selection failure.

---

## 3.5. Filesystem-driven reconciliation

Сохранить watcher reconciliation после реальных filesystem/index changes.

Файлы:

- [`src/harness/watcher.py`](https://github.com/Gorills/harness/blob/9042e3abbf2b303de48e307491c2284868bf868a/src/harness/watcher.py)
- [`src/harness/skill_runtime.py`](https://github.com/Gorills/harness/blob/9042e3abbf2b303de48e307491c2284868bf868a/src/harness/skill_runtime.py)

Правильная логика:

```text
project files changed
        ↓
authoritative scan
        ↓
project stack may have changed
        ↓
reconcile_workspace_skills()
```

Это нужно.

Неправильная логика, которую надо удалить:

```text
Task state changed
        ↓
skill relevance changed
        ↓
reconcile skills
```

---

# 4. Что в текущем коде является лишним

## 4.1. Task lookup внутри skill resolver

Сейчас `skills.py` импортирует:

```python
get_relevant_task
get_task
get_task_stack_hints
```

и `resolve_workspace_skills()` зависит от Task.

Целевое состояние:

```python
resolve_workspace_skills(
    connection,
    workspace_id,
    definitions,
    *,
    explicit_include=(),
    explicit_exclude=(),
    policy=None,
    deadline=None,
)
```

Логика:

```text
stack = detect_workspace_stack(...)
return resolve_skills(definitions, stack, ...)
```

Без Task lookup.

---

## 4.2. Task match в `resolve_skills()`

Удалить task-specific ranking dimension:

```text
task_matches
task_hint:* match reasons
recognized-hints narrowing
```

Было концептуально:

```text
explicit
task hint
facet
dependency
manifest
language
```

Стать должно:

```text
explicit
facet
dependency
manifest
language
```

Не требуется новый scoring engine.

Достаточно сохранить текущий простой deterministic ordering без Task dimension.

---

## 4.3. Skill relevance key

Файл:

- [`src/harness/tasks.py`](https://github.com/Gorills/harness/blob/9042e3abbf2b303de48e307491c2284868bf868a/src/harness/tasks.py)

Сейчас существуют:

```python
SkillRelevanceKey
skill_relevance_key()
enqueue_skill_reconcile_if_relevance_changed()
```

Они существуют только потому, что Task state влияет на projected Skills.

После отвязки Task от Skills эти сущности больше не нужны.

Удалить их после проверки всех callers.

При этом **не удалять**:

```python
get_relevant_task()
get_task_stack_hints()
normalize_task_stack_hints()
Task stack_hints storage
```

Эти вещи относятся к Task domain и Dashboard/history.

Никакой миграции БД ради упрощения Skills не нужна.

---

## 4.4. Task-triggered invalidations в daemon

Файл:

- [`src/harness/daemon.py`](https://github.com/Gorills/harness/blob/9042e3abbf2b303de48e307491c2284868bf868a/src/harness/daemon.py)

Сейчас `mutate_task_start()` и `mutate_task_checkpoint()` делают:

```text
before = skill_relevance_key(...)
Task mutation
after = skill_relevance_key(...)
enqueue skill reconcile if changed
```

Удалить только эту skill-specific обвязку.

Task create/resume/checkpoint semantics не менять.

То есть:

```text
mutate_task_start()
mutate_task_checkpoint()
```

продолжают делать ровно Task работу и перестают иметь side effect на Skill projection.

Если `watcher_invalidations` после этого не используется в конкретной функции, удалить параметр только после проверки всех production callers.

Не устраивать массовую перестройку IPC API ради косметики.

---

## 4.5. Task-triggered invalidations в Dashboard

Файл:

- [`src/harness/dashboard.py`](https://github.com/Gorills/harness/blob/9042e3abbf2b303de48e307491c2284868bf868a/src/harness/dashboard.py)

Сейчас `mutate_dashboard_task()`:

1. вычисляет skill relevance key;
2. выполняет Task mutation;
3. вычисляет key снова;
4. возвращает, изменился ли skill relevance;
5. может enqueue Workspace для skill reconcile.

Эта логика больше не нужна.

Dashboard actions:

```text
accept
feedback
cancel
reopen
comment
set_jira
set_operator_status
```

не должны менять project skill pack.

Сами Task workflows не менять.

Если return value `bool` используется только для старой skill-relevance семантики и production caller его игнорирует, заменить функцию на обычный mutation result/`None` минимальным diff.

---

# 5. Что делать с `stack_hints`

Важно не спутать две разные вещи.

## 5.1. Task `stack_hints`

Текущий Task API и DB уже умеют хранить:

```text
stack_hints
```

Их **не нужно удалять**.

Они могут оставаться полезной Task metadata для:

- Dashboard;
- истории;
- Project Intelligence;
- поиска;
- human inspection.

Но:

```text
Task stack_hints != Skill selector
```

Агент больше не обязан подбирать их ради Skills.

---

## 5.2. MCP wording

Файл:

- [`src/harness/mcp_bridge.py`](https://github.com/Gorills/harness/blob/9042e3abbf2b303de48e307491c2284868bf868a/src/harness/mcp_bridge.py)

Сейчас instructions говорят:

```text
New-Task stack_hints must be task-focused
```

а `task_start` description объясняет, что hints поддерживают task-focused skill projection и next discovery boundary.

Эту семантику удалить.

Целевой смысл:

```text
stack_hints are optional durable Task metadata.
They are not required for Skill selection.
Omit them when they add no useful Task metadata.
```

Не добавлять вместо этого:

```text
recommended_skills
selected_skills
skill_body
skill refs
MCP skill injection
```

Host уже умеет выбирать Skills нативно.

---

# 6. Что делать с `harness.yaml task_hints`

В canonical Skill metadata сейчас тоже существует поле:

```yaml
task_hints:
  - ...
```

Оно используется resolver-ом как counterpart к Task `stack_hints`.

После перехода на stable project pack оно больше не влияет на production selection.

## Простая compatibility strategy

Не ломать существующие custom registry Skills только ради чистоты.

На первом изменении:

- разрешить старое `task_hints` в parser;
- перестать использовать его в resolver;
- считать его legacy ignored metadata;
- built-in pack постепенно перестаёт его генерировать при catalog cleanup.

Не делать сейчас format migration для пользовательских Skills.

Позже поле можно удалить из формата в отдельном breaking release, если вообще будет смысл.

---

# 7. Что уже лишнее в документации и тестах

## 7.1. ADR-0032

Файл:

- [`docs/decisions/0032-continuous-project-skill-reconciliation.md`](https://github.com/Gorills/harness/blob/9042e3abbf2b303de48e307491c2284868bf868a/docs/decisions/0032-continuous-project-skill-reconciliation.md)

Сейчас ADR связывает Task relevance и watcher reconciliation.

Новая архитектура должна сохранить:

```text
continuous project skill reconciliation after project/index changes
```

и отменить:

```text
Task mutation -> skill relevance invalidation
```

Не удалять ADR из истории.

Добавить короткий superseding amendment или новый concise ADR.

---

## 7.2. ADR-0041

Файл:

- [`docs/decisions/0041-task-skill-session-delivery.md`](https://github.com/Gorills/harness/blob/9042e3abbf2b303de48e307491c2284868bf868a/docs/decisions/0041-task-skill-session-delivery.md)

ADR-0041 решает проблему:

```text
task_start selected new Skill
but current host session may not hot reload it
```

После stable project pack этот specific problem исчезает.

Новый контракт:

```text
Harness does not rotate project Skills by Task.
Host receives the stable project-visible pack.
Host-native selection decides which Skill is used.
Harness does not own current-session Skill-body delivery.
```

ADR-0041 не удалять. Пометить superseded в части task-selected filesystem delivery.

---

## 7.3. Specification

Файл:

- [`docs/specification.md`](https://github.com/Gorills/harness/blob/9042e3abbf2b303de48e307491c2284868bf868a/docs/specification.md)

Сейчас specification явно задаёт:

```text
detected project stack
+
current Task stack_hints
+
explicit project configuration
```

и описывает task-hint suppression для polyglot/monorepo.

Заменить на:

```text
detected project stack
+
existing explicit include/exclude policy where actually supplied
```

Project stack описывает доступные project capabilities.

Per-task relevance остаётся host-native.

---

## 7.4. Architecture

Файл:

- [`ARCHITECTURE.md`](https://github.com/Gorills/harness/blob/9042e3abbf2b303de48e307491c2284868bf868a/ARCHITECTURE.md)

Сейчас Skills architecture говорит о resolver-е из:

```text
project stack + Task hints + explicit configuration
```

и отдельно описывает next-discovery-boundary semantics task-selected Skills.

Обновить только этот раздел.

Не переписывать остальную архитектуру.

---

## 7.5. Host compatibility

Файл:

- [`docs/host-compatibility.md`](https://github.com/Gorills/harness/blob/9042e3abbf2b303de48e307491c2284868bf868a/docs/host-compatibility.md)

Удалить старые утверждения:

```text
Task mutation changes skill relevance key
task-selected projection
next-session-only task skill delivery
```

Оставить реальные host facts:

- Codex/Cursor видят `.agents/skills`;
- host discovery/progressive disclosure является native;
- restart remains fallback when host does not refresh changed files;
- projection safety remains Harness-owned.

---

# 8. Тесты, которые нельзя просто бездумно удалить

## 8.1. `test_skill_relevance_reconciliation.py`

Файл:

- [`tests/test_skill_relevance_reconciliation.py`](https://github.com/Gorills/harness/blob/9042e3abbf2b303de48e307491c2284868bf868a/tests/test_skill_relevance_reconciliation.py)

Этот файл почти целиком доказывает старую Task → Skill invalidation модель:

```text
task_start
completion
cancel
reopen
feedback
operator accept
...
-> skill relevance key
-> watcher invalidation
```

После новой архитектуры эти assertions не нужны.

Но замена должна доказать противоположный invariant:

```text
Task lifecycle does NOT change resolved project skill pack.
```

Минимальные новые проверки:

```text
test_task_start_does_not_change_project_skills
test_task_checkpoint_does_not_change_project_skills
test_task_terminal_transition_does_not_change_project_skills
test_dashboard_task_action_does_not_request_skill_reconcile
```

Не нужно повторять каждую Task transition в отдельном огромном файле.

Достаточно проверить boundary.

---

## 8.2. `test_task_skill_session_delivery.py`

Файл:

- [`tests/test_task_skill_session_delivery.py`](https://github.com/Gorills/harness/blob/9042e3abbf2b303de48e307491c2284868bf868a/tests/test_task_skill_session_delivery.py)

Удалить assertions, которые существуют только для:

```text
ADR-0041 Option A
next-session-only
task_start selects skill X
```

Но сохранить полезные независимые protections:

- MCP не доставляет `skill_body`;
- MCP не добавляет `recommended_skills`;
- five-tool surface остаётся прежним;
- `project_context` не превращается в skill delivery API;
- skill body markers не протекают в MCP metadata.

При необходимости перенести эти проверки в более подходящий test module.

Не выбрасывать security/negative-disclosure coverage вместе со старой архитектурной идеей.

---

## 8.3. `test_skills.py`

Файл:

- [`tests/test_skills.py`](https://github.com/Gorills/harness/blob/9042e3abbf2b303de48e307491c2284868bf868a/tests/test_skills.py)

Сохранить большую часть файла:

- registry trust;
- metadata parsing;
- stack detection;
- dependency parsing;
- facets;
- projection planning;
- collisions;
- rollback;
- Git exclude;
- worktrees.

Удалить/заменить только Task-specific cases:

```text
greenfield Task hints activate skills
recognized Task hints suppress stack skills
resolve_skills(... task_hints=...)
```

Добавить простой invariant:

```text
same DetectedProjectStack -> same resolved Skills
regardless of current Task
```

---

## 8.4. `test_builtin_skills.py`

Файл:

- [`tests/test_builtin_skills.py`](https://github.com/Gorills/harness/blob/9042e3abbf2b303de48e307491c2284868bf868a/tests/test_builtin_skills.py)

Сейчас часть pack composition тестов построена вокруг `task_hints`.

Их заменить на project-stack fixtures.

Не писать 50 сценариев.

Достаточно representative matrix:

```text
Python CLI
FastAPI + database
Next/web frontend
Expo/mobile
Docker/CI backend
mixed backend + mobile monorepo
```

Для каждого:

```text
required Skills
clearly forbidden Skills
total count <= 12
```

---

# 9. Built-in pack: что реально есть сейчас

Текущий pack содержит 22 Skills.

Есть две группы.

## 9.1. Уже project-detectable Skills

У этих Skills уже есть `applies_*`, поэтому они естественно подходят stable project pack:

```text
testing-strategy
secure-by-design
container-infrastructure
observability
ci-release
public-frontend
frontend-design
server-application
mobile-application
godot-development
deployment-operations
language-engineering
data-integrity
```

Это хорошая основа.

Именно их сначала надо проверить на реальных stack fixtures.

---

## 9.2. Skills, которые сейчас в основном живут через Task hints

Текущая архитектура использует Task hints для таких Skills:

```text
architecture-decisions
backend-security
scalability-architecture
project-architecture
legacy-preservation
complex-change-planning
spec-audit
independent-review
project-conventions
```

После удаления Task-based selection нельзя просто:

```text
добавить всем applies_facets: software-project
```

Иначе каждый проект получит пачку generic Skills, а лимит 12 начнёт выдавливать реальные stack-specific Skills.

Нужен один простой catalog cleanup.

---

# 10. Правило catalog cleanup

Для каждого из 9 Task-only Skills выбрать ровно одно:

```text
KEEP AS PROJECT-WIDE
MERGE
RETIRE
```

Не создавать четвёртую category, новую routing DSL или новый selector.

## Критерий KEEP AS PROJECT-WIDE

Skill действительно полезен практически в любом software project, а его `description` достаточно узкий, чтобы host не активировал его постоянно.

## Критерий MERGE

Skill сильно пересекается с уже project-visible Skill или соседним quality Skill.

Уникальная полезная инструкция переносится в:

```text
main SKILL.md
или
references/*.md
```

## Критерий RETIRE

Skill не добавляет уникальной operational guidance после merge либо дублирует Harness workflow.

---

# 11. Кандидаты на консолидацию

Это не приказ удалить всё вслепую. Агент должен сначала сравнить body/references.

Но текущий код уже показывает очевидные группы.

## 11.1. Security

```text
backend-security
secure-by-design
```

`secure-by-design` уже:

- project-visible;
- покрывает auth/authz;
- имеет `web-backend.md`;
- имеет verification guidance;
- покрывает infrastructure/mobile/frontend.

Поэтому `backend-security` является сильным кандидатом на merge/retire.

**Не держать два Skill только потому, что они исторически существуют.**

---

## 11.2. Architecture

Сравнить вместе:

```text
architecture-decisions
project-architecture
scalability-architecture
```

Цель не в том, чтобы все три сделать always-visible.

Цель:

- оставить минимальное число activation boundaries;
- сохранить полезную ADR guidance;
- сохранить project-structure guidance;
- сохранить scalability guidance как reference, если она не требует отдельного Skill.

Вероятный хороший результат:

```text
один project-visible architecture Skill
+
references для ADR/scalability
```

Но merge делать только после проверки уникального содержания.

---

## 11.3. Change quality

Сравнить:

```text
complex-change-planning
spec-audit
independent-review
legacy-preservation
```

Это разные этапы одной более широкой проблемы: безопасно провести сложное/рискованное изменение.

Не проецировать четыре generic Skills на каждый проект только ради доступности.

Предпочтительно:

```text
1 хорошо очерченный project-wide Skill
+
references
```

или меньше, если часть guidance уже покрывается testing/architecture.

---

## 11.4. Project conventions

Проверить:

```text
project-conventions
```

Если он в основном повторяет:

- inspect repository contracts;
- follow existing conventions;
- use Project Intelligence;
- preserve local architecture;

то это не обязательно отдельный Skill.

Не держать Skill только ради красивого каталога.

---

# 12. Требования к `description`

Это одна из самых важных частей работы.

Codex и Cursor используют description для native relevance.

## Правила

Каждый project-visible Skill должен:

1. начинать с ясного `Use when ...`;
2. назвать реальные trigger situations;
3. назвать границу scope;
4. не конкурировать с соседним Skill за одни и те же generic prompts;
5. быть коротким;
6. помещать ключевые trigger words в начало.

Codex может сокращать descriptions, когда initial skill list становится большим.

Поэтому плохой вариант:

```yaml
description: Guidance for backend engineering.
```

Лучше:

```yaml
description: >
  Use when implementing, debugging, or reviewing server-side APIs,
  request validation, service boundaries, background jobs, or backend
  error handling.
```

---

# 13. Требования к SKILL.md

Не переписывать весь pack ради переписывания.

Текущие built-ins уже содержат много хорошей operational guidance.

Аудитировать только по критериям:

```text
Does it tell the agent when to use it?
Does it tell the agent what to inspect first?
Does it preserve existing contracts?
Does it define important invariants/failure modes?
Does it define verification?
Does it duplicate another Skill?
Can detailed material move to references?
```

Skill должен быть playbook, а не учебной статьёй.

---

# 14. Greenfield projects

Старый resolver использовал Task hints как способ выбрать FastAPI/Expo/etc. ещё до появления manifest.

Не надо строить новый greenfield intent subsystem только ради этого edge case.

Простой контракт:

```text
Если stack ещё не существует в project files,
Harness не притворяется, что знает stack.
```

После появления:

```text
pyproject.toml
package.json
pubspec.yaml
Dockerfile
source files
```

watcher/scan обновит stack и stable project skill pack.

Если когда-нибудь реально потребуется pre-file greenfield selection, это отдельная product requirement.

Не тащить её обратно скрыто через Task hints.

---

# 15. Monorepo

На первом этапе ничего специального не делать.

Пример Workspace:

```text
apps/mobile -> Expo
services/api -> FastAPI
infra -> Docker
```

Harness может положить стабильный superset:

```text
language-engineering
testing-strategy
secure-by-design
mobile-application
frontend-design
server-application
data-integrity
container-infrastructure
...
```

Пока итоговый pack укладывается в 12 и host нормально выбирает нужное, это нормальное поведение.

Не создавать сейчас:

```text
path-aware Harness resolver
touched-path scoring
task-path state
dynamic nested projection
```

Cursor/Codex уже имеют native mechanisms для project/local Skills. Использовать их только если acceptance покажет конкретную проблему.

---

# 16. Implementation Task A: отвязать Task от Skills

**Priority:** P1  
**Один coherent pass.**  
**Цель:** project skill pack больше не меняется из-за Task lifecycle.

## Перед изменениями

Агент обязан:

1. проверить exact HEAD;
2. найти все callers:
   - `resolve_workspace_skills`;
   - `resolve_skills(... task_hints=...)`;
   - `SkillRelevanceKey`;
   - `skill_relevance_key`;
   - `enqueue_skill_reconcile_if_relevance_changed`;
   - `watcher_invalidations` в Task mutation paths;
3. прочитать затронутые tests/docs до редактирования.

## Production changes

### `src/harness/skills.py`

- убрать Task imports из resolver path;
- `resolve_workspace_skills()` больше не читает relevant Task;
- убрать `task_id` из resolver API, если caller audit подтверждает, что он не является нужным external contract;
- убрать Task match из `resolve_skills()`;
- убрать Task-specific narrowing branch;
- сохранить project stack, explicit include/exclude, budget, reasons, deterministic order;
- старое skill metadata `task_hints` можно пока принимать как ignored legacy input.

### `src/harness/tasks.py`

- удалить `SkillRelevanceKey`;
- удалить `skill_relevance_key()`;
- удалить `enqueue_skill_reconcile_if_relevance_changed()`;
- сохранить Task `stack_hints` storage/read/validation.

### `src/harness/daemon.py`

- удалить before/after skill relevance around `mutate_task_start`;
- удалить before/after skill relevance around `mutate_task_checkpoint`;
- не менять Task semantics.

### `src/harness/dashboard.py`

- убрать skill relevance calculations/enqueue из `mutate_dashboard_task`;
- не менять Task operator workflows;
- упростить return type только если production callers подтверждают, что bool был нужен исключительно old skill relevance behavior.

### `src/harness/mcp_bridge.py`

- убрать требование task-focused `stack_hints`;
- убрать next-discovery skill-projection wording;
- оставить `stack_hints` optional Task metadata;
- не добавлять новые MCP fields/tools.

## Tests

Заменить task-driven assertions на boundary invariants:

```text
Task creation does not alter resolved project skills
Task completion does not alter resolved project skills
Dashboard Task mutation does not enqueue skill reconcile
stack/project change still causes normal skill reconciliation
```

Сохранить projection safety tests.

## Docs

Обновить только impacted sections:

```text
ADR-0032
ADR-0041
ARCHITECTURE.md
docs/specification.md
docs/host-compatibility.md
```

Лучше один короткий новый ADR, который supersedes task-specific skill selection, чем переписывать историю.

## Acceptance

Для одного polyglot fixture:

```text
FastAPI + Expo
```

до Task:

```text
resolved = X
```

после:

```text
task_start(stack_hints=("expo",))
```

должно быть:

```text
resolved == X
```

после completion/reopen:

```text
resolved == X
```

После реального stack change:

```text
add Dockerfile
scan
```

pack может измениться.

## STOP CONDITION

После Task A:

- focused tests;
- adjacent skill/task/dashboard tests;
- repository quality gate;
- diff review;
- report:
  - VERIFIED
  - NOT VERIFIED
  - ASSUMPTIONS
  - BLOCKERS
- STOP.

Не начинать catalog rewrite в том же pass.

---

# 17. Implementation Task B: привести built-in pack к stable project model

**Priority:** P1  
**Зависимость:** Task A  
**Цель:** ни один важный built-in Skill не зависит от Task hints для того, чтобы быть полезным.

## Шаг 1. Инвентаризация

Для всех 22 built-ins составить внутреннюю таблицу:

```text
skill_id
current applies
current task_hints
overlap
unique guidance
decision = KEEP / MERGE / RETIRE
```

Не создавать новый runtime object/table ради этой инвентаризации.

Это просто implementation analysis.

## Шаг 2. Сначала проверить 13 уже project-detectable Skills

Не менять их без причины:

```text
testing-strategy
secure-by-design
container-infrastructure
observability
ci-release
public-frontend
frontend-design
server-application
mobile-application
godot-development
deployment-operations
language-engineering
data-integrity
```

Проверить только:

- правильный `applies`;
- description activation boundary;
- overlap;
- useful references.

## Шаг 3. Разобрать 9 Task-only Skills

```text
architecture-decisions
backend-security
scalability-architecture
project-architecture
legacy-preservation
complex-change-planning
spec-audit
independent-review
project-conventions
```

Для каждого выбрать:

```text
KEEP PROJECT-WIDE
MERGE
RETIRE
```

Не добавлять fake stack heuristics.

Не пытаться определять:

```text
"legacy project"
"complex task"
"scalability task"
```

по путям файлов или названию Task.

Host знает текущую задачу лучше.

## Шаг 4. Built-in metadata

Built-ins больше не должны нуждаться в `task_hints` для composition.

Можно перестать генерировать built-in `task_hints` в `harness.yaml`.

При этом parser временно может принимать old user-owned `task_hints` как legacy ignored metadata.

Это даёт backward compatibility без сохранения старой behavior.

## Шаг 5. Pack fixtures

Минимальная matrix:

### Python CLI

Ожидается маленький core/language pack.

Не должны появляться:

```text
mobile
frontend
database
container
```

без evidence.

### FastAPI + SQLAlchemy/Alembic

Обязательные capabilities:

```text
language engineering
server application
data integrity
security
testing
```

Плюс architecture/change quality только если они выбраны как project-wide core после catalog audit.

### Next.js

Обязательные capabilities:

```text
language engineering
frontend
public frontend
security
testing
```

### Expo

Обязательные capabilities:

```text
language engineering
mobile
frontend design
security
testing
```

### Dockerized backend + CI

Добавляются:

```text
container infrastructure
ci/release
```

### Mixed FastAPI + Expo monorepo

Должны быть доступны обе реальные surfaces.

Не требовать искусственного task narrowing.

## Acceptance

Для каждого fixture:

```text
required ⊆ resolved
clearly_irrelevant ∩ resolved == ∅
len(resolved) <= 12
```

Не фиксировать exact alphabetical list без необходимости.

Exact full list assertion использовать только там, где ordering itself является contract.

## STOP CONDITION

После Task B:

- full focused built-in + skill resolver tests;
- quality gate;
- diff review на accidental skill content loss;
- report VERIFIED / NOT VERIFIED / ASSUMPTIONS / BLOCKERS;
- STOP.

---

# 18. Implementation Task C: native host acceptance

**Priority:** P1 final gate  
**Зависимость:** Task B  
**Цель:** доказать, что больше никакой Harness task selector не нужен.

## Codex

Не создавать новый acceptance framework.

Использовать текущий:

- [`scripts/accept_codex.py`](https://github.com/Gorills/harness/blob/9042e3abbf2b303de48e307491c2284868bf868a/scripts/accept_codex.py)

Сейчас runner уже:

- создаёт synthetic Skills;
- проверяет `.agents/skills`;
- использует hidden nonce в Skill body;
- имеет positive/negative native skill checks;
- умеет optional real model run.

Что изменить:

- удалить `TASK_SKILL_SESSION_DELIVERY_EXPECTED_RESULT`;
- удалить synthetic scenario `task_start selects X`;
- synthetic acceptance Skills должны проектироваться только по project `applies`, что они уже умеют делать;
- positive Skill должен выбираться по description;
- negative sibling не должен выбираться;
- Task hints не участвуют.

### Важный invariant

Skill body nonce:

```text
не должен попадать в prompt metadata заранее
```

Он появляется в evidence только если Codex реально выбрал и прочитал `SKILL.md`.

Это уже хороший proof native selection.

---

## Cursor

Не строить автоматизацию вокруг undocumented internals.

Минимальная manual/real-host acceptance:

1. stable project pack существует до старта новой session;
2. Cursor видит project Skills;
3. backend prompt не активирует явно unrelated mobile Skill;
4. mobile prompt активирует mobile guidance;
5. mixed repo не показывает систематическую путаницу.

Если текущий host tooling позволяет детерминированно увидеть selected Skill, использовать его.

Если нет, документировать manual evidence.

Не строить новый daemon telemetry channel.

---

# 19. Финальный decision gate после Task C

Если Codex/Cursor нормально выбирают Skills из stable pack:

```text
STOP.
```

Skill architecture закончена.

Не делать:

```text
R-08 path routing
R-09 host-neutral path evidence
R-10 dynamic path-scoped Harness projection
```

просто потому, что такие задачи можно придумать.

---

# 20. Когда всё-таки разрешено вернуться к path scoping

Только при воспроизводимом результате вида:

```text
Fixture:
FastAPI + Expo monorepo

Prompt:
fix Alembic migration

Observed repeatedly:
mobile-application is selected
server/data skill is missed
```

И только если проблема не исправляется:

1. description boundary;
2. catalog overlap;
3. уменьшением project pack.

После этого сначала использовать native host capabilities.

Не писать свой task classifier первой реакцией.

---

# 21. Что агенту ЗАПРЕЩЕНО добавлять в эти три задачи

Без отдельного доказанного требования не добавлять:

```text
new database tables
Task-to-Skill mapping table
recommended_skills MCP field
skill bodies through MCP
dynamic current-session injection
new watcher state machine
task path classifier
touched file tracker
per-task materialized directories
embeddings for skill selection
LLM-based stack classifier
new project config DSL
mandatory paid model CI
Cursor-specific path logic in core resolver
```

Также не переписывать:

```text
retrieval
Task lifecycle
Knowledge
dashboard UX
host MCP transport
projection ownership
Git exclude handling
```

если изменение не требуется непосредственно для удаления Task → Skill coupling.

---

# 22. Что НЕ надо удалять ради "чистоты"

Не удалять сейчас:

```text
Task stack_hints DB schema
Task stack_hints API field
Dashboard stack_hints display
explicit_include / explicit_exclude
max_visible_skills
Skill registry trust validation
projection collision handling
watcher filesystem reconciliation
```

Если эти элементы больше не нужны когда-нибудь потом, это отдельная задача с отдельным доказательством.

Не смешивать cleanup с архитектурным переходом.

---

# 23. Expected final code shape

## Resolver

Концептуально:

```python
def resolve_workspace_skills(
    connection,
    workspace_id,
    definitions,
    *,
    explicit_include=(),
    explicit_exclude=(),
    policy=None,
    deadline=None,
):
    stack = detect_workspace_stack(
        connection,
        workspace_id,
        deadline=deadline,
    )
    return resolve_skills(
        definitions,
        stack,
        explicit_include=explicit_include,
        explicit_exclude=explicit_exclude,
        policy=policy,
    )
```

И:

```python
def resolve_skills(
    definitions,
    stack,
    *,
    explicit_include=(),
    explicit_exclude=(),
    policy=None,
):
    ...
```

Без:

```text
task_hints
get_relevant_task
current Task
task-focused suppression
```

---

# 24. Expected final runtime behavior

## Task starts

```text
task_start
```

Результат:

```text
Task created/resumed
```

Не:

```text
Task created
+
Skills recomputed
+
watcher invalidated
```

---

## User changes task

```text
backend work -> mobile work
```

Harness project skill files не прыгают.

Host нативно использует другой доступный Skill.

---

## Project stack changes

```text
add Expo
add Docker
remove backend framework
```

Результат:

```text
watcher scan
-> detected project stack changes
-> reconcile stable project skill pack
```

Это правильное место для динамики.

---

# 25. Проверка, что мы действительно упростили систему

После завершения трёх задач должны быть истинны все пункты:

```text
[ ] Skill resolver не импортирует Task domain
[ ] Task mutations не enqueue skill reconciliation
[ ] Dashboard Task actions не enqueue skill reconciliation
[ ] MCP не говорит, что stack_hints выбирают Skills
[ ] Task stack_hints всё ещё безопасно сохраняются как optional metadata
[ ] project stack detector остался единственным Harness relevance source
[ ] max visible Skills <= 12
[ ] projection safety не ослаблена
[ ] Codex/Cursor получают stable project pack
[ ] descriptions управляют native activation
[ ] no new selector subsystem exists
```

---

# 26. Verification protocol для агента

Для каждой Task A/B/C отдельно:

```text
1. Re-read exact current source.
2. Identify affected callers/tests/contracts.
3. Change only the current slice.
4. Run focused tests.
5. Run adjacent regression tests.
6. Run repository quality gate.
7. Review diff for unrelated changes.
8. Report:
   VERIFIED
   NOT VERIFIED
   ASSUMPTIONS
   BLOCKERS
9. STOP.
```

Не начинать следующую Task автоматически.

---

# 27. Короткий порядок работ

```text
Task A
Retire Task-driven skill resolution and Task-triggered skill reconciliation
        ↓
STOP

Task B
Rationalize current built-in pack for stable project-level projection
        ↓
STOP

Task C
Reuse current acceptance runner to prove native host skill selection
        ↓
STOP
```

На этом работа по skill architecture заканчивается, если acceptance не показывает конкретный failure.

---

# 28. Итог

В актуальном коде уже есть почти вся нужная инфраструктура.

Нужно сохранить:

```text
stack detection
bounded resolver
canonical registry
safe projection
watcher after filesystem changes
Codex/Cursor native roots
```

Нужно удалить:

```text
Task -> skill resolver input
Task -> skill relevance key
Task lifecycle -> skill invalidation
task-focused projection contract
next-session task-selected skill delivery contract
```

Нужно улучшить:

```text
built-in catalog overlap
project applicability
descriptions
native-selection acceptance
```

И больше ничего не строить до появления реального провала.

Целевой принцип:

> Harness выбирает качественные Skills для проекта. Host выбирает Skill для задачи.

Это и есть минимальная нормальная архитектура для текущего кода.
