from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from harness.vault.assets import VAULT_HTML, VAULT_JS

_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is required for vault browser behavior")

# Small event/storage double; layout, focus and actual forms are checked in browser acceptance.
_PROBE = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {source, html, scenario} = JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const storage = new Map();
const key = 'harness-vault-session';
const secret = 'private-ui-canary';
function page() {
  const calls = [], listeners = {}, nodes = {}, all = [];
  class Element {
    constructor() { this.value=''; this.children=[]; this.listeners={}; this.hidden=false;
      this.dataset={}; this.classList={toggle() {}}; this.textContent=''; all.push(this); }
    addEventListener(event, fn) { this.listeners[event]=fn; }
    fire(event) { return this.listeners[event]?.({preventDefault(){}, target:this}); }
    append(...nodes) { this.children.push(...nodes); }
    replaceChildren(...nodes) { this.children=nodes; }
    get childElementCount() { return this.children.length; }
    setAttribute(name, value) { this[name]=value; }
    reset() { for(const field of this.fields || []) field.value=''; }
    querySelectorAll(selector) {
      return ['[name]', 'input[name],textarea[name],select[name]'].includes(selector) ? this.fields : [];
    }
    close() {} showModal() {} focus() {}
  }
  for (const [,id] of html.matchAll(/id="([^"]+)"/g)) nodes[id]=new Element();
  const fields = [...html.matchAll(/name="([^"]+)"/g)].map(([,name])=>Object.assign(new Element(),{name}));
  nodes['record-form'].fields=fields;
  nodes['record-form'].elements={namedItem(name){return fields.find(f=>f.name===name);}};
  const record = {id:'one',kind:'password',title:'Panel',project_id:'p',project_name:'Demo',
    host:'',username:'operator',password:secret,url:'',port:'',private_key:'',notes:'test note'};
  const device = {available:true,enabled:scenario==='automatic'||scenario==='lock',automatic:true};
  const access_mode = scenario==='no_password' ? 'no_password' : 'password';
  const state = {revision:'revision',records:[{...record,password:undefined,notes:undefined}],
    backup_folder:'/test/backups',backup_at:'',device,access_mode};
  let copied='', confirmResult=true;
  const context = {
    document:{getElementById:id=>nodes[id],createElement:()=>new Element(),
      querySelectorAll:selector=>selector==='[data-kind]'?[]:all,
      addEventListener(){},hidden:false},
    window:{addEventListener:(event,fn)=>{listeners[event]=fn;}},
    sessionStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
    location:{hash:'#project=p&name=Demo'}, URLSearchParams, Date, setInterval(){},setTimeout(){},
    requestAnimationFrame(fn){fn();},confirm:()=>confirmResult,
    navigator:{clipboard:{async writeText(value){copied=value;}}},
    async fetch(url, options){
      const body=JSON.parse(options.body); calls.push(body);
      let value;
      if(body.action==='status') value={exists:true,device,access_mode};
      else if(body.action==='state') {
        assert.equal(options.headers.Authorization, 'Bearer saved-bearer'); value=state;
      } else if(body.action==='device_unlock') value={...state,token:'device-bearer'};
      else if(body.action==='open_without_password') value={...state,token:'open-bearer'};
      else if(body.action==='detail') value={record};
      else if(body.action==='lock') { assert.equal(options.keepalive,true); value={}; }
      else throw Error('unexpected '+body.action);
      return {ok:true,status:200,json:async()=>value};
    }
  };
  vm.runInNewContext(source, context);
  return {calls,nodes,listeners,get copied(){return copied;},
    set confirmResult(value){confirmResult=value;}};
}
async function settle(){for(let i=0;i<8;i++) await new Promise(resolve=>setImmediate(resolve));}
(async()=>{
  if(!['automatic','no_password'].includes(scenario)) storage.set(key,'saved-bearer');
  if(scenario==='no_password') storage.set(key+'-locked','1');
  const first=page(); await settle();
  assert.equal(first.nodes.workspace.hidden,false);
  assert(!JSON.stringify([...storage]).includes(secret));
  if(scenario==='no_password') {
    assert.deepEqual(first.calls.map(c=>c.action),['status','open_without_password']);
    assert.equal(storage.get(key),'open-bearer');
    assert.equal(first.nodes.lock.hidden,true);
    assert.equal(first.nodes['device-settings'].hidden,true);
    assert.equal(first.nodes['master'].required,false);
    assert.equal(first.nodes['unlock-form'].hidden,true);
    return;
  }
  if(scenario==='automatic') {
    assert.deepEqual(first.calls.map(c=>c.action),['status','device_unlock']);
    assert.equal(storage.get(key),'device-bearer');
    return;
  }
  first.nodes.records.children[0].fire('click'); await settle();
  assert.equal(first.nodes.viewer.hidden,false);
  assert.equal(first.nodes.editor.hidden,true);
  assert.equal(first.nodes['view-title'].textContent,'Panel');
  function descendants(node) {return [node,...node.children.flatMap(descendants)];}
  const values=descendants(first.nodes['view-fields']);
  assert(!values.some(n=>n.textContent===secret));
  values.find(n=>n['aria-label']==='Копировать: Пароль').fire('click'); await settle();
  assert.equal(first.copied,secret);
  if(scenario==='record-navigation') {
    first.nodes['back-to-records'].fire('click');
    assert.equal(first.nodes.viewer.hidden,true);
    assert.equal(first.nodes['selection-empty'].hidden,false);
    assert.equal(first.nodes['view-fields'].children.length,0);
    first.nodes.records.children[0].fire('click'); await settle();
    first.nodes['edit-record'].fire('click');
    const notes=first.nodes['record-form'].elements.namedItem('notes');
    notes.value='unfinished note'; first.nodes['record-form'].fire('input');
    first.confirmResult=false;
    first.nodes['back-to-records'].fire('click');
    assert.equal(first.nodes.editor.hidden,false,'cancel preserves editor');
    assert.equal(notes.value,'unfinished note','cancel preserves draft');
    first.confirmResult=true;
    first.nodes['back-to-records'].fire('click');
    assert.equal(first.nodes.editor.hidden,true);
    assert.equal(first.nodes['selection-empty'].hidden,false);
    assert.equal(notes.value,'');
    assert(!first.calls.some(c=>c.action==='save'||c.action==='lock'));
    return;
  }
  if(scenario==='lock') {
    first.nodes.lock.fire('click'); await settle();
    assert(!storage.has(key)); assert.equal(first.calls.at(-1).action,'lock');
    assert.equal(first.nodes.workspace.hidden,true);
    assert.equal(first.nodes['view-fields'].children.length,0);
    const second=page(); await settle();
    assert.deepEqual(second.calls.map(c=>c.action),['status']);
  } else {
    first.listeners.pagehide({}); await settle();
    assert.equal(storage.get(key),'saved-bearer');
    assert(!first.calls.some(c=>c.action==='lock'));
    assert.equal(first.nodes['view-fields'].children.length,0);
    const second=page(); await settle();
    assert.deepEqual(second.calls.map(c=>c.action),['status','state']);
    assert.equal(second.nodes.workspace.hidden,false);
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
"""


@pytest.mark.parametrize(
    "scenario", ["navigation", "record-navigation", "lock", "automatic", "no_password"]
)
def test_vault_session_navigation_lock_and_private_view(scenario: str) -> None:
    result = subprocess.run(
        [_NODE or "node", "-e", _PROBE],
        input=json.dumps({"source": VAULT_JS, "html": VAULT_HTML, "scenario": scenario}),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
