from __future__ import annotations

import http.client
import json
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from harness.dashboard import DashboardServerManager
from harness.dashboard_tunnels import TUNNELS_JS
from harness.storage import connect_database, initialize_database


@pytest.fixture
def dashboard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, Path]]:
    monkeypatch.setenv("HARNESS_DEV_ROOT", str(tmp_path))
    database = tmp_path / "state.db"
    initialize_database(database)
    manager = DashboardServerManager(database)
    url = manager.get_url().rstrip("/")
    yield url, database
    manager.close()


def post(
    url: str, payload: dict[str, object], *, origin: str | None = None
) -> tuple[int, dict[str, object]]:
    headers = {"Content-Type": "application/json", "Origin": url if origin is None else origin}
    request = Request(
        url + "/tunnels/action", data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.loads(response.read())


def profile_payload() -> dict[str, object]:
    return {
        "action": "save",
        "profile_id": "synthetic",
        "expected_revision": 0,
        "settings": {
            "name": "Проверочный туннель",
            "endpoints": [{"host": "203.0.113.10", "user": "root", "port": 22}],
            "exceptions": ["example.org", "192.0.2.0/24"],
            "lan_bypass": True,
        },
    }


def test_standalone_navigation_and_native_backend_persistence(dashboard: tuple[str, Path]) -> None:
    url, database = dashboard
    with urlopen(url + "/tunnels/", timeout=5) as response:
        html = response.read().decode()
        assert 'href="/tunnels/"' in html
        assert "Туннели" in html
        assert "data-tunnel-profile" in html
        assert "/assets/tunnels.js" in html
        assert '<script defer src="/assets/dashboard.js"' not in html
    status, _ = post(url, profile_payload())
    assert status == 200
    with urlopen(url + "/tunnels/data?profile=synthetic", timeout=5) as response:
        state = json.loads(response.read())
    assert state["selected"]["settings"]["name"] == "Проверочный туннель"
    assert state["selected"]["runtime"]["state"] == "disconnected"
    connection = connect_database(database)
    try:
        assert connection.execute("SELECT count(*) FROM servers").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM server_events").fetchone()[0] == 0
    finally:
        connection.close()


def test_origin_cas_and_invalid_settings_preserve_stored_profile(
    dashboard: tuple[str, Path],
) -> None:
    url, _ = dashboard
    assert post(url, profile_payload(), origin="https://untrusted.example")[0] == 403
    assert post(url, profile_payload())[0] == 200
    assert post(url, profile_payload())[0] == 409
    malformed = profile_payload()
    malformed["expected_revision"] = 1
    malformed["settings"] = {"name": "DO-NOT-SAVE", "password": "CANARY"}
    status, body = post(url, malformed)
    assert status == 400 and "CANARY" not in json.dumps(body)
    with urlopen(url + "/tunnels/data?profile=synthetic", timeout=5) as response:
        state = json.loads(response.read())
    assert state["selected"]["revision"] == 1
    assert state["selected"]["settings"]["name"] == "Проверочный туннель"


@pytest.mark.parametrize("action", ["prepare", "remove"])
def test_credentials_never_enter_database_sse_or_browser_storage(
    dashboard: tuple[str, Path],
    action: str,
) -> None:
    url, database = dashboard
    assert post(url, profile_payload())[0] == 200
    status, _ = post(
        url,
        {
            "action": action,
            "profile_id": "synthetic",
            "expected_revision": 1,
            "credentials": {
                "password": "TUNNEL_SECRET_CANARY",
                "sudo_password": "TUNNEL_SECRET_CANARY",
            },
        },
    )
    assert status == 202
    for _ in range(50):
        with urlopen(url + "/tunnels/data?profile=synthetic", timeout=5) as response:
            raw = response.read()
        assert b"TUNNEL_SECRET_CANARY" not in raw
        if b'"pending"' not in raw:
            break
        time.sleep(0.02)
    assert b'"failed"' in raw
    with urlopen(url + "/tunnels/events?profile=synthetic", timeout=5) as response:
        assert response.headers["Content-Type"].startswith("text/event-stream")
        lines = response.readline() + response.readline()
        assert b"event: snapshot" in lines
        assert b"TUNNEL_SECRET_CANARY" not in lines
    connection = connect_database(database)
    try:
        dump = "\n".join(connection.iterdump())
        assert "TUNNEL_SECRET_CANARY" not in dump
        assert connection.execute("SELECT state,protection FROM tunnel_runtime").fetchone() == (
            "disconnected",
            "inactive",
        )
    finally:
        connection.close()
    assert "localStorage" not in TUNNELS_JS and "sessionStorage" not in TUNNELS_JS


def test_sse_cross_origin_and_oversized_mutation_are_rejected(dashboard: tuple[str, Path]) -> None:
    url, _ = dashboard
    request = Request(url + "/tunnels/events", headers={"Origin": "https://untrusted.example"})
    with pytest.raises(HTTPError) as error:
        urlopen(request, timeout=5)
    assert error.value.code == 403
    host = url.split("://", 1)[1]
    connection = http.client.HTTPConnection(host, timeout=5)
    try:
        connection.request(
            "POST",
            "/tunnels/action",
            body=b"x" * 65537,
            headers={"Content-Type": "application/json", "Origin": url},
        )
        response = connection.getresponse()
        assert response.status == 400
        response.read()
    finally:
        connection.close()


def test_deleted_profile_has_no_server_side_effect(dashboard: tuple[str, Path]) -> None:
    url, database = dashboard
    assert post(url, profile_payload())[0] == 200
    assert (
        post(url, {"action": "delete", "profile_id": "synthetic", "expected_revision": 1})[0] == 200
    )
    connection = connect_database(database)
    try:
        assert connection.execute("SELECT count(*) FROM tunnel_profiles").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM servers").fetchone()[0] == 0
    finally:
        connection.close()


@pytest.mark.parametrize(
    "scenario", ["existing", "onboarding", "resume", "rail", "settings", "recovery", "partial"]
)
def test_guided_flow_notices_and_draft_safety(scenario: str) -> None:
    """Run shipped JS event listeners; DOM double, separate from rendered UI proof."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute tunnel UI listeners")
    script = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {source,scenario} = JSON.parse(require('node:fs').readFileSync(0,'utf8'));
let activeElement = null;
class Element {
  constructor() { this.listeners={}; this.dataset={}; this.children=[]; this.disabled=false; this.value=''; this.type='text';this.focusCount=0;this.scrollLeft=0;this.scrollTop=0; }
  addEventListener(name, callback) { this.listeners[name]=callback; }
  fire(name) { if(!this.disabled) return this.listeners[name]?.({preventDefault(){}}); }
  setAttribute(name,value) { this[name]=value; }
  removeAttribute(name) { delete this[name]; }
  append(...children) { for(const child of children)this.insertBefore(child,null); }
  insertBefore(child,before) { if(child===before)return;child.remove();const index=this.children.indexOf(before);this.children.splice(index<0?this.children.length:index,0,child);child.parentElement=this; }
  remove() {if(this.parentElement){this.parentElement.children=this.parentElement.children.filter(child=>child!==this);this.parentElement=null;if(activeElement===this)activeElement=null;}}
  replaceChildren() {for(const child of [...this.children])child.remove();}
  get firstElementChild() {return this.children[0]||null;}
  get childElementCount() { return this.children.length; }
  focus() { activeElement=this;this.focusCount++; }
  querySelector() { return submit; }
  querySelectorAll() { return this.items || []; }
  reportValidity() { return true; }
}
const selectors = ['notice','profile','credentials','editor','detail','empty','list','title','state','protection','error','metrics','latency-help','events','history','new','edit','settings-help','cancel','delete','editor-title','recover-remove','recovery-credentials','layout','sidebar','steps','editor-kicker','advanced','edit-help','save','endpoint','detail-kicker','access','monitor','secondary','status-title','status-copy','technical','technical-panel','prepare-feedback','monitor-feedback','recovery-feedback','phone','phone-dialog','phone-qr','phone-status','phone-name','phone-close','phone-retry','prepare','prepare-help','setup-back','start','password-fields','key-fields','sudo-fields','backup-access'];
const elements = Object.fromEntries(selectors.map(key=>[`[data-tunnel-${key}]`,new Element()]));
const buttons = ['connect','disconnect','check','remove'].map(action=>{ const e=new Element(); e.dataset.tunnelAction=action; elements[`[data-tunnel-action="${action}"]`]=e; return e; });
const submit=new Element(); const form=elements['[data-tunnel-profile]']; const credentials=elements['[data-tunnel-credentials]'];
function fields(form,names,defaults={}) {
 form.elements=[];
 for(const name of names){ const field=new Element();field.name=name;field.value=defaults[name]||'';form.elements.push(field);form.elements[name]=field; }
 form.elements.namedItem=name=>form.elements[name];
 form.reset=()=>{for(const field of form.elements){field.value=defaults[field.name]||'';field.checked=field.name==='lan_bypass';}};
 form.reset();
}
fields(form,['name','primary_host','primary_user','primary_port','backup_host','backup_user','backup_port','exceptions','lan_bypass'],{primary_user:'root',primary_port:'22',backup_user:'root',backup_port:'22'});
form.elements.lan_bypass.type='checkbox';
const keys=['password','private_key','key_passphrase','sudo_password','backup_password','backup_private_key','backup_key_passphrase','backup_sudo_password'];
fields(credentials,[...keys,'auth_method'],{auth_method:'password'});
const recovery=elements['[data-tunnel-recovery-credentials]'];fields(recovery,keys);recovery.append(...recovery.elements,elements['[data-tunnel-recovery-feedback]'],elements['[data-tunnel-recover-remove]']);
for(const [group,names] of [['password-fields',['password']],['key-fields',['private_key','key_passphrase']],['sudo-fields',['sudo_password']],['backup-access',keys.filter(name=>name.startsWith('backup_'))]]) elements[`[data-tunnel-${group}]`].items=names.map(name=>credentials.elements[name]);
credentials.items=[credentials.elements.auth_method];
const steps=[1,2,3].map(number=>{const element=new Element();element.dataset.tunnelStep=String(number);return element;});
for (const key of ['phone','phone-dialog','phone-qr','phone-status','phone-name','phone-close','phone-retry']) elements[`[data-tunnel-${key}]`]=new Element();
const root=new Element();root.querySelector=key=>elements[key];root.querySelectorAll=key=>key==='[data-tunnel-step]'?steps:key==='[data-tunnel-action]'?buttons:[...buttons,submit,...['save','prepare','setup-back','edit','delete','recover-remove','new','start','cancel'].map(key=>elements[`[data-tunnel-${key}]`])];
let state={profiles:[],selected:{id:'fixture',revision:1,settings:{name:'Fixture',endpoints:[{host:'203.0.113.10',user:'root',port:22}],exceptions:[],lan_bypass:true},runtime:{state:'disconnected',protection:'inactive',desired:false,snapshot:{prepared:true}},operations:[],events:[],metrics:[]}};
state.profiles=[state.selected];
if(scenario==='rail'){const other=JSON.parse(JSON.stringify(state.selected));other.id='second';other.settings.name='Second';state.profiles.push(other);}
if(scenario==='onboarding') state={profiles:[],selected:null};
if(scenario==='resume'){ state.selected.runtime.snapshot.prepared=false;state.selected.settings.endpoints.push({host:'198.51.100.20',user:'ubuntu',port:2222});state.selected.settings.exceptions=[{kind:'domain',value:'example.org'}]; }
let stream, submitted=[], networkFail=false, failSave=true;
class EventSource { constructor(){stream=this;this.listeners={};} addEventListener(name,callback){this.listeners[name]=callback;} snapshot(){this.listeners.snapshot({data:JSON.stringify(state)});} }
class FormData { constructor(form){this.fields=Object.entries(form.elements).filter(([name,value])=>value instanceof Element && !value.disabled).map(([name,value])=>[name,value.value]);} [Symbol.iterator](){return this.fields[Symbol.iterator]();} }
const context={URL,EventSource,FormData,location:{href:'http://fixture/tunnels/?profile=fixture',origin:'http://fixture'},history:{pushState(a,b,url){context.location.href=String(url);},replaceState(a,b,url){context.location.href=String(url);}},window:{addEventListener(){}},crypto:{randomUUID:()=> 'fixture-new'},confirm:()=>true,document:{get activeElement(){return activeElement;},querySelector:()=>root,querySelectorAll:()=>[],createElement:()=>new Element()},fetch:async (url,options)=>{
 if(options?.method==='POST') {
  if(networkFail) throw new Error('Temporary network failure');
  const payload=JSON.parse(options.body);submitted.push(payload);
  if(payload.action==='save') {
   if(failSave) return {ok:false,json:async()=>({error:'Invalid exception'})};
   const previous=state.selected;
   state.selected={id:payload.profile_id,revision:(previous?.revision||0)+1,settings:{...payload.settings,exceptions:payload.settings.exceptions.map(value=>({kind:'domain',value}))},runtime:previous?.runtime||{state:'disconnected',protection:'inactive',desired:false,snapshot:{prepared:false}},operations:[],events:[],metrics:[]};state.profiles=[state.selected];
   return {ok:true,json:async()=>({})};
  }
  state.selected.operations=[{id:'recovery',action:payload.action,state:'pending',message:'Accepted',created_at:'2026-11-09T00:00:00Z'}];
  return {ok:true,json:async()=>({operation:{id:'recovery'}})};
 }
 return {ok:true,json:async()=>JSON.parse(JSON.stringify(state))};
}};
vm.runInNewContext(source,context);
const flush=async()=>{for(let i=0;i<10;i++)await new Promise(resolve=>setImmediate(resolve));};
const notice=elements['[data-tunnel-notice]'];
const operation=(id,opState,message)=>{const action=state.selected.operations.find(op=>op.id===id)?.action;state.selected.operations=[{id,action,state:opState,message,created_at:'2026-11-09T00:00:00Z'}];stream.snapshot();};
(async()=>{
 await flush();
 if(scenario==='rail') {
  const rail=elements['[data-tunnel-list]'], first=rail.children[0],second=rail.children[1];
  rail.scrollLeft=137;rail.scrollTop=7;first.focus();const calls=first.focusCount;
  for(let i=0;i<3;i++){state.selected.settings.name='Renamed'+i;stream.snapshot();assert.equal(rail.children[0],first);assert.equal(rail.children[1],second);assert.equal(activeElement,first);assert.equal(first.focusCount,calls);assert.equal(rail.scrollLeft,137);assert.equal(rail.scrollTop,7);assert.equal(first.children[0].textContent,'Renamed'+i);}
  state.profiles.reverse();stream.snapshot();assert.equal(rail.children[1],first);assert.equal(activeElement,first);assert.equal(rail.scrollLeft,137);
  form.elements.name.focus();stream.snapshot();assert.equal(activeElement,form.elements.name);
  first.focus();const other=state.profiles.find(profile=>profile.id==='second');state.profiles=[state.selected];stream.snapshot();assert.equal(activeElement,elements['[data-tunnel-new]']);assert.equal(rail.children[0],first);
  state.profiles.push(other);stream.snapshot();rail.children[1].focus();state.profiles=[state.selected];stream.snapshot();assert.equal(activeElement,elements['[data-tunnel-new]']);
  return;
 }
 if(scenario==='settings') {
  assert.equal(buttons[2].hidden,false);assert.equal(buttons[2].textContent,'Проверить VPS');assert.equal(elements['[data-tunnel-settings-help]'].hidden,true);
  failSave=false;elements['[data-tunnel-edit]'].fire('click');form.elements.name.value='New name';form.elements.exceptions.value='first.example\nsecond.example';form.fire('submit');await flush();
  assert.equal(state.selected.runtime.snapshot.prepared,true);assert.equal(elements['[data-tunnel-access]'].hidden,true);assert.equal(elements['[data-tunnel-monitor]'].hidden,false);assert.match(notice.textContent,/Настройки.*сохранены/);assert.doesNotMatch(notice.textContent,/SSH/);assert.equal(elements['[data-tunnel-monitor-feedback]'].textContent,notice.textContent);assert.equal(activeElement,buttons[0]);assert.equal(submitted.at(-1).settings.exceptions.length,2);
  return;
 }
 if(scenario==='partial') {
  state.selected.runtime.snapshot={prepared:false,setup_incomplete:true};stream.snapshot();
  assert.equal(elements['[data-tunnel-access]'].hidden,false);assert.equal(buttons[0].disabled,true);
  assert.equal(elements['[data-tunnel-delete]'].disabled,true);assert.equal(buttons[3].disabled,false);
  assert.equal(elements['[data-tunnel-prepare]'].disabled,false);assert.equal(elements['[data-tunnel-recover-remove]'].disabled,false);
  elements['[data-tunnel-edit]'].fire('click');assert.equal(form.elements.primary_host.disabled,true);
  elements['[data-tunnel-cancel]'].fire('click');
  state.selected.runtime.snapshot.removal_pending=true;stream.snapshot();
  assert.equal(elements['[data-tunnel-access]'].hidden,true);assert.equal(elements['[data-tunnel-monitor]'].hidden,false);
  assert.equal(elements['[data-tunnel-recover-remove]'].disabled,false);assert.equal(elements['[data-tunnel-delete]'].disabled,true);
  state.selected.runtime.snapshot={prepared:false,setup_incomplete:false,removal_pending:false};stream.snapshot();
  assert.equal(elements['[data-tunnel-delete]'].disabled,false);assert.equal(buttons[3].disabled,true);
  return;
 }
 if(scenario==='recovery') {
  const local=elements['[data-tunnel-recovery-feedback]'],button=elements['[data-tunnel-recover-remove]'];const before=submitted.length;
  button.fire('click');assert.equal(submitted.length,before);assert.equal(local.hidden,false);assert.equal(local.role,'alert');assert.equal(recovery.firstElementChild,local);assert.equal(activeElement,recovery.elements.password);
  form.elements.name.focus();stream.snapshot();assert.equal(activeElement,form.elements.name);assert.equal(local.role,'alert');
  recovery.elements.key_passphrase.value='PASSPHRASE_ONLY';button.fire('click');assert.equal(activeElement,recovery.elements.private_key);assert.equal(submitted.length,before);
  for(const key of keys)recovery.elements[key].value='TRANSIENT_'+key;
  networkFail=true;button.fire('click');for(const key of keys)assert.equal(recovery.elements[key].value,'');assert.match(local.textContent,/Удаляем/);assert.equal(local.parentElement,recovery);assert.equal(recovery.children[recovery.children.indexOf(button)-1],local);await flush();assert.equal(local.textContent,'Temporary network failure');assert.equal(local.role,'alert');
  networkFail=false;for(const key of keys)recovery.elements[key].value='TRANSIENT_'+key;button.fire('click');await flush();assert.equal(Object.keys(submitted.at(-1).credentials).length,8);assert.equal(local.hidden,false);assert.match(local.textContent,/выполняется/);
  operation('recovery','failed','Removal failed');assert.equal(local.textContent,'Removal failed');assert.equal(local.role,'alert');
  recovery.elements.password.value='RETRY';button.fire('click');await flush();operation('recovery','succeeded','Removed successfully');assert.equal(local.textContent,'Removed successfully');assert.equal(local.dataset.error,'false');
  form.elements.name.focus();stream.snapshot();assert.equal(activeElement,form.elements.name);
  return;
 }
 if(scenario==='onboarding') {
  assert.equal(elements['[data-tunnel-editor]'].hidden,false);assert.equal(elements['[data-tunnel-sidebar]'].hidden,true);assert.equal(steps[0]['aria-current'],'step');assert.equal(elements['[data-tunnel-advanced]'].hidden,true);
  form.elements.primary_host.value='203.0.113.10';form.elements.primary_user.value='ubuntu';
  form.fire('submit');await flush();assert.equal(notice.textContent,'Invalid exception');assert.equal(form.elements.primary_host.value,'203.0.113.10');assert.equal(elements['[data-tunnel-editor]'].hidden,false);
  failSave=false;form.fire('submit');await flush();
  assert.equal(elements['[data-tunnel-editor]'].hidden,true);assert.equal(elements['[data-tunnel-access]'].hidden,false);assert.equal(elements['[data-tunnel-monitor]'].hidden,true);assert.equal(steps[1]['aria-current'],'step');assert.equal(activeElement,credentials.elements.password);
  assert.equal(submitted.at(-1).settings.name,'VPS · 203.0.113.10');assert.equal(submitted.at(-1).settings.lan_bypass,true);assert.equal(credentials.elements.sudo_password.disabled,false);
  credentials.elements.password.value='DISCARDED_CANARY';credentials.elements.auth_method.value='key';credentials.fire('change');assert.equal(credentials.elements.password.value,'');assert.equal(credentials.elements.password.disabled,true);
  credentials.elements.private_key.value='PRIVATE_KEY_CANARY';credentials.elements.key_passphrase.value='PASSPHRASE_CANARY';credentials.elements.sudo_password.value='SUDO_CANARY';credentials.fire('submit');
  assert.equal(credentials.elements.private_key.value,'');await flush();
  const access=submitted.at(-1).credentials;assert.equal(access.private_key,'PRIVATE_KEY_CANARY');assert.equal(access.password,undefined);assert.equal(access.auth_method,undefined);assert.equal(access.sudo_password,'SUDO_CANARY');assert.equal(elements['[data-tunnel-prepare]'].disabled,true);assert.equal(elements['[data-tunnel-monitor]'].hidden,true);
  operation('recovery','failed','SSH failed; retry');assert.equal(elements['[data-tunnel-prepare-feedback]'].textContent,'SSH failed; retry');assert.equal(elements['[data-tunnel-prepare-feedback]'].hidden,false);assert.equal(activeElement,credentials.elements.private_key);assert.equal(credentials.elements.auth_method.value,'key');form.elements.primary_host.focus();stream.snapshot();assert.equal(activeElement,form.elements.primary_host);assert.equal(elements['[data-tunnel-access]'].hidden,false);assert.equal(notice.dataset.error,'true');assert.equal(credentials.elements.password.value,'');assert.equal(credentials.elements.private_key.value,'');
  credentials.elements.auth_method.value='password';credentials.fire('change');credentials.elements.password.value='RETRY_CANARY';credentials.fire('submit');await flush();
  state.selected.runtime.snapshot.prepared=true;operation('recovery','succeeded','Prepared');
  assert.equal(elements['[data-tunnel-access]'].hidden,true);assert.equal(elements['[data-tunnel-monitor]'].hidden,false);assert.equal(steps[2]['aria-current'],'step');assert.equal(buttons[0].disabled,false);assert.equal(activeElement,buttons[0]);form.elements.name.focus();stream.snapshot();assert.equal(activeElement,form.elements.name);assert.match(elements['[data-tunnel-status-title]'].textContent,/VPS готов/);
  buttons[0].fire('click');assert.equal(buttons[0].textContent,'Подключаем…');assert.match(elements['[data-tunnel-monitor-feedback]'].textContent,/Подключаем/);await flush();operation('recovery','failed','Connect failed');assert.equal(elements['[data-tunnel-monitor-feedback]'].textContent,'Connect failed');
  state.selected.runtime={state:'reconnecting',protection:'unknown',desired:true,snapshot:{prepared:true,rx_bytes:100,tx_bytes:50}};stream.snapshot();assert.match(elements['[data-tunnel-protection]'].textContent,/не подтверждено/);assert.equal(elements['[data-tunnel-metrics]'].children[0].children[1].textContent,'—');
  state.selected.runtime={state:'connected',protection:'active',desired:true,snapshot:{prepared:true,tcp_ok:true,dns_ok:true,bulk_ok:true,latency_ms:50,rx_bps:0,tx_bps:20}};stream.snapshot();assert.match(elements['[data-tunnel-status-title]'].textContent,/работает/);assert.equal(elements['[data-tunnel-metrics]'].children[0].children[1].textContent,'0 Б/с');assert.equal(elements['[data-tunnel-edit]'].disabled,true);assert.equal(elements['[data-tunnel-metrics]'].children[2].children[0].textContent,'Отклик HTTPS');assert.equal(elements['[data-tunnel-metrics]'].children[2].children[1].textContent,'50 мс');
  return;
 }
 if(scenario==='resume') {
  assert.equal(elements['[data-tunnel-access]'].hidden,false);assert.equal(steps[1]['aria-current'],'step');assert.equal(elements['[data-tunnel-backup-access]'].hidden,false);
  credentials.elements.password.value='UNSENT_CANARY';stream.snapshot();assert.equal(credentials.elements.password.value,'UNSENT_CANARY');
  elements['[data-tunnel-setup-back]'].fire('click');assert.equal(credentials.elements.password.value,'');assert.equal(form.elements.backup_host.value,'198.51.100.20');assert.equal(form.elements.backup_port.value,2222);assert.equal(form.elements.exceptions.value,'example.org');
  failSave=false;form.elements.name.value='Renamed';form.fire('submit');await flush();assert.equal(submitted.at(-1).settings.endpoints.length,2);assert.equal(submitted.at(-1).settings.exceptions[0],'example.org');assert.equal(submitted.at(-1).expected_revision,1);assert.equal(elements['[data-tunnel-access]'].hidden,false);
  return;
 }
 operation('one' ,'pending','Accepted'); assert.match(notice.textContent,/выполняется/);
 operation('one','succeeded','Prepared successfully'); assert.equal(notice.textContent,'Prepared successfully');assert.equal(notice.dataset.error,'false');
 operation('two','pending','Accepted');operation('two','failed','Remote unavailable'); assert.equal(notice.textContent,'Remote unavailable');assert.equal(notice.dataset.error,'true');
 stream.onerror();assert.match(notice.textContent,/переподключается/);stream.snapshot();assert.match(notice.textContent,/актуальны/);assert.equal(notice.dataset.error,'false');
 stream.onerror();elements['[data-tunnel-edit]'].fire('click');assert.equal(form.elements.primary_host.disabled,true);assert.equal(form.elements.backup_host.disabled,true);assert.equal(notice.dataset.error,'true');form.elements.name.value='Unsaved draft';form.elements.name.focus();
 stream.onerror();stream.snapshot();assert.equal(form.elements.name.value,'Unsaved draft');assert.equal(activeElement,form.elements.name);assert.equal(notice.dataset.error,'false');
 form.fire('submit');await flush();assert.equal(notice.textContent,'Invalid exception');
 stream.onerror();stream.snapshot();assert.equal(notice.textContent,'Invalid exception');assert.equal(notice.dataset.error,'true');assert.equal(form.elements.name.value,'Unsaved draft');assert.equal(activeElement,form.elements.name);
 elements['[data-tunnel-cancel]'].fire('click');
 networkFail=true;buttons.find(button=>button.dataset.tunnelAction==='check').fire('click');await flush();assert.equal(notice.textContent,'Temporary network failure');networkFail=false;stream.snapshot();assert.equal(notice.dataset.error,'false');
 let prevented=false;recovery.listeners.submit({preventDefault(){prevented=true;}});assert.equal(prevented,true);
 const before=submitted.length;elements['[data-tunnel-recover-remove]'].fire('click');await flush();assert.equal(submitted.length,before);
 recovery.elements.password.value='TRANSIENT_UI_CANARY';elements['[data-tunnel-recover-remove]'].fire('click');assert.equal(recovery.elements.password.value,'');await flush();
 assert.equal(submitted.at(-1).action,'remove');assert.equal(submitted.at(-1).credentials.password,'TRANSIENT_UI_CANARY');assert.match(notice.textContent,/выполняется/);
 operation('recovery','succeeded','Removed successfully');assert.equal(notice.textContent,'Removed successfully');assert.equal(notice.dataset.error,'false');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run(
        [node, "-e", script],
        input=json.dumps({"source": TUNNELS_JS, "scenario": scenario}),
        text=True,
        check=False,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
