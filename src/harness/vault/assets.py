"""Private-origin vault UI; no external assets, telemetry or parent messaging."""

VAULT_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Личное хранилище</title><link rel="stylesheet" href="/vault.css"></head>
<body><main>
<header class="heading"><div><p class="eyebrow">ЛИЧНОЕ ХРАНИЛИЩЕ</p>
<h1 id="project-heading">Заметки и доступы</h1></div><div class="heading-actions">
<button id="settings-open" hidden>Настройки хранилища</button><button id="lock" hidden>Заблокировать</button>
</div></header>
<p id="message" role="status" aria-live="polite"></p>
<section id="gate" class="gate">
<p class="eyebrow">ТОЛЬКО ДЛЯ ВАС</p><h2 id="gate-title">Хранилище закрыто</h2>
<p class="muted">Все доступы и заметки по проектам в одном месте.</p>
<button id="device-unlock" class="primary" hidden>Открыть через системное хранилище</button>
<button id="open-without-password" class="primary" hidden>Открыть хранилище</button>
<form id="unlock-form" autocomplete="off">
<label id="create-mode" hidden>Режим хранения<select id="access-mode">
<option value="no_password">Без пароля</option><option value="password">С мастер-паролем</option>
</select></label><p id="no-password-hint" class="muted" hidden>Открывается сразу, без автоблокировки.
Файл хранилища и новые копии можно открыть без пароля.</p>
<label id="master-label">Мастер-пароль<input id="master" type="password" required maxlength="1024"
 autocomplete="current-password"></label>
<div id="setup" hidden>
<label id="master-confirm-label">Повторите мастер-пароль<input id="master-confirm" type="password"
 autocomplete="new-password" maxlength="1024"></label>
<label>Папка для резервных копий<input id="initial-folder" placeholder="/media/backup/projects"
 maxlength="4096" spellcheck="false"></label>
<p class="muted">Выберите существующую папку на другом диске или в синхронизируемом каталоге.
</p>
</div>
<label id="remember-label" class="check"><input id="remember" type="checkbox">
<span>Открывать автоматически на этом компьютере<small>Мастер-пароль сохранится в системном хранилище ключей.</small></span></label>
<button class="primary" id="unlock-submit">Открыть хранилище</button></form>
<details id="initial-restore" hidden><summary>Восстановить из резервной копии</summary>
<p class="muted">Выберите файл .kdbx. Укажите пароль, только если копия была защищена.
 Текущая версия будет заменена с сохранением отдельной копии.</p>
<label>Пароль копии (если был)<input id="initial-backup-password" type="password" maxlength="1024" autocomplete="off"></label>
<label>Папка копий<input id="recovery-folder" maxlength="4096" spellcheck="false"
 placeholder="/media/backup/projects"></label>
<input id="initial-file" type="file" accept=".kdbx" aria-label="Резервная копия">
<button id="initial-restore-button" type="button">Восстановить хранилище</button></details>
</section>
<section id="workspace" hidden>
<div class="toolbar"><div class="filters" aria-label="Тип записей">
<button data-kind="all" class="selected" aria-pressed="true">Все</button>
<button data-kind="note" aria-pressed="false">Заметки</button>
<button data-kind="server" aria-pressed="false">Серверы</button>
<button data-kind="password" aria-pressed="false">Пароли</button>
<button data-kind="ssh" aria-pressed="false">SSH</button></div>
<button class="primary" id="new">Добавить запись</button></div>
<div class="records-layout">
<aside class="list-pane" aria-label="Записи хранилища">
<input id="search" type="search" placeholder="Поиск по записям…"
 aria-label="Поиск в хранилище" autocomplete="off">
<div class="list-meta"><label class="scope"><input id="all-projects" type="checkbox">Все проекты</label>
<span id="count" class="muted"></span></div>
<div id="records" class="records"></div></aside>
<div class="detail-pane">
<button id="back-to-records" class="back-to-records" type="button">К списку записей</button>
<div id="selection-empty" class="selection-empty"><h2>Ваши записи под рукой</h2>
<p>Выберите запись слева, чтобы посмотреть или скопировать доступы.</p>
<button id="empty-new">Добавить запись</button></div>
<section id="viewer" class="viewer" hidden aria-label="Просмотр записи">
<header class="editor-heading"><div><p id="view-meta" class="eyebrow"></p>
<h2 id="view-title" tabindex="-1"></h2></div><button id="edit-record">Изменить</button></header>
<div id="view-fields"></div></section>
<section id="editor" class="editor" hidden><form id="record-form" autocomplete="off">
<header class="editor-heading"><h2 id="editor-title">Новая запись</h2>
<button id="close-editor" type="button">Отмена</button></header>
<label>Название<input name="title" required maxlength="256" placeholder="Например, Production / SSH"></label>
<div class="two"><label>Тип<select name="kind"><option value="note">Заметка</option>
<option value="password">Пароль</option><option value="server">Сервер</option>
<option value="ssh">SSH-доступ</option></select></label>
<label>Проект<input name="project_name" required maxlength="256"></label></div>
<input name="project_id" type="hidden"><button id="assign-project" type="button">Перенести в текущий проект</button>
<div class="two" data-server><label>Сервер / IP<input name="host" maxlength="1024" spellcheck="false"></label>
<label>SSH-порт<input name="port" inputmode="numeric" maxlength="5" placeholder="22"></label></div>
<label data-access>Логин<input name="username" maxlength="1024" autocomplete="off" spellcheck="false"></label>
<div data-access><label for="record-password">Пароль</label><div class="secret-field">
<input id="record-password" name="password" type="password" maxlength="16384" autocomplete="new-password">
<button type="button" id="reveal">Показать</button><button type="button" id="copy">Копировать</button></div></div>
<label data-access>Адрес сайта<input name="url" maxlength="2048" placeholder="https://…" spellcheck="false"></label>
<details data-server><summary>Приватный SSH-ключ</summary>
<textarea name="private_key" maxlength="65536" rows="6" spellcheck="false"
 aria-label="Приватный SSH-ключ"></textarea></details>
<label>Заметки<textarea name="notes" maxlength="65536" rows="7" spellcheck="false"></textarea></label>
<footer class="editor-actions"><button class="primary">Сохранить запись</button>
<button id="delete" type="button" class="danger" hidden>Удалить</button></footer>
<p class="muted save-hint">При сохранении автоматически создаётся резервная копия.</p>
</form></section></div></div>
<div class="vault-footer"><span id="backup-state"></span><span id="access-badge">Личное хранилище</span></div>
<dialog id="settings-dialog" aria-labelledby="settings-title">
<header class="editor-heading"><h2 id="settings-title">Настройки хранилища</h2>
<button id="settings-close" type="button">Закрыть</button></header>
<p id="settings-message" role="status" aria-live="polite"></p>
<section class="settings-section"><h3>Режим хранения</h3>
<p id="access-state" class="muted"></p><button id="disable-password" type="button">Использовать без пароля</button>
<p id="disable-password-hint" class="muted">Пароль больше не потребуется при открытии.
Новые копии будут без пароля; старые сохранят прежний пароль.</p>
<div id="device-settings"><h3>Быстрый вход</h3><p id="device-state" class="muted"></p>
<button id="device-remember" type="button">Включить быстрый вход</button>
<button id="device-forget" type="button" hidden>Отключить быстрый вход</button>
<p class="muted">Системное хранилище ключей открывает доступ после входа в учётную запись компьютера.
 Мастер-пароль нужен для восстановления на другом устройстве.</p></div></section>
<section class="settings-section"><h3>Резервные копии</h3>
<p class="muted">Полная копия при каждом сохранении. Старые версии сохраняются.</p>
<form id="backup-form"><label>Папка резервных копий<input id="folder" required maxlength="4096"
 spellcheck="false"></label><button>Сохранить папку и сделать копию</button></form>
<details><summary>Восстановить сохранённую версию</summary>
<form id="restore-form" autocomplete="off"><label>Файл .kdbx<input id="restore-file" type="file"
 accept=".kdbx" required></label><label>Пароль копии (если был)<input id="restore-password" type="password"
 maxlength="1024" autocomplete="off"></label>
<p class="muted">Текущее содержимое будет заменено. Перед заменой сохранится отдельная копия.</p>
<button class="danger">Проверить и восстановить</button></form></details></section>
<section class="settings-section"><details><summary id="password-title">Изменить мастер-пароль</summary>
<form id="password-form" autocomplete="off"><label>Новый пароль<input id="new-password" type="password"
 required minlength="12" maxlength="1024" autocomplete="new-password"></label>
<label>Повторите пароль<input id="new-password-confirm" type="password" required
 maxlength="1024" autocomplete="new-password"></label>
<p class="muted">Старые копии открываются старым паролем. После смены включите быстрый вход заново.</p>
<button>Изменить пароль</button></form></details></section>
<p id="idle-hint" class="muted">Через 15 минут бездействия хранилище блокируется. Переходы между проектами
 и обновление страницы не требуют повторного ввода пароля.</p></dialog>
</section><noscript>Для разблокировки хранилища включите JavaScript.</noscript>
</main><script src="/vault.js" defer></script></body></html>"""

VAULT_CSS = """
:root{color-scheme:dark;--bg:#0b0d12;--panel:#121620;--line:#252c3a;--text:#edf0f7;
--muted:#9ba6b9;--accent:#9badff;--selected:#1c2540;font-family:Inter,"Segoe UI",system-ui,sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-size:14px}
main{max-width:1440px;margin:auto;padding:24px}h1{font-size:24px;letter-spacing:-.6px;margin:4px 0}
h2{font-size:21px;letter-spacing:-.3px;margin:0}h3{font-size:16px;margin:0 0 10px}
.eyebrow{font-size:10px;letter-spacing:1.6px;color:var(--muted);margin:0 0 7px}
.heading,.heading-actions,.toolbar,.editor-heading,.editor-actions,.list-meta,.vault-footer{
display:flex;align-items:center;justify-content:space-between;gap:12px}
.heading{margin-bottom:24px}.heading-actions{justify-content:flex-end}.muted{color:var(--muted);line-height:1.6}
button,input,textarea,select{font:inherit;color:inherit}button{border:1px solid var(--line);border-radius:7px;
padding:9px 12px;cursor:pointer;background:var(--panel);line-height:1.3}button:hover{border-color:var(--accent)}
button:disabled{opacity:.55;cursor:wait}.primary{background:var(--accent);color:#13182a;border-color:var(--accent);font-weight:650}
.danger{color:#ffa4a4}input:not([type=checkbox]),textarea,select{display:block;width:100%;border:1px solid var(--line);
border-radius:7px;padding:10px 12px;background:var(--bg);margin-top:6px}textarea{resize:vertical}
:focus-visible{outline:2px solid var(--accent);outline-offset:3px}label{display:block;font-size:12px;
color:var(--muted);margin:0 0 16px}label input,label textarea,label select{color:var(--text);font-size:14px}
.gate{max-width:470px;margin:48px auto;padding:30px;border:1px solid var(--line);border-radius:12px;background:var(--panel)}
.gate h2{margin:8px 0}.gate form>button,.gate>#device-unlock{width:100%}.gate>#device-unlock{margin:8px 0 20px}
.check{display:flex;gap:10px;align-items:flex-start;line-height:1.5;color:var(--text)}.check small{display:block;color:var(--muted);margin-top:5px}
.toolbar{border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:0}.filters{display:flex;gap:4px;flex-wrap:wrap}
.filters button{border-color:transparent;background:transparent;color:var(--muted)}.filters .selected{color:var(--accent);background:var(--selected)}
.records-layout{display:grid;grid-template-columns:minmax(240px,300px) minmax(0,1fr);min-height:max(300px,calc(100dvh - 215px))}
.list-pane{padding:18px 16px 18px 0;border-right:1px solid var(--line);min-width:0}
.list-pane>input{margin:0}.list-meta{margin:15px 0;font-size:11px}.scope{display:flex;align-items:center;gap:6px;margin:0}
.records{display:grid;gap:4px;align-content:start;max-height:65vh;overflow:auto}
.record{display:flex;align-items:center;text-align:left;gap:10px;width:100%;padding:13px 10px;border:1px solid transparent;background:transparent;border-radius:7px}
.record.selected{background:var(--selected);border-color:#35436a}.record-copy{display:grid;gap:6px;min-width:0}
.record-copy strong{font-size:13px;font-weight:600;overflow-wrap:anywhere}.record-copy small{font-size:11px;color:var(--muted);overflow-wrap:anywhere}
.detail-pane{padding:26px 0 26px 28px;min-width:0}.editor-heading{align-items:flex-start;margin-bottom:24px}
.back-to-records{display:none}
.editor-heading h2{overflow-wrap:anywhere}.editor-heading>button{flex-shrink:0}.two{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.secret-field{display:flex;gap:6px;margin:-8px 0 16px}.secret-field input{margin:0;min-width:0}.secret-field button{font-size:12px}
.editor-actions{justify-content:flex-start;margin-top:20px}.save-hint{font-size:11px}
.selection-empty{padding:85px 25px;text-align:center;color:var(--muted);line-height:1.7}.selection-empty h2{font-size:19px;color:var(--text)}
.selection-empty button{margin-top:10px}.empty{padding:32px 12px;color:var(--muted);line-height:1.7;text-align:center}
.detail-field{padding:16px 0;border-bottom:1px solid var(--line)}.detail-label{display:block;color:var(--muted);font-size:11px;margin-bottom:9px}
.detail-value-row{display:flex;gap:10px;align-items:center;justify-content:space-between}.detail-value{white-space:pre-wrap;overflow-wrap:anywhere;min-width:0;line-height:1.65}
.detail-buttons{display:flex;gap:5px;flex-shrink:0}.detail-buttons button{font-size:11px;padding:7px 9px}
.detail-field.notes{padding-top:25px;border:0}.notes .detail-value{font-size:14px;line-height:1.8}
.vault-footer{border-top:1px solid var(--line);padding-top:14px;color:var(--muted);font-size:11px}
details{margin-top:16px}summary{cursor:pointer;padding:10px 0;color:var(--muted)}
dialog{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:12px;
width:min(600px,calc(100vw - 32px));max-height:85vh;padding:26px}dialog::backdrop{background:#0009}
.settings-section{padding:20px 0;border-top:1px solid var(--line)}.settings-section .muted{font-size:12px}
#message{margin:0}#message:not(:empty){margin-bottom:16px;padding:12px 15px;background:#20283d;color:#d5deff;border-radius:7px;line-height:1.5}
#message.error{background:#3b2027;color:#ffbbc7}[hidden]{display:none!important}
@media(max-width:700px){main{padding:16px}.heading{gap:12px;align-items:flex-start}.heading-actions{flex-direction:column;gap:6px}
.toolbar{flex-wrap:wrap}.records-layout{grid-template-columns:1fr}.list-pane{padding:16px 0;border-right:0;border-bottom:1px solid var(--line)}
.records{max-height:240px}.detail-pane{padding:22px 0}.two{grid-template-columns:1fr;gap:0}.vault-footer{flex-wrap:wrap;line-height:1.5}
.back-to-records{display:block;margin-bottom:18px}.detail-pane:has(#selection-empty:not([hidden])) .back-to-records{display:none}
#workspace:has(#selection-empty[hidden]) .toolbar,.records-layout:has(#selection-empty[hidden]) .list-pane{display:none}
.detail-pane:has(#selection-empty:not([hidden])){display:none}
.gate{padding:22px;margin:20px auto}.detail-value-row{align-items:flex-start;flex-wrap:wrap}.heading h1{font-size:21px}}
@media(prefers-color-scheme:light){:root{color-scheme:light;--bg:#f8f9fc;--panel:#fff;--line:#dce1ec;
--text:#202636;--muted:#606c82;--accent:#425bd0;--selected:#e9edff}.primary{color:#fff}}
"""

VAULT_JS = r"""
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const context = new URLSearchParams(location.hash.slice(1));
  const projectId = context.get('project') || 'personal';
  const projectName = context.get('name') || 'Личное';
  const kinds = {note: 'Заметка', password: 'Пароль', server: 'Сервер', ssh: 'SSH-доступ'};

  const errors = {
    password_required: 'Это хранилище защищено мастер-паролем. Сначала откройте его, затем смените режим в настройках.',
    device_unavailable: 'Системное хранилище ключей недоступно или запрос отменён. Откройте мастер-паролем и проверьте GNOME Keyring / KWallet.',
    locked: 'Хранилище заблокировано. Введите мастер-пароль.',
    weak_password: 'Мастер-пароль должен содержать не менее 12 символов.',
    invalid_password_or_backup: 'Неверный пароль или повреждённая резервная копия.',
    invalid_backup: 'Этот файл не является поддерживаемой копией хранилища.',
    already_exists: 'Хранилище уже создано. Обновите страницу и разблокируйте его.',
    backup_directory: 'Укажите существующую папку для копий. Проверьте подключение диска.',
    backup_failed: 'Не удалось проверить резервную копию. Изменения не сохранены.',
    unsafe_path: 'Путь недоступен или имеет неподходящие права доступа.',
    storage_failed: 'Ошибка записи. Проверьте папку копий, свободное место и откройте хранилище снова.',
    conflict: 'Хранилище изменилось. Откройте его заново, чтобы не затереть другую версию.',
    too_large: 'Превышен допустимый размер: до 16 МиБ на хранилище и до 5000 записей.',
    invalid_record: 'Проверьте поля записи, название и SSH-порт (1–65535).',
    not_found: 'Запись больше не существует.', try_later: 'Повторите попытку через несколько секунд.',
    invalid_request: 'Не удалось обработать запрос.'
  };
  let token = '', revision = '', records = [], exists = true, selected = '', filter = 'all';
  let busy = false, epoch = 0, idleAt = Date.now(), dirty = false;
  let hiddenAt = 0, currentRecord = null, device = {}, accessMode = 'password';
  const sessionKey = 'harness-vault-session';
  const session = (value) => {
    try {
      if (value === undefined) return sessionStorage.getItem(sessionKey) || '';
      if (value) sessionStorage.setItem(sessionKey, value);
      else sessionStorage.removeItem(sessionKey);
    } catch (_) { /* Storage may be disabled; keychain/manual entry still works. */ }
    return '';
  };
  const manuallyLocked = (value) => {
    try {
      if (value === undefined) return sessionStorage.getItem(`${sessionKey}-locked`) === '1';
      if (value) sessionStorage.setItem(`${sessionKey}-locked`, '1');
      else sessionStorage.removeItem(`${sessionKey}-locked`);
    } catch (_) { /* Server-side lock remains authoritative. */ }
    return false;
  };
  token = session();
  $('project-heading').textContent = projectName;
  $('all-projects').checked = projectId === 'all';
  const message = (text = '', error = false) => {
    $('message').textContent = text; $('message').classList.toggle('error', error);
    $('settings-message').textContent = text;
  };
  const clear = (forget = true) => {
    if (forget) session('');
    epoch++; token = ''; revision = ''; records = []; selected = ''; dirty = false;
    $('records').replaceChildren(); $('record-form').reset(); $('restore-form').reset();
    $('password-form').reset(); $('unlock-form').reset(); $('folder').value = '';
    $('initial-backup-password').value = '';
    $('backup-state').textContent = ''; $('editor').hidden = true;
    $('workspace').hidden = true; $('lock').hidden = true; $('gate').hidden = false;
    $('initial-restore').hidden = false;
    currentRecord = null; $('viewer').hidden = true; $('view-fields').replaceChildren();
    $('view-title').textContent = ''; $('view-meta').textContent = '';
    $('selection-empty').hidden = false; $('settings-open').hidden = true;
    $('settings-dialog').close(); gateMode();
  };
  const api = async (action, data = {}, credential = token) => {
    const requestEpoch = epoch;
    const response = await fetch('/api', {method: 'POST', cache: 'no-store', keepalive: action === 'lock',
      headers: {'Content-Type': 'application/json', 'Authorization': `Bearer ${credential}`},
      body: JSON.stringify({action, ...data})});
    const value = await response.json();
    if (requestEpoch !== epoch) throw new Error(errors.locked);
    if (!response.ok) {
      if (response.status === 401) clear();
      throw new Error(errors[value.error] || 'Хранилище недоступно. Обновите страницу.');
    }
    return value;
  };
  const run = async (callback) => {
    if (busy) return;
    busy = true;
    document.querySelectorAll('button,input,textarea,select').forEach((field) => { field.disabled = true; });
    message('Подождите…');
    try {
      await callback();
      if ($('message').textContent === 'Подождите…') message();
    }
    catch (error) { message(error.message || 'Хранилище недоступно.', true); }
    finally {
      busy = false;
      document.querySelectorAll('button,input,textarea,select').forEach((field) => { field.disabled = false; });
    }
  };
  const scope = (item) => $('all-projects').checked || item.project_id === projectId;
  const render = () => {
    const query = $('search').value.toLocaleLowerCase().trim();
    const matches = records.filter((item) => scope(item) && (filter === 'all' || item.kind === filter)
      && `${item.title} ${item.host} ${item.project_name}`.toLocaleLowerCase().includes(query));
    $('count').textContent = `Записей: ${matches.length}`;
    $('records').replaceChildren();
    if (!matches.length) {
      const empty = document.createElement('div'); empty.className = 'empty';
      empty.textContent = query ? 'Записи не найдены' : 'Добавьте первую заметку, сервер или доступ';
      $('records').append(empty);
    }
    for (const item of matches) {
      const button = document.createElement('button'); button.className = 'record';
      button.classList.toggle('selected', item.id === selected);
      button.setAttribute('aria-pressed', String(item.id === selected));
      const copy = document.createElement('span'); copy.className = 'record-copy';
      const title = document.createElement('strong'); title.textContent = item.title;
      const meta = document.createElement('small');
      meta.textContent = [kinds[item.kind], item.host, $('all-projects').checked ? item.project_name : '']
        .filter(Boolean).join(' · ');
      copy.append(title, meta); button.append(copy);
      button.addEventListener('click', () => run(async () => {
        if (!discard()) return;
        const currentEpoch = epoch;
        const result = await api('detail', {id: item.id});
        if (epoch === currentEpoch) view(result.record);
      }));
      $('records').append(button);
    }
  };
  const apply = (value) => {
    revision = value.revision; records = value.records; exists = true;
    if (value.token) { token = value.token; session(token); manuallyLocked(false); }
    accessMode = value.access_mode || 'password';
    deviceState(value.device);
    idleAt = Date.now();
    $('folder').value = value.backup_folder;
    $('backup-state').textContent = value.backup_at
      ? `Резервная копия · ${new Date(value.backup_at).toLocaleString('ru-RU')}` : 'Копий пока нет';
    $('workspace').hidden = false; $('gate').hidden = true; $('lock').hidden = accessMode === 'no_password';
    $('settings-open').hidden = false;
    const open = accessMode === 'no_password';
    $('access-badge').textContent = open ? 'Без пароля' : 'Защищено мастер-паролем';
    $('access-state').textContent = open
      ? 'Без пароля. Хранилище открывается сразу, без системной связки ключей и автоблокировки.'
      : 'Хранилище защищено мастер-паролем.';
    $('disable-password').hidden = open; $('disable-password-hint').hidden = open;
    $('device-settings').hidden = open; $('idle-hint').hidden = open;
    $('password-title').textContent = open ? 'Установить мастер-пароль' : 'Изменить мастер-пароль';
    $('setup').hidden = true; $('initial-restore').hidden = true;
    $('gate-title').textContent = 'Хранилище закрыто';
    $('unlock-submit').textContent = 'Открыть хранилище';
    $('unlock-form').reset(); gateMode(); render();
    if (value.device_error) message(errors[value.device_error], true);
  };
  const deviceState = (value = {}) => {
    device = value;
    $('device-unlock').hidden = !device.enabled || accessMode === 'no_password';
    $('remember-label').hidden = !device.available;
    $('remember').checked = !!device.available;
    $('device-remember').hidden = !!device.enabled;
    $('device-forget').hidden = !device.enabled;
    $('device-state').textContent = device.enabled
      ? 'Быстрый вход включён. Пароль хранится в системной связке ключей.'
      : 'Быстрый вход выключен. Включите его, чтобы не вводить пароль после перезапуска.';
  };
  const gateMode = () => {
    const open = exists ? accessMode === 'no_password' : $('access-mode').value === 'no_password';
    $('create-mode').hidden = exists;
    $('unlock-form').hidden = exists && open;
    $('open-without-password').hidden = !exists || !open;
    $('master-label').hidden = open; $('master').required = !open;
    $('master-confirm-label').hidden = open;
    $('remember-label').hidden = open || !device.available;
    $('no-password-hint').hidden = exists || !open;
    $('gate-title').textContent = !exists ? 'Создайте личное хранилище'
      : open ? 'Открыть хранилище' : 'Хранилище закрыто';
  };
  const copyValue = async (value, label) => {
    await navigator.clipboard.writeText(value); message(`${label}: скопировано.`);
  };
  const view = (item) => {
    currentRecord = item; selected = item.id; dirty = false; form.reset();
    $('editor').hidden = true; $('selection-empty').hidden = true; $('viewer').hidden = false;
    $('view-title').textContent = item.title;
    $('view-meta').textContent = `${kinds[item.kind]} · ${item.project_name}`;
    const container = $('view-fields'); container.replaceChildren();
    const add = (label, value, secret = false, notes = false) => {
      if (!value) return;
      const row = document.createElement('div'); row.className = `detail-field${notes ? ' notes' : ''}`;
      const name = document.createElement('span'); name.className = 'detail-label'; name.textContent = label;
      const body = document.createElement('div'); body.className = 'detail-value-row';
      const text = document.createElement('div'); text.className = 'detail-value';
      text.textContent = secret ? '••••••••••••' : value;
      const actions = document.createElement('div'); actions.className = 'detail-buttons';
      if (secret) {
        const reveal = document.createElement('button'); reveal.textContent = 'Показать';
        reveal.setAttribute('aria-label', `Показать: ${label}`);
        let visible = false;
        const hide = () => { visible = false; text.textContent = '••••••••••••'; reveal.textContent = 'Показать'; };
        reveal.addEventListener('click', () => {
          visible = !visible; text.textContent = visible ? value : '••••••••••••';
          reveal.textContent = visible ? 'Скрыть' : 'Показать';
          if (visible) setTimeout(hide, 20000);
        }); actions.append(reveal);
      }
      const copy = document.createElement('button'); copy.textContent = 'Копировать';
      copy.setAttribute('aria-label', `Копировать: ${label}`);
      copy.addEventListener('click', () => run(() => copyValue(value, label)));
      actions.append(copy); body.append(text, actions); row.append(name, body); container.append(row);
    };
    if (item.kind !== 'note') {
      add('Сервер', item.host); add('SSH-порт', item.port); add('Логин', item.username);
      add('Пароль', item.password, true); add('Адрес сайта', item.url);
      add('Приватный SSH-ключ', item.private_key, true);
    }
    add('Заметки', item.notes, false, true);
    if (!container.childElementCount) {
      const empty = document.createElement('p'); empty.className = 'muted';
      empty.textContent = 'Пока нет данных. Нажмите «Изменить», чтобы добавить их.'; container.append(empty);
    }
    render();
    requestAnimationFrame(() => $('view-title').focus());
  };
  const discard = () => !dirty || confirm('Закрыть запись без сохранения изменений?');
  const form = $('record-form');
  const field = (name) => form.elements.namedItem(name);
  const types = () => {
    const kind = field('kind').value;
    form.querySelectorAll('[data-access]').forEach((item) => { item.hidden = kind === 'note'; });
    form.querySelectorAll('[data-server]').forEach((item) => {
      item.hidden = !['server', 'ssh'].includes(kind);
    });
  };
  const edit = (item = {}) => {
    form.reset(); selected = item.id || '';
    $('viewer').hidden = true; $('selection-empty').hidden = true;
    for (const input of form.querySelectorAll('input[name],textarea[name],select[name]')) {
      input.value = item[input.name] || ({kind: filter === 'all' ? 'note' : filter,
        project_id: projectId === 'all' ? 'personal' : projectId,
        project_name: projectId === 'all' ? 'Личное' : projectName}[input.name] || '');
    }
    field('password').type = 'password'; $('reveal').textContent = 'Показать';
    form.querySelectorAll('details').forEach((item) => { item.open = false; });
    $('editor-title').textContent = selected ? 'Редактировать запись' : 'Новая запись';
    $('delete').hidden = !selected; $('editor').hidden = false; dirty = false; types();
    $('assign-project').hidden = projectId === 'all' || field('project_id').value === projectId;
    render(); requestAnimationFrame(() => field('title').focus());
  };
  $('unlock-form').addEventListener('submit', (event) => {
    event.preventDefault(); void run(async () => {
      if (!exists && $('access-mode').value !== 'no_password' && $('master').value !== $('master-confirm').value) {
        throw new Error('Пароли не совпадают.');
      }
      apply(await api(exists ? 'unlock' : 'create', {password: $('master').value,
        folder: $('initial-folder').value, remember: $('remember').checked, access_mode: $('access-mode').value}));
    });
  });
  $('lock').addEventListener('click', () => {
    if (!discard()) return;
    manuallyLocked(true);
    const previous = token; clear(); message('Хранилище заблокировано.');
    void api('lock', {}, previous).catch(() => {});
  });
  $('empty-new').addEventListener('click', () => { if (discard()) edit(); });
  $('edit-record').addEventListener('click', () => { if (currentRecord) edit(currentRecord); });
  $('settings-open').addEventListener('click', () => { message(); $('settings-dialog').showModal(); });
  $('settings-close').addEventListener('click', () => $('settings-dialog').close());
  $('back-to-records').addEventListener('click', () => {
    if (!discard()) return;
    form.reset(); dirty = false; currentRecord = null; selected = '';
    $('editor').hidden = true; $('viewer').hidden = true; $('view-fields').replaceChildren();
    $('view-title').textContent = ''; $('view-meta').textContent = '';
    $('selection-empty').hidden = false; render(); $('search').focus();
  });
  $('access-mode').addEventListener('change', gateMode);
  $('open-without-password').addEventListener('click', () => run(async () => {
    apply(await api('open_without_password'));
  }));
  $('disable-password').addEventListener('click', () => run(async () => {
    apply(await api('disable_password', {revision}));
    manuallyLocked(false); message('Режим без пароля включён. Новая резервная копия создана.');
  }));
  $('device-unlock').addEventListener('click', () => run(async () => {
    apply(await api('device_unlock', {automatic: false}));
  }));
  $('device-remember').addEventListener('click', () => run(async () => {
    apply(await api('device_remember')); message('Быстрый вход включён на этом компьютере.');
  }));
  $('device-forget').addEventListener('click', () => run(async () => {
    apply(await api('device_forget')); message('Пароль удалён из системного хранилища ключей.');
  }));
  $('new').addEventListener('click', () => { if (discard()) edit(); });
  $('close-editor').addEventListener('click', () => {
    if (discard()) {
      form.reset(); dirty = false; $('editor').hidden = true;
      if (currentRecord) view(currentRecord);
      else { selected = ''; $('selection-empty').hidden = false; render(); }
    }
  });
  form.addEventListener('input', () => { dirty = true; });
  field('kind').addEventListener('change', types);
  $('assign-project').addEventListener('click', () => {
    field('project_id').value = projectId; field('project_name').value = projectName;
    $('assign-project').hidden = true; dirty = true;
  });
  form.addEventListener('submit', (event) => {
    event.preventDefault(); void run(async () => {
      const record = Object.fromEntries(Array.from(form.querySelectorAll('[name]')).map(
        (input) => [input.name, input.value]
      ));
      const before = new Set(records.map((item) => item.id));
      const result = await api('save', {id: selected, revision, record});
      const savedId = selected || result.records.find((item) => !before.has(item.id))?.id;
      form.reset(); dirty = false; $('editor').hidden = true; apply(result);
      if (savedId) view((await api('detail', {id: savedId})).record);
      message('Запись сохранена. Резервная копия создана.');
    });
  });
  $('delete').addEventListener('click', () => run(async () => {
    if (!confirm('Удалить эту запись? Предыдущая версия останется в резервных копиях.')) return;
    const result = await api('delete', {id: selected, revision});
    form.reset(); dirty = false; currentRecord = null; selected = '';
    $('editor').hidden = true; $('viewer').hidden = true; $('view-fields').replaceChildren();
    $('selection-empty').hidden = false; apply(result); message('Запись удалена.');
  }));
  $('reveal').addEventListener('click', () => {
    const hidden = field('password').type === 'password';
    field('password').type = hidden ? 'text' : 'password';
    $('reveal').textContent = hidden ? 'Скрыть' : 'Показать';
    if (hidden) setTimeout(() => { field('password').type = 'password';
      $('reveal').textContent = 'Показать'; }, 20000);
  });
  $('copy').addEventListener('click', () => run(async () => {
    await navigator.clipboard.writeText(field('password').value);
    message('Пароль скопирован в буфер обмена.');
  }));
  for (const button of document.querySelectorAll('[data-kind]')) {
    button.addEventListener('click', () => {
      filter = button.dataset.kind;
      document.querySelectorAll('[data-kind]').forEach((item) => {
        item.classList.toggle('selected', item === button);
        item.setAttribute('aria-pressed', String(item === button));
      }); render();
    });
  }
  $('search').addEventListener('input', render); $('all-projects').addEventListener('change', render);
  $('backup-form').addEventListener('submit', (event) => {
    event.preventDefault(); void run(async () => {
      apply(await api('backup', {revision, folder: $('folder').value}));
      message('Полная резервная копия создана и проверена.');
    });
  });
  const fileData = async (input) => {
    const file = input.files[0];
    if (!file || file.size > 16 * 1024 * 1024) throw new Error('Выберите файл .kdbx размером до 16 МиБ.');
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = '';
    for (let offset = 0; offset < bytes.length; offset += 8192) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
    }
    return btoa(binary);
  };
  $('restore-form').addEventListener('submit', (event) => {
    event.preventDefault(); void run(async () => {
      if (!confirm('Заменить всё хранилище выбранной копией?')) return;
      const result = await api('restore', {revision, file: await fileData($('restore-file')),
        password: $('restore-password').value, folder: $('folder').value});
      $('restore-form').reset(); form.reset(); dirty = false; $('editor').hidden = true;
      currentRecord = null; selected = ''; $('viewer').hidden = true; $('view-fields').replaceChildren();
      $('selection-empty').hidden = false;
      apply(result); message('Хранилище восстановлено. Предыдущая версия сохранена отдельно.');
    });
  });
  $('initial-restore-button').addEventListener('click', () => run(async () => {
    if (exists && !confirm('Восстановить всё хранилище из выбранной копии?')) return;
    apply(await api('initial_restore', {file: await fileData($('initial-file')),
      password: $('initial-backup-password').value, folder: $('recovery-folder').value}));
    $('initial-file').value = ''; $('initial-backup-password').value = ''; message('Хранилище восстановлено.');
  }));
  $('password-form').addEventListener('submit', (event) => {
    event.preventDefault(); void run(async () => {
      if ($('new-password').value !== $('new-password-confirm').value) throw new Error('Пароли не совпадают.');
      apply(await api('password', {revision, password: $('new-password').value}));
      $('password-form').reset(); message('Мастер-пароль изменён. Создана копия с новым паролем.');
    });
  });
  const activity = () => { idleAt = Date.now(); };
  document.addEventListener('pointerdown', activity); document.addEventListener('keydown', activity);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) hiddenAt = Date.now();
    else if (token && accessMode !== 'no_password' && hiddenAt && Date.now() - hiddenAt >= 15 * 60 * 1000) {
      const previous = token; clear(); void api('lock', {}, previous).catch(() => {});
    }
  });
  setInterval(() => {
    if (token && accessMode !== 'no_password' && Date.now() - idleAt >= 15 * 60 * 1000) {
      const previous = token; clear(); message('Хранилище заблокировано после бездействия.');
      void api('lock', {}, previous).catch(() => {});
    }
  }, 1000);
  setInterval(() => {
    if (token && !busy && !document.hidden && Date.now() - idleAt < 60000) {
      void api('touch').catch(() => {});
    }
  }, 60000);
  window.addEventListener('pagehide', () => { clear(false); });
  window.addEventListener('pageshow', (event) => { if (event.persisted) void start(); });
  window.addEventListener('beforeunload', (event) => {
    if (dirty) { event.preventDefault(); event.returnValue = ''; }
  });
  const start = () => run(async () => {
    const status = await api('status'); exists = status.exists;
    accessMode = status.access_mode || 'password'; deviceState(status.device);
    $('setup').hidden = exists; $('initial-restore').hidden = false;
    $('gate-title').textContent = exists ? 'Хранилище закрыто' : 'Создайте личное хранилище';
    $('unlock-submit').textContent = exists ? 'Открыть мастер-паролем' : 'Создать хранилище';
    $('master').autocomplete = exists ? 'current-password' : 'new-password'; gateMode();
    token = session();
    if (token) {
      try { apply(await api('state')); return; }
      catch (_) { clear(); }
    }
    if (exists && accessMode === 'no_password') {
      apply(await api('open_without_password')); return;
    }
    if (exists && device.enabled && device.automatic && !manuallyLocked()) {
      apply(await api('device_unlock', {automatic: true}));
    }
  });
  void start();
})();
"""
