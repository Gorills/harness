# Harness — финальное техническое задание

**Версия:** 1.0
**Статус:** Approved architecture baseline
**Тип:** local-first control plane for coding agents

---

# 1. Цель продукта

Harness — глобально установленная локальная система для работы coding agents с множеством программных проектов.

Harness должен решать четыре основные проблемы:

1. агент не должен заново исследовать проект при каждой новой сессии;
2. человек должен мгновенно понимать, где остановилась работа над каждым проектом;
3. агент должен получать только релевантный контекст, не расходуя context window на ненужные данные;
4. Claude Code, Codex, Cursor, Antigravity и будущие совместимые клиенты должны использовать Harness через свои нативные механизмы.

Harness **не является новым coding agent**.

Он является инфраструктурным слоем между:

- repository;
- накопленной информацией о проекте;
- текущей рабочей задачей;
- coding agent;
- человеком.

Основная продуктовая формула:

> Harness хранит то, что уже пришлось понять во время разработки, и делает это знание дешёвым для следующей задачи.

---

# 2. Главные продуктовые инварианты

## 2.1. Harness глобален

Harness устанавливается один раз на машину пользователя.

После установки он должен быть доступен из всех поддерживаемых проектов.

Не требуется отдельная установка runtime Harness в каждый repository.

---

## 2.2. Project context локален

Глобальная установка не означает глобальную загрузку данных в model context.

Каждый проект имеет собственные:

- Structural Index;
- Tasks;
- Knowledge;
- Documentation index;
- active skills;
- Workspaces;
- Working Sets.

Нерелевантные данные другого проекта никогда не должны попадать в ответы MCP.

---

## 2.3. Native agent first

Harness MUST использовать штатные интерфейсы agent host.

Основной runtime interface:

```text
MCP

```

Harness MUST NOT:

- проксировать model API;
- подменять Claude/Codex/Cursor/Antigravity;
- требовать собственный agent runtime;
- заставлять пользователя запускать агента через Harness wrapper;
- заменять native file read/edit;
- заменять shell;
- заменять Git;
- заменять browser;
- скрывать от агента стандартные возможности его host.

Harness отвечает прежде всего за:

```text
orientation
retrieval
project intelligence
task continuity
skill delivery
human coordination

```

---

## 2.4. Правильный путь должен быть самым дешёвым

Harness не должен строиться на огромном prompt с запретами.

Агент должен использовать Harness потому, что это:

- быстрее;
- точнее;
- дешевле по tokens;
- требует меньше tool calls.

Например:

```text
Harness search

```

должен быть выгоднее repository-wide grep.

```text
project_status

```

должен быть выгоднее ручного восстановления истории проекта.

---

## 2.5. Progressive disclosure

Модель не получает всю Project Intelligence.

Она получает:

1. минимальный bootstrap;
2. status;
3. результаты конкретного search;
4. подробный context только для выбранных сущностей.

Большие dumps запрещены архитектурно.

---

## 2.6. Harness не должен усложнять жизнь трём сторонам

Архитектура должна одновременно минимизировать сложность для:

### Agent

Небольшое число понятных операций.

### Human

Минимум project-management overhead.

### Harness developer

Один core implementation без четырёх независимых реализаций business logic для четырёх host.

---

# 3. Non-goals

Harness v1 не является:

- IDE;
- Jira;
- GitHub replacement;
- Git client;
- CI platform;
- orchestration platform;
- generic autonomous agent;
- workflow DSL;
- team collaboration SaaS;
- LLM memory dump;
- distributed search engine;
- knowledge graph platform;
- source-code vector database;
- generic plugin marketplace.

В v1 не нужны:

- Elasticsearch;
- Neo4j;
- Redis;
- Kafka;
- PostgreSQL server;
- Qdrant server;
- Kubernetes;
- Docker как обязательное условие запуска;
- отдельный frontend build pipeline;
- cloud backend.

---

# 4. Основные сущности

## 4.1. Project

Логический программный проект.

Один Project может содержать несколько Workspaces.

---

## 4.2. Workspace

Физический checkout проекта.

Например Git worktrees:

```text
~/projects/shop
~/projects/shop-payment

```

могут принадлежать одному Project.

Filesystem state относится именно к Workspace.

---

## 4.3. Task

Логическая рабочая задача.

Пример:

```text
Fix refresh token race condition

```

Task не равна agent session.

Одна Task может продолжаться:

```text
Claude Code
→ человек
→ Cursor
→ Codex

```

и сохранять тот же `task_id`.

---

## 4.4. Agent Session

Конкретная сессия agent host, подключившаяся к Harness MCP.

Session может закончиться.

Task при этом продолжает существовать.

---

## 4.5. Structural Index

Автоматически получаемая карта repository.

---

## 4.6. Knowledge Card

Компактная единица проверяемого семантического знания о проекте.

---

## 4.7. Working Set

Небольшой текущий набор:

- files;
- symbols;
- docs;
- Knowledge Cards;

наиболее релевантных Task.

Он вычисляется Harness автоматически.

---

# 5. Архитектура

```text
Claude Code ─┐
Codex       ─┤
Cursor      ─┼──── MCP ───── harness mcp
Antigravity ─┘                   │
                                 │ local IPC
                                 ▼
                             harnessd
                 ┌───────────────┼────────────────┐
                 │               │                │
                 ▼               ▼                ▼
              SQLite          Indexer       Skill Resolver
                 │               │                │
                 │               ▼                ▼
                 │             Watcher      Native Skills
                 │
                 ├── Projects / Workspaces
                 ├── Tasks / Sessions
                 ├── Structural Index
                 ├── Knowledge
                 ├── Documentation
                 ├── Search
                 └── Dashboard API
                                  │
                                  ▼
                              Dashboard

```

Архитектура v1 — **modular monolith**.

---

# 6. Технологический стек

Основной язык:

```text
Python 3.13

```

## Core

```text
Python standard library
Pydantic
asyncio

```

## Storage

```text
SQLite
SQLite WAL
FTS5

```

## MCP

```text
Official MCP Python SDK
stdio transport
thin Harness adapter

```

## Filesystem

```text
watchfiles

```

или эквивалентный небольшой filesystem watcher, если потребуется по platform compatibility.

## Dashboard backend

```text
FastAPI
Starlette
Uvicorn

```

## Dashboard frontend

```text
Jinja2
HTML
CSS
Vanilla JavaScript
Server-Sent Events

```

На старте НЕ используется:

```text
React
Vue
Next.js
Vite
Webpack

```

если только реальная сложность dashboard позже не докажет необходимость.

## Tests

```text
pytest
httpx
subprocess
raw MCP JSON-RPC test client

```

---

# 7. Почему выбран такой стек

Стек должен удовлетворять не только production requirements, но и реальной проверяемости разработки.

В доступном execution environment должны быть возможны:

- запуск daemon;
- SQLite;
- FTS5;
- filesystem operations;
- subprocess;
- stdio;
- HTTP server;
- HTTP tests;
- MCP wire tests;
- exact payload inspection;
- integration fixtures.

Core Harness MUST быть тестируем без установки proprietary GUI agent hosts.

---

# 8. Process model

Минимально существует:

```text
harness
harnessd

```

Физически допустим один executable/package с subcommands.

Например:

```bash
harness daemon
harness mcp
harness scan
harness dashboard

```

---

# 9. harnessd

Один daemon на пользователя.

Он является владельцем:

- database;
- indexing;
- watcher;
- Task state;
- search;
- skills;
- dashboard API.

MCP bridge не должен реализовывать отдельную копию business logic.

---

# 10. Local IPC

`harness mcp` соединяется с daemon через локальный IPC.

Unix/macOS:

```text
Unix domain socket

```

Windows:

```text
Named Pipe

```

или эквивалент.

Local HTTP loopback MAY использоваться внутри dashboard subsystem, но не должен быть обязательным IPC для model-facing MCP.

---

# 11. Global installation

Команда:

```bash
harness install

```

должна:

1. создать Harness data directory;
2. создать/мигрировать database;
3. настроить daemon;
4. обнаружить поддерживаемые agent hosts;
5. зарегистрировать Harness MCP глобально;
6. установить только необходимые Harness-owned integration artifacts;
7. не повреждать пользовательские настройки;
8. быть идемпотентной;
9. выполнить либо предложить `harness doctor`.

---

# 12. Uninstall

```bash
harness uninstall

```

удаляет только integration artifacts Harness.

Project Intelligence сохраняется по умолчанию.

Полное удаление:

```bash
harness uninstall --purge

```

---

# 13. Doctor

```bash
harness doctor

```

проверяет:

- daemon;
- SQLite;
- schema;
- permissions;
- MCP registrations;
- host adapters;
- active projects;
- index state;
- generated skills;
- dashboard;
- stale integrations.

Doctor по умолчанию read-only.

---

# 14. Host adapters

Для каждого host существует небольшой adapter:

```text
ClaudeAdapter
CodexAdapter
CursorAdapter
AntigravityAdapter

```

HostAdapter отвечает только за:

- обнаружение host;
- global MCP configuration;
- Harness bootstrap instructions, если необходимы;
- native skill materialization;
- cleanup;
- doctor;
- optional hooks.

Business logic не может находиться в HostAdapter.

---

# 15. Hooks

Hooks — OPTIONAL enhancement.

Harness MUST полностью работать без них.

Hooks могут позже использоваться для:

- observability;
- command verification;
- session lifecycle;
- additional telemetry.

Они не являются source of truth для:

- Tasks;
- Structural Index;
- Knowledge;
- project state.

---

# 16. Project registration

Legacy project:

```bash
cd project
harness scan

```

или:

```bash
harness scan /path/to/project

```

---

# 17. `harness scan`

Scan должен быть:

```text
deterministic
local
non-LLM

```

Scan MUST NOT:

- вызывать LLM;
- генерировать semantic summaries кода;
- придумывать предназначение функций;
- автоматически создавать ADR;
- автоматически объяснять архитектуру проекта.

---

# 18. Structural Index

Scan должен определить максимум доступной механической информации:

- files;
- directories;
- languages;
- manifests;
- packages;
- modules;
- symbols;
- symbol locations;
- imports;
- exports;
- basic dependency relationships;
- documentation files;
- ADR-like files;
- tests;
- Git metadata;
- file hashes;
- symbol fingerprints.

---

# 19. Parser architecture

Используется интерфейс:

```text
ParserAdapter

```

Конкретная parsing library не является public contract.

Допускается Tree-sitter.

Для неподдерживаемого языка Harness должен продолжать работать в degraded mode:

- filenames;
- paths;
- text tokens;
- FTS;
- docs;
- Git.

---

# 20. Incremental indexing

После initial scan daemon наблюдает filesystem.

При:

- create;
- modify;
- delete;
- rename;

Structural Index обновляется автоматически.

Agent никогда не обязан выполнять:

```text
update_file_map
update_symbol_map

```

---

# 21. Greenfield project

Для нового проекта explicit full scan может быть почти пустым.

Harness watcher должен строить карту по мере создания файлов.

Агент создал:

```text
src/server.py

```

Harness автоматически добавляет его в Structural Index.

Агент не сообщает Harness о существовании файла вручную.

---

# 22. Source of truth

Для code:

```text
filesystem

```

Для Git state/history:

```text
Git

```

Structural Index является индексом, а не отдельной истиной.

---

# 23. Ignore policy

Harness уважает:

```text
.gitignore
.harnessignore

```

По умолчанию исключаются распространённые:

```text
.git
node_modules
vendor
dist
build
target
caches
binaries
generated files

```

Sensitive patterns по умолчанию:

```text
.env
.env.*
*.pem
*.key

```

---

# 24. Project Intelligence

Project Intelligence состоит из:

```text
Structural Index
Documentation
Semantic Knowledge
Task History
Working Sets

```

---

# 25. Главный semantic принцип

Harness не должен заранее пытаться понять весь проект.

Правило:

> Harness запоминает то, что агент уже был вынужден понять для выполнения реальной задачи.

Это основной механизм semantic enrichment.

---

# 26. Первый поиск в legacy project

После первого scan Semantic Knowledge может отсутствовать.

Поиск использует:

- paths;
- filenames;
- symbols;
- normalized identifiers;
- imports;
- lexical FTS;
- docs;
- Git;
- structural relations.

Harness не гарантирует магический semantic understanding неизвестного legacy code.

Он должен **значительно сужать область исследования**.

---

# 27. Identifier normalization

Indexer должен уметь сопоставлять:

```text
rotateRefreshToken
rotate_refresh_token
RotateRefreshToken

```

с concepts:

```text
rotate
refresh
token

```

Это даёт natural-language discovery до появления Knowledge.

---

# 28. Search architecture

Основная search implementation v1:

```text
SQLite FTS5
+
exact matching
+
structural graph
+
semantic Knowledge
+
optional embeddings

```

---

# 29. Search corpora

Search объединяет результаты из:

- code;
- docs;
- Knowledge Cards;
- Task history.

---

# 30. Embeddings

Raw source code MUST NOT массово embedding'иться в v1.

Embeddings используются преимущественно для уже семантического текста:

- docs;
- ADR;
- Knowledge Cards;
- Task summaries.

Embedding provider изолирован:

```text
EmbeddingProvider

```

Default Harness MUST работать без внешнего cloud embedding API.

---

# 31. Ranking

Ranking учитывает:

- exact symbol match;
- exact path match;
- normalized identifiers;
- FTS score;
- semantic relevance;
- current Working Set;
- structural proximity;
- freshness;
- current Task;
- stale penalty.

Для объединения независимых ranking systems предпочтителен простой механизм вроде Reciprocal Rank Fusion.

Не создавать ранний набор из десятков вручную настроенных коэффициентов.

---

# 32. Semantic Knowledge

Agent добавляет knowledge только после реального исследования.

Примеры полезных Knowledge Card:

```text
behavior
data flow
invariant
architecture rationale
decision
caveat
operational detail

```

---

# 33. Knowledge quality rule

Knowledge Card создаётся, только если информация вероятно позволит будущему агенту избежать повторного исследования.

Agent MUST NOT:

- описывать каждый прочитанный файл;
- summarise всё подряд;
- заполнять базу ради заполнения;
- сохранять speculative understanding как факт.

---

# 34. Knowledge Card schema

Минимально:

```text
id
project_id
kind
title
body

source_type
source_task_id

created_at
updated_at

freshness

```

---

# 35. Knowledge provenance

Knowledge обязательно имеет provenance.

Допустимые sources:

```text
agent_asserted
operator
repository_document
ADR

```

Code-related knowledge SHOULD иметь anchors.

---

# 36. Knowledge anchor

Anchor может указывать на:

- file;
- symbol;
- document;
- Task;
- Knowledge Card.

Для code:

```text
path
symbol
file fingerprint
symbol fingerprint

```

---

# 37. Staleness

Если код, на котором основано Knowledge, изменился:

```text
fresh
→ needs_revalidation

```

Запись не удаляется автоматически.

Она:

- получает ranking penalty;
- не выдаётся как current verified fact;
- может использоваться как historical clue.

---

# 38. Никакого автоматического semantic repair

После изменения файла Harness MUST NOT автоматически вызывать LLM для обновления Knowledge.

Knowledge обновляется, когда следующая реальная Task снова исследует соответствующую область.

---

# 39. Documentation

Harness индексирует repository documentation:

```text
README*
docs/**
ADR*
architecture notes
runbooks
Markdown/text docs

```

Documentation редактируется обычными native file tools агента.

Watcher переиндексирует изменения автоматически.

---

# 40. Task как центральная runtime entity

Task переживает:

- agent restart;
- host switch;
- human feedback;
- несколько рабочих сессий.

---

# 41. Task state

Минимальные состояния:

```text
working
waiting
completed
cancelled

```

---

# 42. Waiting reason

Для `waiting` указывается:

```text
operator_review
operator_input
external

```

Отдельные workflow statuses типа:

```text
QA_PENDING
IN_REVIEW
BLOCKED_MANUAL

```

не нужны.

---

# 43. Task lifecycle

```text
User request
   ↓
project_status
   ↓
task_start / resume
   ↓
search
   ↓
native work
   ↓
task_checkpoint
   ├── working
   ├── waiting
   └── completed

```

---

# 44. Human acceptance

Если результат требует субъективной оценки:

```text
waiting(operator_review)

```

Dashboard показывает:

```text
Ready for review

```

Пользователь нажимает:

```text
Accept

```

Task становится:

```text
completed

```

---

# 45. Human feedback

Если вместо Accept пользователь оставляет замечание:

```text
На mobile всё ещё слишком большой отступ

```

Harness НЕ создаёт новую Task.

Тот же `task_id`:

```text
waiting
→ working

```

Feedback добавляется в Task history.

---

# 46. Automatic completion

Agent может поставить:

```text
completed

```

если:

- работа действительно выполнена;
- необходимые objective checks выполнены;
- known failures отсутствуют;
- human review не требуется.

---

# 47. Verification

Checkpoint сохраняет:

```text
verification:
    name
    status
    evidence

```

Status:

```text
passed
failed
not_run

```

Источник:

```text
agent_reported
observed

```

В v1 достаточно `agent_reported`.

Hooks позже могут добавлять `observed`.

---

# 48. Task baseline

На `task_start` Harness автоматически фиксирует:

- HEAD;
- branch;
- dirty state;
- Workspace;
- timestamp;
- index freshness.

На checkpoint Harness механически определяет changed files.

Agent не должен перечислять данные, которые Harness может получить сам.

---

# 49. Human changes

Если человек вручную меняет код в том же Workspace во время Task, Harness считает это частью текущего workspace state.

v1 не пытается определять авторство каждой строки:

```text
human
Claude
Codex
Cursor

```

---

# 50. Concurrency

Один Workspace SHOULD иметь не более одной различной активной `working` Task.

Для параллельной работы используются отдельные Git worktrees / Workspaces.

Это позволяет не строить сложную систему attribution/conflicts.

---

# 51. Agent Sessions

Session содержит:

```text
id
client
workspace_id
task_id?
started_at
last_activity_at
ended_at?

```

Harness использует доступную MCP client metadata.

---

# 52. Working Set

Harness автоматически формирует Working Set из:

- search hits;
- accessed references;
- changed files;
- related symbols;
- relevant docs;
- Knowledge Cards.

Agent не обязан вручную управлять Working Set.

---

# 53. Working Set ranking

Следующие searches получают ranking boost для:

- Working Set;
- его graph neighbours;
- связанных docs/knowledge.

Это boost, а не hard filter.

---

# 54. Model-facing MCP surface

v1 имеет всего пять основных tools:

```text
project_status
project_search
project_context
task_start
task_checkpoint

```

Новый tool добавляется только если существующая схема объективно ухудшает usability.

---

# 55. `project_status`

Назначение:

> Дать модели быстрое понимание текущего состояния проекта.

Response содержит только:

- project;
- Workspace;
- branch/HEAD;
- dirty summary;
- index state;
- current Task;
- relevant waiting Task;
- last checkpoint;
- current focus;
- next step;
- pending operator feedback;
- compact Working Set summary.

---

# 56. `project_status` не возвращает

- полный file tree;
- все symbols;
- всю Knowledge Base;
- всю Task history;
- полный diff;
- все ADR;
- embeddings;
- внутренние ranking data.

---

# 57. `project_search`

Conceptual input:

```json
{
  "query": "where refresh token rotation happens",
  "scope": "all"
}

```

Scopes:

```text
all
code
docs
knowledge
tasks

```

---

# 58. Search result

Каждый result содержит минимум:

```text
ref
kind
title
location
short_summary
match_reason
freshness

```

Для code дополнительно:

```text
path
symbol?
line range?

```

---

# 59. Search не заменяет native file read

Harness возвращает местоположение и небольшой snippet/signature.

Полный source agent читает native file tools своего host.

---

# 60. `project_context`

Используется для детализации уже выбранных refs.

Например:

```json
{
  "refs": [
    "knowledge:42",
    "doc:adr-14"
  ]
}

```

Harness возвращает только эти данные и непосредственно связанные необходимые metadata.

---

# 61. `task_start`

Новая Task:

```json
{
  "title": "Fix refresh token race condition"
}

```

Greenfield Task может передать:

```json
{
  "title": "Create API",
  "stack_hints": [
    "fastapi",
    "postgres"
  ]
}

```

Resume:

```json
{
  "task_id": "TASK-184"
}

```

---

# 62. Task binding

После `task_start` MCP session считается связанной с Task.

Другим calls обычно не требуется повторять `task_id`.

---

# 63. `task_checkpoint`

Один write tool заменяет:

```text
update_task
save_checkpoint
finish_task
update_semantics
update_map

```

Conceptual payload:

```json
{
  "state": "completed",
  "summary": "Made refresh-token replacement transactional.",
  "next_step": null,
  "verification": [],
  "knowledge": []
}

```

---

# 64. Intermediate checkpoint

```text
state=working

```

используется только после meaningful progress.

Agent не делает checkpoint после каждого tool call.

---

# 65. Waiting checkpoint

```text
state=waiting

```

обязательно содержит:

```text
wait_reason
next_step

```

---

# 66. Completed checkpoint

Harness сохраняет автоматически:

- Task state;
- timestamp;
- changed files;
- Git state;
- supplied verification;
- Knowledge;
- session event.

---

# 67. Semantic enrichment через Task

Knowledge update добавляется прямо в `task_checkpoint`.

Пример:

```json
{
  "kind": "invariant",
  "title": "Refresh token replacement is atomic",
  "body": "Old token remains valid until the replacement transaction commits.",
  "anchors": [
    {
      "path": "src/auth/session_repository.py",
      "symbol": "SessionRepository.replace"
    }
  ]
}

```

---

# 68. MCP Context Exposure Contract

Каждый MCP tool имеет явный contract:

```text
allowed fields
forbidden internal fields
default item limit
hard item limit
hard serialized byte limit
pagination policy

```

Context size — часть API contract, а не рекомендация.

---

# 69. Response budgets

Design targets:

```text
project_status
  обычно < ~500 model tokens

project_search
  default top 5
  обычно < ~800 model tokens

project_context
  только explicit refs

task calls
  короткий structured response

```

Implementation enforce'ит прежде всего:

```text
byte limits
character limits
item limits

```

поскольку tokenizer зависит от модели.

---

# 70. Negative disclosure

Tests должны проверять не только наличие нужных данных, но и отсутствие ненужных.

Например `project_status` не должен случайно содержать:

```text
full files
old ADR body
unrelated task
unrelated knowledge
internal DB data
ranking internals

```

---

# 71. Agent bootstrap

Always-loaded Harness instructions должны оставаться очень маленькими.

Цель:

```text
< ~1 KB text

```

Смысл:

```text
When Harness is available, obtain project status before broad repository
exploration. Use Harness search to locate likely code, documentation and
existing project knowledge. Read and edit source using native host tools.
Prefer Harness over repository-wide discovery, while targeted native
search remains allowed when needed. Start or resume a Harness task before
meaningful changes and checkpoint meaningful completed or waiting work.
Use checkpoints for durable continuity; operator chat is only the human-relevant delta.
Reply briefly: lead with the result; do not restate the Task/checkpoint, paste unchanged
source, or recap diffs/file lists. Report only material decisions, risks, blockers, and
verification unless the operator asks for detail. This is a soft native-host instruction,
not a hard output-token guarantee.

```

---

# 72. Никакого абсолютного запрета grep/find

Запрещается не инструмент, а расточительный default workflow.

Нормально:

```bash
rg "EXACT_ERROR_729" src/payments

```

после того как Harness уже сузил область.

Нежелательно:

```bash
rg ... entire-repository
find ... entire-repository

```

как первая discovery strategy при доступном Harness.

---

# 73. Skills Registry

Canonical skill registry:

```text
~/.harness/skills/

```

Например:

```text
fastapi/
    SKILL.md
    harness.yaml

playwright/
    SKILL.md
    harness.yaml

godot/
    SKILL.md
    harness.yaml

```

---

# 73.1 Built-in quality pack

Harness поставляет компактный product-owned quality pack в canonical registry. Он не создаёт второй project/task state и не materialize'ится целиком в каждый Project. `install`/`skills sync` обновляют только Harness-owned exact content и fail closed при same-id user-modified collision. `skills validate` проверяет portable skill metadata против всех текущих supported host surfaces. Композиция built-in skills использует detected project stack и explicit include/exclude, без отдельного workflow DSL. Built-in `harness.yaml` не генерирует `task_hints`.

Количество canonical built-ins не равно model-visible budget: resolver по-прежнему выбирает только
релевантный bounded subset. Подробные stack/domain инструкции могут жить в portable
`references/` и должны читаться skill'ом только для затронутого языка или режима. Quality pack
покрывает как минимум Docker lifecycle/configuration, public frontend discoverability для Google и
Яндекса, language-native engineering, project architecture, legacy preservation, data integrity,
backend services, Expo/React Native mobile apps, Godot, deployment operations и secure-by-design
архитектуру/верификацию для web, server, browser, mobile и supply chain. Любой распознанный
пользовательский web/mobile frontend также materialize'ит `frontend-design`: короткий обязательный
design contract, отдельные правила для marketing/editorial и product/mobile surfaces, anti-slop
ограничения и bounded visual review. Его facet applicability сопровождает соответствующие
frontend surface skills; явная project policy остаётся
авторитетной.

---

# 74. Skill principle

Никогда не materialize все skills глобально.

Project должен видеть только релевантный subset.

---

# 75. Skill metadata

Harness-specific applicability хранится отдельно от portable `SKILL.md`.

Пример:

```yaml
id: fastapi

applies:
  languages:
    - python

  dependencies:
    - fastapi

  manifests:
    - pyproject.toml

  facets:
    - backend-service

task_hints:
  - fastapi
  - python-api

```

Поле `task_hints` — ignored legacy parser input. Resolver его не читает и не ранжирует по нему pack.

---

# 76. Skill Resolver

Использует:

```text
detected project stack
+
existing explicit include/exclude where supplied

```

`detected project stack` включает raw languages/dependencies/manifests и детерминированные derived
facets. Facet объединяет несколько контекстных сигналов, когда один dependency неоднозначен.
Например, `react-dom` внутри package с `expo`/`react-native` не классифицирует native app как
`web-frontend`; отдельный web package в monorepo классифицирует Workspace одновременно как mobile и
web. Facets не являются ручным workflow DSL. Per-task relevance остаётся host-native.

Stack evidence описывает весь Workspace. Resolver не сужает pack по `current Task stack_hints`.
Task `stack_hints` остаются optional durable Task metadata и не являются Skill selector.
Portable skill description должен кратко указывать, когда skill следует загружать host'у.

---

# 77. Legacy skill selection

После scan:

```text
package manifests
config files
dependencies
languages

```

позволяют определить stack.

Например проект:

```text
Next.js
Postgres
Playwright

```

не должен получить:

```text
Godot
Unity
FastAPI

```

---

# 78. Greenfield skill selection

Пустой repository может не иметь stack.

Task `stack_hints` never activate Skills. Empty stack stays empty until indexed evidence exists.

После появления manifests deterministic detection подтверждает stack.

---

# 79. Skill projection

Harness materialize'ит skills в native project locations конкретного host.

Generated skills должны иметь Harness ownership metadata.

Harness никогда не изменяет неизвестный user-owned skill.

---

# 80. Git pollution

Generated project skills по умолчанию не должны попадать в commit.

Предпочтительно:

```text
.git/info/exclude

```

Harness не должен менять `.gitignore` без необходимости.

---

# 81. Skill budget

Количество model-visible Harness skills ограничено.

Design target:

```text
около 12 или меньше

```

на project по умолчанию.

Это configurable policy, а не protocol constant.

---

# 82. Skill hot reload

Correctness не зависит от live detection нового skill в уже существующей agent session.

Если host видит skill live — хорошо.

Если нет — skill гарантируется следующей session.

---

# 83. Database

v1 использует:

```text
SQLite
WAL

```

---

# 84. Logical schema

Минимальные logical tables:

```text
projects
workspaces

files
symbols
edges

documents
document_chunks

knowledge
knowledge_anchors

tasks
task_events
task_refs

agent_sessions

skills
project_skills

embeddings

index_state
schema_meta

```

Физическая schema может отличаться.

---

# 85. Structural graph

Минимальные relations:

```text
file contains symbol
file imports file/module
module depends on module
symbol belongs to file

```

Дополнительные language-specific edges возможны.

Cross-language perfect call graph не является v1 requirement.

---

# 86. Dashboard

Dashboard является человеческим интерфейсом к той же базе, которой пользуется MCP.

Отдельной dashboard database быть не должно.

---

# 87. Projects screen

Показывает:

| ПолеНазначение |                           |
| -------------- | ------------------------- |
| Project        | проект                    |
| Focus          | текущий фокус             |
| Task           | активная/последняя задача |
| State          | working/waiting/completed |
| Last activity  | последняя активность      |
| Branch         | ветка                     |
| Dirty          | незакоммиченные изменения |
| Index          | состояние индекса         |
| Next           | следующий шаг             |

Главный UX:

> После недели отсутствия пользователь должен за несколько секунд понять, где остановился.

---

# 88. Project page

Показывает:

- current Task;
- waiting Tasks;
- recent completed Tasks;
- current focus;
- next step;
- pending feedback;
- Git state;
- changed files;
- latest verification;
- project search;
- relevant Knowledge;
- docs;
- active skills;
- index health.

---

# 89. Task page

Timeline:

```text
created
agent session attached
checkpoint
filesystem changes
waiting
operator feedback
resume
verification
completed

```

Также:

- summary;
- next step;
- changed files;
- Working Set;
- Knowledge created;
- verification;
- sessions.

---

# 90. Dashboard actions

Минимальные:

```text
Accept
Send feedback
Cancel

```

---

# 91. Realtime

Dashboard realtime v1:

```text
Server-Sent Events

```

WebSockets не нужны без доказанной необходимости.

---

# 92. Что означает «agent сейчас работает»

Harness не должен притворяться, что видит internal reasoning модели.

Без hooks dashboard знает:

- есть active MCP session;
- last MCP activity;
- Task state;
- filesystem changes;
- index activity;
- checkpoint activity.

UI показывает только наблюдаемое.

---

# 93. Project focus

При наличии `working` Task focus получается из неё.

При `waiting` Task — из waiting Task.

Если активных Tasks нет — из latest checkpoint.

Пользователь MAY установить explicit focus.

---

# 94. Legacy normal flow

```text
harness scan
    ↓
Structural Index
    ↓
Agent starts task
    ↓
project_search
    ↓
small candidate set
    ↓
agent investigates with native tools
    ↓
task_checkpoint
    ↓
Semantic Knowledge grows
    ↓
future search improves

```

---

# 95. Greenfield normal flow

```text
empty repository
    ↓
Task starts
    ↓
optional stack_hints
    ↓
agent creates files
    ↓
watcher indexes them
    ↓
agent understands code because it created it
    ↓
checkpoint records useful durable semantics

```

---

# 96. Agent workflow invariant

Для обычной работы агенту достаточно помнить:

```text
status
→ start/resume
→ search
→ native work
→ checkpoint

```

Если для корректной работы Harness требуется значительно больше ritual calls, дизайн считается ухудшившимся.

---

# 97. Human workflow invariant

Человеку достаточно:

1. открыть dashboard;
2. выбрать Project;
3. увидеть current work;
4. понять next step;
5. Accept либо написать feedback.

Human не обязан вручную поддерживать карту проекта.

---

# 98. Harness developer invariant

Новая core feature не должна требовать четырёх отдельных реализаций business logic.

Host-specific behavior ограничивается adapters.

---

# 99. Security

Harness по умолчанию local-only.

Dashboard bind:

```text
127.0.0.1 / ::1

```

Daemon IPC доступен только текущему OS user.

---

# 100. External services

Raw source MUST NOT уходить во внешние LLM/embedding services по умолчанию.

External provider требует explicit opt-in.

---

# 101. Transcript storage

Полные agent transcripts не сохраняются по умолчанию.

Harness сохраняет только structured data:

- Task events;
- checkpoint;
- Knowledge;
- verification;
- session metadata.

---

# 102. Crash consistency

Database operations transactional.

Indexer idempotent.

После crash:

- Task не становится completed автоматически;
- filesystem переиндексируется;
- stale session может стать inactive;
- последний explicit Task state сохраняется.

---

# 103. Database migrations

Schema version хранится явно.

Migrations:

```text
ordered
tested
transactional where possible

```

Перед потенциально destructive migration должен создаваться backup.

---

# 104. CLI v1

```bash
harness install
harness uninstall
harness doctor

harness scan [path]
harness status [path]

harness dashboard

harness skills list
harness skills sync
harness skills validate

```

Не расширять CLI без реального use case.

---

# 105. Configuration

Global:

```text
~/.harness/config.*

```

Project-specific runtime config преимущественно хранится в database.

Repository configuration file не обязателен в v1.

---

# 106. Extension interfaces

v1 имеет только необходимые abstractions:

```text
HostAdapter
ParserAdapter
EmbeddingProvider

```

Не создавать generic plugin framework заранее.

---

# 107. Logging

Subsystems:

```text
daemon
mcp
index
search
skills
database
dashboard
integration

```

Logs не должны dump'ить полный source/context по умолчанию.

---

# 108. Performance targets

Warm local daemon:

```text
project_status
p95 < 150 ms

```

```text
project_search
p95 < 500 ms

```

для нормально индексированного локального проекта без remote providers.

Обычный изменённый source file должен попадать в Structural Index в течение нескольких секунд.

---

# 109. MCP testing является first-class requirement

Harness должен тестироваться не только на уровне Python functions.

Test suite обязан проверять реальный MCP transport.

---

# 110. Test structure

Рекомендуемая структура:

```text
tests/
    unit/
    integration/
    search/
    mcp_contract/
    mcp_wire/
    dashboard/
    fixtures/

```

---

# 111. MCP contract tests

Snapshot/contract testing для:

```text
server capabilities
server instructions
tools/list
tool names
tool descriptions
input schemas

```

Непреднамеренное изменение model-visible contract должно быть видно в diff/tests.

---

# 112. MCP wire tests

Test запускает настоящий subprocess:

```bash
harness mcp

```

и взаимодействует через stdio JSON-RPC.

Тест должен проверять финальный serialized payload.

Не только Python object до transport layer.

---

# 113. MCP wire assertions

Примеры:

```text
test_project_status_payload_is_bounded
test_search_returns_only_default_limit
test_context_contains_only_requested_refs
test_internal_fields_never_leak
test_status_does_not_include_file_map
test_status_does_not_include_old_tasks
test_tool_catalog_is_stable

```

---

# 114. Context budget tests

Для каждого response contract существуют hard limits.

CI падает, если изменение начинает отдавать чрезмерный payload.

---

# 115. Negative disclosure tests

Fixture может содержать:

```text
1000 symbols
100 Knowledge Cards
50 Tasks
30 ADR

```

`project_status` при этом обязан возвращать только небольшой релевантный subset.

Тест явно проверяет:

```text
assert unrelated_knowledge not in payload
assert old_task not in payload
assert file_map not in payload

```

---

# 116. Synthetic project fixtures

Test repository должен включать несколько domain areas.

Например:

```text
legacy_auth/
    auth/
    billing/
    notifications/
    docs/
    tests/

```

Search:

```text
refresh token rotation

```

должен находить auth area и не загрязняться billing/notifications.

---

# 117. Semantic learning acceptance test

Исходно:

```text
legacy scan
no relevant Knowledge

```

Первый search возвращает structural candidates.

Test выполняет checkpoint с Knowledge:

```text
Refresh token replacement is atomic through SessionRepository.replace.

```

Повторный search обязан использовать это знание и улучшить retrieval.

---

# 118. Staleness acceptance test

Knowledge anchored на symbol.

Symbol изменяется.

Indexer обновляет fingerprint.

Knowledge:

```text
fresh
→ needs_revalidation

```

Search не представляет его как гарантированно актуальный факт.

---

# 119. Task continuity test

1. Session A начинает Task.
2. Делает checkpoint.
3. Session A завершается.
4. Session B получает `project_status`.
5. Session B видит Task и next step.
6. Session B resume'ит тот же `task_id`.

---

# 120. Human review acceptance test

1. Task `working`.
2. Agent делает implementation.
3. `task_checkpoint(waiting, operator_review)`.
4. Dashboard показывает Ready for review.
5. User feedback.
6. Та же Task возвращается в `working`.
7. New agent session продолжает.
8. User Accept.
9. Task `completed`.

---

# 121. Skill relevance acceptance test

Project stack:

```text
Next.js
Postgres
Playwright

```

Registry:

```text
Next.js
Postgres
Playwright
Godot
Unity
FastAPI

```

Model-visible generated skills не должны содержать:

```text
Godot
Unity
FastAPI

```

## 121.1 Contextual mobile/web relevance acceptance

Expo/React Native package:

```text
expo
react
react-dom
react-native
react-native-web

```

и даже сохранённые CSS/HTML design artifacts не должны сами по себе активировать
`public-frontend`. Resolver активирует `mobile-application`. Если отдельный package того же
Workspace содержит однозначный web framework (например Next/Nuxt/SvelteKit), Workspace получает
одновременно `mobile-app` и `web-frontend`, и оба surface skill остаются релевантными.

## 121.2 Polyglot project pack acceptance

Workspace одновременно содержит Expo/Android frontend и FastAPI/Alembic backend. Resolver
materialize bounded stack baseline для обеих частей независимо от current Task `stack_hints`.
Per-task выбор среди projected Skills остаётся host-native. Явно включённый project skill остаётся
выбранным.

---

# 122. Greenfield skill acceptance

Greenfield без manifests не получает stack-matched skills.

После создания manifests stack определяется автоматически.

---

# 123. No manual map maintenance test

Agent создаёт новый source file.

Watcher индексирует его.

Никакой `update_map` call не требуется.

---

# 124. Dashboard tests

FastAPI endpoints тестируются через HTTP client.

Должны проверяться:

- project list;
- project page;
- Task state;
- Accept;
- Feedback;
- search;
- SSE events;
- error states.

---

# 125. Реальная граница проверяемости

Core Harness должен быть полностью проверяем без proprietary agent host.

Можно детерминированно проверить:

```text
DB
→ search
→ MCP tool implementation
→ serialization
→ exact stdout payload

```

То есть можно точно установить:

> Что Harness реально отдал MCP client.

---

# 126. Что не может считаться core automated proof

MCP protocol не определяет внутренний final model prompt конкретного proprietary host.

Поэтому локальные core tests не могут доказать:

```text
как Claude Code сформировал final internal context
как Cursor вставил payload в prompt
как Codex ранжировал возможность tool call

```

Это отдельный integration boundary.

---

# 127. Agent Host Acceptance Matrix

Для каждого поддерживаемого host существует acceptance checklist:

```text
MCP discovered
tools visible
server instructions visible/used
project_status callable
search callable
Task lifecycle works
native skill discovered
irrelevant skills absent
normal agent naturally uses Harness

```

---

# 128. Automated vs host-specific verification

| RequirementCore automatedReal host |   |   |
| ---------------------------------- | - | - |
| Database                           | ✓ |   |
| Search                             | ✓ |   |
| Task lifecycle                     | ✓ |   |
| Exact MCP response                 | ✓ |   |
| Response size                      | ✓ |   |
| Forbidden data absent              | ✓ |   |
| Tool schemas                       | ✓ |   |
| Server instructions payload        | ✓ |   |
| Dashboard                          | ✓ |   |
| Host sees MCP                      |   | ✓ |
| Host invokes MCP naturally         |   | ✓ |
| Host discovers skill               |   | ✓ |
| Host-specific context behaviour    |   | ✓ |

---

# 129. Dependency reproducibility

Repository должен иметь reproducible dependency definition:

```text
pyproject.toml
lock file

```

Поддерживаемый development environment должен позволять запустить:

```bash
tests
daemon
MCP
dashboard
scanner

```

без ручного редактирования environment.

---

# 130. Не писать собственный MCP protocol stack

Production implementation MUST использовать официальный MCP SDK, если не обнаружена конкретная несовместимость, делающая это невозможным.

Raw JSON-RPC client допускается для независимого transport-level testing.

---

# 131. Failure behaviour

Если Harness недоступен:

- agent не должен становиться unusable;
- native workflow остаётся возможным;
- Harness bootstrap не должен запрещать работу;
- host может сообщить пользователю, что Harness context unavailable.

Harness — accelerator/control plane, а не обязательный single point of failure coding runtime.

---

# 132. MVP scope

Первая production-capable версия включает:

1. global installation;
2. daemon;
3. SQLite;
4. global MCP integrations;
5. Claude/Codex/Cursor/Antigravity adapters;
6. Project/Workspace registry;
7. deterministic legacy scan;
8. incremental indexing;
9. structural search;
10. documentation search;
11. Tasks;
12. Agent Sessions;
13. Working Sets;
14. Knowledge Cards;
15. staleness;
16. five MCP tools;
17. response budgets;
18. MCP exposure tests;
19. skills registry;
20. relevant skill projection;
21. optional Task `stack_hints` as durable metadata, not a Skill selector;
22. basic dashboard;
23. Accept/Feedback;
24. doctor/uninstall;
25. end-to-end fixture tests.

---

# 133. Explicitly postponed

После v1 и только при доказанной необходимости:

- cloud sync;
- multiple users;
- remote dashboard;
- shared team Knowledge;
- issue tracker integrations;
- GitHub synchronization;
- PR automation;
- transcript ingestion;
- advanced call graph;
- automatic LLM documentation;
- raw-code vectorization;
- remote mandatory embeddings;
- generic plugin system;
- complex token analytics;
- agent quality scoring;
- automatic Task planning;
- autonomous Task creation;
- cross-device synchronization.

---

# 134. Search acceptance criteria

### Exact path

Correct path MUST попадать в top results.

### Exact symbol

Indexed exact symbol MUST попадать в top results.

### Relevant docs

Known ADR/document должен быть discoverable.

### Fresh Knowledge

Directly relevant fresh Knowledge Card SHOULD попадать в top results.

### Stale Knowledge

Stale record не должен выглядеть как current authoritative fact.

---

# 135. MCP acceptance criteria

`project_status`:

- bounded;
- current;
- no bulk project data.

`project_search`:

- top relevant results;
- bounded;
- no full code dumps.

`project_context`:

- explicit refs only.

`task_start`:

- creates/resumes stable Task.

`task_checkpoint`:

- persists Task progress;
- accepts semantic updates;
- updates state;
- does not require manual structural map updates.

---

# 136. Primary product invariant

После каждой meaningful Task проект должен становиться немного дешевле для следующей Task.

Это улучшение не должно требовать от агента отдельного большого процесса документирования.

Harness автоматически сохраняет всё механическое.

Agent сообщает только семантически ценное знание, которое уже получил во время работы.

---

# 137. Primary complexity invariant

При выборе между двумя архитектурными решениями предпочтение отдаётся более простому, если более сложное решение не даёт доказуемого пользовательского преимущества.

Это относится ко всем трём направлениям:

```text
agent complexity
human complexity
Harness implementation complexity

```

---

# 138. Definition of Done v1

Harness v1 считается соответствующим ТЗ только если одновременно выполняется всё следующее:

- Harness устанавливается глобально;
- один daemon обслуживает все проекты;
- поддерживаемые hosts видят Harness MCP;
- legacy repository индексируется без LLM;
- новый code индексируется автоматически;
- `project_status` даёт компактное актуальное состояние;
- `project_search` находит relevant code/docs/knowledge;
- `project_context` раскрывает только выбранные сущности;
- Task переживает смену agent session;
- Task может пережить смену agent host;
- human feedback продолжает ту же Task;
- human может механически Accept Task;
- objective-complete Task может закрываться агентом;
- Semantic Knowledge добавляется через checkpoint;
- Knowledge имеет provenance;
- изменённые anchors инвалидируют freshness;
- stale Knowledge не выдаётся как актуальный факт;
- agent не обязан обновлять Structural Index вручную;
- irrelevant skills не загружаются в project;
- Skills выбираются по detected project stack; per-task выбор среди projected Skills остаётся host-native;
- greenfield без stack evidence не получает stack-matched skills;
- project stack далее определяется автоматически;
- normal agent workflow ограничивается status/start/search/work/checkpoint;
- MCP responses имеют hard exposure limits;
- exact MCP wire payloads покрыты automated tests;
- tests проверяют отсутствие ненужных данных;
- dashboard и MCP используют один source of truth;
- hooks не требуются для correctness;
- Harness работает без обязательного cloud service;
- development stack полностью запускаем и тестируем в доступном execution environment;
- host-specific uncertainties изолированы отдельным acceptance layer.

---

# 139. Финальная архитектурная формула

```text
                    Human
                      │
                      ▼
                  Dashboard
                      │
                      ▼
Claude ─┐
Codex  ─┤
Cursor ─┼──── MCP ─── harnessd
AG     ─┘               │
                        ├── Tasks
                        ├── Structural Index
                        ├── Search
                        ├── Knowledge
                        ├── Documentation
                        └── Skill Resolver

```

Harness не заставляет агента изучать Harness.

Harness не заставляет человека быть project manager.

Harness не заставляет собственного разработчика обслуживать четыре разных продукта.

Он создаёт один компактный, проверяемый и host-independent слой Project Intelligence, который делает следующую работу над проектом дешевле предыдущей.
