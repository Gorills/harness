# Финальный независимый аудит Harness Skills

**Репозиторий:** `Gorills/harness`  
**Проверенная ветка:** `main`  
**Проверенный commit:** `3138012837142806f4fb7ed046823a7235b13d27`  
**Дата аудита:** 2026-08-31  
**Область:** built-in skills, canonical registry, resolution/composition, projection, install/upgrade lifecycle, doctor, acceptance, tests и фактическое содержание 22 built-in skills.

---

## 1. Итог

Skill subsystem Harness в целом спроектирован качественно: ownership и collision handling строгие, projection bounded и host-aware, progressive disclosure реализован осмысленно, а содержимое большинства built-in skills существенно сильнее обычного набора общих best practices.

При этом в текущем `main` есть несколько дефектов, которые затрагивают корректность lifecycle, trust boundary и composition реальных инструкций, устанавливаемых в проекты.

### Production verdict

**Не рекомендую считать текущую skill subsystem production-grade для безусловного unattended rollout, пока не закрыты три P1.**

Архитектуру переписывать не требуется. Основные проблемы локализованы и исправимы в существующей модели.

### P1 release blockers

1. Retired/renamed built-in остаётся в canonical registry и продолжает участвовать в resolution/projection.
2. Built-in sync обещает rollback, но rollback реализован best-effort и может оставить частично изменённый registry без явного сообщения об этом.
3. `backend-security` / `server-auth-review` task focus может оставить только короткий `backend-security`, отфильтровав более полный `secure-by-design` с security verification guidance.

### P2

4. Runtime skill loading не enforcing тот же owner/mode trust contract, который `doctor` уже считает обязательным.
5. Real Codex acceptance проверяет MCP и projected set, но не доказывает native skill invocation/read.
6. Stack detector существенно уже заявленной ecosystem breadth skills pack.
7. `ci-release` недостаточно самодостаточен по supply-chain security для task-focused `github-actions`.
8. `observability` не содержит cardinality guard для metrics.

### P3

9. Универсальное `44 x 44 logical pixels` лучше сделать platform-specific.
10. `isolated-development.md` всё ещё пишет про 12 built-in skills, хотя count давно не invariant.
11. Built-in YAML frontmatter строится raw string interpolation вместо безопасного scalar serialization.

---

# 2. Область проверки

В рамках аудита проверены:

- `src/harness/builtin_skills.py`
  - `BuiltinSkill.files`
  - весь `BUILTIN_SKILLS`
  - `sync_builtin_skills`
  - manifest ownership
  - staging/replacement/rollback/finalization;
- `src/harness/skills.py`
  - registry loading
  - stack detection
  - manifest parsing
  - task-focused resolution
  - budget/ranking
  - projection planning/application;
- `src/harness/skill_runtime.py`
  - daemon/runtime reconciliation path;
- `src/harness/installation.py`
  - install/upgrade skill reconciliation
  - purge safety;
- `src/harness/doctor.py`
  - skill registry permissions
  - generated skill inspection;
- `src/harness/mcp_bridge.py`
  - contract вокруг `stack_hints`;
- `tests/test_builtin_skills.py`;
- `tests/test_skills.py`;
- `tests/test_installation_upgrade.py`;
- `scripts/accept_codex.py`;
- `scripts/quality.py`;
- `.github/workflows/ci.yml`;
- ADR-0029;
- `docs/release-linux.md`;
- `docs/host-compatibility.md`;
- `docs/development/isolated-development.md`.

Также сверены меняющиеся внешние факты с актуальной документацией:

- Cursor Agent Skills;
- GitHub Actions secure use;
- Prometheus instrumentation/cardinality;
- Apple accessibility control sizing;
- Android touch target sizing;
- Spring Boot build systems;
- Flutter `pubspec.yaml`.

---

# 3. P1-1: retired/renamed built-in остаётся активным

**Severity: HIGH**  
**Priority: P1**  
**Статус: подтверждено по source path и lifecycle**

## 3.1. Фактический путь

`sync_builtin_skills()`:

1. читает старый `.harness-builtin-skills.json` в `owned`;
2. строит `desired` только из текущего `BUILTIN_SKILLS`;
3. итерируется только по текущему `BUILTIN_SKILLS`;
4. обновляет `owned[current_id]`;
5. пишет весь `owned` обратно.

Отсутствует операция:

```python
stale_ids = set(owned) - set(desired)
```

Следовательно, ID, исчезнувший из новой версии продукта, не удаляется ни из manifest, ни с диска.

Source:

- `src/harness/builtin_skills.py::sync_builtin_skills`

## 3.2. Почему это реально влияет на проекты

Это не просто оставшийся каталог.

`load_skill_registry()` загружает **каждую non-hidden директорию** canonical registry как `SkillDefinition`. Он не фильтрует directory IDs через текущий `BUILTIN_SKILLS` или ownership manifest.

Source:

- `src/harness/skills.py::load_skill_registry`

Далее `reconcile_workspace_skills()` использует этот registry как authoritative input:

```text
registry -> load_skill_registry -> resolve_workspace_skills -> projection
```

Source:

- `src/harness/skill_runtime.py::reconcile_workspace_skills`

То есть retired skill остаётся полноценно живым.

## 3.3. Upgrade path тоже не спасает

`install_harness()` действительно вызывает:

```python
sync_builtin_skills(_skill_registry_path(environment))
```

до host/runtime mutation.

Source:

- `src/harness/installation.py::install_harness`

Это усиливает finding: product contract ожидает, что reinstall/upgrade reconciliation приведёт built-ins к текущему состоянию, но remove-side reconciliation отсутствует.

## 3.4. Doctor тоже не поймает retirement

`doctor` проверяет, что registry:

- безопасен по типу/правам;
- парсится;
- projected state соответствует **текущему registry**.

Но retired skill уже считается валидной частью текущего registry.

Поэтому после upgrade можно получить:

```text
old retired built-in
+
new current built-ins
=
doctor-consistent canonical registry
```

Это особенно неприятный класс дефекта: диагностика способна сказать, что система консистентна относительно неправильного источника истины.

## 3.5. Исправление

Нужен symmetric reconciliation.

Рекомендуемая политика:

```python
desired = {current built-ins}
owned_before = load_manifest()
stale = owned_before.keys() - desired.keys()
```

Для каждого stale ID:

### Stale path отсутствует

Удалить ID из manifest.

### Stale path существует и hash == recorded ownership hash

Это всё ещё exact Harness-owned old content.

Удалить его **транзакционно**, через staging/backup, а не `rmtree` до commit manifest.

### Stale path существует, но пользователь его изменил

Не удалять пользовательские данные.

Предпочтительный UX:

- relinquish Harness ownership;
- сохранить каталог как user-owned skill;
- удалить ID из built-in ownership manifest;
- явно отразить `released`/`preserved_modified_retired` в результате sync.

Альтернатива — fail closed с инструкцией оператору. Она безопаснее формально, но способна навсегда блокировать upgrade из-за легитимной пользовательской модификации старого skill.

## 3.6. Обязательные regression tests

```text
test_builtin_pack_removes_unmodified_retired_skill
test_builtin_pack_rename_removes_old_and_installs_new
test_retired_missing_directory_drops_manifest_ownership
test_modified_retired_skill_is_preserved_and_released
test_retired_skill_no_longer_loads_after_sync
test_retired_skill_no_longer_projects_after_sync
test_retirement_rolls_back_if_manifest_commit_fails
```

---

# 4. P1-2: built-in sync не выполняет заявленный rollback contract

**Severity: HIGH**  
**Priority: P1**  
**Статус: подтверждено по реализации rollback path**

ADR-0029 явно говорит:

> Failures roll back in-process replacements.

Но фактическая реализация слабее этого контракта.

Source:

- `docs/decisions/0029-quality-discipline-verification-and-response-economy.md`
- `src/harness/builtin_skills.py::_rollback_replacements`

Текущий код концептуально:

```python
def _rollback_replacements(items):
    for item in reversed(items):
        try:
            remove_current_target()
            restore_backup()
        except OSError:
            continue
```

## 4.1. Failure mode №1: rollback failure скрывается

Если удаление нового target или `os.replace(backup, target)` падает с `OSError`, ошибка игнорируется.

Затем `sync_builtin_skills()` выбрасывает **исходную** ошибку.

Caller видит:

```text
built-in sync failed
```

но не получает информацию:

```text
previous registry state could not be restored
```

При этом часть skill directories уже может остаться в новом состоянии.

## 4.2. Failure mode №2: rollback может оборваться неожиданным wrapper exception

`_path_exists()` сам преобразует `OSError` в `BuiltinSkillError`.

Но `_rollback_replacements()` ловит только `OSError`.

Следовательно, ошибка inspection внутри rollback способна:

- оборвать оставшуюся rollback-последовательность;
- замаскировать исходную причину;
- оставить ещё больше частично изменённого состояния.

## 4.3. Почему это material

Manifest пишется **после** directory replacements.

Поэтому естественный сценарий:

1. один или несколько skills заменены;
2. `_write_manifest()` падает;
3. начинается rollback;
4. rollback одного target падает;
5. sync завершается ошибкой;
6. registry уже не равен состоянию до операции.

Для системы, которая проецирует инструкции в реальные проекты, это нарушение атомарности продукта, а не косметическая обработка исключения.

## 4.4. Исправление

Использовать тот же принцип, который уже реализован качественнее в projection subsystem:

```python
def _rollback_replacements(...) -> Exception | None:
    first_error = None

    for item in reversed(items):
        try:
            ...
        except (OSError, BuiltinSkillError) as exc:
            if first_error is None:
                first_error = exc

    return first_error
```

И caller:

```python
except Exception as exc:
    rollback_error = _rollback_replacements(replacements)
    if rollback_error is not None:
        raise BuiltinSkillError(
            "built-in skill sync failed and prior registry state could not be restored"
        ) from rollback_error
    raise
```

Важно:

- продолжать попытку rollback остальных entries;
- не удалять surviving backup при failed restore;
- сообщать путь сохранённого backup, если автоматическое восстановление невозможно;
- не заявлять atomic rollback, если он не доказан.

## 4.5. Тесты

Минимум:

```text
manifest write fails after one successful replacement -> exact old state restored

manifest write fails after multiple replacements -> all old states restored

rollback target removal fails -> explicit "could not restore" failure

rollback backup restore fails -> backup preserved + explicit recovery path

one rollback item fails -> remaining rollback items are still attempted
```

Сейчас в `tests/test_builtin_skills.py` отдельного rollback-failure coverage не обнаружено.

---

# 5. P1-3: `backend-security` теряет security verification из-за неполной composition metadata

**Severity: HIGH**  
**Priority: P1**  
**Статус: подтверждено как defect built-in composition metadata**

## 5.1. Архитектурный контекст

Task-focused filtering — **намеренная архитектура**, зафиксированная в ADR и покрытая тестами:

если хоть один skill распознал текущий task hint, stack-only skills исключаются.

Это позволяет polyglot repository не тащить mobile/backend/web instructions одновременно.

Source:

- `src/harness/skills.py::resolve_skills`
- `tests/test_skills.py::test_recognized_task_hints_suppress_unrelated_stack_only_skills`
- ADR-0029 task-focused amendment

Значит исправлять общий resolver только потому, что он делает именно то, что обещано, было бы странным способом провести code review.

## 5.2. Где настоящий дефект

`backend-security` имеет только:

```text
backend-security
server-auth-review
```

как `task_hints`.

Ни один другой built-in этих hints не содержит.

В частности, `secure-by-design` их не содержит.

Поэтому для software project:

```text
task_hints = ["backend-security"]
```

получаем:

1. `backend-security` имеет task match;
2. `secure-by-design` совпадает только по stack facet `software-project`;
3. task-focus включается;
4. stack-only `secure-by-design` удаляется.

С единственным hint `backend-security` текущая built-in metadata фактически сводит focused set к `backend-security`.

## 5.3. Почему это не просто «хотелось бы больше текста»

`backend-security` содержит короткий базовый список:

- auth/authz separation;
- structural input validation;
- parameterized DB access;
- output encoding;
- cookie flags/SameSite;
- password KDF;
- secret hygiene;
- fail closed.

Но `secure-by-design` содержит отдельные progressive references, включая:

- threat/security architecture;
- horizontal/vertical authorization verification;
- tenant/object/action negative tests;
- guessed identifiers;
- stale roles;
- token/session lifecycle;
- SSRF;
- upload/archive handling;
- CORS/CSRF;
- abuse/resource bounds;
- отдельный `security verification` reference.

Для задачи, которая **явно называется server auth review**, потеря verification guidance является реальным снижением корректности.

## 5.4. В проекте уже есть правильный паттерн решения

ADR-0029 отдельно объясняет, почему `frontend-design` дублирует task hints public/mobile skills:

> чтобы task-focused projection не сохранил surface skill, одновременно молча выкинув design guidance.

Это прямой архитектурный прецедент для security composition.

## 5.5. Что исправить

### Не вводить `requires:` DSL в рамках этого исправления

ADR-0029 прямо фиксирует:

> Harness does not add a composition DSL.

Добавлять новую DSL только ради этой проблемы сейчас избыточно.

### Минимально правильный patch

Добавить в `secure-by-design.task_hints`:

```text
backend-security
server-auth-review
```

И добавить regression tests для обоих hints.

Дополнительно стоит решить, должен ли `testing-strategy` тоже получать эти hints. Для security review это разумно, но `secure-by-design/references/verification.md` уже несёт специализированный verification contract, поэтому минимальный correctness fix — гарантировать именно `secure-by-design`.

### Regression expectation

```python
selected = resolve_skills(
    definitions,
    software_project_stack,
    task_hints=("backend-security",),
)

assert "backend-security" in ids(selected)
assert "secure-by-design" in ids(selected)
```

То же для `server-auth-review`.

Также нужен negative assertion, что unrelated frontend/mobile skills не вернулись.

## 5.6. Долгосрочно

Если подобных overlap-зависимостей станет много, тогда можно заново обсудить composition metadata/DSL как отдельное архитектурное решение.

Сейчас это не оправдано: существующая модель с deliberate shared hints уже используется и понятна.

---

# 6. P2-1: registry trust contract диагностируется doctor, но не enforced runtime

**Severity: HIGH с ограниченной предпосылкой**  
**Priority: P2**  
**Статус: новый finding**

## 6.1. Что сделано правильно

`doctor` проверяет canonical skill registry как trust boundary:

- real directory;
- current user owner;
- нет group/other write bits.

То есть проект уже признаёт:

```text
group/world-writable skill registry == FAIL
```

Source:

- `src/harness/doctor.py::_inspect_skill_registry_permissions`

## 6.2. Где разрыв

Обычный runtime reconciliation:

```python
reconcile_workspace_skills(...)
    -> load_skill_registry(root)
```

не вызывает эту проверку.

`load_skill_registry()` проверяет:

- directory exists;
- not symlink;
- directory type;
- child type/symlink safety;

но не проверяет owner/mode canonical root.

Source:

- `src/harness/skill_runtime.py::reconcile_workspace_skills`
- `src/harness/skills.py::load_skill_registry`

`_prepare_registry()` при built-in sync проверяет owner, но не запрещает уже существующий group/world-writable mode.

Source:

- `src/harness/builtin_skills.py::_prepare_registry`

## 6.3. Failure mode

На shared system или при unsafe custom `HARNESS_SKILL_REGISTRY`:

1. registry принадлежит пользователю Harness, но writable группой;
2. другой локальный principal меняет `SKILL.md`;
3. оператор не запускает `harness doctor`;
4. watcher/scan вызывает normal reconciliation;
5. modified instruction загружается и проецируется в project skills.

Это instruction injection через локальную trust-boundary конфигурацию.

Default path создаётся `0700`, поэтому это **не default remote exploit**. Именно поэтому finding P2, а не P1.

Но если doctor уже считает состояние `FAIL`, runtime не должен продолжать его использовать.

## 6.4. Исправление

Вынести один shared validator, например:

```python
validate_skill_registry_trust(root)
```

и использовать его в:

- `load_skill_registry`;
- `sync_builtin_skills`;
- doctor;
- purge preflight.

Минимальный POSIX contract:

- real directory;
- not symlink;
- owner == effective uid;
- `mode & 0o022 == 0`.

Нужно отдельно решить parent replacement boundary для custom registry paths. Если parent writable чужими principals, проверка только root недостаточна против rename/replace. Для default `~/.harness/skills` лучше обеспечить private Harness parent.

## 6.5. Тесты

```text
runtime load rejects group-writable registry
runtime load rejects world-writable registry
runtime load rejects foreign-owned registry
doctor and runtime use identical trust decision
sync refuses unsafe pre-existing registry
workspace reconcile fails closed on unsafe registry
```

---

# 7. P2-2: semantic acceptance существует, но не доказывает native skill invocation

**Severity: MEDIUM**  
**Priority: P2**  
**Статус: подтверждён gap behavioral skill acceptance**

## 7.1. Что acceptance уже доказывает

В проекте действительно есть:

- `scripts/accept_codex.py`;
- `--preflight-only`;
- optional `--run-model`;
- temporary isolated Harness/Codex state;
- exact wheel install;
- два temporary Git Workspace;
- вызов всех пяти Harness MCP tools;
- проверка exact relevant/no irrelevant projected skill set;
- cleanup;
- защита `~/.codex/config.toml`.

`docs/release-linux.md` честно описывает, что proprietary Codex acceptance пока open.

Это хороший уровень дисциплины.

## 7.2. Чего всё же нет

Acceptance доказывает:

```text
Harness projected the expected skills
+
Codex MCP behaved correctly
+
model used Harness MCP tools in model mode
```

Но не доказывает:

```text
native host presented skill metadata to model
+
model selected the relevant skill from description
+
model actually read SKILL.md
+
model followed its instructions
```

В `accept_codex.py` model evidence ориентирован прежде всего на MCP calls; отдельного proof-of-skill-read нет.

## 7.3. Лучшее исправление: nonce behavioral acceptance

Не стоит пытаться угадать по prose ответа, «кажется ли он достаточно security-aware».

Для acceptance можно создать temporary synthetic skill:

```text
name: acceptance-skill
description: Use when asked to perform the synthetic acceptance workflow.
```

В body положить случайный nonce, неизвестный prompt:

```text
When this skill is applied, return this exact marker in field skill_marker:
<random-runtime-nonce>
```

Model prompt описывает задачу, совпадающую только с `description`, но **не содержит nonce**.

Acceptance проходит только если output содержит nonce.

Это доказывает:

1. discovery;
2. description relevance;
3. invocation;
4. body read.

Нужен также negative prompt, где skill не должен активироваться.

После synthetic proof можно держать небольшой built-in scenario corpus:

```text
auth review
database migration
github-actions edit
React Native APK fix
public SEO page
Docker production rollout
legacy bugfix
```

Для каждого:

- expected projected set;
- forbidden unrelated set;
- behavioral invariant.

## 7.4. Host scope

Codex CLI можно автоматизировать первым.

Cursor/Claude, где полноценная automation зависит от proprietary runtime, должны оставаться explicit acceptance matrix, без выдуманного «CI доказал UI».

---

# 8. P2-3: stack detection не соответствует ширине skill pack

**Severity: MEDIUM**  
**Priority: P2**  
**Статус: подтверждено с уточнениями**

## 8.1. Что детектор реально парсит

Dependency extraction реализован для:

- `package.json`;
- `pyproject.toml`;
- `requirements*.txt`;
- `Cargo.toml`;
- `go.mod`;
- `composer.json`.

Language suffix map не содержит `.dart`.

## 8.2. Rails/Sinatra

`_BACKEND_DEPENDENCIES` содержит:

```text
rails
sinatra
```

но Gemfile/Gemfile.lock не анализируются.

Следовательно, это vocabulary без нормального source of evidence для canonical Ruby ecosystem.

Ruby source всё ещё даст `language-engineering`, но backend facet может не появиться.

## 8.3. Spring/JVM

Java/Kotlin source определяется.

Но Maven/Gradle dependencies не анализируются, поэтому backend-service classification для типичного Spring Boot проекта отсутствует.

Текущая Spring Boot документация рекомендует Maven/Gradle как основные build systems.

## 8.4. .NET

`.csproj` помогает получить `software-project` path facet, но framework/package metadata не разбирается.

Типичный ASP.NET проект поэтому не получает аналогичный automatic backend-service signal.

## 8.5. Flutter

`_path_facets()` способен получить `mobile-app` из Android-generated files (`AndroidManifest.xml`, Android Gradle paths), поэтому обычный Flutter mobile checkout часто всё же классифицируется как mobile.

Но остаются реальные gaps:

- `.dart` не является language;
- `pubspec.yaml` не анализируется;
- dependency token `flutter` находится в `_MOBILE_DEPENDENCIES`, который фактически применяется к package.json-derived dependency set;
- canonical Flutter dependency source — `pubspec.yaml`.

То есть **сам `flutter` dependency marker действительно находится не в том evidence path**, хотя mobile facet может появиться другим путём.

## 8.6. Исправление

Добавлять ecosystem support только через deterministic bounded parsers, без выполнения project code.

Приоритет:

1. Dart:
   - `.dart -> dart`
   - `pubspec.yaml`
   - Flutter SDK dependency -> `mobile-app`.

2. Ruby:
   - предпочтительно `Gemfile.lock` как более детерминированный источник;
   - Rails/Sinatra facet.

3. JVM:
   - `pom.xml` через bounded XML parsing;
   - conservative Gradle/version-catalog evidence без выполнения Gradle.

4. .NET:
   - `.csproj`
   - `Microsoft.NET.Sdk.Web`
   - bounded `PackageReference` extraction.

И для каждого — fixture tests на реальные минимальные manifests.

---

# 9. P2-4: `ci-release` теряет важную supply-chain часть в focused GitHub Actions task

**Severity: MEDIUM**  
**Priority: P2**

`ci-release` имеет task hint:

```text
github-actions
```

и в focused task с таким единственным hint фактически должен быть самодостаточным.

Сейчас skill хорошо требует:

- existing CI contract;
- reproducible versions;
- required gates;
- secret mechanism;
- minimum permission scope;
- migration/recovery;
- exact candidate verification.

Но не фиксирует важные GitHub Actions supply-chain boundaries.

Актуальная GitHub документация говорит, что pin action к **full-length commit SHA** — способ использовать immutable action release.

Показательно, что **сам Harness CI уже делает это правильно**:

```yaml
uses: actions/checkout@3d3c42...
uses: actions/upload-artifact@043fb...
uses: astral-sh/setup-uv@20cfd...
```

Source:

- `.github/workflows/ci.yml`

То есть repository practice сильнее собственного reusable skill.

## Исправление

Добавить в `ci-release` примерно такие требования:

- preserve repository/org policy;
- third-party actions pin to reviewed full commit SHA where policy supports it;
- verify pinned SHA belongs to expected upstream;
- keep `GITHUB_TOKEN` permissions minimum;
- never expose privileged secrets to untrusted fork PR code;
- prefer short-lived OIDC credentials over static cloud keys where supported;
- use protected environments/approval for privileged release;
- preserve artifact provenance/attestations when repository already uses them.

Не обязательно тащить весь `secure-by-design` в каждый CI edit. `ci-release` может быть самодостаточным именно на своей trust boundary.

---

# 10. P2-5: `observability` не защищает от metrics cardinality explosion

**Severity: MEDIUM**  
**Priority: P2**

Текущий skill хорошо говорит про:

- structured events;
- correlation IDs;
- secret/PII redaction;
- user-impact/saturation/error metrics;
- traces;
- actionable alerts;
- incident evidence.

Но ничего не говорит про bounded metric labels/attributes.

Prometheus прямо предупреждает:

- каждый unique labelset создаёт отдельную time series;
- labels имеют RAM/CPU/disk/network cost;
- user IDs/email/unbounded dimensions не должны становиться metric labels.

Для production observability это не редкая микрооптимизация. Одна строка с `user_id` label может превратить красивый dashboard в дорогой способ узнать, что RAM конечна.

## Исправление

Добавить:

```text
- Keep metric labels/attributes bounded. Never use user IDs, emails, request IDs,
  raw URLs, arbitrary exception text, or other unbounded values as metric dimensions;
  use normalized route/error classes and put high-cardinality detail in logs/traces.
- Define units and histogram/bucket semantics deliberately and account for telemetry cost.
```

Забавная деталь репозитория: regression test, который симулирует изменение `observability`, уже использует строку:

```text
Prefer bounded cardinality for metric labels.
```

как тестовый новый body, но production skill её не содержит.

Это хороший кандидат перестать использовать как шутку теста и перенести в реальный skill.

---

# 11. P3: mobile target wording

**Severity: LOW**  
**Priority: P3**  
**Предыдущая severity была завышена**

Текущий текст:

> Primary touch controls should usually be at least 44 x 44 logical pixels; never go below the applicable accessibility minimum.

Это не грубая ошибка, потому что вторая часть прямо требует соблюдать applicable platform minimum.

Но wording всё равно может повести слабую модель к универсальному `44` baseline.

Актуально:

- Apple: iOS/iPadOS default control size `44x44 pt`;
- Android/Compose: типичный minimum interactive/touch target `48x48 dp`.

## Исправление

```text
Follow the target platform's current accessibility guidance.
For iOS/iPadOS, 44x44 pt is the normal default control size.
For Android, touch targets should normally provide at least 48x48 dp.
Do not go below the applicable platform/accessibility minimum.
```

Это P3, не release blocker.

---

# 12. P3: hardcoded `12 built-in skills` в документации

**Severity: LOW**

`docs/development/isolated-development.md` всё ещё утверждает:

```text
The first isolated scan also reconciles the 12 built-in skills
```

При этом текущий pack содержит 22 `BuiltinSkill(...)` entries.

Что особенно показательно: ADR-0029 уже правильно говорит:

> The first implementation contained 12 skills; that count is not a product invariant.

## Исправление

Не обновлять `12` на `22`, иначе через неделю человечеству снова понадобится аудитор.

Заменить на:

```text
The first isolated scan also reconciles the current built-in skill pack...
```

Если count где-то действительно нужен, генерировать его из `BUILTIN_SKILLS`.

---

# 13. P3: raw YAML frontmatter interpolation

**Severity: LOW / hardening**

`BuiltinSkill.files()` строит:

```python
frontmatter = f"---\nname: {self.skill_id}\ndescription: {self.description}\n---\n\n"
```

Текущие descriptions проверены и не дают известного malformed frontmatter.

Поэтому это **не текущий production bug**.

Но будущий description с YAML-sensitive содержимым может разойтись между:

- Harness conservative parser;
- настоящим host YAML parser.

## Исправление без новой dependency

JSON string syntax является допустимым YAML scalar representation.

Можно сериализовать значения:

```python
name = json.dumps(self.skill_id, ensure_ascii=False)
description = json.dumps(self.description, ensure_ascii=False)
```

и использовать quoted values в frontmatter.

Добавить test с:

- `:`
- `#`
- quotes
- Unicode
- newline-like escape

и проверить generated frontmatter тем же contract, который ожидают host surfaces.

---

# 14. Что в системе выглядит действительно сильным

Важно не превратить аудит в коллекцию способов испортить настроение.

## 14.1. Projection ownership

Сильные свойства:

- user/tracked collision refusal;
- ownership marker;
- tree hash;
- no symlink projection;
- stale owned cleanup;
- late-race checks;
- rollback path;
- Git-local `info/exclude`, без переписывания `.gitignore`.

Projection subsystem заметно строже built-in registry sync. Собственно поэтому rollback finding выше так хорошо виден: рядом уже есть более правильный образец.

## 14.2. Host graph planning

Codex/Cursor sharing `.agents/skills` и fail-closed incompatible three-host graph выглядят архитектурно разумно.

Текущая Cursor документация подтверждает:

- `.agents/skills/`;
- `.cursor/skills/`;
- compatibility roots;
- recursive discovery;
- `name`/`description`;
- `description` используется для relevance.

Current Codex source также использует repo `.agents/skills`.

## 14.3. Progressive disclosure

References реально отделены от entrypoint и тестируется наличие links.

Это снижает context pressure без потери deep guidance.

## 14.4. Content quality

Большинство skills остаются сильными:

| Skill | Повторная оценка |
|---|---|
| `architecture-decisions` | Strong |
| `testing-strategy` | Strong |
| `backend-security` | Needs composition fix |
| `secure-by-design` | Strong |
| `container-infrastructure` | Strong |
| `observability` | Good, cardinality gap |
| `scalability-architecture` | Good |
| `ci-release` | Good base, supply-chain gap |
| `public-frontend` | Strong |
| `frontend-design` | Strong, minor platform wording |
| `server-application` | Strong |
| `mobile-application` | Good/Strong in stated scope |
| `godot-development` | Strong |
| `deployment-operations` | Strong |
| `project-architecture` | Strong |
| `legacy-preservation` | Strong |
| `language-engineering` | Strong content; detector narrower |
| `data-integrity` | Strong |
| `complex-change-planning` | Good |
| `spec-audit` | Strong |
| `independent-review` | Strong |
| `project-conventions` | Good |

## 14.5. CI itself

`.github/workflows/ci.yml`:

- checks exact candidate identity;
- pins third-party actions to full SHAs;
- uses locked environment;
- runs the repository quality gate;
- builds exact PR source/toolchain evidence.

Это хороший сигнал инженерной дисциплины, хотя не отменяет найденные lifecycle bugs.

## 14.6. Release docs честно не преувеличивают proprietary acceptance

`docs/release-linux.md` явно говорит, что Codex/Cursor proprietary-host acceptance остаётся open.

Это лучше, чем объявить VERIFIED то, что никто не проверял.

---

# 15. Рекомендуемый порядок исправлений

## Slice 1 — retired built-ins + atomic removal

Один законченный bounded task:

- stale owned ID reconciliation;
- preserve/release modified retired skill policy;
- transactional removal;
- tests;
- install/skills-sync integration coverage.

**STOP после этого slice.**

## Slice 2 — built-in rollback correctness

Независимый следующий task:

- rollback error propagation;
- continue-restoring remaining entries;
- preserve recovery artifacts;
- failure-injection tests.

## Slice 3 — security task composition

- shared `backend-security` / `server-auth-review` hints;
- regression tests;
- no new DSL.

## Slice 4 — registry trust enforcement

- shared root validator;
- runtime loader enforcement;
- doctor reuse;
- tests.

После этих четырёх система уже заметно ближе к production-grade.

Затем:

5. behavioral host skill acceptance;
6. ecosystem detection coverage;
7. `ci-release`;
8. `observability`;
9. docs/frontmatter/mobile wording.

---

# 16. Конкретный acceptance matrix после исправлений

## Registry lifecycle

| Scenario | Expected |
|---|---|
| same version sync | no mutation |
| built-in body update | exact owned old -> new |
| user modified current built-in | refuse overwrite |
| built-in retired | exact owned old removed |
| modified built-in retired | preserved + ownership released/warned |
| rename | old retirement + new install |
| manifest write failure | exact pre-sync state restored |
| rollback failure | explicit recovery failure, no silent success |

## Resolver/composition

| Task hint | Must include | Must not regress to |
|---|---|---|
| `backend-security` | backend-security, secure-by-design | backend-security only |
| `server-auth-review` | backend-security, secure-by-design | backend-security only |
| `auth` | architecture-decisions, secure-by-design, testing-strategy | unrelated frontend/mobile |
| `github-actions` | ci-release | generic unrelated surfaces |
| `expo` | mobile-application, frontend-design, secure-by-design where currently intended | public-frontend |
| unknown hint | stack baseline | empty selection |

## Registry trust

| Registry state | Runtime |
|---|---|
| current-user, non-writable by group/other | accept |
| symlink root | reject |
| foreign owner | reject |
| group writable | reject |
| world writable | reject |
| unsafe root during watcher reconciliation | fail closed |

## Host behavioral acceptance

| Check | Proof |
|---|---|
| discovery | synthetic skill appears |
| relevance | matching prompt triggers |
| body read | hidden runtime nonce returned |
| negative relevance | unrelated prompt does not return nonce |
| references | selected reference instruction affects evidence |
| de-duplication | one visible copy |
| cleanup | retired projection disappears |

---

# 17. Финальный вердикт

Главный риск skill subsystem находится не столько в качестве prose-инструкций, сколько в lifecycle, trust enforcement и task-focused composition.

Ключевые архитектурные выводы:

1. **Task-focused resolver сам по себе соответствует принятому contract.** Дефект находится в неполной built-in composition metadata для security review.
2. **Acceptance infrastructure существует**, но её текущая граница не доказывает behavioral evidence native skill invocation/read.
3. **Built-in rollback contract и runtime registry trust enforcement требуют отдельного исправления**, потому что затрагивают целостность инструкций, проецируемых в реальные проекты.
4. **Большинство built-in skills содержательно качественные**, поэтому исправления должны быть точечными, а не сопровождаться переписыванием всей подсистемы.

### Production status

**CONDITIONAL / NOT YET PRODUCTION-GRADE FOR UNATTENDED SKILL LIFECYCLE**

Причины:

- P1 retirement correctness;
- P1 rollback correctness;
- P1 security composition.

После закрытия этих трёх P1 основной release objection можно снять при условии прохождения regression suite.

Registry permission enforcement следует закрыть сразу следом до широкого multi-user/custom-registry deployment.

Остальные findings являются важными quality improvements, но не требуют архитектурной перезаписи системы.

---

# 18. Источники

## Repository

Pinned commit:

`https://github.com/Gorills/harness/tree/3138012837142806f4fb7ed046823a7235b13d27`

Ключевые файлы:

- `src/harness/builtin_skills.py`
- `src/harness/skills.py`
- `src/harness/skill_runtime.py`
- `src/harness/installation.py`
- `src/harness/doctor.py`
- `src/harness/mcp_bridge.py`
- `tests/test_builtin_skills.py`
- `tests/test_skills.py`
- `tests/test_installation_upgrade.py`
- `scripts/accept_codex.py`
- `.github/workflows/ci.yml`
- `docs/decisions/0029-quality-discipline-verification-and-response-economy.md`
- `docs/release-linux.md`
- `docs/host-compatibility.md`
- `docs/development/isolated-development.md`

## Current external contracts checked on 2026-08-31

Cursor Agent Skills:

`https://prod.cursor.com/docs/skills`

GitHub Actions secure use:

`https://docs.github.com/en/actions/reference/security/secure-use`

Prometheus instrumentation:

`https://prometheus.io/docs/practices/instrumentation/`

Prometheus metric/label naming:

`https://prometheus.io/docs/practices/naming/`

Apple accessibility:

`https://developer.apple.com/design/human-interface-guidelines/accessibility`

Android interactive target reference:

`https://developer.android.com/reference/kotlin/androidx/compose/ui/Modifier`

Spring Boot build systems:

`https://docs.spring.io/spring-boot/reference/using/build-systems.html`

Flutter pubspec:

`https://docs.flutter.dev/tools/pubspec`

---

# 19. Verification status

## VERIFIED

- Текущий `main` зафиксирован на commit `3138012837142806f4fb7ed046823a7235b13d27`.
- Прочитан полный built-in pack и relevant registry/resolver/projection/runtime lifecycle.
- Подтверждён code path `install_harness -> sync_builtin_skills`.
- Подтверждено отсутствие stale-ID subtraction в `sync_builtin_skills`.
- Подтверждено, что `load_skill_registry` загружает оставшийся retired directory.
- Подтверждён deliberate task-focus contract resolver.
- Подтверждено, что `backend-security` и `server-auth-review` встречаются только в specialized skill metadata и не дублируются в `secure-by-design`.
- Подтверждён ADR precedent shared task hints для `frontend-design`.
- Подтверждён rollback implementation и несоответствие заявленному rollback contract.
- Подтверждено различие между doctor permission check и runtime load enforcement.
- Подтверждена реальная Codex acceptance infrastructure и её фактическая граница.
- Подтверждён stack detector manifest/language scope.
- Проверены current external facts для Cursor, GitHub Actions, Prometheus, Apple/Android, Spring и Flutter.
- Проверено, что Harness CI itself pins external actions к full commit SHA.

## NOT VERIFIED

- Репозиторные тесты **не запускались локально в этом audit runtime**.
- Попытка локально клонировать exact repository commit была сделана, но container runtime не смог разрешить `github.com` через DNS.
- Не выполнялся proprietary Codex/Cursor/Claude interactive run в этой сессии.
- Не выполнялся exploit/race test на shared multi-user filesystem.
- Не выполнялся реальный upgrade с исторически retired built-in, потому что для этого нужна runnable checkout/history fixture.

Поэтому source-proven findings выше не маркируются как runtime-reproduced там, где runtime reproduction фактически не было.

## ASSUMPTIONS

- Canonical skill registry является trusted instruction input. Это следует из architecture/runtime usage и подтверждается тем, что doctor считает group/other-writable registry `FAIL`.
- User-modified retired skill следует сохранять как user-owned content, а не молча удалять. Это рекомендация policy, а не существующий contract.
- Severity registry-permission finding предполагает shared/misconfigured/custom filesystem boundary; для default freshly-created `0700` registry риск существенно ниже.
- Behavioral skill acceptance должен доказывать body read, а не только наличие файлов. Это критерий качества данного аудита, а не заявленный текущий Harness contract.

## BLOCKERS

Нет blocker для завершения source-level независимого аудита.

Ограничение локального исполнения зафиксировано в `NOT VERIFIED` и не маскируется под успешный test run.
