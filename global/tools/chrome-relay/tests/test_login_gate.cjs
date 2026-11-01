'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

const source=fs.readFileSync(path.join(__dirname,'..','extension','login_gate.js'),'utf8');
const ORIGIN='https://billingo.synthetic.test';
const plain=value=>JSON.parse(JSON.stringify(value));

function fixture(options={}) {
  class Element {
    constructor(tag,attrs={}) { this._tag=tag.toUpperCase();this.attrs={...attrs};this.children=[];this.disabled=false;this.autofilled=attrs.autofilled===true; }
    get type(){return this.attrs.type||'';}
    get autocomplete(){return this.attrs.autocomplete||'';}
    getClientRects(){return this.attrs.hidden?[]:[{}];}
    getAttribute(name){return Object.hasOwn(this.attrs,name)?String(this.attrs[name]):null;}
    hasAttribute(name){return Object.hasOwn(this.attrs,name);}
    contains(node){return node===this||this.children.includes(node);}
    matches(selector){return selector===':-webkit-autofill'&&this.autofilled;}
    getBoundingClientRect(){return {left:10,top:10,width:40,height:20};}
  }
  Object.defineProperty(Element.prototype,'tagName',{get(){return this._tag;}});
  class HTMLFormElement extends Element {
    constructor(attrs,elements){super('form',attrs);this._elements=elements;this.children=elements;this.isConnected=true;}
  }
  Object.defineProperties(HTMLFormElement.prototype,{
    elements:{get(){return this._elements;}},
    action:{get(){return new URL(this.attrs.action,ORIGIN+pagePath).href;}},
    method:{get(){return this.attrs.method||'get';}},
    target:{get(){return this.attrs.target||'';}}
  });
  class Document {
    querySelectorAll(selector){
      if(selector==='form')return [form];
      if(selector==='input')return form.elements.filter(el=>el.tagName==='INPUT');
      return [];
    }
    elementFromPoint(){return button;}
  }
  const pagePath=options.pagePath||'/appmaker/list_connection.php';
  const username=new Element('input',{type:'text',name:'user_name',autocomplete:'username',autofilled:options.usernameAutofilled!==false});
  const password=new Element('input',{type:'password',name:'user_passwd',autocomplete:'current-password',autofilled:options.passwordAutofilled!==false});
  Object.defineProperty(username,'value',{get(){throw new Error('credential_value_access');}});
  Object.defineProperty(password,'value',{get(){throw new Error('credential_value_access');}});
  const button=new Element('input',{type:'submit',...(options.buttonAttrs||{})});
  const form=new HTMLFormElement({method:'post',action:options.action||'/appmaker/login.php',...(options.formAttrs||{})},[username,password,button]);
  const document=new Document();
  const listeners=[];
  const location={origin:ORIGIN,pathname:pagePath,search:'',hash:'',href:ORIGIN+pagePath};
  const sandbox={document,Element,HTMLFormElement,Document,URL,Date,location,addEventListener:(...args)=>listeners.push(args),removeEventListener:()=>{},setTimeout:()=>1,clearTimeout:()=>{},getComputedStyle:()=>({visibility:'visible'}),innerWidth:100,innerHeight:100};
  const context=vm.createContext(sandbox);
  vm.runInContext('globalThis.window=globalThis;globalThis.top=globalThis',context);
  if(options.framed)vm.runInContext('globalThis.top={}',context);
  vm.runInContext(source,context,{filename:'login_gate.js'});
  return {context,probe:command=>context.FleetLoginGate.loginFormProbe(command),listeners};
}

test('Billingo inline handlers are described during inspect and allowed only on an explicitly frozen arm',()=>{
  const h=fixture({formAttrs:{onsubmit:'return formVerifyData(this)'},buttonAttrs:{onclick:'submitWithAppCode()'}});
  const inspect=h.probe({mode:'inspect',origin:ORIGIN,pages:['/appmaker/list_connection.php'],posts:['/appmaker/login.php']});
  assert.equal(inspect.eligible,true);assert.equal(inspect.inline_submit,true);assert.equal(inspect.inline_click,true);
  const unsafeArm=h.probe({mode:'arm',origin:ORIGIN,pages:['/appmaker/list_connection.php'],posts:['/appmaker/login.php'],deadline:Date.now()+1000});
  assert.deepEqual(plain(unsafeArm),{eligible:false,reason:'unsupported_login'});
  const arm=h.probe({mode:'arm',origin:ORIGIN,pages:['/appmaker/list_connection.php'],posts:['/appmaker/login.php'],allowInline:true,deadline:Date.now()+1000});
  assert.equal(arm.eligible,true);assert.equal(arm.armed,true);assert.equal(h.listeners.length,3);
});

test('password autofill alone cannot authorize Billingo when its username field is empty',()=>{
  const h=fixture({usernameAutofilled:false,passwordAutofilled:true});
  const result=h.probe({mode:'inspect',origin:ORIGIN,pages:['/appmaker/list_connection.php'],posts:['/appmaker/login.php']});
  assert.deepEqual(plain(result),{eligible:false,reason:'needs_owner'});
});

test('missing password autofill, unsafe native overrides, wrong route and iframe are rejected',()=>{
  const base={mode:'inspect',origin:ORIGIN,pages:['/appmaker/list_connection.php'],posts:['/appmaker/login.php']};
  assert.equal(fixture({passwordAutofilled:false}).probe(base).reason,'needs_owner');
  assert.equal(fixture({buttonAttrs:{formaction:'/steal.php'}}).probe(base).reason,'unsupported_login');
  assert.equal(fixture({action:'/other.php'}).probe(base).reason,'login_route');
  assert.equal(fixture({pagePath:'/appmaker/other.php'}).probe(base).reason,'login_route');
  assert.equal(fixture({framed:true}).probe(base).reason,'unsupported_login');
});
