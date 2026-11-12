# ruff: noqa: RUF001
"""Independent tunnel workspace; private controls never use dashboard draft storage."""

TUNNELS_HTML = r"""
<section class="tunnel-workspace" data-tunnel-workspace>
  <header class="tunnel-heading"><div><p class="eyebrow">Интернет через ваш VPS</p>
    <h1>Туннели</h1><p class="section-note">Подключите сервер, настройте доступ и включите защищённое соединение.</p></div>
    <button class="btn" type="button" data-tunnel-new>Новое подключение</button></header>
  <div class="tunnel-notice" role="status" aria-live="polite" data-tunnel-notice>Загрузка подключений…</div>
  <div class="tunnel-layout" data-tunnel-layout>
    <aside class="tunnel-list-panel" data-tunnel-sidebar hidden><h2>Подключения</h2><div data-tunnel-list></div></aside>
    <div class="tunnel-main">
      <ol class="tunnel-steps" aria-label="Настройка подключения" data-tunnel-steps hidden>
        <li data-tunnel-step="1"><span>1</span>Сервер</li><li data-tunnel-step="2"><span>2</span>SSH-доступ</li><li data-tunnel-step="3"><span>3</span>Подключение</li>
      </ol>
      <section class="tunnel-editor tunnel-card" data-tunnel-editor hidden aria-labelledby="tunnel-editor-heading">
        <div class="tunnel-form-heading"><p class="eyebrow" data-tunnel-editor-kicker>Шаг 1 из 3</p>
          <h2 id="tunnel-editor-heading" data-tunnel-editor-title>Добавьте свой сервер</h2>
          <p class="section-note">Возьмите IP-адрес и данные SSH в панели вашего VPS-провайдера. Домен не нужен.</p>
          <p class="tunnel-help">Подходит Ubuntu / Debian amd64 с systemd.</p></div>
        <form data-tunnel-profile>
          <div class="tunnel-form-grid"><label>IP-адрес VPS<input name="primary_host" required maxlength="64" placeholder="Например, 203.0.113.10" autocomplete="off" aria-describedby="tunnel-host-help">
            <span class="tunnel-help" id="tunnel-host-help">Публичный IPv4 или IPv6 сервера</span></label>
            <label>Пользователь SSH<input name="primary_user" required maxlength="32" value="root" autocomplete="off">
              <span class="tunnel-help">Обычно root или ubuntu</span></label></div>
          <details class="tunnel-disclosure tunnel-server-options"><summary>Название и порт SSH</summary>
            <div class="tunnel-form-grid"><label>Название — необязательно<input name="name" maxlength="160" placeholder="Например, мой VPS"></label>
              <label>Порт SSH<input name="primary_port" type="number" required min="1" max="65535" value="22"></label></div>
          </details>
          <div data-tunnel-advanced hidden>
            <details class="tunnel-disclosure"><summary>Резервный VPS</summary>
              <p class="section-note">Необязательно. Используется автоматически, когда основной сервер недоступен.</p>
              <label>IP-адрес резерва<input name="backup_host" maxlength="64" autocomplete="off"></label>
              <div class="tunnel-form-grid"><label>Пользователь SSH резерва<input name="backup_user" maxlength="32" value="root" autocomplete="off"></label>
                <label>Порт SSH резерва<input name="backup_port" type="number" min="1" max="65535" value="22"></label></div>
            </details>
            <details class="tunnel-disclosure"><summary>Маршрутизация и локальная сеть</summary>
              <label>Напрямую, минуя туннель<textarea name="exceptions" rows="4" maxlength="16384" spellcheck="false" placeholder="example.org&#10;192.0.2.10&#10;10.20.0.0/16"></textarea>
                <span class="tunnel-help">По одному домену, IP или CIDR на строку. Домен включает поддомены.</span></label>
              <label class="tunnel-checkbox"><input name="lan_bypass" type="checkbox" checked>Локальная сеть напрямую — роутер, принтеры и NAS</label>
            </details>
          </div>
          <p class="tunnel-help" data-tunnel-edit-help hidden>Перед сменой IP или SSH-пользователя удалите прежнюю серверную часть. Настройки маршрутизации можно менять при отключённом соединении.</p>
          <div class="tunnel-form-footer"><button class="btn btn-primary" type="submit" data-tunnel-save>Продолжить к SSH-доступу <span aria-hidden="true">→</span></button>
            <button class="btn tunnel-quiet" type="button" data-tunnel-cancel>Отмена</button></div>
        </form>
      </section>
      <section class="tunnel-detail tunnel-card" data-tunnel-detail hidden>
        <header class="tunnel-detail-heading"><div><p class="eyebrow" data-tunnel-detail-kicker>Ваше подключение</p><h2 data-tunnel-title>Подключение</h2>
          <p class="tunnel-endpoint" data-tunnel-endpoint></p></div><span class="tunnel-state" data-tunnel-state></span></header>
        <p class="tunnel-notice" data-tunnel-error data-error="true" role="alert" hidden></p>
        <section class="tunnel-setup" data-tunnel-access hidden aria-labelledby="tunnel-access-heading">
          <h3 id="tunnel-access-heading">Как войти на VPS?</h3>
          <p class="section-note">Введите доступ к указанному серверу. Harness установит и проверит серверную часть.</p>
          <p class="tunnel-requirements">Нужен Ubuntu / Debian amd64 с systemd, Python 3 и OpenSSH. Вход — root или пользователь с sudo. TCP 443 и UDP 443 должны быть свободны.</p>
          <form data-tunnel-credentials autocomplete="off">
            <fieldset class="tunnel-auth-choice"><legend>Способ входа</legend>
              <label><input name="auth_method" type="radio" value="password" checked>Пароль</label>
              <label><input name="auth_method" type="radio" value="key">SSH-ключ</label></fieldset>
            <p class="tunnel-notice tunnel-inline-feedback" data-tunnel-prepare-feedback role="status" aria-live="polite" hidden></p>
            <div data-tunnel-password-fields><label>Пароль SSH<input name="password" type="password" autocomplete="new-password" maxlength="1024" required></label></div>
            <div data-tunnel-key-fields hidden><label>Приватный SSH-ключ<textarea name="private_key" rows="4" autocomplete="off" spellcheck="false" maxlength="16384" disabled></textarea></label>
              <label>Пароль ключа — если задан<input name="key_passphrase" type="password" autocomplete="new-password" maxlength="1024" disabled></label></div>
            <div data-tunnel-sudo-fields hidden><label>Пароль sudo — если требуется<input name="sudo_password" type="password" autocomplete="new-password" maxlength="1024" disabled>
              <span class="tunnel-help">Пользователь должен иметь права sudo. Если sudo работает без пароля, оставьте поле пустым.</span></label></div>
            <details class="tunnel-disclosure" data-tunnel-backup-access hidden><summary>Отдельный доступ к резервному VPS</summary>
              <p class="section-note">Пустые поля используют доступ основного VPS.</p>
              <label>Пароль SSH резерва<input name="backup_password" type="password" autocomplete="new-password" maxlength="1024"></label>
              <label>Пароль sudo резерва<input name="backup_sudo_password" type="password" autocomplete="new-password" maxlength="1024"></label>
              <label>Приватный ключ резерва<textarea name="backup_private_key" rows="4" autocomplete="off" spellcheck="false" maxlength="16384"></textarea></label>
              <label>Пароль ключа резерва<input name="backup_key_passphrase" type="password" autocomplete="new-password" maxlength="1024"></label>
            </details>
            <p class="tunnel-privacy">Пароль и ключ используются только для этой операции и не сохраняются. При первой установке ОС запросит права администратора; подготовка может занять несколько минут.</p>
            <div class="tunnel-form-footer"><button class="btn btn-primary" type="submit" data-tunnel-prepare>Настроить VPS</button>
              <button class="btn tunnel-quiet" type="button" data-tunnel-setup-back>← Назад к серверу</button></div>
            <p class="tunnel-help" data-tunnel-prepare-help>Если настройка завершится ошибкой, проверьте SSH-доступ и введите пароль или ключ заново.</p>
          </form>
        </section>
        <section data-tunnel-monitor hidden>
          <div class="tunnel-status-panel"><h3 data-tunnel-status-title>VPS готов</h3><p data-tunnel-status-copy class="section-note"></p>
            <div class="tunnel-protection" data-tunnel-protection role="status"></div>
            <p class="tunnel-notice tunnel-inline-feedback" data-tunnel-monitor-feedback role="status" aria-live="polite" hidden></p>
            <div class="tunnel-actions"><button class="btn btn-primary" type="button" data-tunnel-action="connect">Подключить</button>
              <button class="btn" type="button" data-tunnel-action="disconnect">Отключить</button>
              <button class="btn" type="button" data-tunnel-action="check">Проверить соединение</button>
              <button class="btn" type="button" data-tunnel-phone>Добавить телефон</button></div></div>
          <dl class="tunnel-metrics" data-tunnel-metrics></dl>
          <p class="tunnel-help" data-tunnel-latency-help hidden>Отклик HTTPS — время запроса через туннель, включая TCP/TLS и ответ сервера.</p>
          <details class="tunnel-disclosure" data-tunnel-technical-panel><summary>Сведения о соединении</summary><dl class="tunnel-secondary-metrics" data-tunnel-technical></dl></details>
        </section>
        <div class="tunnel-secondary" data-tunnel-secondary hidden>
          <details class="tunnel-disclosure"><summary>Настройки подключения</summary><p class="section-note">Резервный сервер, исключения и доступ к локальной сети.</p>
            <button class="btn" type="button" data-tunnel-edit>Изменить настройки</button><p class="tunnel-help" data-tunnel-settings-help>Для изменения настроек сначала отключите соединение.</p></details>
          <details class="tunnel-disclosure"><summary>Последние операции и события</summary><ol class="tunnel-events" data-tunnel-events></ol></details>
          <details class="tunnel-disclosure"><summary>Передача данных за 24 часа</summary><div data-tunnel-history></div></details>
        </div>
        <details class="tunnel-disclosure tunnel-removal"><summary>Удаление подключения</summary>
          <p class="section-note">Сначала отключите туннель. Удаляются только серверная часть и служебный доступ этого подключения.</p>
          <div class="tunnel-actions"><button class="btn" type="button" data-tunnel-action="remove">Удалить с VPS</button>
            <button class="btn btn-danger" type="button" data-tunnel-delete>Удалить подключение</button></div>
          <details class="tunnel-disclosure" data-tunnel-recovery><summary>Удаление прервалось — восстановить с SSH-доступом</summary>
            <p class="section-note">Введите свежий исходный доступ. Уже удалённые VPS пропускаются; на остальных проверяется принадлежность файлов. Ошибка входа не считается удалением.</p>
            <form data-tunnel-recovery-credentials autocomplete="off">
              <label>Пароль SSH для удаления<input name="password" type="password" autocomplete="new-password" maxlength="1024"></label>
              <label>Пароль sudo для удаления<input name="sudo_password" type="password" autocomplete="new-password" maxlength="1024"></label>
              <label>Приватный SSH-ключ для удаления<textarea name="private_key" rows="4" autocomplete="off" spellcheck="false" maxlength="16384"></textarea></label>
              <label>Пароль ключа для удаления<input name="key_passphrase" type="password" autocomplete="new-password" maxlength="1024"></label>
              <details class="tunnel-disclosure"><summary>Доступ к резервному VPS для удаления</summary>
                <label>Пароль SSH резерва для удаления<input name="backup_password" type="password" autocomplete="new-password" maxlength="1024"></label>
                <label>Пароль sudo резерва для удаления<input name="backup_sudo_password" type="password" autocomplete="new-password" maxlength="1024"></label>
                <label>Приватный ключ резерва для удаления<textarea name="backup_private_key" rows="4" autocomplete="off" spellcheck="false" maxlength="16384"></textarea></label>
                <label>Пароль ключа резерва для удаления<input name="backup_key_passphrase" type="password" autocomplete="new-password" maxlength="1024"></label>
              </details>
              <p class="tunnel-help">Исходные пароли и ключи не сохраняются.</p>
              <p class="tunnel-notice tunnel-inline-feedback" id="tunnel-recovery-feedback" data-tunnel-recovery-feedback role="status" aria-live="polite" hidden></p><button class="btn" type="button" data-tunnel-recover-remove>Завершить удаление со свежим SSH-доступом</button>
            </form>
          </details>
        </details>
      </section>
      <section class="tunnel-empty tunnel-card" data-tunnel-empty hidden><p class="eyebrow">Первое подключение</p><h2>Начните со своего VPS</h2>
        <p class="section-note">Понадобятся IP-адрес сервера и доступ по SSH. Настройка пройдёт в три шага.</p>
        <button class="btn btn-primary" type="button" data-tunnel-start>Добавить сервер <span aria-hidden="true">→</span></button></section>
    </div>
  </div>
  <dialog class="tunnel-phone-dialog" data-tunnel-phone-dialog aria-labelledby="tunnel-phone-heading">
    <h2 id="tunnel-phone-heading">Добавить телефон</h2>
    <p class="section-note" data-tunnel-phone-name></p>
    <p>В Android-приложении Harness нажмите «Сканировать QR-код», затем выберите приложения для туннеля.</p>
    <p class="tunnel-notice" data-tunnel-phone-status role="status" aria-live="polite"></p>
    <canvas data-tunnel-phone-qr role="img" aria-label="QR-код доступа к выбранному VPS" hidden></canvas>
    <p class="tunnel-help">Общий доступ к этому подключению. QR-код даёт доступ к VPS, как пароль. Отдельный отзыв телефона пока недоступен. Экспорт не меняет текущее подключение компьютера.</p>
    <div class="tunnel-actions"><button class="btn" type="button" data-tunnel-phone-retry hidden>Повторить</button><button class="btn btn-primary" type="button" data-tunnel-phone-close>Закрыть</button></div>
  </dialog>
</section>
<noscript><p role="alert">Для управления туннелями нужен JavaScript. Без него подключения и сеть не изменяются.</p></noscript>
"""

TUNNELS_CSS = r"""
.tunnel-workspace{max-width:1120px;margin:0 auto}.tunnel-workspace [hidden]{display:none!important}
.tunnel-heading,.tunnel-detail-heading{display:flex;align-items:center;justify-content:space-between;gap:16px}.tunnel-heading{margin-bottom:12px}.tunnel-heading h1{font-size:27px;letter-spacing:-.6px;margin:0 0 7px}.tunnel-heading .section-note{margin:0;max-width:620px;font-size:13px;line-height:1.6}.tunnel-heading .eyebrow{margin:0 0 8px}.tunnel-heading>.btn{flex-shrink:0}
.tunnel-notice{font-size:12px;line-height:1.55;color:var(--text-secondary);overflow-wrap:anywhere;min-height:20px;margin:8px 0 16px}.tunnel-inline-feedback{padding:10px 12px;background:var(--accent-soft);border-radius:7px;margin:12px 0 16px}.tunnel-notice[data-error=true]{padding:12px 14px;border:1px solid #efc7bc;border-radius:8px;background:#fff5f1;color:#8d3624}
.tunnel-layout{min-width:0;display:grid;grid-template-columns:minmax(0,1fr);gap:24px;align-items:start}.tunnel-layout[data-has-list=true]{grid-template-columns:210px minmax(0,1fr)}.tunnel-main{min-width:0}
.tunnel-list-panel{min-width:0;max-width:100%;padding:14px 0}.tunnel-list-panel>div{min-width:0;max-width:100%;box-sizing:border-box}.tunnel-list-panel h2{font-size:11px;text-transform:uppercase;letter-spacing:.08em;margin:0 10px 12px;color:var(--text-muted)}.tunnel-entry{display:block;width:100%;padding:12px 10px;border:1px solid transparent;border-radius:8px;background:transparent;text-align:left;cursor:pointer;color:inherit;margin:3px 0;overflow-wrap:anywhere}.tunnel-entry strong{display:block;font-size:13px;font-weight:600;line-height:1.5}.tunnel-entry small{display:block;color:var(--text-muted);font-size:11px;margin-top:5px;line-height:1.5}.tunnel-entry:hover{background:var(--accent-soft)}.tunnel-entry[aria-current=true]{background:var(--accent-soft);border-color:#b7dcd5}
.tunnel-steps{display:flex;list-style:none;padding:0 0 20px;margin:0;gap:0;max-width:760px}.tunnel-steps li{display:flex;flex:1;align-items:center;gap:9px;position:relative;font-size:12px;color:var(--text-muted);font-weight:500}.tunnel-steps li:after{content:'';height:1px;background:var(--border);flex:1;margin:0 12px}.tunnel-steps li:last-child{flex:0 0 auto}.tunnel-steps li:last-child:after{display:none}.tunnel-steps li>span{display:grid;place-items:center;flex:0 0 28px;width:28px;height:28px;border:1px solid var(--border);border-radius:50%;font-size:12px;background:#fff}.tunnel-steps li[aria-current=step]{color:var(--accent);font-weight:650}.tunnel-steps li[aria-current=step]>span{background:var(--accent);border-color:var(--accent);color:#fff}.tunnel-steps li[data-complete=true]>span{background:var(--accent-soft);border-color:#a8d6cd;color:var(--accent)}
.tunnel-card{background:#fff;border:1px solid var(--border);border-radius:12px;padding:30px;min-width:0;box-shadow:0 3px 14px #1a494b04}.tunnel-editor{max-width:760px}.tunnel-form-heading{margin-bottom:24px}.tunnel-card h2{font-size:23px;line-height:1.3;letter-spacing:-.4px;margin:0 0 10px;overflow-wrap:anywhere}.tunnel-card h3{font-size:19px;letter-spacing:-.2px;margin:0 0 8px}.tunnel-card .section-note{font-size:13px;line-height:1.65;margin:8px 0 16px}.tunnel-card .eyebrow{margin:0 0 9px}.tunnel-editor form,.tunnel-setup{max-width:620px}
.tunnel-workspace label{display:flex;flex-direction:column;gap:8px;font-size:13px;font-weight:550;margin:0 0 19px}.tunnel-workspace input:not([type=checkbox]):not([type=radio]),.tunnel-workspace textarea{width:100%;box-sizing:border-box;border:1px solid var(--border-strong,#bdcbd0);border-radius:7px;padding:11px 12px;font:inherit;font-size:14px;line-height:1.5;background:#fff;color:inherit;min-width:0;min-height:44px}.tunnel-workspace input::placeholder,.tunnel-workspace textarea::placeholder{color:#839097;font-size:13px}.tunnel-help{font-size:12px;font-weight:400;color:var(--text-muted);line-height:1.6}.tunnel-form-grid{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(0,1fr);gap:18px}.tunnel-checkbox{flex-direction:row!important;align-items:flex-start;gap:10px!important;line-height:1.5}.tunnel-checkbox input{flex:0 0 auto;margin-top:3px}
.tunnel-form-footer{display:flex;align-items:center;gap:12px;border-top:1px solid var(--border);margin-top:24px;padding-top:22px}.tunnel-workspace .btn{min-height:42px;padding:10px 15px;font-size:13px;white-space:normal;text-align:center;line-height:1.4}.tunnel-workspace .btn-primary{font-weight:600;gap:16px}.tunnel-quiet{background:transparent!important;border-color:transparent!important;color:var(--text-secondary)!important}.tunnel-actions{display:flex;gap:8px;flex-wrap:wrap}.tunnel-state{flex-shrink:0;font-size:11px;background:var(--bg);border:1px solid var(--border);padding:6px 10px;border-radius:20px}.tunnel-state[data-state=connected]{background:var(--accent-soft);color:#096558;border-color:#bfdfd7}.tunnel-state[data-state=blocked],.tunnel-state[data-state=reconnecting]{background:#fff5e4;color:#8c5a14;border-color:#eddbb8}.tunnel-detail-heading{align-items:flex-start;padding-bottom:22px;margin-bottom:24px;border-bottom:1px solid var(--border)}.tunnel-endpoint{font-size:12px;color:var(--text-muted);margin:8px 0 0;overflow-wrap:anywhere}
.tunnel-requirements{padding:12px 14px;background:#f5f8f8;border-left:3px solid #bbd5d1;font-size:12px;line-height:1.7;color:var(--text-secondary);margin:0 0 24px}.tunnel-auth-choice{border:0;padding:0;margin:0 0 22px;display:flex;gap:8px}.tunnel-auth-choice legend{font-size:12px;color:var(--text-secondary);margin-bottom:10px}.tunnel-auth-choice label{flex:1;flex-direction:row;align-items:center;border:1px solid var(--border);border-radius:7px;padding:12px;margin:0;cursor:pointer;font-size:13px}.tunnel-auth-choice label:has(input:checked){border-color:var(--accent);background:var(--accent-soft);color:#0d655b}.tunnel-auth-choice input{accent-color:var(--accent);margin:0}.tunnel-privacy{font-size:12px;line-height:1.7;color:var(--text-muted);margin:20px 0 0}
.tunnel-status-panel{padding:20px;background:#f5faf9;border:1px solid #d6e8e3;border-radius:9px}.tunnel-status-panel .section-note{margin-bottom:8px}.tunnel-protection{font-size:12px;line-height:1.6;color:var(--text-secondary);margin-bottom:18px}.tunnel-protection[data-active=true]{color:#087555}.tunnel-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));margin:25px 0 4px;gap:18px}.tunnel-metrics div{min-width:0}.tunnel-metrics dt,.tunnel-secondary-metrics dt{font-size:11px;color:var(--text-muted);margin-bottom:6px}.tunnel-metrics dd{font-size:21px;font-weight:550;letter-spacing:-.4px;line-height:1.4;margin:0;overflow-wrap:anywhere}.tunnel-secondary-metrics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px;margin:18px 0 0}.tunnel-secondary-metrics dd{font-size:13px;line-height:1.6;margin:0;overflow-wrap:anywhere}
.tunnel-disclosure{border-top:1px solid var(--border);padding-top:16px;margin-top:20px}.tunnel-disclosure>summary{cursor:pointer;font-size:13px;font-weight:550;line-height:1.6;padding:2px 0;min-height:28px}.tunnel-disclosure[open]>summary{margin-bottom:16px}.tunnel-disclosure form{margin-top:16px}.tunnel-server-options{margin:0;border:0;padding:0}.tunnel-server-options>summary{color:var(--accent);font-size:12px}.tunnel-removal{margin-top:26px}.tunnel-removal>summary{color:var(--text-muted);font-size:12px}.tunnel-events{padding-left:20px;font-size:12px}.tunnel-events li{margin:12px 0;line-height:1.6;overflow-wrap:anywhere}.tunnel-events time{display:block;color:var(--text-muted);font-size:11px;margin-top:3px}.tunnel-empty{max-width:760px}.tunnel-empty .btn{margin-top:8px}
.tunnel-workspace button:focus-visible,.tunnel-workspace input:focus-visible,.tunnel-workspace textarea:focus-visible,.tunnel-workspace summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px}.tunnel-workspace [aria-busy=true]{opacity:.7}.tunnel-workspace button:disabled{cursor:default}
@media(max-width:1100px){.tunnel-layout[data-has-list=true]{grid-template-columns:minmax(0,1fr)}.tunnel-list-panel{padding:0}.tunnel-list-panel h2{display:none}.tunnel-list-panel>div{display:flex;width:100%;gap:8px;overflow-x:auto;padding-bottom:4px}.tunnel-entry{min-width:160px;max-width:230px;flex-shrink:0}.tunnel-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:680px){.tunnel-heading{align-items:flex-start}.tunnel-heading h1{font-size:24px}.tunnel-heading .section-note{font-size:12px}.tunnel-heading>.btn{padding:8px 10px;min-height:38px;max-width:150px;font-size:12px}.tunnel-card{padding:23px}.tunnel-card h2{font-size:21px}.tunnel-form-grid{gap:14px}.tunnel-notice{margin-bottom:12px}.tunnel-steps{padding-bottom:18px}.tunnel-steps li{font-size:11px;gap:7px}.tunnel-steps li:after{margin:0 9px}.tunnel-detail-heading{gap:12px}.tunnel-state{max-width:110px;text-align:center}.tunnel-endpoint{line-height:1.6}.tunnel-status-panel{padding:17px}}
.tunnel-phone-dialog{width:min(600px,calc(100vw - 32px));max-height:calc(100vh - 32px);overflow:auto;border:1px solid var(--border);border-radius:16px;padding:24px;background:var(--panel);color:var(--text);box-sizing:border-box}.tunnel-phone-dialog::backdrop{background:#10203599}.tunnel-phone-dialog h2{margin-top:0}.tunnel-phone-dialog canvas{display:block;width:100%;height:auto;max-width:512px;margin:16px auto;background:#fff;image-rendering:pixelated}.tunnel-phone-dialog canvas[hidden]{display:none}
@media(max-width:440px){.tunnel-heading{display:block}.tunnel-heading>.btn{margin-top:12px;max-width:none}.tunnel-card{padding:19px}.tunnel-form-grid{grid-template-columns:1fr;gap:0}.tunnel-steps li{gap:5px;font-size:10px}.tunnel-steps li>span{width:24px;height:24px;flex-basis:24px;font-size:11px}.tunnel-steps li:after{margin:0 7px}.tunnel-form-footer{flex-direction:column;align-items:stretch;gap:5px}.tunnel-form-footer>.btn{width:100%}.tunnel-detail-heading{flex-direction:column;gap:8px}.tunnel-detail-heading h2{font-size:20px}.tunnel-secondary-metrics{grid-template-columns:1fr}.tunnel-metrics dd{font-size:20px}.tunnel-actions .btn{flex:1;min-width:100px}.tunnel-requirements{padding:10px 12px}}
"""

TUNNELS_JS = r"""
(() => {
  'use strict';
  const root = document.querySelector('[data-tunnel-workspace]');
  if (!root) return;
  const select = (selector) => root.querySelector(selector);
  const notice = select('[data-tunnel-notice]');
  const profileForm = select('[data-tunnel-profile]');
  const credentialForm = select('[data-tunnel-credentials]');
  const recoveryForm = select('[data-tunnel-recovery-credentials]');
  const credentialKeys = new Set(['password','private_key','key_passphrase','sudo_password','backup_password','backup_private_key','backup_key_passphrase','backup_sudo_password']);
  const phoneDialog = select('[data-tunnel-phone-dialog]');
  const phoneCanvas = select('[data-tunnel-phone-qr]');
  let phoneRequest = null, phoneProfile = null;
  function clearPhone() {
    phoneRequest?.abort(); phoneRequest = null; phoneProfile = null;
    phoneCanvas.getContext('2d').clearRect(0,0,phoneCanvas.width,phoneCanvas.height);
    phoneCanvas.width = phoneCanvas.height = 1; phoneCanvas.hidden = true;
    select('[data-tunnel-phone-status]').textContent = '';
  }
  phoneDialog.addEventListener('close',() => { clearPhone(); select('[data-tunnel-phone]').focus(); });
  select('[data-tunnel-phone-close]').addEventListener('click',() => phoneDialog.close());
  async function exportPhone() {
    if (!detail || busy) return;
    phoneRequest?.abort(); const request = new AbortController(); phoneRequest = request; phoneProfile = detail.id;
    const profileId = detail.id;
    if (!phoneDialog.open) phoneDialog.showModal();
    phoneCanvas.hidden = true;
    select('[data-tunnel-phone-name]').textContent = detail.settings.name;
    const feedback = select('[data-tunnel-phone-status]'); feedback.textContent = 'Подготавливаем QR-код…'; feedback.dataset.error = 'false';
    select('[data-tunnel-phone-retry]').hidden = true;
    try {
      const response = await fetch('/tunnels/action',{method:'POST',credentials:'same-origin',cache:'no-store',signal:request.signal,headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'phone_export',profile_id:profileId,expected_revision:detail.revision})});
      const result = await response.json(); if (!response.ok) throw new Error(result.error || 'Не удалось получить QR-код');
      if (phoneRequest !== request || !phoneDialog.open || detail?.id !== profileId) return;
      const matrix = result.phone?.qr;
      if (!Array.isArray(matrix) || matrix.length < 29 || matrix.length > 185 || matrix.some(row => !Array.isArray(row) || row.length !== matrix.length || row.some(cell => typeof cell !== 'boolean'))) throw new Error('Некорректный QR-код');
      const size = matrix.length; phoneCanvas.width = phoneCanvas.height = size * 4;
      const context = phoneCanvas.getContext('2d'); context.fillStyle = '#fff'; context.fillRect(0,0,size*4,size*4); context.fillStyle = '#000';
      matrix.forEach((row,y) => row.forEach((cell,x) => { if (cell) context.fillRect(x*4,y*4,4,4); }));
      phoneCanvas.hidden = false; feedback.textContent = 'Отсканируйте QR-код камерой приложения.';
    } catch (error) {
      if (request.signal.aborted || phoneRequest !== request) return;
      feedback.textContent = error.message || 'Не удалось получить QR-код'; feedback.dataset.error = 'true'; select('[data-tunnel-phone-retry]').hidden = false;
    }
  }
  select('[data-tunnel-phone]').addEventListener('click',exportPhone);
  select('[data-tunnel-phone-retry]').addEventListener('click',exportPhone);
  let initialized = false;
  const profileButtons = new Map();
  let recoveryFeedbackProfile = null;
  const labels = { disconnected:'Отключён', preparing:'Подготовка', connecting:'Подключение', connected:'Подключён', reconnecting:'Восстановление', blocked:'Нет рабочего пути' };
  const transports = { naive:'NaiveProxy · HTTPS', reality:'REALITY · TCP', hysteria2:'Hysteria2 · UDP', 'reality-udp-over-tcp':'REALITY · UDP через TCP', 'naive-udp-over-tcp':'Naive · UDP через TCP' };
  let selected = new URL(location.href).searchParams.get('profile');
  let detail = null, profiles = [], editing = false, busy = false, epoch = 0, stream = null, draftBase = {};
  let noticeSource = 'initial', pendingOperation = null, currentAction = null, focusTask = null;
  const report = (message, error = false, source = 'action') => { notice.textContent = message; notice.dataset.error = String(error); noticeSource = source;
    for (const selector of ['prepare-feedback','monitor-feedback','recovery-feedback']) { const local = select(`[data-tunnel-${selector}]`); local.hidden = ['initial','current'].includes(source) || (selector === 'recovery-feedback' && recoveryFeedbackProfile !== detail?.id); local.textContent = message; local.dataset.error = String(error); local.setAttribute('role',error ? 'alert' : 'status'); }
  };
  function reconcileNotice(recovered = false) {
    const pending = detail?.operations.find(operation => operation.state === 'pending');
    if (pending) pendingOperation = pending.id;
    const completed = pendingOperation && detail?.operations.find(operation => operation.id === pendingOperation && operation.state !== 'pending');
    if (completed) {
      pendingOperation = null;
      if (noticeSource !== 'validation') report(completed.message,completed.state === 'failed','operation');
    } else if (pending && noticeSource !== 'validation') {
      report('Сетевая операция выполняется. Дождитесь подтверждения службы.',false,'pending');
    } else if (recovered && ['stream','action-error','pending'].includes(noticeSource)) report('Мониторинг восстановлен. Данные актуальны.',false,'current');
  }
  const formatBytes = (value) => {
    if (!Number.isFinite(value)) return '—';
    const units = ['Б','КиБ','МиБ','ГиБ','ТиБ']; let index = 0;
    while (value >= 1024 && index < units.length - 1) { value /= 1024; index++; }
    return `${value.toLocaleString('ru-RU', {maximumFractionDigits:1})} ${units[index]}`;
  };
  function node(tag, value, cls) { const item = document.createElement(tag); if (value != null) item.textContent = value; if (cls) item.className = cls; return item; }
  function list(visible) {
    const target = select('[data-tunnel-list]'), left = target.scrollLeft, top = target.scrollTop;
    const focused = [...profileButtons.values()].find(entry => entry.button === document.activeElement);
    const ids = new Set(profiles.map(profile => profile.id));
    for (const [id,entry] of profileButtons) if (!ids.has(id)) { entry.button.remove(); profileButtons.delete(id); }
    profiles.forEach((profile,index) => {
      let entry = profileButtons.get(profile.id);
      if (!entry) {
        const button = node('button',null,'tunnel-entry'), name = node('strong'), summary = node('small');
        button.type = 'button'; button.append(name,summary);
        button.addEventListener('click',() => {
          if (busy) return;
          if (editing && !confirm('Закрыть редактор без сохранения?')) return;
          focusTask = null; resetSecrets(true); editing = false; selected = profile.id;
          const url = new URL(location.href); url.searchParams.set('profile',selected); history.pushState(null,'',url); load();
        });
        entry = {button,name,summary}; profileButtons.set(profile.id,entry);
      }
      entry.button.setAttribute('aria-current',String(profile.id === selected));
      entry.name.textContent = profile.settings.name;
      entry.summary.textContent = `${profile.settings.endpoints[0].host} · ${labels[profile.runtime.state] || 'Неизвестно'}`;
      if (target.children[index] !== entry.button) target.insertBefore(entry.button,target.children[index] || null);
    });
    if (focused && (!visible || document.activeElement !== focused.button)) {
      const retained = [...profileButtons.values()].includes(focused);
      const fallback = editing ? profileForm.elements.primary_host : select(profiles.length ? '[data-tunnel-new]' : '[data-tunnel-start]');
      (retained && visible ? focused.button : fallback).focus({preventScroll:true});
    }
    target.scrollLeft = left; target.scrollTop = top;
  }
  function syncAccess(clearHidden = false) {
    const key = credentialForm.elements.auth_method.value === 'key';
    const pending = busy || detail?.operations.some(operation => operation.state === 'pending');
    for (const [selector,hidden] of [
      ['password-fields',key], ['key-fields',!key],
      ['sudo-fields',detail?.settings.endpoints[0].user === 'root'],
      ['backup-access',detail?.settings.endpoints.length !== 2]
    ]) {
      const group = select(`[data-tunnel-${selector}]`); group.hidden = hidden;
      group.querySelectorAll('input,textarea').forEach(input => { input.disabled = hidden || pending; if (clearHidden && hidden) input.value = ''; });
    }
    credentialForm.elements.password.required = !key;
    credentialForm.elements.private_key.required = key;
    credentialForm.querySelectorAll('[name="auth_method"]').forEach(input => { input.disabled = pending; });
  }
  function resetSecrets(resetMethod = false) { if (resetMethod) { recoveryFeedbackProfile = null; select('[data-tunnel-recovery-feedback]').hidden = true; } const method = credentialForm.elements.auth_method.value; credentialForm.reset(); recoveryForm.reset(); if (!resetMethod) credentialForm.elements.auth_method.value = method; syncAccess(); }
  function credentials(form) {
    return Object.fromEntries([...new FormData(form)].filter(([name,value]) => credentialKeys.has(name) && String(value).length));
  }
  function draftChanged() {
    return Object.entries(draftBase).some(([name,value]) => { const input = profileForm.elements.namedItem(name); return input && (input.type === 'checkbox' ? input.checked : input.value) !== value; });
  }
  function display() {
    if (phoneDialog.open && phoneProfile !== detail?.id) phoneDialog.close();
    reconcileNotice();
    select('[data-tunnel-editor]').hidden = !editing;
    select('[data-tunnel-detail]').hidden = editing || !detail;
    select('[data-tunnel-empty]').hidden = editing || !!detail;
    select('[data-tunnel-new]').hidden = editing || profiles.length === 0;
    const hasList = profiles.length > 1 && !editing;
    list(hasList);
    select('[data-tunnel-sidebar]').hidden = !hasList;
    select('[data-tunnel-layout]').dataset.hasList = String(hasList);
    const stage = editing ? 1 : detail?.runtime.snapshot.prepared ? 3 : 2;
    select('[data-tunnel-steps]').hidden = (!editing && !detail) || (!editing && detail?.runtime.state === 'connected');
    root.querySelectorAll('[data-tunnel-step]').forEach(step => {
      const number = Number(step.dataset.tunnelStep);
      if (stage === number) step.setAttribute('aria-current','step'); else step.removeAttribute('aria-current');
      step.dataset.complete = String(number < stage);
    });
    if (!detail) return;
    const runtime = detail.runtime, stats = runtime.snapshot, prepared = stats.prepared === true;
    const hasServerState = prepared || !!stats.setup_incomplete || !!stats.removal_pending;
    const isOff = runtime.state === 'disconnected' && runtime.protection === 'inactive';
    const pending = detail.operations.find(operation => operation.state === 'pending');
    const activeAction = currentAction || pending?.action;
    const inProgress = busy || !!pending;
    select('[data-tunnel-title]').textContent = detail.settings.name;
    const endpoint = detail.settings.endpoints[0];
    select('[data-tunnel-endpoint]').textContent = `${endpoint.host} · ${endpoint.user} · SSH ${endpoint.port}`;
    select('[data-tunnel-detail-kicker]').textContent = prepared ? 'Ваше подключение' : 'Шаг 2 из 3 · SSH-доступ';
    select('[data-tunnel-state]').textContent = prepared ? labels[runtime.state] || 'Неизвестно' : 'Ожидает настройки';
    select('[data-tunnel-state]').dataset.state = runtime.state;
    select('[data-tunnel-access]').hidden = prepared || !!stats.removal_pending || !isOff;
    select('[data-tunnel-monitor]').hidden = !prepared && !stats.removal_pending && isOff;
    select('[data-tunnel-secondary]').hidden = !hasServerState && isOff;
    const hasMeasurements = !isOff || stats.rx_bytes != null || stats.tx_bytes != null;
    select('[data-tunnel-metrics]').hidden = !hasMeasurements;
    select('[data-tunnel-latency-help]').hidden = !hasMeasurements;
    select('[data-tunnel-technical-panel]').hidden = !hasMeasurements;
    const protection = select('[data-tunnel-protection]'); protection.dataset.active = String(runtime.protection === 'active');
    protection.textContent = ({ active:'Защита активна: трафик туннеля не выходит напрямую', inactive:'Защита не включена. Сейчас действует обычная сеть компьютера.', unavailable:'Сетевая защита недоступна. Подключение не включено.', unknown:'Состояние сетевой защиты не подтверждено. Проверьте службу.' })[runtime.protection] || 'Состояние защиты неизвестно';
    select('[data-tunnel-error]').hidden = !stats.last_error;
    select('[data-tunnel-error]').textContent = stats.last_error || '';
    select('[data-tunnel-status-title]').textContent = !prepared && !isOff ? 'Проверьте состояние сетевой службы' : stats.removal_pending ? 'Завершите удаление серверной части' : ({disconnected:'VPS готов к подключению',connected:'Соединение работает',connecting:'Проверяем соединение…',reconnecting:'Восстанавливаем соединение…',blocked:'Рабочий путь не подтверждён',preparing:'Настраиваем VPS…'})[runtime.state] || 'Проверьте состояние службы';
    select('[data-tunnel-status-copy]').textContent = !prepared && !isOff ? 'Подготовка VPS не подтверждена. Сначала отключите соединение и проверьте службу перед повторной настройкой.' : stats.removal_pending ? 'Подтверждение удаления не получено. Откройте раздел удаления ниже и завершите его со свежим SSH-доступом.' : ({disconnected:'Нажмите «Подключить», чтобы направить интернет этого компьютера через VPS.',connected:'Доступ через VPS подтверждён проверкой DNS и передачи данных.',connecting:'Ожидаем подтверждения защиты, DNS и передачи данных от сетевой службы.',reconnecting:'Служба ищет рабочий транспорт или резервный VPS. Состояние защиты показано ниже.',blocked:'Служба не подтвердила передачу данных. Дождитесь восстановления или отключите соединение.'})[runtime.state] || 'Дождитесь результата сетевой службы.';
    function metrics(selector, values) {
      const target = select(selector); target.replaceChildren();
      for (const [label,value] of values) { const cell = node('div'); cell.append(node('dt',label),node('dd',String(value))); target.append(cell); }
    }
    metrics('[data-tunnel-metrics]',[
      ['Приём',stats.rx_bps == null ? '—' : `${formatBytes(stats.rx_bps)}/с`],
      ['Передача',stats.tx_bps == null ? '—' : `${formatBytes(stats.tx_bps)}/с`],
      ['Отклик HTTPS',Number.isFinite(stats.latency_ms) ? `${Math.round(stats.latency_ms)} мс` : '—'],
      ['Объём сессии',stats.rx_bytes == null ? '—' : `${formatBytes(stats.rx_bytes)} / ${formatBytes(stats.tx_bytes)}`]
    ]);
    metrics('[data-tunnel-technical]',[
      ['Внешний IP',stats.external_ip || '—'], ['Транспорт',transports[stats.transport] || '—'],
      ['UDP',stats.udp_ok === false ? 'Не подтверждён · качество снижено' : transports[stats.udp_transport] || 'Не проверен'],
      ['Подключён с',stats.connected_at ? new Date(stats.connected_at).toLocaleString('ru-RU') : '—'],
      ['Переподключения',stats.reconnects ?? '—'],
      ['Локальная сеть',detail.settings.lan_bypass ? (stats.lan_routes?.join(', ') || 'Подключённые сети не обнаружены') : 'Обход выключен']
    ]);
    if (inProgress && activeAction === 'connect') { select('[data-tunnel-status-title]').textContent = 'Подключаем…'; select('[data-tunnel-status-copy]').textContent = 'Ожидаем подтверждения защиты, DNS и передачи данных от сетевой службы.'; }
    if (inProgress && activeAction === 'disconnect') select('[data-tunnel-status-title]').textContent = 'Отключаем…';
    root.querySelectorAll('[data-tunnel-action], [data-tunnel-delete], [data-tunnel-edit], [data-tunnel-setup-back], [data-tunnel-credentials] button, [data-tunnel-recovery-credentials] button').forEach(button => { button.disabled = inProgress; });
    select('[data-tunnel-phone]').disabled = inProgress || !prepared || !!stats.removal_pending;
    const connect = select('[data-tunnel-action="connect"]'), disconnect = select('[data-tunnel-action="disconnect"]');
    connect.textContent = inProgress && activeAction === 'connect' ? 'Подключаем…' : 'Подключить';
    disconnect.textContent = inProgress && activeAction === 'disconnect' ? 'Отключаем…' : 'Отключить';
    select('[data-tunnel-action="check"]').textContent = inProgress && activeAction === 'check' ? 'Проверяем…' : isOff ? 'Проверить VPS' : 'Проверить соединение';
    connect.hidden = !isOff && runtime.desired; connect.disabled ||= !prepared || !!stats.removal_pending || runtime.state === 'connected';
    disconnect.hidden = isOff; disconnect.disabled ||= isOff; disconnect.className = 'btn btn-primary';
    select('[data-tunnel-action="check"]').hidden = isOff && (!prepared || !!stats.removal_pending);
    select('[data-tunnel-settings-help]').hidden = isOff;
    select('[data-tunnel-prepare]').disabled ||= !!stats.removal_pending || !isOff;
    select('[data-tunnel-setup-back]').disabled ||= !isOff;
    select('[data-tunnel-prepare]').textContent = inProgress && activeAction === 'prepare' ? 'Настраиваем VPS…' : 'Настроить VPS';
    select('[data-tunnel-prepare-help]').textContent = inProgress && activeAction === 'prepare' ? 'Ожидаем результат службы. Подготовка может занять несколько минут; подтвердите системный запрос ОС, если он появился.' : 'Если настройка завершилась ошибкой, проверьте SSH-доступ и введите пароль или ключ заново.';
    select('[data-tunnel-recover-remove]').disabled ||= !isOff || !hasServerState;
    select('[data-tunnel-edit]').disabled ||= !isOff;
    select('[data-tunnel-delete]').disabled ||= !isOff || hasServerState;
    select('[data-tunnel-action="remove"]').disabled ||= !isOff || !hasServerState;
    syncAccess();
    const events = select('[data-tunnel-events]'); events.replaceChildren();
    for (const event of [...detail.operations.map(op => ({message:`${op.message}${op.state === 'pending' ? '…' : ''}`,created_at:op.created_at})),...detail.events].slice(0,30)) {
      const item = node('li',event.message); const time = node('time',new Date(event.created_at).toLocaleString('ru-RU')); time.dateTime = event.created_at; item.append(time); events.append(item);
    }
    if (!events.childElementCount) events.append(node('li','Событий пока нет'));
    const history = select('[data-tunnel-history]'); history.replaceChildren();
    if (!detail.metrics.length) history.append(node('p','Измерения появятся после подтверждённого подключения.'));
    else {
      const rx = detail.metrics.reduce((sum,item) => sum+item.rx_bytes,0), tx = detail.metrics.reduce((sum,item) => sum+item.tx_bytes,0);
      history.append(node('p',`За 24 часа: принято ${formatBytes(rx)}, передано ${formatBytes(tx)}.`));
      const table = node('table'); table.append(node('caption','Последние минутные измерения'));
      const heading = node('tr'); for (const label of ['Время','Приём','Передача','Отклик HTTPS']) heading.append(node('th',label)); table.append(heading);
      for (const sample of detail.metrics.slice(-12).reverse()) { const row = node('tr'); for (const value of [new Date(sample.minute).toLocaleTimeString('ru-RU'),formatBytes(sample.rx_bytes),formatBytes(sample.tx_bytes),sample.latency_ms == null ? '—' : `${Math.round(sample.latency_ms)} мс`]) row.append(node('td',value)); table.append(row); }
      history.append(table);
    }
    if (focusTask && !busy && !editing && detail.id === focusTask.profile) {
      const completed = focusTask.id && detail.operations.find(operation => operation.id === focusTask.id && operation.state !== 'pending');
      if (completed || focusTask.failed) {
        const action = focusTask.action; focusTask = null;
        if (action === 'prepare') {
          if (completed?.state === 'succeeded' && prepared) connect.focus();
          else if (!prepared && isOff) credentialForm.elements[credentialForm.elements.auth_method.value === 'key' ? 'private_key' : 'password'].focus();
        } else if (['connect','disconnect','check'].includes(action)) (isOff ? connect : disconnect).focus();
      }
    }
  }
  async function load(announce = true) {
    const ownEpoch = ++epoch;
    try {
      const url = new URL('/tunnels/data',location.origin); if (selected) url.searchParams.set('profile',selected);
      const response = await fetch(url,{cache:'no-store',credentials:'same-origin'});
      if (!response.ok) throw new Error('Не удалось обновить подключения');
      const result = await response.json(); if (ownEpoch !== epoch) return;
      profiles = result.profiles; detail = result.selected;
      if (noticeSource === 'initial') report('Данные подключений загружены.',false,'current');
      if (!selected && profiles.length) { selected = profiles[0].id; const target = new URL(location.href); target.searchParams.set('profile',selected); history.replaceState(null,'',target); return load(announce); }
      if (!initialized) { initialized = true; if (!profiles.length && !editing) { editing = true; populate(null); } }
      if (selected && !detail) selected = null;
      if (announce && editing && detail && profileForm.dataset.profile === detail.id) {
        const changed = {};
        for (const [name,baseline] of Object.entries(draftBase)) {
          const input = profileForm.elements.namedItem(name), value = input.type === 'checkbox' ? input.checked : input.value;
          if (value !== baseline) changed[name] = value;
        }
        populate(detail);
        for (const [name,value] of Object.entries(changed)) { const input = profileForm.elements.namedItem(name); if (input.type === 'checkbox') input.checked = value; else input.value = value; }
      }
      if (announce) report('Данные подключений обновлены',false,'current'); reconcileNotice(true); display();
    } catch (error) { if (ownEpoch === epoch) report(error.message || 'Подключения недоступны',true,'stream'); }
  }
  function populate(profile) {
    profileForm.reset(); profileForm.dataset.profile = profile?.id || crypto.randomUUID().replaceAll('-','');
    profileForm.dataset.revision = String(profile?.revision || 0);
    const existing = !!profile && ['prepared','setup_incomplete','removal_pending'].some(key => profile.runtime.snapshot[key]);
    select('[data-tunnel-editor-title]').textContent = existing ? 'Настройки подключения' : 'Добавьте свой сервер';
    select('[data-tunnel-editor-kicker]').textContent = existing ? 'Настройки' : 'Шаг 1 из 3';
    select('[data-tunnel-advanced]').hidden = !profile;
    for (const prefix of ['primary','backup']) for (const key of ['host','user','port']) profileForm.elements[`${prefix}_${key}`].disabled = existing;
    select('[data-tunnel-edit-help]').hidden = !existing;
    select('[data-tunnel-save]').textContent = existing ? 'Сохранить настройки' : 'Продолжить к SSH-доступу →';
    if (profile) {
      profileForm.elements.name.value = profile.settings.name;
      for (const [index,prefix] of [[0,'primary'],[1,'backup']]) { const endpoint = profile.settings.endpoints[index]; if (endpoint) for (const key of ['host','user','port']) profileForm.elements[`${prefix}_${key}`].value = endpoint[key]; }
      profileForm.elements.exceptions.value = profile.settings.exceptions.map(rule => rule.value).join('\n');
      profileForm.elements.lan_bypass.checked = profile.settings.lan_bypass;
    }
    draftBase = Object.fromEntries(Array.from(profileForm.elements).filter(input => input.name).map(input => [input.name,input.type === 'checkbox' ? input.checked : input.value]));
  }
  function edit(profile) {
    focusTask = null; resetSecrets(true); editing = true; populate(profile);
    display(); profileForm.elements.primary_host.focus();
  }
  async function mutate(payload, successMessage, recovery = false) {
    if (busy) return false;
    recoveryFeedbackProfile = recovery ? payload.profile_id : null;
    if (!recovery) select('[data-tunnel-recovery-feedback]').hidden = true;
    busy = true; currentAction = payload.action;
    focusTask = payload.action === 'save' || payload.action === 'delete' ? null : {action:payload.action,profile:payload.profile_id,id:null,failed:false};
    root.setAttribute('aria-busy','true');
    root.querySelectorAll('button').forEach(button => { button.disabled = true; });
    ++epoch; report(({prepare:'Настраиваем VPS… Дождитесь ответа службы.',connect:'Подключаем… Ожидаем проверку соединения.',disconnect:'Отключаем…',check:'Проверяем соединение…',remove:'Удаляем серверную часть…'})[payload.action] || 'Сохранение…',false,payload.action === 'save' ? 'action' : 'pending'); display();
    let failureSource = 'action-error';
    try {
      const response = await fetch('/tunnels/action',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const result = await response.json(); if (!response.ok) { failureSource = 'validation'; throw new Error(result.error || 'Операция не выполнена'); }
      if (payload.action === 'save') { editing = false; selected = payload.profile_id; const url = new URL(location.href); url.searchParams.set('profile',selected); history.replaceState(null,'',url); }
      if (payload.action === 'delete') { selected = null; detail = null; const url = new URL(location.href); url.searchParams.delete('profile'); history.replaceState(null,'',url); }
      if (result.operation?.id) { pendingOperation = result.operation.id; if (focusTask) focusTask.id = result.operation.id; }
      report(successMessage); await load(false); return true;
    } catch (error) { if (focusTask) focusTask.failed = true; report(error.message || 'Ответ не получен. Проверьте состояние перед повтором.',true,failureSource); return false; }
    finally { busy = false; currentAction = null; root.removeAttribute('aria-busy'); root.querySelectorAll('button').forEach(button => { button.disabled = false; }); display(); }
  }
  profileForm.addEventListener('submit',async event => {
    event.preventDefault(); if (!profileForm.reportValidity()) return;
    const fields = profileForm.elements;
    const endpoint = prefix => ({host:fields[`${prefix}_host`].value.trim(),user:fields[`${prefix}_user`].value.trim(),port:Number(fields[`${prefix}_port`].value)});
    const endpoints = [endpoint('primary')]; if (fields.backup_host.value.trim()) endpoints.push(endpoint('backup'));
    const preparedSettings = detail?.id === profileForm.dataset.profile && detail.runtime.snapshot.prepared === true;
    const saved = await mutate({action:'save',profile_id:profileForm.dataset.profile,expected_revision:Number(profileForm.dataset.revision),settings:{name:fields.name.value.trim() || `VPS · ${endpoints[0].host}`,endpoints,exceptions:fields.exceptions.value.split('\n').map(value => value.trim()).filter(Boolean),lan_bypass:fields.lan_bypass.checked}},preparedSettings ? 'Настройки сохранены. Можно подключить туннель.' : 'Сервер сохранён. Теперь введите SSH-доступ.');
    if (saved) { if (detail?.runtime.snapshot.prepared) select('[data-tunnel-action="connect"]').focus(); else credentialForm.elements.password.focus(); }
  });
  credentialForm.addEventListener('change',() => syncAccess(true));
  credentialForm.addEventListener('submit',event => {
    event.preventDefault(); if (!detail || busy || !credentialForm.reportValidity()) return;
    const access = credentials(credentialForm); resetSecrets();
    mutate({action:'prepare',profile_id:detail.id,expected_revision:detail.revision,credentials:access},'Подготовка запущена. Дождитесь ответа службы.');
  });
  recoveryForm.addEventListener('submit',event => event.preventDefault());
  select('[data-tunnel-recover-remove]').addEventListener('click',() => {
    if (!detail || busy) return;
    const access = credentials(recoveryForm);
    if (!access.password && !access.private_key) {
      recoveryFeedbackProfile = detail.id;
      recoveryForm.insertBefore(select('[data-tunnel-recovery-feedback]'),recoveryForm.firstElementChild);
      report('Введите свежий пароль или приватный SSH-ключ для восстановления удаления.',true,'validation');
      recoveryForm.elements[access.key_passphrase ? 'private_key' : 'password'].focus(); return;
    }
    if (!confirm('Подтвердить принадлежность и завершить удаление серверной части со свежим SSH-доступом?')) return;
    recoveryForm.insertBefore(select('[data-tunnel-recovery-feedback]'),select('[data-tunnel-recover-remove]'));
    resetSecrets();
    mutate({action:'remove',profile_id:detail.id,expected_revision:detail.revision,credentials:access},'Удаление запущено. Дождитесь подтверждения VPS.',true);
  });
  select('[data-tunnel-new]').addEventListener('click',() => { if (!editing || confirm('Закрыть редактор без сохранения?')) edit(null); });
  select('[data-tunnel-edit]').addEventListener('click',() => edit(detail));
  select('[data-tunnel-setup-back]').addEventListener('click',() => edit(detail));
  select('[data-tunnel-start]').addEventListener('click',() => edit(null));
  select('[data-tunnel-cancel]').addEventListener('click',() => { if (draftChanged() && !confirm('Отменить изменения подключения?')) return; editing = false; display(); if (detail) (detail.runtime.snapshot.prepared ? select('[data-tunnel-edit]') : credentialForm.elements.password).focus(); else select('[data-tunnel-start]').focus(); });
  root.querySelectorAll('[data-tunnel-action]').forEach(button => button.addEventListener('click',() => {
    if (!detail) return;
    if (button.dataset.tunnelAction === 'remove' && !confirm('Удалить принадлежащую этому подключению серверную часть и служебный доступ?')) return;
    mutate({action:button.dataset.tunnelAction,profile_id:detail.id,expected_revision:detail.revision},'Операция запущена. Дождитесь результата службы.');
  }));
  select('[data-tunnel-delete]').addEventListener('click',() => { if (detail && confirm('Удалить сохранённое подключение?')) mutate({action:'delete',profile_id:detail.id,expected_revision:detail.revision},'Подключение удалено'); });
  window.addEventListener('beforeunload',event => { if (editing || busy) { event.preventDefault(); event.returnValue = ''; } });
  window.addEventListener('popstate',() => { if (!busy) { focusTask = null; resetSecrets(true); selected = new URL(location.href).searchParams.get('profile'); editing = false; load(); } });
  document.querySelectorAll('[data-refresh-now]').forEach(button => button.addEventListener('click',() => load()));
  load();
  stream = new EventSource('/tunnels/events' + (selected ? `?profile=${encodeURIComponent(selected)}` : ''));
  stream.addEventListener('snapshot',event => {
    if (busy) return;
    const result = JSON.parse(event.data); profiles = result.profiles;
    if (result.selected?.id === selected) detail = result.selected;
    else if (selected) { load(false); return; }
    reconcileNotice(true);
    if (editing) return;
    display();
  });
  stream.onerror = () => { if (!busy && noticeSource !== 'validation') report('Мониторинг переподключается. Показаны последние полученные данные.',true,'stream'); };
})();
"""
