/* Page checks: the rules of the Jarviz page (viewer/app.js), proved in Node without a browser.

   It loads the real core.js and app.js into a fake page: a few fake page elements, a fake 3D graph
   library, a fake microphone, fake timers and a scripted server. Then it plays short stories (a reply
   that comes late, a read of the galaxy that fails, a reader closed too early ...) and checks what the
   page did. Nothing goes to the network, and no file is read except the two that are named.

   preflight runs it:   node page_checks.js viewer/core.js viewer/app.js
   It prints ONE line on stdout, JSON: {"pass": 143, "fail": 0, "failures": []}. The exit code is 0 when it
   ran to its end, also when a rule failed. Any other exit code means it could not run (a file is
   missing, or the page code threw while it loaded); the reason is on stderr.
   For people:          node page_checks.js viewer/core.js viewer/app.js --list   (the name of every check)

   To add a case: add a story(...) below, with a short plain name for each check it makes (the name
   says the rule), and call t.check(key, ok, detail) once for each key. A check that is never reached
   fails. A story that throws or never ends fails as a whole, also after all its checks passed. A promise
   that the page lets fall (no handler) is a failure of the story that runs then; a story that drops one on
   purpose calls t.expectRejection(/its message/) first. Node's standard library only. */
'use strict';
const fs = require('fs');
const vm = require('vm');

const STORY_MS = 3000;                  // a story that has not ended by then failed (an await never settled)
let loaded = null;                      // the two page files (main reads them): the harness story runs stories of its own

// ------------------------------------------------------------------ the fake page

const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const settle = async () => { for (let i = 0; i < 30; i++) await new Promise(done => setImmediate(done)); };

function makeElement(id, tag = 'div') {
  const classes = new Set();
  return {
    id, tagName: tag.toUpperCase(), hidden: false, textContent: '', className: '', dataset: {}, style: {},
    children: [], attrs: {}, listeners: {}, scrollTop: 0, scrollHeight: 0, value: '', disabled: false, title: '',
    classList: {
      toggle(name, on) { if (on === undefined ? !classes.has(name) : on) classes.add(name); else classes.delete(name); },
      add(name) { classes.add(name); }, remove(name) { classes.delete(name); }, contains: name => classes.has(name),
    },
    setAttribute(key, value) { this.attrs[key] = String(value); }, removeAttribute(key) { delete this.attrs[key]; },
    replaceChildren(...kids) { this.children = kids; }, append(...kids) { this.children.push(...kids); },
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    getBoundingClientRect: () => ({ left: 0, right: 100, top: 0, bottom: 20, width: 100, height: 20 }),
    get lastElementChild() { return this._last || (this._last = makeElement(id + ':last', 'b')); },
  };
}

// Elements that index.html starts hidden.
const HIDDEN_AT_START = ['panel', 'teardown', 'reader', 'answer', 'heard', 'reader-obsidian', 'td-line', 'td-done',
                         'td-shot', 'brain', 'voice', 'muted'];

// A handler may answer with a whole response instead of a JSON body: the audio of /voice/speak, or a refusal with
// its HTTP status.
const RESPONSE = Symbol('response');
const audioResponse = (parts, bytes = 'mp3') => ({ [RESPONSE]: true, status: 200, type: 'audio/mpeg',
  headers: { 'x-jarviz-parts': String(parts) }, blob: { bytes } });
const refusal = (status, body) => ({ [RESPONSE]: true, status, type: 'application/json', headers: {}, body });

// One fresh page. options: graph (the GRAPH on the page), handlers (path -> answer), reduced (reduced motion),
// search (the address, default "?mute=1"), synth (a fake speechSynthesis), storage (localStorage items, or
// "blocked"), audio (play -> "ok" | "reject", for each new audio element in turn).
function page(sources, options = {}) {
  let now = 0, nextId = 1, timers = [];
  const setTimer = (fn, ms = 0) => { const id = nextId++; timers.push({ id, at: now + (ms || 0), fn }); return id; };
  const clearTimer = id => { timers = timers.filter(timer => timer.id !== id); };
  const repeating = new Set();
  const setRepeat = (fn, ms = 0) => {
    const id = nextId++;
    const run = () => { if (!repeating.has(id)) return; fn(); if (repeating.has(id)) timers.push({ id, at: now + ms, fn: run }); };
    repeating.add(id);
    timers.push({ id, at: now + ms, fn: run });
    return id;
  };
  const clearRepeat = id => { repeating.delete(id); clearTimer(id); };
  async function tick(ms) {             // let fake time pass, running every timer that falls due
    const end = now + ms;
    await settle();
    for (;;) {
      timers.sort((a, b) => a.at - b.at || a.id - b.id);
      const next = timers[0];
      if (!next || next.at > end) break;
      timers.shift();
      now = next.at;
      next.fn();
      await settle();
    }
    now = end;
    await settle();
  }

  const elements = new Map();
  const document = {
    getElementById(id) {
      if (!elements.has(id)) {
        const node = makeElement(id);
        node.hidden = HIDDEN_AT_START.includes(id);
        elements.set(id, node);
      }
      return elements.get(id);
    },
    createElement: tag => makeElement(null, tag),
  };

  const controls = { listeners: {}, autoRotate: false, autoRotateSpeed: 2,
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); } };
  const graphLog = [];
  function ForceGraph3D() {             // every call is logged; a call with no value gives the last value back
    const store = {};
    const special = {
      controls: () => controls,
      camera: () => ({ position: { x: 0, y: 0, z: 1000 } }),
      scene: () => ({ add() {} }),
      d3Force: () => ({ strength() { return this; }, distance() { return this; } }),
    };
    const graph = new Proxy({}, { get(_, name) {
      if (typeof name !== 'string') return undefined;
      if (special[name]) return (...args) => { graphLog.push([name, args]); return special[name](...args); };
      return (...args) => { graphLog.push([name, args]); if (!args.length) return store[name]; store[name] = args; return graph; };
    } });
    return graph;
  }

  const recognizers = [];
  class FakeRecognition {
    constructor() { this.started = 0; this.stopped = 0; this.throwOnStart = false; recognizers.push(this); }
    start() { if (this.throwOnStart) throw new Error('InvalidStateError'); this.started += 1; }
    stop() { this.stopped += 1; }
  }

  const fetchLog = [];
  const handlers = Object.assign({
    '/status': () => ({ ok: true, active: { label: 'Claude', model: 'm', has_key: true }, lines: {}, graph: meta(5) }),
  }, options.handlers || {});
  const aborted = () => Object.assign(new Error('aborted'), { name: 'AbortError' });
  function fakeFetch(url, init = {}) {
    const key = String(url).split('?')[0];
    fetchLog.push({ url: String(url), key, body: init.body ? JSON.parse(init.body) : null });
    const handler = handlers[key];
    return new Promise((resolve, reject) => {
      if (init.signal) {
        if (init.signal.aborted) return reject(aborted());
        init.signal.addEventListener('abort', () => reject(aborted()));
      }
      if (!handler) return reject(new Error('no answer for ' + url));
      Promise.resolve().then(() => handler(String(url), init)).then(answer => {
        const whole = answer && answer[RESPONSE] ? answer : { status: 200, type: 'application/json', headers: {}, body: answer };
        const body = whole.body;
        const headers = Object.assign({ 'content-type': whole.type }, whole.headers);
        resolve({
          ok: whole.status < 400, status: whole.status,
          headers: { get: name => (headers[String(name).toLowerCase()] === undefined ? null : headers[String(name).toLowerCase()]) },
          json: async () => (typeof body === 'string' ? JSON.parse(body) : body),
          text: async () => (typeof body === 'string' ? body : JSON.stringify(body)),
          blob: async () => whole.blob || { bytes: '' },
        });
      }, reject);
    });
  }

  // The browser's voice, when the story asks for it: every utterance is kept; finish() ends the one being said.
  const synth = options.synth ? {
    speaking: false, pending: false, said: [], cancels: 0, now: null,
    speak(utterance) {
      this.said.push(utterance.text);
      this.now = utterance;
      this.speaking = true;
      Promise.resolve().then(() => { if (this.now === utterance && utterance.onstart) utterance.onstart(); });
    },
    cancel() {
      this.cancels += 1;
      const was = this.now;
      this.now = null;
      this.speaking = false;
      if (was && was.onerror) Promise.resolve().then(() => was.onerror({ error: 'canceled' }));
    },
    finish() {
      const was = this.now;
      this.now = null;
      this.speaking = false;
      if (was && was.onend) was.onend();
    },
    getVoices: () => [],
  } : undefined;
  function FakeUtterance(text) { this.text = text; }

  // Audio elements and object URLs. play() resolves, or rejects when options.audio says so for that element.
  const audios = [];
  const liveUrls = new Set();
  let urlCount = 0;
  const playPlan = (options.audio || []).slice();
  class FakeAudio {
    constructor(src) { this.src = src; this.played = false; this.paused = true; audios.push(this); }
    play() {
      this.played = true;
      this.paused = false;
      return playPlan.shift() === 'reject' ? Promise.reject(new Error('NotAllowedError')) : Promise.resolve();
    }
    pause() { this.paused = true; }
    end() { this.paused = true; if (this.onended) this.onended(); }        // the part played to its end
    fail() { if (this.onerror) this.onerror(new Error('decode')); }        // an error event on the element
  }
  class PageURL extends URL {
    static createObjectURL(blob) { const url = 'blob:part-' + (++urlCount) + ':' + (blob && blob.bytes); liveUrls.add(url); return url; }
    static revokeObjectURL(url) { liveUrls.delete(url); }
  }

  const stored = options.storage === 'blocked' ? null : Object.assign({}, options.storage || {});
  const refuse = () => { throw new Error('SecurityError: the storage is blocked'); };
  const localStorage = stored === null ? { getItem: refuse, setItem: refuse, removeItem: refuse } : {
    getItem: key => (key in stored ? stored[key] : null),
    setItem: (key, value) => { stored[key] = String(value); },
    removeItem: key => { delete stored[key]; },
  };

  const consoleLines = [];             // the page's console goes here, never to stdout
  const keep = (...parts) => consoleLines.push(parts.map(String).join(' '));
  const sandbox = {
    console: { log: keep, warn: keep, error: keep, info: keep, debug: keep },
    document, setTimeout: setTimer, clearTimeout: clearTimer, setInterval: setRepeat, clearInterval: clearRepeat,
    requestAnimationFrame: () => 0,
    performance: { now: () => now }, fetch: fakeFetch, AbortController, URLSearchParams, URL: PageURL, crypto: globalThis.crypto,
    sessionStorage: { items: {}, getItem(k) { return this.items[k] || null; }, setItem(k, v) { this.items[k] = String(v); } },
    localStorage, Audio: FakeAudio, speechSynthesis: synth, SpeechSynthesisUtterance: synth ? FakeUtterance : undefined,
    // a muted tab by default: the page never speaks
    navigator: { language: 'en-GB' }, location: { search: options.search === undefined ? '?mute=1' : options.search },
    matchMedia: () => ({ matches: Boolean(options.reduced) }), addEventListener() {}, innerWidth: 1400, innerHeight: 900,
    SpeechRecognition: FakeRecognition, ForceGraph3D,
    GRAPH: options.graph || galaxy(5, ['A', 'B', 'C']),
  };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(sources.core, sandbox, { filename: 'core.js' });
  // Every "speaking ended" call that the speaker makes (its env.onEnd), counted where it is made: a second call
  // changes nothing on the status chip, so the chip cannot show it.
  const ends = [];
  const makeSpeaker = sandbox.JarvizCore.createSpeaker;
  sandbox.JarvizCore.createSpeaker = env => makeSpeaker(Object.assign({}, env, {
    onEnd: (...args) => { ends.push(args[0]); return env.onEnd ? env.onEnd(...args) : undefined; },
  }));
  vm.runInContext(sources.app, sandbox, { filename: 'app.js' });
  return { $: id => document.getElementById(id), J: () => sandbox.Jarviz, tick, fetchLog, handlers, recognizers,
           controls, graphLog, consoleLines, synth, audios, liveUrls, stored, ends };
}

// ------------------------------------------------------------------ small makers

const meta = (rev, build_id = 'b1') => ({ build_id, rev });
function galaxy(rev, labels, build_id = 'b1') {
  return { build_id, rev, nodes: labels.map((label, id) => ({ id, label, group: 'notes', excerpt: '' })), links: [] };
}
const graphFile = data => 'window.GRAPH = ' + JSON.stringify(data) + ';\n';
const receipt = (index, label, build = 'b1') => ({ index, title: label, file: 'captures/' + label + '.md', related: 0, build,
  born: 'born-' + label, node: { id: index, label, group: 'captures', excerpt: '' }, links: [{ source: index, target: 0 }] });
const rememberReply = (rev, index, label, build = 'b1') => ({ ok: true, answer: 'Saved ' + label, nodes: [index], graph: meta(rev),
  action: { type: 'remember', receipt: receipt(index, label, build) } });
const chatReply = (answer, rev, nodes = []) => ({ ok: true, answer, nodes, graph: meta(rev) });
const job = (id, state, extra = {}) => Object.assign({ id, label: 'Teardown · x', state, step: 2, steps: ['one', 'two', 'three'] }, extra);
const count = (log, key) => log.filter(entry => entry.key === key).length;
const labels = p => p.J().data().nodes.map(node => node.label).join(',');
const finalResult = text => ({ resultIndex: 0, results: [Object.assign([{ transcript: text }], { isFinal: true })] });

// ------------------------------------------------------------------ stories

const STORIES = [];
function story(title, checks, run) { STORIES.push({ title, checks, run }); }

story('camera start', {
  place: 'the camera starts at (0, 70, 560), set once, by Focus.start',
  spin: 'the idle rotation is on, at speed 0.5',
  hands: 'grabbing and letting go of the galaxy are wired once each',
}, async (t, open) => {
  const p = await open();
  const starts = p.graphLog.filter(([name, args]) => name === 'cameraPosition' && args[0] && args[0].z === 560 && args[0].y === 70);
  t.check('place', starts.length === 1 && typeof p.J().Focus.start === 'function', `${starts.length} start calls`);
  t.check('spin', p.controls.autoRotate === true && p.controls.autoRotateSpeed === 0.5, `${p.controls.autoRotate} ${p.controls.autoRotateSpeed}`);
  t.check('hands', (p.controls.listeners.start || []).length === 1 && (p.controls.listeners.end || []).length === 1);
});

story('camera start with reduced motion', {
  still: 'with reduced motion the camera starts in the same place and does not rotate',
}, async (t, open) => {
  const p = await open({ reduced: true });
  const placed = p.graphLog.some(([name, args]) => name === 'cameraPosition' && args[0] && args[0].z === 560);
  t.check('still', placed && p.controls.autoRotate === false && p.controls.autoRotateSpeed === 0.5, `${p.controls.autoRotate}`);
});

story('microphone', {
  stop: 'turning the microphone off stops the recognizer',
  late: 'a final result that comes after the microphone is off saves nothing',
  live: 'a live microphone still delivers a sentence',
  restart: 'after silence the same recognizer starts again',
  replace: 'a recognizer that cannot start again is replaced by a new one',
  dead: 'a replaced recognizer delivers nothing and cannot turn the microphone off',
  fresh: 'the new recognizer delivers',
}, async (t, open) => {
  const p = await open({ handlers: { '/remember': () => rememberReply(6, 3, 'Late') } });
  const click = () => p.$('mic').listeners.click[0]();
  click();
  const first = p.recognizers[0];
  click();                                            // off, before the phrase was final
  t.check('stop', first.stopped === 1);
  first.onresult(finalResult('remember that the till was short'));
  await p.tick(3000);
  t.check('late', count(p.fetchLog, '/remember') === 0);
  click();
  const second = p.recognizers[1];
  second.onresult(finalResult('remember that the live one works'));
  await p.tick(1500);
  t.check('live', count(p.fetchLog, '/remember') === 1);
  second.onend();                                     // Chrome ends a session after some silence
  await p.tick(300);
  t.check('restart', second.started === 2, `started ${second.started} times`);
  second.throwOnStart = true;
  second.onend();
  await p.tick(300);
  const third = p.recognizers[2];
  t.check('replace', Boolean(third) && third.started === 1);
  second.onresult(finalResult('remember that the old one is dead'));
  second.onerror({ error: 'not-allowed' });
  await p.tick(1500);
  t.check('dead', count(p.fetchLog, '/remember') === 1 && p.$('mic').attrs['aria-pressed'] === 'true');
  third.onresult(finalResult('remember that the third works'));
  await p.tick(1500);
  t.check('fresh', count(p.fetchLog, '/remember') === 2);
});

story('a capture one step ahead (base)', {
  born: 'a capture one step ahead of the page is born at its index',
  noRead: 'a capture one step ahead needs no read of the galaxy',
  focus: 'the new note is focused and the answer is shown',
}, async (t, open) => {
  const p = await open({ handlers: { '/remember': () => rememberReply(6, 3, 'T') } });
  await p.J().handleInput('remember that T');
  await p.tick(0);
  const nodes = p.J().data().nodes;
  t.check('born', nodes.length === 4 && nodes[3].id === 3 && nodes[3].label === 'T', labels(p));
  t.check('noRead', count(p.fetchLog, 'graph-data.js') === 0);
  await p.tick(1000);
  t.check('focus', p.J().Focus.current().primary === 3 && p.$('answer').textContent === 'Saved T');
});

story('a capture after a read of the galaxy (reloaded)', {
  once: 'after a read of the galaxy a capture is not added a second time, and ids equal indexes',
  focus: 'after a read that lands on the reply\'s own rev the saved note is found by its index and name, and focused',
  numbers: 'after a read the numbers of the file are kept, so the next reply needs no read',
}, async (t, open) => {
  let p = await open({ handlers: {
    '/remember': () => rememberReply(7, 3, 'T'),                               // the server counted two steps
    'graph-data.js': () => graphFile(galaxy(7, ['A', 'B', 'C', 'T'])),          // the file is the reply's snapshot
  } });
  await p.J().handleInput('remember that T');
  await p.tick(1000);
  const nodes = p.J().data().nodes;
  t.check('once', nodes.length === 4 && nodes.every((node, index) => node.id === index), labels(p));
  t.check('focus', p.J().Focus.current().primary === 3 && p.$('answer').textContent === 'Saved T');
  p = await open({ handlers: {
    '/remember': () => rememberReply(7, 3, 'T'),
    'graph-data.js': () => graphFile(galaxy(9, ['A', 'B', 'C', 'T', 'Other tab'])),     // newer than the reply
    '/chat': () => chatReply('chat', 9),
  } });
  await p.J().handleInput('remember that T');
  await p.tick(1000);
  await p.J().handleInput('what now');
  await p.tick(0);
  t.check('numbers', count(p.fetchLog, 'graph-data.js') === 1, `${count(p.fetchLog, 'graph-data.js')} reads`);
});

story('a capture whose place holds another note after a read', {
  asFile: 'after a read, a capture whose place holds another note leaves the galaxy as the file says',
  still: 'that capture focuses nothing, and its answer is still shown',
}, async (t, open) => {
  const p = await open({ handlers: {
    '/remember': () => rememberReply(7, 3, 'T'),
    'graph-data.js': () => graphFile(galaxy(7, ['A', 'B', 'C', 'Someone else'])),
  } });
  await p.J().handleInput('remember that T');
  await p.tick(1000);
  const nodes = p.J().data().nodes;
  t.check('asFile', nodes.length === 4 && nodes[3].label === 'Someone else', labels(p));
  t.check('still', p.J().Focus.current().primary === null && p.$('answer').textContent === 'Saved T');
});

story('a read of the galaxy that fails', {
  noNote: 'a failed read of the galaxy adds no note',
  answer: 'after a failed read the answer stays on screen, and the save is not sent again',
  again: 'a failed read is tried again at the next reply',
}, async (t, open) => {
  let fail = true;
  const p = await open({ handlers: {
    '/remember': () => rememberReply(7, 3, 'T'),
    'graph-data.js': () => { if (fail) throw new Error('offline'); return graphFile(galaxy(7, ['A', 'B', 'C', 'T'])); },
    '/chat': () => chatReply('chat', 7),
  } });
  await p.J().handleInput('remember that T');
  await p.tick(1000);
  t.check('noNote', p.J().data().nodes.length === 3, labels(p));
  t.check('answer', p.$('answer').textContent === 'Saved T' && !p.$('answer').hidden && count(p.fetchLog, '/remember') === 1);
  fail = false;
  await p.J().handleInput('what now');
  await p.tick(0);
  const nodes = p.J().data().nodes;
  t.check('again', count(p.fetchLog, 'graph-data.js') === 2 && nodes.length === 4 && nodes[3].label === 'T',
    `${count(p.fetchLog, 'graph-data.js')} reads, ${labels(p)}`);
});

story('the one door for new notes', {
  wrong: 'addNote never puts a note at a wrong index',
  next: 'addNote puts a new note at the next index',
}, async (t, open) => {
  const p = await open();
  const placed = p.J().addNote(receipt(7, 'Far away'), { quiet: true });
  t.check('wrong', placed === null && p.J().data().nodes.length === 3, labels(p));
  p.J().addNote(receipt(3, 'Next'), { quiet: true });
  const nodes = p.J().data().nodes;
  t.check('next', nodes.length === 4 && nodes[3].id === 3 && nodes[3].label === 'Next', labels(p));
});

story('forget', {
  base: 'a forget one step ahead takes the newest star away',
  noReplay: 'a forget is not replayed after a read: another tab\'s note at that index stays',
}, async (t, open) => {
  let p = await open({ handlers: {
    '/remember': () => rememberReply(6, 3, 'T'),
    '/forget': () => ({ ok: true, answer: 'Forgotten', nodes: [], graph: meta(7), action: { type: 'forget', index: 3, title: 'T' } }),
  } });
  await p.J().handleInput('remember that T');
  await p.tick(1000);
  await p.J().handleInput('forget that');
  await p.tick(0);
  t.check('base', p.J().data().nodes.length === 3, labels(p));
  p = await open({ handlers: {
    '/remember': () => rememberReply(6, 3, 'T'),
    '/forget': () => ({ ok: true, answer: 'Forgotten', nodes: [], graph: meta(9), action: { type: 'forget', index: 3, title: 'T' } }),
    'graph-data.js': () => graphFile(galaxy(9, ['A', 'B', 'C', 'Other tab'])),
  } });
  await p.J().handleInput('remember that T');
  await p.tick(1000);
  await p.J().handleInput('forget that');
  await p.tick(0);
  const nodes = p.J().data().nodes;
  t.check('noReplay', nodes.length === 4 && nodes[3].label === 'Other tab' && p.$('answer').textContent === 'Forgotten', labels(p));
});

story('a late reply (reply ownership)', {
  answer: 'a late answer does not replace the newer answer',
  focus: 'a late answer does not move the focus',
  note: 'the note of a late answer still reaches the galaxy',
  oneRead: 'a reply that comes during a read of the galaxy waits for it: one read only',
  quiet: 'a late capture one step ahead is added quietly at its index, and moves no focus',
}, async (t, open) => {
  const read = deferred();
  const p = await open({ handlers: {
    '/remember': () => rememberReply(7, 3, 'T'),
    'graph-data.js': () => read.promise,
    '/chat': () => chatReply('B answer', 7, [1]),
  } });
  const a = p.J().handleInput('remember that T');
  await p.tick(0);                                    // the capture's reply is in, its read is in the air
  const b = p.J().handleInput('tell me about B');
  await p.tick(0);
  read.resolve(graphFile(galaxy(7, ['A', 'B', 'C', 'T'])));
  await a; await b;
  await p.tick(2000);
  t.check('answer', p.$('answer').textContent === 'B answer', p.$('answer').textContent);
  t.check('focus', p.J().Focus.current().primary === 1, String(p.J().Focus.current().primary));
  t.check('note', p.J().data().nodes.length === 4 && p.J().data().nodes[3].label === 'T', labels(p));
  t.check('oneRead', count(p.fetchLog, 'graph-data.js') === 1, `${count(p.fetchLog, 'graph-data.js')} reads`);

  const read2 = deferred();
  const q = await open({ handlers: {
    '/remember': () => rememberReply(6, 3, 'T'),
    'graph-data.js': () => read2.promise,
    '/chat': () => chatReply('B answer', 6),
  } });
  const reload = q.J().reloadGraph();                 // a read in the air that the capture must wait for
  const a2 = q.J().handleInput('remember that T');
  await q.tick(0);
  const b2 = q.J().handleInput('hello');
  await q.tick(0);
  read2.resolve(graphFile(galaxy(5, ['A', 'B', 'C'])));
  await reload; await a2; await b2;
  await q.tick(2000);
  const nodes = q.J().data().nodes;
  t.check('quiet', nodes.length === 4 && nodes[3].id === 3 && nodes[3].label === 'T' && q.J().Focus.current().primary === null
    && q.$('answer').textContent === 'B answer', `${labels(q)} focus ${q.J().Focus.current().primary}`);
});

story('a late teardown start', {
  tracked: 'a late teardown start is tracked: its job is followed and polled',
  reader: 'a late teardown start leaves the open reader open',
  card: 'a late teardown start does not repaint the card, also while the job runs',
  quiet: 'a late teardown start says nothing; the newer answer stands',
  end: 'the end of a tracked job shows the card, adds its note and says the result',
  owner: 'a teardown start that owns the answer closes the reader and shows the steps',
}, async (t, open) => {
  const read = deferred();
  const chat = deferred();
  const oldCard = { name: 'n1', label: 'Old card', host: 'x', stack: [], totals: '', snapshot: '', seconds: 3 };
  const result = { births: [receipt(3, 'Teardown x')], spoken: 'Result line', seconds: 40, label: 'Teardown · x', host: 'x',
                   name: 'n2', stack: [], totals: '', snapshot: '' };
  let status = () => ({ ok: true, job: job('j9', 'running'), graph: meta(5) });
  const handlers = {
    '/teardown/last': () => ({ ok: true, card: oldCard, answer: 'last' }),
    '/teardown/note': () => ({ ok: true, label: 'Old card', text: 'body', obsidian: '' }),
    'graph-data.js': () => read.promise,
    '/teardown/start': () => ({ ok: true, answer: 'Started', nodes: [], graph: meta(5),
                                action: { type: 'teardown', job: job('j9', 'running') } }),
    '/chat': () => chat.promise,
    '/teardown/status': () => status(),
  };
  const p = await open({ handlers });
  const T = p.J().Teardown;
  await T.openReader();                               // the reader shows an older teardown
  await p.tick(0);
  const reload = p.J().reloadGraph();                 // a read in the air: the start's reply must wait
  const a = T.start({ site: 'stripe.com', question: 'How is it built?' });
  await p.tick(0);
  const b = p.J().handleInput('what is new');         // a newer sentence; its answer is not in yet
  await p.tick(0);
  read.resolve(graphFile(galaxy(5, ['A', 'B', 'C'])));
  await reload; await a; await p.tick(0);
  t.check('reader', p.$('reader').hidden === false && p.$('td-title').textContent === 'Old card');
  chat.resolve(chatReply('B answer', 5));
  await b;
  const before = count(p.fetchLog, '/teardown/status');
  await p.tick(800);
  t.check('tracked', T.current().following === 'j9' && count(p.fetchLog, '/teardown/status') === before + 1);
  t.check('card', p.$('td-title').textContent === 'Old card' && p.$('reader').hidden === false, p.$('td-title').textContent);
  t.check('quiet', p.$('answer').textContent === 'B answer', p.$('answer').textContent);
  status = () => ({ ok: true, job: job('j9', 'done', { result }), graph: meta(6) });
  await p.tick(2000);
  t.check('end', p.$('td-title').textContent === 'Teardown · x' && p.$('answer').textContent === 'Result line'
    && p.J().data().nodes.length === 4, `${p.$('td-title').textContent} / ${p.$('answer').textContent} / ${labels(p)}`);

  const q = await open({ handlers: Object.assign({}, handlers, { 'graph-data.js': () => graphFile(galaxy(5, ['A', 'B', 'C'])) }) });
  await q.J().Teardown.openReader();
  await q.tick(0);
  await q.J().Teardown.start({ site: 'stripe.com', question: 'How is it built?' });
  await q.tick(0);
  t.check('owner', q.$('reader').hidden === true && q.$('td-title').textContent === 'Teardown · x' && q.$('td-steps').hidden === false
    && q.$('answer').textContent === 'Started', `${q.$('td-title').textContent} / ${q.$('answer').textContent}`);
});

story('a late cancel', {
  quiet: 'a late cancel says nothing and changes no card',
  end: 'after a late cancel the job\'s end is quiet and the card says Cancelled.',
}, async (t, open) => {
  const read = deferred();
  const chat = deferred();
  let status = () => ({ ok: true, job: job('j9', 'running'), graph: meta(5) });
  const p = await open({ handlers: {
    'graph-data.js': () => read.promise,
    '/teardown/cancel': () => ({ ok: true, answer: 'Cancel line', nodes: [], graph: meta(5), action: { type: 'teardown_cancel' } }),
    '/chat': () => chat.promise,
    '/teardown/status': () => status(),
  } });
  const T = p.J().Teardown;
  T.follow(job('j9', 'running'));
  await p.tick(800);
  const lineBefore = p.$('td-line').textContent;
  const reload = p.J().reloadGraph();
  const c = T.cancel();
  await p.tick(0);
  const d = p.J().handleInput('and now');
  await p.tick(0);
  read.resolve(graphFile(galaxy(5, ['A', 'B', 'C'])));
  await reload; await c; await p.tick(0);
  t.check('quiet', p.$('answer').textContent !== 'Cancel line' && p.$('td-line').textContent === lineBefore, p.$('answer').textContent);
  chat.resolve(chatReply('C answer', 5));
  await d;
  status = () => ({ ok: true, job: job('j9', 'cancelled', { say: 'It was cancelled' }), graph: meta(5) });
  await p.tick(800);
  t.check('end', p.$('answer').textContent === 'C answer' && p.$('td-line').textContent === 'Cancelled.',
    `${p.$('answer').textContent} / ${p.$('td-line').textContent}`);
});

story('the failed line is said once', {
  reply: 'a cancel reply with error "failed" says the failed line',
  once: 'the job\'s end does not say the failed line a second time',
  card: 'that failed job still paints its card and reads the galaxy again',
  next: 'the next job\'s failure is said again',
  stopping: 'after a "stopping" reply an end as failed is said',
  cancelled: 'after a "cancelled" reply an end as cancelled is quiet',
}, async (t, open) => {
  let cancelReply = { ok: true, answer: 'Failed line', nodes: [], graph: meta(5), error: 'failed' };
  let end = job('j1', 'failed', { say: 'Failed line' });
  const p = await open({ handlers: {
    '/teardown/cancel': () => cancelReply,
    '/teardown/status': () => ({ ok: true, job: end, graph: meta(6) }),
    'graph-data.js': () => graphFile(galaxy(6, ['A', 'B', 'C', 'Left over'])),
  } });
  const T = p.J().Teardown;
  const run = async (id, cancel) => {
    T.follow(job(id, 'running'));
    if (cancel) { await T.cancel(); await p.tick(0); }
    const said = p.$('answer').textContent;
    p.$('answer').textContent = 'sentinel';            // what is on screen before the job's end
    await p.tick(800);
    return said;
  };
  const said = await run('j1', true);
  t.check('reply', said === 'Failed line', said);
  t.check('once', p.$('answer').textContent === 'sentinel', p.$('answer').textContent);
  t.check('card', p.$('td-line').textContent === 'It stopped at this step.' && count(p.fetchLog, 'graph-data.js') === 1
    && p.J().data().nodes.length === 4, labels(p));
  end = job('j2', 'failed', { say: 'Failed again' });
  await run('j2', false);
  t.check('next', p.$('answer').textContent === 'Failed again', p.$('answer').textContent);
  cancelReply = { ok: true, answer: 'Stopping', nodes: [], graph: meta(6), action: { type: 'teardown_cancel' } };
  end = job('j3', 'failed', { say: 'Failed after stopping' });
  await run('j3', true);
  t.check('stopping', p.$('answer').textContent === 'Failed after stopping', p.$('answer').textContent);
  end = job('j4', 'cancelled', { say: 'Cancelled line' });
  await run('j4', true);
  t.check('cancelled', p.$('answer').textContent === 'sentinel', p.$('answer').textContent);
});

story('the start-up compare with /status', {
  read: 'at start-up, other numbers in /status read the galaxy again',
  greet: 'at start-up the greeting and the brain chip work as before',
  same: 'at start-up the same numbers need no read',
  failed: 'at start-up a failed read changes nothing, and the greeting stays',
  later: 'a later /status ("which brain") is compared too',
}, async (t, open) => {
  const status = graph => () => ({ ok: true, active: { label: 'Claude', model: 'm', has_key: true }, lines: {}, say: 'Brain line', graph });
  let p = await open({ handlers: {
    '/status': status(meta(0, 'b2')),
    'graph-data.js': () => graphFile(galaxy(0, ['W', 'X', 'Y', 'Z'], 'b2')),
  } });
  await p.tick(0);
  t.check('read', count(p.fetchLog, 'graph-data.js') === 1 && labels(p) === 'W,X,Y,Z', labels(p));
  t.check('greet', p.$('answer').textContent === 'Hello.' && p.$('brain').hidden === false
    && p.$('brain').textContent === 'brain: Claude · m', `${p.$('answer').textContent} / ${p.$('brain').textContent}`);
  p = await open({ handlers: { '/status': status(meta(5)), 'graph-data.js': () => graphFile(galaxy(5, ['A', 'B', 'C'])) } });
  t.check('same', count(p.fetchLog, 'graph-data.js') === 0);
  p = await open({ handlers: { '/status': status(meta(0, 'b2')), 'graph-data.js': () => { throw new Error('offline'); } } });
  await p.tick(0);
  t.check('failed', p.J().data().nodes.length === 3 && p.$('answer').textContent === 'Hello.', `${labels(p)} / ${p.$('answer').textContent}`);
  let now = meta(5);
  p = await open({ handlers: { '/status': () => status(now)(), 'graph-data.js': () => graphFile(galaxy(6, ['A', 'B', 'C', 'D'])) } });
  now = meta(6);
  await p.J().handleInput('which brain answers');
  await p.tick(0);
  t.check('later', count(p.fetchLog, 'graph-data.js') === 1 && p.J().data().nodes.length === 4 && p.$('answer').textContent === 'Brain line',
    `${count(p.fetchLog, 'graph-data.js')} reads / ${p.$('answer').textContent}`);
});

story('no node numbers on a galaxy that could not be read', {
  still: 'a chat reply with notes after a failed read moves no camera and lights no star',
  answer: 'that reply still shows its answer',
  again: 'the next reply reads again, and then lights its stars in the new galaxy',
  startup: 'after a failed read at start-up, a reply whose read fails too lights nothing',
  startupThen: 'after a failed read at start-up, a reply whose read works lights its stars in the new galaxy',
}, async (t, open) => {
  let fail = true;
  const reads = () => { if (fail) throw new Error('offline'); return graphFile(galaxy(9, ['W', 'X', 'Y', 'Z'], 'b2')); };
  let answer = chatReply('About B', 9, [1]);
  answer.graph = meta(9, 'b2');                       // the server's galaxy is another build now
  const p = await open({ handlers: { '/chat': () => answer, 'graph-data.js': reads } });
  await p.J().handleInput('tell me about B');
  await p.tick(2500);
  const focus = p.J().Focus.current();
  const moved = p.graphLog.some(([name, args]) => name === 'cameraPosition' && args.length > 1);
  t.check('still', focus.primary === null && focus.lit.length === 0 && p.$('panel').hidden === true && !moved,
    `primary ${focus.primary}, lit ${focus.lit}`);
  t.check('answer', p.$('answer').textContent === 'About B');
  fail = false;
  answer = Object.assign(chatReply('About Y', 9, [2]), { graph: meta(9, 'b2') });
  await p.J().handleInput('tell me about Y');
  await p.tick(0);
  t.check('again', count(p.fetchLog, 'graph-data.js') === 2 && p.J().Focus.current().primary === 2
    && p.$('panel-title').textContent === 'Y', `${count(p.fetchLog, 'graph-data.js')} reads, ${p.$('panel-title').textContent}`);

  fail = true;
  answer = Object.assign(chatReply('About Z', 0, [2]), { graph: meta(9, 'b2') });     // 2 is "C" in the old galaxy
  const status = () => ({ ok: true, active: { label: 'Claude', model: 'm', has_key: true }, lines: {}, graph: meta(9, 'b2') });
  const q = await open({ handlers: { '/status': status, '/chat': () => answer, 'graph-data.js': reads } });
  await q.tick(0);                                    // the start-up read failed: the page is as it was
  await q.J().handleInput('tell me about Z');
  await q.tick(0);
  t.check('startup', q.J().Focus.current().primary === null && labels(q) === 'A,B,C' && q.$('answer').textContent === 'About Z',
    `${q.J().Focus.current().primary} / ${labels(q)}`);
  fail = false;
  answer = Object.assign(chatReply('About Z', 0, [3]), { graph: meta(9, 'b2') });
  await q.J().handleInput('tell me about Z');
  await q.tick(0);
  t.check('startupThen', q.J().Focus.current().primary === 3 && q.$('panel-title').textContent === 'Z',
    `${q.J().Focus.current().primary} / ${q.$('panel-title').textContent}`);
});

story('an index used again inside one build', {
  reuse: 'a reply whose read finds the same build moved on (an index used again) shows its answer and lights no star',
  inStep: 'the next reply after that is in step again and lights its stars',
  capture: 'a capture whose read finds the galaxy moved on past its reply shows its answer and focuses nothing',
}, async (t, open) => {
  let chat = () => chatReply('About Note A', 6, [3]);
  // On the page: rev 5 with "Note A" at 3. The reply was made at rev 6. Before the read, another tab forgot
  // "Note A" and saved "Note B" at the same index: the file is at rev 8.
  let p = await open({ graph: galaxy(5, ['A', 'B', 'C', 'Note A']), handlers: {
    '/chat': () => chat(),
    'graph-data.js': () => graphFile(galaxy(8, ['A', 'B', 'C', 'Note B'])),
  } });
  await p.J().handleInput('tell me about note A');
  await p.tick(2500);
  const focus = p.J().Focus.current();
  t.check('reuse', focus.primary === null && focus.lit.length === 0 && p.$('panel').hidden === true
    && p.$('answer').textContent === 'About Note A' && labels(p) === 'A,B,C,Note B', `primary ${focus.primary} / ${labels(p)}`);
  chat = () => chatReply('About Note B', 8, [3]);
  await p.J().handleInput('tell me about note B');
  await p.tick(0);
  t.check('inStep', p.J().Focus.current().primary === 3 && p.$('panel-title').textContent === 'Note B'
    && count(p.fetchLog, 'graph-data.js') === 1, `primary ${p.J().Focus.current().primary}, ${count(p.fetchLog, 'graph-data.js')} reads`);

  p = await open({ handlers: {
    '/remember': () => rememberReply(7, 3, 'T'),
    'graph-data.js': () => graphFile(galaxy(9, ['A', 'B', 'C', 'T', 'Other tab'])),     // moved on past the reply
  } });
  await p.J().handleInput('remember that T');
  await p.tick(1000);
  t.check('capture', p.J().Focus.current().primary === null && p.$('answer').textContent === 'Saved T' && labels(p) === 'A,B,C,T,Other tab',
    `primary ${p.J().Focus.current().primary} / ${labels(p)}`);
});

story('node numbers belong to one build of the galaxy', {
  rebuilt: 'a reply about a galaxy that was built again before the read shows its answer and lights no star',
  sameBuild: 'a reply whose read lands on its own snapshot (same build, same rev) lights its stars',
  otherBase: 'a capture whose receipt names another build adds and focuses nothing, and shows its answer',
  inStep: 'after such a capture the page reads the file, so the next reply lights its stars again',
  sameLabel: 'a receipt from another build does not focus a note with the same index and name',
  teardown: 'a teardown whose notes come from another build focuses nothing; its card and result still show',
}, async (t, open) => {
  let p = await open({ handlers: {
    '/chat': () => chatReply('About B', 6, [1]),
    'graph-data.js': () => graphFile(galaxy(0, ['P', 'Q', 'R'], 'b3')),        // built again meanwhile
  } });
  await p.J().handleInput('tell me about B');
  await p.tick(2500);
  let focus = p.J().Focus.current();
  t.check('rebuilt', focus.primary === null && focus.lit.length === 0 && p.$('panel').hidden === true
    && p.$('answer').textContent === 'About B' && labels(p) === 'P,Q,R', `primary ${focus.primary} / ${labels(p)}`);

  p = await open({ handlers: {
    '/chat': () => chatReply('About B', 7, [1]),                                  // the page is at rev 5: it reads
    'graph-data.js': () => graphFile(galaxy(7, ['A', 'B', 'C', 'D'])),          // ...and finds rev 7, the reply's own
  } });
  await p.J().handleInput('tell me about B');
  await p.tick(0);
  t.check('sameBuild', p.J().Focus.current().primary === 1 && p.$('panel-title').textContent === 'B', String(p.J().Focus.current().primary));

  let chat = () => chatReply('About C', 6, [2]);
  p = await open({ handlers: {
    '/remember': () => rememberReply(6, 3, 'T', 'b0'),                          // one step ahead, but from build b0
    'graph-data.js': () => graphFile(galaxy(6, ['A', 'B', 'C'])),
    '/chat': () => chat(),
  } });
  await p.J().handleInput('remember that T');
  await p.tick(1000);
  t.check('otherBase', p.J().data().nodes.length === 3 && p.J().Focus.current().primary === null && p.$('answer').textContent === 'Saved T',
    `${labels(p)} / ${p.J().Focus.current().primary}`);
  await p.J().handleInput('tell me about C');
  await p.tick(0);
  t.check('inStep', count(p.fetchLog, 'graph-data.js') === 1 && p.J().Focus.current().primary === 2,
    `${count(p.fetchLog, 'graph-data.js')} reads, primary ${p.J().Focus.current().primary}`);

  p = await open({ handlers: {
    '/remember': () => Object.assign(rememberReply(0, 3, 'T', 'b1'), { graph: meta(0, 'b2') }),
    'graph-data.js': () => graphFile(galaxy(0, ['A', 'B', 'C', 'T'], 'b2')),   // another build, T at the same place
  } });
  await p.J().handleInput('remember that T');
  await p.tick(1000);
  t.check('sameLabel', p.J().Focus.current().primary === null && labels(p) === 'A,B,C,T' && p.$('answer').textContent === 'Saved T',
    `primary ${p.J().Focus.current().primary}`);

  const result = { births: [receipt(3, 'Teardown x', 'b1')], spoken: 'Done', seconds: 40, label: 'Teardown · x', host: 'x', name: 'n',
                   stack: [], totals: '', snapshot: '' };
  p = await open({ handlers: {
    '/teardown/status': () => ({ ok: true, job: job('j1', 'done', { result }), graph: meta(0, 'b2') }),
    'graph-data.js': () => graphFile(galaxy(0, ['A', 'B', 'C', 'Teardown x'], 'b2')),
  } });
  p.J().Teardown.follow(job('j1', 'running'));
  await p.tick(2000);
  focus = p.J().Focus.current();
  t.check('teardown', focus.primary === null && p.$('td-title').textContent === 'Teardown · x' && p.$('answer').textContent === 'Done',
    `primary ${focus.primary} / ${p.$('td-title').textContent} / ${p.$('answer').textContent}`);
});

story('forget names the galaxy of its capture', {
  build: 'forget sends the build from the capture\'s receipt, with index, label and session',
  receipt: 'forget sends the receipt\'s build even when the reply\'s graph numbers name another build',
  refused: 'a refused forget shows the server\'s line',
  none: 'a receipt with no build (an old server) sends no build field, and the server\'s refusal is shown',
  born: 'forget sends the receipt\'s born (that one creation of the note) next to its build',
  noBorn: 'a receipt with no born sends no born field, and the server\'s refusal is shown',
  late: 'a capture from a late reply keeps its receipt\'s build and born',
}, async (t, open) => {
  let remembered = rememberReply(6, 3, 'T');
  let forgetAnswer = () => ({ ok: false, answer: 'Refused line', nodes: [], graph: meta(6), error: 'forget_failed' });
  const p = await open({ handlers: {
    '/remember': () => remembered,
    '/forget': () => forgetAnswer(),
    'graph-data.js': () => graphFile(galaxy(0, ['A', 'B', 'C', 'T'], 'b9')),
  } });
  const forgets = () => p.fetchLog.filter(entry => entry.key === '/forget').map(entry => entry.body);
  await p.J().handleInput('remember that T');
  await p.tick(1000);
  await p.J().handleInput('forget that');
  await p.tick(0);
  const sent = forgets();
  t.check('build', sent.length === 1 && sent[0].build === 'b1' && sent[0].index === 3 && sent[0].label === 'T'
    && typeof sent[0].session === 'string', JSON.stringify(sent));
  t.check('refused', p.$('answer').textContent === 'Refused line' && p.$('answer').classList.contains('trouble'));
  remembered = Object.assign(rememberReply(7, 4, 'U', 'r2'), { graph: meta(0, 'b9') });   // the reply names another build
  await p.J().handleInput('remember that U');
  await p.tick(1000);
  await p.J().handleInput('forget that');
  await p.tick(0);
  t.check('receipt', forgets().length === 2 && forgets()[1].build === 'r2', JSON.stringify(forgets()[1]));
  remembered = rememberReply(1, 4, 'V', undefined);                              // an old server: no build
  delete remembered.action.receipt.build;
  forgetAnswer = () => ({ ok: false, answer: 'No build line', nodes: [], graph: meta(1, 'b9'), error: 'forget_failed' });
  p.handlers['graph-data.js'] = () => graphFile(galaxy(1, ['A', 'B', 'C', 'T', 'V'], 'b9'));
  await p.J().handleInput('remember that V');
  await p.tick(1000);
  await p.J().handleInput('forget that');
  await p.tick(0);
  const third = forgets()[2];
  t.check('none', Boolean(third) && !('build' in third) && third.index === 4 && p.$('answer').textContent === 'No build line',
    `${JSON.stringify(third)} / ${p.$('answer').textContent}`);
  t.check('born', sent[0].born === 'born-T' && forgets()[1].born === 'born-U' && third.born === 'born-V',
    JSON.stringify(forgets().map(body => body.born)));
  remembered = rememberReply(2, 5, 'W', 'b9');                                 // a receipt without its born
  delete remembered.action.receipt.born;
  forgetAnswer = () => ({ ok: false, answer: 'No born line', nodes: [], graph: meta(1, 'b9'), error: 'forget_failed' });
  await p.J().handleInput('remember that W');
  await p.tick(1000);
  await p.J().handleInput('forget that');
  await p.tick(0);
  const fourth = forgets()[3];
  t.check('noBorn', Boolean(fourth) && !('born' in fourth) && fourth.build === 'b9' && fourth.index === 5
    && p.$('answer').textContent === 'No born line', `${JSON.stringify(fourth)} / ${p.$('answer').textContent}`);

  const read = deferred();
  const chat = deferred();
  const q = await open({ handlers: {
    '/remember': () => rememberReply(7, 3, 'T'),
    'graph-data.js': () => read.promise,
    '/chat': () => chat.promise,
    '/forget': () => ({ ok: true, answer: 'Forgotten', nodes: [], graph: meta(8), action: { type: 'forget', index: 3, title: 'T' } }),
  } });
  const a = q.J().handleInput('remember that T');
  await q.tick(0);
  const b = q.J().handleInput('something else');
  await q.tick(0);
  read.resolve(graphFile(galaxy(7, ['A', 'B', 'C', 'T'])));
  await a;
  chat.resolve(chatReply('B answer', 7));
  await b;
  await q.J().handleInput('forget that');
  await q.tick(0);
  const late = q.fetchLog.filter(entry => entry.key === '/forget').map(entry => entry.body)[0];
  t.check('late', Boolean(late) && late.build === 'b1' && late.born === 'born-T' && late.index === 3, JSON.stringify(late));
});

story('a teardown that failed', {
  read: 'a failed job whose numbers moved reads the galaxy again',
  message: 'a failed job keeps its failure message on screen and on the card',
  noReplay: 'the receipts of a failed job are not replayed as new notes',
  same: 'a failed job with the same numbers needs no read',
}, async (t, open) => {
  const p = await open({ handlers: {
    '/teardown/status': () => ({ ok: true, job: job('j1', 'failed', { say: 'It failed', births: [receipt(3, 'Jarviz')] }), graph: meta(6) }),
    'graph-data.js': () => graphFile(galaxy(6, ['A', 'B', 'C', 'Jarviz'])),
  } });
  p.J().Teardown.follow(job('j1', 'running'));
  await p.tick(800);
  t.check('read', count(p.fetchLog, 'graph-data.js') === 1 && labels(p) === 'A,B,C,Jarviz', labels(p));
  t.check('message', p.$('answer').textContent === 'It failed' && p.$('td-line').textContent === 'It stopped at this step.');
  t.check('noReplay', p.J().Focus.current().primary === null && p.J().data().nodes.length === 4);
  const q = await open({ handlers: {
    '/teardown/status': () => ({ ok: true, job: job('j1', 'failed', { say: 'It failed', births: [] }), graph: meta(5) }),
  } });
  q.J().Teardown.follow(job('j1', 'running'));
  await q.tick(800);
  t.check('same', count(q.fetchLog, 'graph-data.js') === 0);
});

story('a teardown that finished', {
  births: 'a finished teardown adds its notes in order, ids equal indexes',
  focus: 'a finished teardown focuses its note',
}, async (t, open) => {
  const births = [receipt(3, 'Jarviz'), receipt(4, 'REA'), receipt(5, 'Teardown x')];
  const result = { births, spoken: 'Done', seconds: 40, label: 'Teardown · x', host: 'x', name: 'n', stack: [], totals: '', snapshot: '' };
  const p = await open({ handlers: { '/teardown/status': () => ({ ok: true, job: job('j1', 'done', { result }), graph: meta(8) }) } });
  p.J().Teardown.follow(job('j1', 'running'));
  await p.tick(2000);
  const nodes = p.J().data().nodes;
  t.check('births', nodes.length === 6 && nodes.every((node, index) => node.id === index) && nodes[5].label === 'Teardown x', labels(p));
  t.check('focus', p.J().Focus.current().primary === 5);
});

story('the reader that was closed', {
  cardShut: 'a reader closed before the card came keeps the card shut and asks for no note',
  readerShut: 'a reader closed (by voice) before the note came stays shut',
  later: 'a later open of the reader still works',
  lateError: 'a late error of a closed reader says nothing',
}, async (t, open) => {
  const last = deferred();
  const note = deferred();
  let lastAnswer = () => last.promise;
  let noteAnswer = () => note.promise;
  const card = { name: 'n1', label: 'L', host: 'x', stack: [], totals: '', snapshot: '', seconds: 3 };
  const p = await open({ handlers: { '/teardown/last': () => lastAnswer(), '/teardown/note': () => noteAnswer() } });
  const T = p.J().Teardown;
  const one = T.openReader();
  await p.tick(0);
  p.$('td-close').listeners.click[0]();               // the user closes the card first
  last.resolve({ ok: true, card, answer: 'last' });
  await one; await p.tick(0);
  t.check('cardShut', p.$('teardown').hidden === true && p.$('reader').hidden === true && count(p.fetchLog, '/teardown/note') === 0);
  lastAnswer = () => ({ ok: true, card, answer: 'last' });
  const two = T.openReader();
  await p.tick(0);
  T.close();                                          // "close the teardown" while the note is on its way
  note.resolve({ ok: true, label: 'L', text: '# L\nbody', obsidian: '' });
  await two; await p.tick(0);
  t.check('readerShut', p.$('reader').hidden === true);
  noteAnswer = () => ({ ok: true, label: 'L', text: '# L\nbody', obsidian: '' });
  await T.openReader();
  await p.tick(0);
  t.check('later', p.$('reader').hidden === false && p.$('reader-title').textContent === 'L');
  const bad = deferred();
  noteAnswer = () => bad.promise;
  T.closeReader();
  const three = T.openReader();
  await p.tick(0);
  T.closeReader();
  bad.reject(new Error('offline'));
  await three; await p.tick(0);
  t.check('lateError', p.$('answer').textContent !== 'I cannot reach the server. Is server.py still running?', p.$('answer').textContent);
});

story('the poll round', {
  dropped: 'a status answer for an older follow is dropped: the newer job stays followed',
  oneLoop: 'following a new job while a status request is in the air leaves one poll loop',
  errorDropped: 'a dropped status error starts no second poll loop',
}, async (t, open) => {
  const first = deferred();
  let answer = () => first.promise;
  const p = await open({ handlers: { '/teardown/status': () => answer() } });
  const T = p.J().Teardown;
  T.follow(job('A', 'running'));
  await p.tick(750);                                  // A's request is in the air
  T.follow(job('B', 'running'));
  answer = () => ({ ok: true, job: job('B', 'running'), graph: meta(5) });
  first.resolve({ ok: true, job: null, graph: meta(5) });     // no job: would stop the watch if it were taken
  await p.tick(0);
  t.check('dropped', T.current().following === 'B', String(T.current().following));
  const before = count(p.fetchLog, '/teardown/status');
  await p.tick(2100);
  const polls = count(p.fetchLog, '/teardown/status') - before;
  t.check('oneLoop', polls === 3, `${polls} polls in 2100 ms`);

  const second = deferred();
  answer = () => second.promise;
  const q = await open({ handlers: { '/teardown/status': () => answer() } });
  q.J().Teardown.follow(job('A', 'running'));
  await q.tick(750);
  q.J().Teardown.follow(job('B', 'running'));
  answer = () => ({ ok: true, job: job('B', 'running'), graph: meta(5) });
  second.reject(new Error('offline'));
  await q.tick(0);
  const start = count(q.fetchLog, '/teardown/status');
  await q.tick(2100);
  const loops = count(q.fetchLog, '/teardown/status') - start;
  t.check('errorDropped', loops === 3, `${loops} polls in 2100 ms`);
});

story('switching the brain by voice', {
  short: 'a short "switch to ..." with a new brain name (Codex, Antigravity, agy) gives the name as it was said',
  old: 'the names that worked before give the same name as before ("switch to open ai" stays "open ai")',
  brainWord: 'with the word "brain" any name is taken ("switch brain to codex", "use the antigravity brain")',
  noSwitch: 'a sentence that only mentions a brain name is no switch ("switch to codex mode on my editor", "what is codex")',
  sent: 'the page sends the said name to /brain, and a sentence that is no switch goes to the brain (/chat)',
}, async (t, open) => {
  const p = await open({ handlers: {
    '/brain': () => ({ ok: true, answer: 'Switched', nodes: [], graph: meta(5) }),
    '/chat': () => chatReply('chat', 5),
  } });
  const match = p.J().Core.matchBrainSwitch;
  const wrong = cases => cases.filter(([sentence, name]) => match(sentence) !== name)       // '' when every case holds
    .map(([sentence, name]) => `"${sentence}" gave ${JSON.stringify(match(sentence))}, not ${JSON.stringify(name)}`).join('; ');
  const short = wrong([
    ['switch to codex', 'codex'], ['switch to the codex cli', 'codex cli'], ['change to antigravity', 'antigravity'],
    ['swap to anti gravity', 'anti gravity'], ['switch to agy', 'agy'], ['switch to antigravity cli', 'antigravity cli'],
    ['switch to openai codex', 'openai codex'], ['Jarvis, switch to Codex.', 'Codex'],
  ]);
  t.check('short', !short, short);
  const old = wrong([
    ['switch to claude', 'claude'], ['switch to anthropic', 'anthropic'], ['switch to open ai', 'open ai'],
    ['switch to openai', 'openai'], ['change to chat gpt', 'chat gpt'], ['swap to gpt', 'gpt'], ['switch to deep seek', 'deep seek'],
    ['switch to the venice', 'venice'], ['switch to open router', 'open router'], ['Switch brain to Deep Seek.', 'Deep Seek'],
    ['Jarvis, use the OpenAI brain', 'OpenAI'], ['switch to decaf', null], ['How does a brain switch work?', null],
  ]);
  t.check('old', !old, old);
  const brainWord = wrong([
    ['switch brain to codex', 'codex'], ['use the antigravity brain', 'antigravity'], ['switch to the codex cli brain', 'codex cli'],
  ]);
  t.check('brainWord', !brainWord, brainWord);
  const noSwitch = wrong([
    ['switch to codex mode on my editor', null], ['what is codex', null], ['switch to agy now and then', null],
  ]);
  t.check('noSwitch', !noSwitch, noSwitch);
  await p.J().handleInput('switch to the codex cli');
  await p.tick(0);
  await p.J().handleInput('switch to codex mode on my editor');
  await p.tick(0);
  await p.J().handleInput('what is codex');
  await p.tick(0);
  const names = p.fetchLog.filter(entry => entry.key === '/brain').map(entry => entry.body.name);
  t.check('sent', names.length === 1 && names[0] === 'codex cli' && count(p.fetchLog, '/chat') === 2
    && p.$('answer').textContent === 'chat', `${JSON.stringify(names)}, ${count(p.fetchLog, '/chat')} chats`);
});

story('a sentence that refuses a teardown', {
  refused: 'a refusal with a teardown word anywhere in the sentence starts no teardown ("How does example.com work? Do not tear it down.")',
  asks: 'the question without the refusal still starts one, and "don\'t wait" or "don\'t forget" is no refusal',
  brain: 'the refused sentence goes to the brain as a question',
}, async (t, open) => {
  const p = await open({ handlers: {
    '/chat': () => chatReply('A question', 5),
    '/teardown/start': () => ({ ok: true, answer: 'Started', nodes: [], graph: meta(5), action: { type: 'teardown', job: job('j1', 'running') } }),
  } });
  const site = sentence => { const hit = p.J().Core.matchTeardown(sentence); return hit ? hit.site : null; };
  const wrong = cases => cases.filter(([sentence, wanted]) => site(sentence) !== wanted)
    .map(([sentence, wanted]) => `"${sentence}" gave ${site(sentence)}, not ${wanted}`).join('; ');
  const refused = wrong([
    ['How does example.com work? Do not tear it down.', null], ['What is example.com built with? Never reverse engineer it.', null],
    ["How does example.com work? I can't have you taking it apart.", null], ['How does example.com work? No teardown, please.', null],
  ]);
  t.check('refused', !refused, refused);
  const asks = wrong([
    ['How does example.com work?', 'example.com'], ["Tear down stripe.com, don't wait for me", 'stripe.com'],
    ["Tear down stripe.com and don't forget the pricing page", 'stripe.com'],
  ]);
  t.check('asks', !asks, asks);
  await p.J().handleInput('How does example.com work? Do not tear it down.');
  await p.tick(0);
  t.check('brain', count(p.fetchLog, '/teardown/start') === 0 && count(p.fetchLog, '/chat') === 1 && p.$('answer').textContent === 'A question',
    `${count(p.fetchLog, '/teardown/start')} starts, ${count(p.fetchLog, '/chat')} chats`);
});

story('two replies that wait on one read of the galaxy', {
  both: 'two capture replies that wait on one read of the galaxy both reach it (a numbers step that is not installed reads the file)',
  numbers: 'after them the numbers say what is on screen: the next reply at their rev lights its note with no new read',
  forget: 'a forget and a capture that wait on one read leave the galaxy as the file says, not the forgotten note',
}, async (t, open) => {
  const read = deferred();
  let file = () => read.promise;
  let saves = 0;
  const p = await open({ handlers: {
    '/remember': () => (++saves === 1 ? rememberReply(6, 3, 'T1') : rememberReply(7, 4, 'T2')),
    'graph-data.js': () => file(),
    '/chat': () => chatReply('About T2', 7, [4]),
  } });
  const reload = p.J().reloadGraph();                 // a read in the air: both replies wait for it
  const a = p.J().handleInput('remember that T1');
  await p.tick(0);
  const b = p.J().handleInput('remember that T2');
  await p.tick(0);
  file = () => graphFile(galaxy(7, ['A', 'B', 'C', 'T1', 'T2']));
  read.resolve(graphFile(galaxy(5, ['A', 'B', 'C'])));
  await reload; await a; await b;
  await p.tick(1000);
  t.check('both', labels(p) === 'A,B,C,T1,T2', labels(p));
  const reads = count(p.fetchLog, 'graph-data.js');
  await p.J().handleInput('tell me about T2');
  await p.tick(0);
  t.check('numbers', count(p.fetchLog, 'graph-data.js') === reads && p.J().Focus.current().primary === 4
    && p.$('panel-title').textContent === 'T2', `primary ${p.J().Focus.current().primary}, ${count(p.fetchLog, 'graph-data.js') - reads} more reads`);

  const read2 = deferred();
  let file2 = () => graphFile(galaxy(6, ['A', 'B', 'C', 'T']));
  let saves2 = 0;
  const q = await open({ handlers: {
    '/remember': () => (++saves2 === 1 ? rememberReply(6, 3, 'T') : rememberReply(8, 3, 'U')),
    '/forget': () => ({ ok: true, answer: 'Forgotten', nodes: [], graph: meta(7), action: { type: 'forget', index: 3, title: 'T' } }),
    'graph-data.js': () => file2(),
  } });
  await q.J().handleInput('remember that T');
  await q.tick(1000);
  file2 = () => read2.promise;
  const reload2 = q.J().reloadGraph();
  const f = q.J().handleInput('forget that');
  await q.tick(0);
  const u = q.J().handleInput('remember that U');
  await q.tick(0);
  file2 = () => graphFile(galaxy(8, ['A', 'B', 'C', 'U']));
  read2.resolve(graphFile(galaxy(6, ['A', 'B', 'C', 'T'])));
  await reload2; await f; await u;
  await q.tick(1000);
  t.check('forget', labels(q) === 'A,B,C,U', labels(q));
});

story('a teardown that ends after another tab saved a note', {
  both: 'the end of a teardown after another tab saved a note shows the teardown\'s notes and the other note',
  focus: 'the teardown\'s own note is found in the file that was read, and focused',
}, async (t, open) => {
  const births = [receipt(3, 'Jarviz'), receipt(4, 'REA'), receipt(5, 'Teardown x')];
  const result = { births, spoken: 'Done', seconds: 40, label: 'Teardown · x', host: 'x', name: 'n', stack: [], totals: '', snapshot: '' };
  let status = () => ({ ok: true, job: job('j1', 'running'), graph: meta(6) });
  let file = () => graphFile(galaxy(6, ['A', 'B', 'C', 'Jarviz']));
  const p = await open({ handlers: {
    '/teardown/status': () => status(),
    'graph-data.js': () => file(),
    '/chat': () => chatReply('chat', 6),
  } });
  p.J().Teardown.follow(job('j1', 'running'));
  await p.tick(800);
  await p.J().handleInput('what now');                // other numbers: the page reads the file at rev 6, Jarviz in it
  await p.tick(0);
  // REA (rev 7) and the teardown note (rev 8) are born, then another tab saves Y (rev 9). 6 + 3 births is 9 too.
  file = () => graphFile(galaxy(9, ['A', 'B', 'C', 'Jarviz', 'REA', 'Teardown x', 'Y']));
  status = () => ({ ok: true, job: job('j1', 'done', { result }), graph: meta(9) });
  await p.tick(2000);
  t.check('both', labels(p) === 'A,B,C,Jarviz,REA,Teardown x,Y', labels(p));
  t.check('focus', p.J().Focus.current().primary === 5 && p.$('answer').textContent === 'Done',
    `primary ${p.J().Focus.current().primary} / ${p.$('answer').textContent}`);
});

story('a late start-up answer about a running teardown', {
  newer: 'a start-up status answer that comes after a newer follow() does not take the watch',
  end: 'the newer job is still followed to its end',
}, async (t, open) => {
  const first = deferred();
  let calls = 0;
  let status = () => ({ ok: true, job: job('B', 'running', { label: 'Teardown · B' }), graph: meta(5) });
  const p = await open({ handlers: { '/teardown/status': () => (++calls === 1 ? first.promise : status()) } });
  const T = p.J().Teardown;
  T.follow(job('B', 'running', { label: 'Teardown · B' }));      // a new job, while the start-up answer is on its way
  first.resolve({ ok: true, job: job('A', 'running'), graph: meta(5) });
  await p.tick(0);
  t.check('newer', T.current().following === 'B', String(T.current().following));
  const result = { births: [], spoken: 'B done', seconds: 40, label: 'Teardown · B', host: 'b', name: 'nb', stack: [], totals: '', snapshot: '' };
  status = () => ({ ok: true, job: job('B', 'done', { result }), graph: meta(5) });
  await p.tick(2000);
  t.check('end', p.$('answer').textContent === 'B done' && p.$('td-title').textContent === 'Teardown · B',
    `${p.$('answer').textContent} / ${p.$('td-title').textContent}`);
});

story('a capture whose galaxy is built again before its focus', {
  still: 'the focus timer of a capture opens no note when the galaxy was built again meanwhile',
}, async (t, open) => {
  let now = meta(5);
  const p = await open({ handlers: {
    '/status': () => ({ ok: true, active: { label: 'Claude', model: 'm', has_key: true }, lines: {}, say: 'Brain line', graph: now }),
    '/remember': () => rememberReply(6, 3, 'T'),
    'graph-data.js': () => graphFile(galaxy(0, ['P', 'Q', 'R', 'S'], 'b2')),
  } });
  await p.J().handleInput('remember that T');
  await p.tick(0);                                    // T is born; its focus waits a moment
  now = meta(0, 'b2');
  await p.J().handleInput('which brain answers');     // the status names another build: the file is read again
  await p.tick(1000);
  const focus = p.J().Focus.current();
  t.check('still', labels(p) === 'P,Q,R,S' && focus.primary === null && p.$('panel').hidden === true,
    `${labels(p)} / primary ${focus.primary} / panel ${p.$('panel-title').textContent}`);
});

story('a capture focus that waits gives way to a newer focus', {
  answer: 'a newer answer that lights another note before the capture\'s focus timer fires keeps its focus',
  click: 'a click on another star before that timer fires keeps that star in focus',
}, async (t, open) => {
  const p = await open({ handlers: {
    '/remember': () => rememberReply(6, 3, 'T'),
    '/chat': () => chatReply('About B', 6, [1]),       // the same galaxy: the capture's numbers are still on screen
  } });
  await p.J().handleInput('remember that T');
  await p.tick(0);                                    // T is born; its focus waits a moment
  await p.J().handleInput('tell me about B');
  await p.tick(1000);
  t.check('answer', p.J().Focus.current().primary === 1 && p.$('panel-title').textContent === 'B' && p.$('answer').textContent === 'About B',
    `primary ${p.J().Focus.current().primary} / ${p.$('panel-title').textContent}`);

  const q = await open({ handlers: { '/remember': () => rememberReply(6, 3, 'T') } });
  await q.J().handleInput('remember that T');
  await q.tick(0);
  const clickStar = q.graphLog.find(([name]) => name === 'onNodeClick')[1][0];
  clickStar(q.J().data().nodes[2]);                   // the user picks C while T waits
  await q.tick(1000);
  t.check('click', q.J().Focus.current().primary === 2 && q.$('panel-title').textContent === 'C',
    `primary ${q.J().Focus.current().primary} / ${q.$('panel-title').textContent}`);
});

story('a cancel answer names its job', {
  lateQuiet: 'a cancel answer about job A that comes after A ended does not silence the end of a later job B',
  sameQuiet: 'a cancel answer that names the followed job keeps that job\'s end quiet',
  lateFail: 'a "failed" cancel answer about job D does not silence the failure of the followed job E',
}, async (t, open) => {
  const cancel = deferred();
  let cancelAnswer = () => cancel.promise;
  let end = job('A', 'cancelled', { say: 'A was cancelled' });
  const p = await open({ handlers: {
    '/teardown/cancel': () => cancelAnswer(),
    '/teardown/status': () => ({ ok: true, job: end, graph: meta(5) }),
  } });
  const T = p.J().Teardown;
  T.follow(job('A', 'running'));
  const c = T.cancel();                               // the server answers once the browser is closed
  await p.tick(800);                                  // A's last poll comes first: its end is said
  const saidA = p.$('answer').textContent;
  cancel.resolve({ ok: true, answer: 'Cancelled line', nodes: [], graph: meta(5), action: { type: 'teardown_cancel' }, job_id: 'A' });
  await c; await p.tick(0);
  end = job('B', 'cancelled', { say: 'B was cancelled in another tab' });
  T.follow(job('B', 'running'));
  await p.tick(800);
  t.check('lateQuiet', saidA === 'A was cancelled' && p.$('answer').textContent === 'B was cancelled in another tab',
    `${saidA} / ${p.$('answer').textContent}`);

  cancelAnswer = () => ({ ok: true, answer: 'Cancelled line', nodes: [], graph: meta(5), action: { type: 'teardown_cancel' }, job_id: 'C' });
  end = job('C', 'running');
  T.follow(job('C', 'running'));
  await T.cancel();
  await p.tick(0);
  p.$('answer').textContent = 'sentinel';
  end = job('C', 'cancelled', { say: 'C was cancelled' });
  await p.tick(800);
  t.check('sameQuiet', p.$('answer').textContent === 'sentinel', p.$('answer').textContent);

  end = job('D', 'failed', { say: 'D failed' });
  T.follow(job('D', 'running'));
  await p.tick(800);                                  // D's end is said
  end = job('E', 'failed', { say: 'E failed' });
  T.follow(job('E', 'running'));
  cancelAnswer = () => ({ ok: true, answer: 'D failed line', nodes: [], graph: meta(5), error: 'failed', job_id: 'D' });
  await T.cancel();                                   // a late answer about D, while E is followed
  await p.tick(800);
  t.check('lateFail', p.$('answer').textContent === 'E failed', p.$('answer').textContent);
});

// ------------------------------------------------------------------ the voices

const voiceOffer = (fallback = 'browser', hasKey = true) => ({ default: fallback, voices: [
  { name: 'browser', label: 'browser', model: '', has_key: true },
  { name: 'deepgram', label: 'Deepgram', model: 'aura-2-draco-en', has_key: hasKey },
  { name: 'elevenlabs', label: 'ElevenLabs', model: '6WwXjDDEMyNmFG95zycZ', has_key: hasKey },
] });
const elevenInfo = { name: 'elevenlabs', label: 'ElevenLabs', model: '6WwXjDDEMyNmFG95zycZ', has_key: true };
const deepgramInfo = { name: 'deepgram', label: 'Deepgram', model: 'aura-2-draco-en', has_key: true };
const statusWith = voice => () => ({ ok: true, active: { label: 'Claude', model: 'm', has_key: true }, lines: {}, graph: meta(5), voice });
const speakAsks = p => p.fetchLog.filter(entry => entry.key === '/voice/speak').map(entry => entry.body);
const voiceNotes = p => p.$('answer').children.filter(child => child.className === 'voice-note').map(child => child.textContent);
// How often the speaker said "speaking ended" (its onEnd ran), not how often the status chip left "speaking".
const speakingEnds = p => p.ends.length;
const partOf = init => JSON.parse(init.body).part;
// A tab that speaks (not muted), with a fake browser voice. The greeting that waits for the first click is dropped,
// so a story counts only its own lines.
async function speakingPage(open, options = {}) {
  const p = await open(Object.assign({ search: '', synth: true }, options));
  p.J().speaker.stop();
  p.J().speaker.unlock();
  return p;
}

story('a voice switch by voice', {
  voice: 'with the word "voice" any name is taken; without it only the fixed names, at the end of the sentence',
  notVoice: 'a sentence that only talks about a voice is no voice switch ("what is your voice like", "use your voice to read this")',
  apart: 'a sentence that matchBrainSwitch takes is never taken by matchVoiceSwitch, and the other way round',
}, async (t, open) => {
  const p = await open();
  const { matchVoiceSwitch, matchBrainSwitch } = p.J().Core;
  const voiceCases = [
    ['switch voice to deepgram', 'deepgram'], ['change your voice to Deepgram', 'Deepgram'], ['switch to the deepgram voice', 'deepgram'],
    ['use the browser voice', 'browser'], ['Jarvis, use the Deepgram voice please', 'Deepgram'], ['speak with deepgram', 'deepgram'],
    ['talk with the browser voice', 'browser'], ['speak in the normal voice', 'normal'], ['Switch voice to Deep Gram.', 'Deep Gram'],
    ['switch to elevenlabs', 'elevenlabs'], ['switch voice to eleven labs', 'eleven labs'], ['speak with eleven labs', 'eleven labs'],
    ['switch voice to 11 labs', '11 labs'], ['use the ElevenLabs voice', 'ElevenLabs'], ['switch to 11labs', '11labs'],
    ['talk using the Eleven Labs voice', 'Eleven Labs'], ['switch back to the eleven labs voice', 'eleven labs'],
    ['switch to elevenlabs now', 'elevenlabs'],
  ];
  const wrong = voiceCases.filter(([sentence, name]) => matchVoiceSwitch(sentence) !== name)
    .map(([sentence, name]) => `"${sentence}" gave ${JSON.stringify(matchVoiceSwitch(sentence))}, not ${JSON.stringify(name)}`);
  t.check('voice', !wrong.length, wrong.join('; '));
  const notVoice = ['switch brain to deepseek', 'switch to codex', 'what is your voice like', 'use your voice to read this',
    'my voice is tired', 'speak with the manager', 'use your voice', 'switch to eleven', 'switch to labs', 'speak with eleven',
    'tell me about elevenlabs', 'is eleven labs any good', 'switch to elevenlabs now and then', 'speak with 11 labs about pricing']
    .filter(sentence => matchVoiceSwitch(sentence) !== null);
  t.check('notVoice', !notVoice.length, notVoice.join(' / '));
  const brainSentences = ['Switch brain to Deep Seek.', 'Jarvis, use the OpenAI brain', 'switch to claude', 'switch to codex',
    'switch brain to the deepgram voice', 'use the deepgram voice brain', 'switch to the browser brain', 'change brain to voice',
    'switch brain to eleven labs', 'use the elevenlabs brain', 'switch to the 11 labs brain'];
  const voiceSentences = voiceCases.map(([sentence]) => sentence).concat(['switch voice to the claude brain', 'use the brain voice']);
  const both = brainSentences.concat(voiceSentences).filter(sentence => matchBrainSwitch(sentence) && matchVoiceSwitch(sentence));
  const brainTaken = brainSentences.filter(sentence => !matchBrainSwitch(sentence) || matchVoiceSwitch(sentence));
  const voiceTaken = voiceSentences.filter(sentence => !matchVoiceSwitch(sentence) || matchBrainSwitch(sentence));
  t.check('apart', !both.length && !brainTaken.length && !voiceTaken.length,
    `both: ${both.join(' / ')}; brain lost: ${brainTaken.join(' / ')}; voice lost: ${voiceTaken.join(' / ')}`);
});

// The acceptance set: switches the way Chrome's speech recognition writes them (capitals, marks, misheard names,
// polite words, "2" for "to") -> the name that the page sends. The server judges the names: brains.py and voices.py
// prove that it knows them, and preflight proves the whole chain on the served core.js (SPOKEN_SWITCHES there).
const SPOKEN_BRAIN = [
  ['switch brain to deepseek', 'deepseek'], ['switch brain to claude', 'claude'], ['Switch brain to DeepSeek.', 'DeepSeek'],
  ['switch brain to deep seek', 'deep seek'], ['Switch brain to Deep Seek.', 'Deep Seek'], ['switch brain to deep sea', 'deep sea'],
  ['switch brain to Claude', 'Claude'], ['switch brain to cloud', 'cloud'], ['switch brain to clawed', 'clawed'],
  ['switch brain to clod', 'clod'], ['switch brain to OpenAI', 'OpenAI'], ['switch brain to open AI', 'open AI'],
  ['switch brain to open a i', 'open a i'], ['switch brain to Open Router', 'Open Router'], ['switch brain to codecs', 'codecs'],
  ['switch brain to codex', 'codex'], ['switch brain to anti-gravity', 'anti gravity'], ['switch brain to Venice', 'Venice'],
  ['switch my brain to Claude', 'Claude'], ['switch your brain to Claude', 'Claude'], ['change the brain to DeepSeek', 'DeepSeek'],
  ['can you switch brain to Claude', 'Claude'], ['could you switch the brain to DeepSeek', 'DeepSeek'],
  ['please switch brain to Claude', 'Claude'], ['Jarvis switch brain to Claude', 'Claude'], ['Jarvis, switch brain to Claude.', 'Claude'],
  ['hey Jarvis switch brain to DeepSeek', 'DeepSeek'], ['switch brain to Claude please', 'Claude'],
  ['switch the brain to Claude now', 'Claude'], ['switch brain to Claude brain', 'Claude brain'], ['switch brains to Claude', 'Claude'],
  ['switch to Claude', 'Claude'], ['switch to Claude brain', 'Claude'], ['use Claude', 'Claude'], ['use the Claude brain', 'Claude'],
  ['switch to the DeepSeek', 'DeepSeek'], ['switch the brain to the Claude', 'Claude'], ['switch brain 2 Claude', 'Claude'],
  ['switch brain two Claude', 'Claude'], ['switch brain too Claude', 'Claude'], ['switch Brain To Claude', 'Claude'],
  ['switch-brain to Claude', 'Claude'], ['switch, brain to Claude', 'Claude'], ['Jarvis, switch your brain to OpenAI', 'OpenAI'],
  ['Jarvis. Switch brain to DeepSeek.', 'DeepSeek'], ['switch to brain DeepSeek', 'DeepSeek'],
  // more of the same kinds
  ['would you please switch brain to Venice, thank you', 'Venice'], ['I want you to switch to cloud', 'cloud'],
  ['will you use DeepSeek for me', 'DeepSeek'], ['switch to deep sea thanks', 'deep sea'],
];
const SPOKEN_VOICE = [
  ['switch voice to ElevenLabs', 'ElevenLabs'], ['Switch voice to Eleven Labs.', 'Eleven Labs'], ['switch voice to 11 Labs', '11 Labs'],
  ['switch voice to 11labs', '11labs'], ['switch voice to eleven lab', 'eleven lab'], ['switch voice to level labs', 'level labs'],
  ['switch voice to elevenlabs voice', 'elevenlabs'], ['switch your voice to Eleven Labs', 'Eleven Labs'],
  ['change your voice to Deepgram', 'Deepgram'], ['switch voice to deep gram', 'deep gram'], ['switch voice to Deepgram.', 'Deepgram'],
  ['switch the voice to the browser', 'browser'], ['switch voice to browser voice', 'browser'],
  ['Jarvis, switch your voice to ElevenLabs.', 'ElevenLabs'], ['can you switch your voice to Eleven Labs', 'Eleven Labs'],
  ['switch voice 2 Eleven Labs', 'Eleven Labs'], ['switch voice too Eleven Labs', 'Eleven Labs'],
  ['change voice to Eleven Labs please', 'Eleven Labs'], ['use Eleven Labs voice', 'Eleven Labs'], ['use the Eleven Labs voice', 'Eleven Labs'],
  ['speak with Eleven Labs', 'Eleven Labs'], ['switch to Eleven Labs', 'Eleven Labs'], ['switch to Eleven Labs voice', 'Eleven Labs'],
  ['switch my voice to Eleven Labs', 'Eleven Labs'], ['change the voice to Eleven Labs', 'Eleven Labs'],
  ['switch voices to Eleven Labs', 'Eleven Labs'],
  // more of the same kinds
  ['switch to level labs', 'level labs'], ['switch-voice to Deepgram, thanks', 'Deepgram'], ['speak with the eleven lab voice', 'eleven lab'],
];
// Small talk that has the words of a switch in it. None of it is a switch, and none of it asks "which one?".
const NOT_A_SWITCH = [
  'switch the lights off', 'my brain hurts too', 'use the cloud', 'use the cloud for backups', 'is the cloud safe',
  'use the force', 'switch brain off', 'your voice is too quiet', 'I use Claude at work', 'use Claude to summarize this',
  'switch to two columns', 'what voice do you use', 'tell me about the cloud', 'I changed my voice too', 'switch it off too',
  'can you use your voice', 'switch to cloud storage', 'use two hands', 'the cloud brain is down', 'speak with the manager too',
  'my voice is tired', 'speak with the manager', 'use your voice', 'what is your voice like', 'what brain do you use',
];

story('what speech recognition writes for a switch', {
  brains: 'every spoken brain switch of the acceptance set takes the brain route, with the name that was said',
  voices: 'every spoken voice switch of the acceptance set takes the voice route (never the brain route), with the name that was said',
  small: 'small talk with "switch", "brain", "voice", "use", "cloud" or "too" in it is no switch, and the page asks nothing',
  sent: 'spoken switches reach /brain and /voice with the name that was said, and nothing goes to the brain (/chat)',
}, async (t, open) => {
  const p = await open({ handlers: {
    '/status': statusWith(voiceOffer('browser')),
    '/brain': () => ({ ok: true, answer: 'Switched', nodes: [], graph: meta(5) }),
    '/voice': () => ({ ok: true, answer: 'Voice', nodes: [], graph: meta(5), voice: elevenInfo }),
    '/chat': () => chatReply('chat', 5),
  } });
  const { matchBrainSwitch, matchVoiceSwitch, matchSwitchAlone } = p.J().Core;
  const wrong = (cases, match) => cases.filter(([sentence, name]) => match(sentence) !== name)
    .map(([sentence, name]) => `"${sentence}" gave ${JSON.stringify(match(sentence))}, not ${JSON.stringify(name)}`).join('; ');
  t.check('brains', !wrong(SPOKEN_BRAIN, matchBrainSwitch), wrong(SPOKEN_BRAIN, matchBrainSwitch));
  t.check('voices', !wrong(SPOKEN_VOICE, matchVoiceSwitch), wrong(SPOKEN_VOICE, matchVoiceSwitch));
  const taken = NOT_A_SWITCH.filter(sentence => matchBrainSwitch(sentence) !== null || matchVoiceSwitch(sentence) !== null
    || matchSwitchAlone(sentence) !== null);
  t.check('small', NOT_A_SWITCH.length >= 10 && !taken.length, taken.join(' / '));
  for (const sentence of ['switch brain to cloud', 'can you switch your voice to Eleven Labs', 'switch brain 2 Claude', 'use Claude',
    'switch voice to level labs.']) {
    await p.J().handleInput(sentence);
    await p.tick(0);
  }
  const brainNames = p.fetchLog.filter(entry => entry.key === '/brain').map(entry => entry.body.name);
  const voiceNames = p.fetchLog.filter(entry => entry.key === '/voice').map(entry => entry.body.name);
  t.check('sent', JSON.stringify(brainNames) === '["cloud","Claude","Claude"]' && JSON.stringify(voiceNames) === '["Eleven Labs","level labs"]'
    && count(p.fetchLog, '/chat') === 0, `${JSON.stringify(brainNames)} / ${JSON.stringify(voiceNames)} / ${count(p.fetchLog, '/chat')} chats`);
});

story('a pause before the name', {
  constants: 'FINISH_MS is still 900, and HOLD_MS (the wait for a name) is longer',
  held: 'a spoken "switch brain to", a pause longer than FINISH_MS, then "Claude": one /brain with "Claude", nothing before it',
  voice: 'the same for "switch your voice to", a pause, then "Eleven Labs": one /voice with "Eleven Labs"',
  quick: 'an ordinary sentence is not held: it goes to the brain when FINISH_MS is over, not before and not later',
  stop: '"stop" during the hold acts at once: the held words are dropped, and nothing is sent after the hold',
}, async (t, open) => {
  const p = await open({ handlers: {
    '/status': statusWith(voiceOffer('browser')),
    '/brain': () => ({ ok: true, answer: 'Switched', nodes: [], graph: meta(5) }),
    '/voice': () => ({ ok: true, answer: 'Voice', nodes: [], graph: meta(5), voice: elevenInfo }),
    '/chat': () => chatReply('chat', 5),
  } });
  const { FINISH_MS, HOLD_MS } = p.J().Core;
  t.check('constants', FINISH_MS === 900 && HOLD_MS > FINISH_MS + 1000, `${FINISH_MS} / ${HOLD_MS}`);
  const sent = key => p.fetchLog.filter(entry => entry.key === key).map(entry => entry.body.name || entry.body.message);
  p.$('mic').listeners.click[0]();
  const rec = p.recognizers[0];
  const greeting = p.$('answer').textContent;
  rec.onresult(finalResult('switch brain to'));
  await p.tick(FINISH_MS + 600);                      // a pause longer than FINISH_MS, shorter than HOLD_MS
  const early = p.fetchLog.length;
  const waited = p.$('answer').textContent === greeting && p.$('heard').textContent === '“switch brain to”';    // not sent alone
  rec.onresult(finalResult('Claude'));
  await p.tick(FINISH_MS + 10);
  t.check('held', waited && p.fetchLog.length === early + 1 && JSON.stringify(sent('/brain')) === '["Claude"]' && !sent('/chat').length
    && p.$('heard').textContent === '“switch brain to Claude”',
    `${waited} / ${JSON.stringify(sent('/brain'))} / ${JSON.stringify(sent('/chat'))} / ${p.$('heard').textContent}`);
  rec.onresult(finalResult('switch your voice to'));
  await p.tick(FINISH_MS + 600);
  const between = p.$('answer').textContent;          // still the answer to the brain switch: nothing was asked
  rec.onresult(finalResult('Eleven Labs'));
  await p.tick(FINISH_MS + 10);
  t.check('voice', between === 'Switched' && JSON.stringify(sent('/voice')) === '["Eleven Labs"]' && !sent('/chat').length
    && p.$('heard').textContent === '“switch your voice to Eleven Labs”',
    `${between} / ${JSON.stringify(sent('/voice'))} / ${JSON.stringify(sent('/chat'))} / ${p.$('heard').textContent}`);
  rec.onresult(finalResult('what did we agree'));
  await p.tick(FINISH_MS - 1);
  const before = sent('/chat').length;
  await p.tick(1);
  t.check('quick', before === 0 && JSON.stringify(sent('/chat')) === '["what did we agree"]', `${before} then ${JSON.stringify(sent('/chat'))}`);
  rec.onresult(finalResult('switch brain to'));
  await p.tick(500);
  const answer = p.$('answer').textContent;
  rec.onresult(finalResult('stop'));
  await p.tick(HOLD_MS * 2);
  t.check('stop', sent('/brain').length === 1 && sent('/chat').length === 1 && p.$('answer').textContent === answer,
    `${JSON.stringify(sent('/brain'))} / ${JSON.stringify(sent('/chat'))} / ${p.$('answer').textContent}`);
});

story('a switch with no name asks which one', {
  alone: 'a spoken "switch brain to" with nothing after it asks which brain once the hold is over, with the line from /status, and sends nothing',
  answer: 'the next sentence, a name alone ("Claude"), switches to it: one /brain with "Claude"',
  once: 'only the next sentence answers: after it, a lone name goes to the brain as a question',
  other: 'a sentence that is no name ("what time is it") goes its own way and closes the question',
  late: 'an answer after NAME_WAIT_MS is no answer: it goes to the brain',
  typed: 'a typed lone "switch brain" asks too, and a typed "to DeepSeek" after it switches to DeepSeek',
  voice: 'a lone "change your voice" asks which voice, and "the Eleven Labs voice" after it makes one /voice with "Eleven Labs"',
  mixed: 'a voice name is no answer to "which brain?": it goes to the brain',
  oldServer: 'with an old server that has no voices, a lone "switch voice" goes to the brain, as before',
}, async (t, open) => {
  const words = { brain_which: 'Server words: which brain?', voice_which: 'Server words: which voice?' };
  const p = await open({ handlers: {
    '/status': () => Object.assign(statusWith(voiceOffer('browser'))(), { lines: words }),
    '/brain': () => ({ ok: true, answer: 'Switched', nodes: [], graph: meta(5) }),
    '/voice': () => ({ ok: true, answer: 'Voice', nodes: [], graph: meta(5), voice: elevenInfo }),
    '/chat': () => chatReply('chat', 5),
  } });
  const { FINISH_MS, HOLD_MS, NAME_WAIT_MS } = p.J().Core;
  const sent = key => p.fetchLog.filter(entry => entry.key === key).map(entry => entry.body.name || entry.body.message);
  const say = async sentence => { await p.J().handleInput(sentence); await p.tick(0); };
  p.$('mic').listeners.click[0]();
  const rec = p.recognizers[0];
  rec.onresult(finalResult('switch brain to'));
  await p.tick(HOLD_MS - 1);
  const held = p.$('answer').textContent !== words.brain_which;
  await p.tick(2);
  t.check('alone', held && p.$('answer').textContent === words.brain_which && !sent('/brain').length && !sent('/chat').length,
    `${held} / ${p.$('answer').textContent} / ${JSON.stringify(sent('/brain'))} / ${JSON.stringify(sent('/chat'))}`);
  rec.onresult(finalResult('Claude'));
  await p.tick(FINISH_MS + 10);
  t.check('answer', JSON.stringify(sent('/brain')) === '["Claude"]' && !sent('/chat').length, JSON.stringify(sent('/brain')));
  rec.onresult(finalResult('Claude'));
  await p.tick(FINISH_MS + 10);
  t.check('once', sent('/brain').length === 1 && JSON.stringify(sent('/chat')) === '["Claude"]', JSON.stringify(sent('/chat')));
  await say('switch brain');
  const asked = p.$('answer').textContent === words.brain_which;
  await say('what time is it');
  await say('Claude');
  t.check('other', asked && sent('/brain').length === 1 && JSON.stringify(sent('/chat').slice(1)) === '["what time is it","Claude"]',
    `${asked} / ${JSON.stringify(sent('/brain'))} / ${JSON.stringify(sent('/chat'))}`);
  await say('switch brain');
  await p.tick(NAME_WAIT_MS + 1);
  await say('Claude');
  t.check('late', sent('/brain').length === 1 && sent('/chat').length === 4, `${JSON.stringify(sent('/brain'))} / ${sent('/chat').length} chats`);
  await say('switch brain');
  await say('to DeepSeek');
  t.check('typed', JSON.stringify(sent('/brain')) === '["Claude","DeepSeek"]' && sent('/chat').length === 4, JSON.stringify(sent('/brain')));
  await say('change your voice');
  const voiceAsked = p.$('answer').textContent === words.voice_which;
  await say('the Eleven Labs voice');
  t.check('voice', voiceAsked && JSON.stringify(sent('/voice')) === '["Eleven Labs"]' && sent('/chat').length === 4,
    `${voiceAsked} / ${JSON.stringify(sent('/voice'))}`);
  await say('switch brain');
  await say('Eleven Labs');
  t.check('mixed', sent('/brain').length === 2 && sent('/voice').length === 1 && sent('/chat').length === 5,
    `${JSON.stringify(sent('/brain'))} / ${JSON.stringify(sent('/voice'))} / ${sent('/chat').length} chats`);

  const q = await open({ handlers: { '/chat': () => chatReply('chat', 5) } });     // its /status has no voices
  await q.J().handleInput('switch voice');
  await q.tick(0);
  t.check('oldServer', count(q.fetchLog, '/chat') === 1 && q.$('answer').textContent === 'chat', `${count(q.fetchLog, '/chat')} chats`);
});

story('the voice switch', {
  route: '"switch voice to deepgram" posts /voice once, with the name that was said, and nothing goes to the brain',
  taken: 'an ok answer makes its voice the page\'s voice: kept in localStorage under jarviz_voice, shown on the chip',
  newVoice: 'the answer line is shown and said with the NEW voice (it asks /voice/speak for that line)',
  refused: 'a refused switch keeps the old voice: its line is shown and said with the browser voice, nothing is kept',
  offline: 'no answer from the server shows the plain line for it',
}, async (t, open) => {
  const deepgram = { name: 'deepgram', label: 'Deepgram', model: 'aura-2-draco-en', has_key: true };
  const p = await speakingPage(open, { handlers: {
    '/status': statusWith(voiceOffer('browser')),
    '/voice': () => ({ ok: true, answer: 'Deepgram it is.', nodes: [], graph: meta(5), voice: deepgram }),
    '/voice/speak': () => audioResponse(1),
  } });
  await p.J().handleInput('switch voice to deepgram');
  await p.tick(0);
  const posts = p.fetchLog.filter(entry => entry.key === '/voice');
  t.check('route', posts.length === 1 && posts[0].body.name === 'deepgram' && count(p.fetchLog, '/chat') === 0, JSON.stringify(posts));
  t.check('taken', p.stored.jarviz_voice === 'deepgram' && p.$('voice').hidden === false
    && p.$('voice').textContent === 'voice: Deepgram · aura-2-draco-en', `${JSON.stringify(p.stored)} / ${p.$('voice').textContent}`);
  const asked = speakAsks(p);
  t.check('newVoice', asked.length === 1 && asked[0].text === 'Deepgram it is.' && asked[0].part === 0
    && p.$('answer').textContent === 'Deepgram it is.' && !p.synth.said.includes('Deepgram it is.'),
    `${JSON.stringify(asked)} / browser said ${JSON.stringify(p.synth.said)}`);

  const q = await speakingPage(open, { handlers: {
    '/status': statusWith(voiceOffer('browser', false)),
    '/voice': () => refusal(409, { ok: false, error: 'voice_no_key', answer: 'No key for that voice.', nodes: [], graph: meta(5) }),
    '/voice/speak': () => audioResponse(1),
  } });
  await q.J().handleInput('switch voice to deepgram');
  await q.tick(0);
  t.check('refused', q.$('voice').textContent === 'voice: browser' && speakAsks(q).length === 0 && q.synth.said.includes('No key for that voice.')
    && q.$('answer').textContent === 'No key for that voice.' && !('jarviz_voice' in q.stored),
    `${q.$('voice').textContent} / ${JSON.stringify(q.synth.said)} / ${JSON.stringify(q.stored)}`);

  const r = await speakingPage(open, { handlers: { '/status': statusWith(voiceOffer('browser')) } });     // /voice does not answer
  await r.J().handleInput('switch voice to deepgram');
  await r.tick(0);
  t.check('offline', r.$('answer').textContent === 'I could not switch voices. I could not reach the server.'
    && r.$('voice').textContent === 'voice: browser', `${r.$('answer').textContent} / ${r.$('voice').textContent}`);
});

story('a late voice switch', {
  kept: 'a /voice answer that comes after a newer sentence changes no voice: the speaker, the chip and jarviz_voice stay',
  newer: 'the newer answer is said in the voice that was on before, and the late switch says nothing',
}, async (t, open) => {
  const read = deferred();
  const p = await speakingPage(open, { handlers: {
    '/status': statusWith(voiceOffer('browser')),
    '/voice': () => ({ ok: true, answer: 'Deepgram it is.', nodes: [], graph: meta(6), voice: deepgramInfo }),
    '/voice/speak': () => audioResponse(1),
    'graph-data.js': () => read.promise,
    '/chat': () => chatReply('The newer answer', 6),
  } });
  const a = p.J().handleInput('switch voice to deepgram');     // its answer waits for a read of the galaxy
  await p.tick(0);
  const b = p.J().handleInput('what now');           // a newer sentence: it owns the turn
  await p.tick(0);
  read.resolve(graphFile(galaxy(6, ['A', 'B', 'C', 'D'])));
  await a; await b;
  await p.tick(0);
  t.check('kept', p.J().speaker.voiceState().name === 'browser' && p.$('voice').textContent === 'voice: browser'
    && !('jarviz_voice' in p.stored), `${JSON.stringify(p.J().speaker.voiceState())} / ${p.$('voice').textContent} / ${JSON.stringify(p.stored)}`);
  t.check('newer', p.$('answer').textContent === 'The newer answer' && speakAsks(p).length === 0
    && p.synth.said.join('|') === 'The newer answer', `${p.$('answer').textContent} / ${speakAsks(p).length} asks / ${JSON.stringify(p.synth.said)}`);
});

story('the ElevenLabs voice switch', {
  route: '"switch voice to eleven labs" posts /voice once, with the name that was said, and nothing goes to the brain',
  taken: 'the ElevenLabs answer is kept in localStorage as jarviz_voice = "elevenlabs" and shown on the chip',
  named: 'the answer line asks /voice/speak with { text, part, voice: "elevenlabs" }, and nothing else',
  back: '"switch voice to deepgram" after it keeps "deepgram", and its line asks with voice: "deepgram"',
  browser: '"switch voice to browser" asks for no audio any more',
}, async (t, open) => {
  let answer = { ok: true, answer: 'ElevenLabs it is.', nodes: [], graph: meta(5), voice: elevenInfo };
  const p = await speakingPage(open, { handlers: {
    '/status': statusWith(voiceOffer('browser')),
    '/voice': () => answer,
    '/voice/speak': () => audioResponse(1),
  } });
  await p.J().handleInput('switch voice to eleven labs');
  await p.tick(0);
  const posts = p.fetchLog.filter(entry => entry.key === '/voice');
  t.check('route', posts.length === 1 && posts[0].body.name === 'eleven labs' && count(p.fetchLog, '/chat') === 0, JSON.stringify(posts));
  t.check('taken', p.stored.jarviz_voice === 'elevenlabs' && p.$('voice').textContent === 'voice: ElevenLabs · 6WwXjDDEMyNmFG95zycZ',
    `${JSON.stringify(p.stored)} / ${p.$('voice').textContent}`);
  let asked = speakAsks(p);
  t.check('named', asked.length === 1 && JSON.stringify(asked[0]) === JSON.stringify({ text: 'ElevenLabs it is.', part: 0, voice: 'elevenlabs' })
    && !p.synth.said.includes('ElevenLabs it is.'), `${JSON.stringify(asked)} / browser said ${JSON.stringify(p.synth.said)}`);
  p.audios[p.audios.length - 1].end();
  await p.tick(0);
  answer = { ok: true, answer: 'Deepgram it is.', nodes: [], graph: meta(5), voice: deepgramInfo };
  await p.J().handleInput('switch voice to deepgram');
  await p.tick(0);
  asked = speakAsks(p);
  t.check('back', p.stored.jarviz_voice === 'deepgram' && asked.length === 2 && asked[1].voice === 'deepgram'
    && asked[1].text === 'Deepgram it is.' && p.$('voice').textContent === 'voice: Deepgram · aura-2-draco-en',
    `${JSON.stringify(p.stored)} / ${JSON.stringify(asked)} / ${p.$('voice').textContent}`);
  p.audios[p.audios.length - 1].end();
  await p.tick(0);
  answer = { ok: true, answer: 'The browser it is.', nodes: [], graph: meta(5),
             voice: { name: 'browser', label: 'browser', model: '', has_key: true } };
  await p.J().handleInput('switch voice to browser');
  await p.tick(0);
  t.check('browser', speakAsks(p).length === 2 && p.synth.said.includes('The browser it is.') && p.stored.jarviz_voice === 'browser'
    && p.$('voice').textContent === 'voice: browser', `${speakAsks(p).length} asks / ${JSON.stringify(p.synth.said)}`);
});

story('a line keeps the voice it started with', {
  start: 'a line that starts in the Deepgram voice asks for its parts with voice: "deepgram"',
  kept: 'a switch to ElevenLabs while the line plays does not change the voice of the parts still to come',
  next: 'the next line asks with the new voice: "elevenlabs"',
}, async (t, open) => {
  const p = await speakingPage(open, { handlers: {
    '/status': statusWith(voiceOffer('deepgram')),
    '/voice/speak': (url, init) => audioResponse(3, 'part' + partOf(init)),
  } });
  p.J().speaker.speak('One. Two. Three.');
  await p.tick(0);
  const early = speakAsks(p).map(ask => ask.voice);
  t.check('start', early.length === 2 && early.every(voice => voice === 'deepgram'), JSON.stringify(early));
  p.J().speaker.setVoice('elevenlabs');               // the switch, in the middle of the line
  p.audios[0].end();
  await p.tick(0);
  p.audios[1].end();
  await p.tick(0);
  p.audios[2].end();
  await p.tick(0);
  const line = speakAsks(p);
  t.check('kept', line.length === 3 && line.map(ask => ask.part).join() === '0,1,2' && line.every(ask => ask.voice === 'deepgram')
    && p.audios.length === 3, `${JSON.stringify(line)} / ${p.audios.length} audios`);
  p.J().speaker.speak('Next.');
  await p.tick(0);
  const next = speakAsks(p).slice(3);
  t.check('next', next.length >= 1 && next[0].text === 'Next.' && next.every(ask => ask.voice === 'elevenlabs'), JSON.stringify(next));
});

story('the server voice says a line in its parts', {
  first: 'with the server voice a line asks /voice/speak for part 0, with the whole text',
  ahead: 'part 1 is asked for while part 0 plays, with the same whole text (the header said 2 parts)',
  order: 'the parts play in order: part 1 starts only after part 0 ended, and no part 2 is asked for',
  ended: 'the "speaking ended" signal comes once, after the last part',
  urls: 'every object URL is given back at the end',
  shape: 'every ask is { text, part, voice } and names the voice of the line ("deepgram"), and nothing else',
}, async (t, open) => {
  const p = await speakingPage(open, { handlers: {
    '/status': statusWith(voiceOffer('deepgram')),
    '/voice/speak': (url, init) => audioResponse(2, 'part' + partOf(init)),
  } });
  p.J().speaker.speak('One sentence. Another sentence.');
  await p.tick(0);
  const asked = speakAsks(p);
  t.check('first', asked.length >= 1 && asked[0].part === 0 && asked[0].text === 'One sentence. Another sentence.', JSON.stringify(asked));
  t.check('shape', asked.length === 2 && asked.every(ask => Object.keys(ask).join() === 'text,part,voice' && ask.voice === 'deepgram'),
    JSON.stringify(asked));
  const first = p.audios[0];
  t.check('ahead', asked.length === 2 && asked[1].part === 1 && asked[1].text === asked[0].text && p.audios.length === 1
    && first.played && !first.paused && p.J().status() === 'speaking', `${JSON.stringify(asked)} / ${p.audios.length} audios`);
  first.end();
  await p.tick(0);
  const second = p.audios[1];
  const between = speakingEnds(p);
  t.check('order', p.audios.length === 2 && /part0$/.test(first.src) && Boolean(second) && /part1$/.test(second.src) && second.played
    && speakAsks(p).length === 2, `${p.audios.map(audio => audio.src)} / ${speakAsks(p).length} asks`);
  second.end();
  await p.tick(0);
  t.check('ended', between === 0 && speakingEnds(p) === 1 && p.J().status() !== 'speaking', `${between} then ${speakingEnds(p)} ends`);
  t.check('urls', p.liveUrls.size === 0, [...p.liveUrls].join(', '));
});

story('a server voice part that fails', {
  rest: 'a part that cannot be fetched hands the line to the browser voice at once',
  ended: 'that line ends once: one "speaking ended" signal, after the browser voice finished',
  once: 'the server\'s line is shown once, and again only after a part played well',
  play: 'a part whose play() is refused, or whose audio has an error event, hands the line to the browser voice too',
}, async (t, open) => {
  const refused = () => refusal(503, { ok: false, error: 'voice_key_invalid', answer: 'The Deepgram key was refused.' });
  let plan = (part) => (part === 0 ? audioResponse(2) : refused());           // part 0 plays, part 1 fails
  const p = await speakingPage(open, { handlers: {
    '/status': statusWith(voiceOffer('deepgram')),
    '/voice/speak': (url, init) => plan(partOf(init)),
  } });
  p.J().speaker.speak('Line one. Its end.');
  await p.tick(0);
  p.audios[0].end();                                  // part 0 played well; part 1 cannot be had
  await p.tick(0);
  t.check('rest', p.synth.said.length === 1 && p.synth.said[0] === 'Line one. Its end.' && p.audios.length === 1,
    `${JSON.stringify(p.synth.said)} / ${p.audios.length} audios`);
  p.synth.finish();
  await p.tick(0);
  t.check('ended', speakingEnds(p) === 1, `${speakingEnds(p)} ends`);
  plan = () => refused();
  p.J().speaker.speak('Line two.');                   // fails too: no second note
  await p.tick(0);
  p.synth.finish();
  await p.tick(0);
  const afterTwo = voiceNotes(p).length;
  plan = () => audioResponse(1);
  p.J().speaker.speak('Line three.');                 // plays well
  await p.tick(0);
  p.audios[p.audios.length - 1].end();
  await p.tick(0);
  plan = () => refused();
  p.J().speaker.speak('Line four.');                  // fails after a good part: the note comes again
  await p.tick(0);
  t.check('once', afterTwo === 1 && voiceNotes(p).length === 2 && voiceNotes(p)[0] === 'The Deepgram key was refused.',
    `${afterTwo} then ${JSON.stringify(voiceNotes(p))}`);

  const q = await speakingPage(open, { audio: ['reject'], handlers: {
    '/status': statusWith(voiceOffer('deepgram')), '/voice/speak': () => audioResponse(1),
  } });
  q.J().speaker.speak('Refused play.');
  await q.tick(0);
  q.J().speaker.speak('Broken audio.');
  await q.tick(0);
  const broken = q.audios[1];
  if (broken) broken.fail();
  await q.tick(0);
  t.check('play', q.synth.said.join('|') === 'Refused play.|Broken audio.' && q.liveUrls.size === 0 && voiceNotes(q).length === 1
    && voiceNotes(q)[0] === 'The server voice did not play. The browser voice speaks instead.',
    `${JSON.stringify(q.synth.said)} / ${q.liveUrls.size} urls / ${JSON.stringify(voiceNotes(q))}`);
});

story('three server voice failures in a row', {
  stop: 'after three failing lines in a row the page asks the server voice no more: the browser voice speaks',
  chip: 'the chip shows that the server voice is off',
  again: '"switch voice to deepgram" asks the server voice again',
}, async (t, open) => {
  let plan = () => refusal(503, { ok: false, error: 'voice_unreachable', answer: 'Deepgram cannot be reached.' });
  const p = await speakingPage(open, { handlers: {
    '/status': statusWith(voiceOffer('deepgram')),
    '/voice/speak': () => plan(),
    '/voice': () => ({ ok: true, answer: 'Back to Deepgram.', nodes: [], graph: meta(5),
                       voice: { name: 'deepgram', label: 'Deepgram', model: 'aura-2-draco-en', has_key: true } }),
  } });
  for (const line of ['One.', 'Two.', 'Three.', 'Four.']) {
    p.J().speaker.speak(line);
    await p.tick(0);
    p.synth.finish();
    await p.tick(0);
  }
  t.check('stop', speakAsks(p).length === 3 && p.synth.said.join('|') === 'One.|Two.|Three.|Four.',
    `${speakAsks(p).length} asks / ${JSON.stringify(p.synth.said)}`);
  t.check('chip', p.$('voice').textContent === 'voice: browser · Deepgram off' && p.$('voice').classList.contains('warn'), p.$('voice').textContent);
  plan = () => audioResponse(1);
  await p.J().handleInput('switch voice to deepgram');
  await p.tick(0);
  const asks = speakAsks(p);
  t.check('again', asks.length === 4 && asks[3].text === 'Back to Deepgram.' && p.$('voice').textContent === 'voice: Deepgram · aura-2-draco-en',
    `${asks.length} asks / ${p.$('voice').textContent}`);
});

story('a line whose later part fails is a failed line', {
  counted: 'three lines in a row whose part 0 plays and part 1 fails give the server voice up at the third',
  fourth: 'the fourth line asks the server voice for nothing: the browser voice says it',
  note: 'the server\'s trouble line still comes again after each part that played well',
}, async (t, open) => {
  const p = await speakingPage(open, { handlers: {
    '/status': statusWith(voiceOffer('deepgram')),
    '/voice/speak': (url, init) => (partOf(init) === 0 ? audioResponse(2, 'part0')
      : refusal(503, { ok: false, error: 'voice_unreachable', answer: 'Deepgram cannot be reached.' })),
  } });
  const given = [];
  for (const line of ['One. More.', 'Two. More.', 'Three. More.']) {
    p.J().speaker.speak(line);
    await p.tick(0);
    p.audios[p.audios.length - 1].end();              // part 0 played well; part 1 cannot be had
    await p.tick(0);
    p.synth.finish();                                 // the browser voice said the line
    await p.tick(0);
    given.push(p.J().speaker.voiceState().gaveUp);
  }
  t.check('counted', given.join() === 'false,false,true' && p.$('voice').textContent === 'voice: browser · Deepgram off',
    `${given.join()} / ${p.$('voice').textContent}`);
  const asks = speakAsks(p).length;
  p.J().speaker.speak('Four.');
  await p.tick(0);
  t.check('fourth', speakAsks(p).length === asks && p.synth.said[p.synth.said.length - 1] === 'Four.',
    `${speakAsks(p).length - asks} more asks / ${JSON.stringify(p.synth.said)}`);
  t.check('note', voiceNotes(p).length === 3 && voiceNotes(p).every(note => note === 'Deepgram cannot be reached.'), JSON.stringify(voiceNotes(p)));
});

story('a new line that cannot play ends the line it cut off', {
  ended: 'with no browser voice, a new line whose audio cannot be had ends the line it cut off: one "speaking ended"',
  listens: 'after it the microphone hears an ordinary sentence again',
  played: 'a new line that plays sends no "speaking ended" between the two lines, and one at its end',
}, async (t, open) => {
  let plan = () => audioResponse(1);
  const p = await speakingPage(open, { synth: false, handlers: {
    '/status': statusWith(voiceOffer('deepgram')),
    '/voice/speak': () => plan(),
    '/chat': () => chatReply('Heard you', 5),
  } });
  p.J().speaker.speak('The first line.');
  await p.tick(0);
  const before = p.J().status();
  plan = () => refusal(503, { ok: false, error: 'voice_unreachable', answer: 'Deepgram cannot be reached.' });
  p.J().speaker.speak('The new line.');               // it cuts the first line, and its own audio cannot be had
  await p.tick(0);
  t.check('ended', before === 'speaking' && speakingEnds(p) === 1 && p.J().status() !== 'speaking' && !p.J().speaker.isSpeaking(),
    `${before} then ${p.J().status()} / ${speakingEnds(p)} ends`);
  await p.tick(1000);                                 // past the echo guard
  p.$('mic').listeners.click[0]();
  p.recognizers[0].onresult(finalResult('what now'));
  await p.tick(1500);
  t.check('listens', count(p.fetchLog, '/chat') === 1, `${count(p.fetchLog, '/chat')} chats`);

  const q = await speakingPage(open, { synth: false, handlers: {
    '/status': statusWith(voiceOffer('deepgram')), '/voice/speak': () => audioResponse(1),
  } });
  q.J().speaker.speak('The first line.');
  await q.tick(0);
  q.J().speaker.speak('The new line.');
  await q.tick(0);
  const between = speakingEnds(q);
  q.audios[1].end();
  await q.tick(0);
  t.check('played', between === 0 && speakingEnds(q) === 1 && q.J().status() !== 'speaking', `${between} then ${speakingEnds(q)} ends`);
});

story('a line of an earlier voice choice counts only for that choice', {
  oldLine: 'a Deepgram line whose part fails after "switch voice to ElevenLabs" is still said, by the browser voice',
  notCounted: 'that failure does not count for ElevenLabs: after two ElevenLabs failures it is not given up yet',
  ownNote: 'the first ElevenLabs failure still shows its own line',
  third: 'the third ElevenLabs failure in a row gives ElevenLabs up, as before',
}, async (t, open) => {
  const late = deferred();
  const refused = who => refusal(503, { ok: false, error: 'voice_error', answer: who + ' failed.' });
  const p = await speakingPage(open, { handlers: {
    '/status': statusWith(voiceOffer('deepgram')),
    '/voice/speak': (url, init) => {
      const ask = JSON.parse(init.body);
      if (ask.voice === 'elevenlabs') return refused('ElevenLabs');
      return ask.part === 0 ? audioResponse(2, 'part0') : late.promise;
    },
  } });
  p.J().speaker.speak('Old line. Its end.');
  await p.tick(0);
  p.J().speaker.setVoice('elevenlabs');               // the switch, while part 1 of the old line is on its way
  p.audios[0].end();
  late.resolve(refused('Deepgram'));
  await p.tick(0);
  t.check('oldLine', p.synth.said.includes('Old line. Its end.'), JSON.stringify(p.synth.said));
  p.synth.finish();
  await p.tick(0);
  const notesBefore = voiceNotes(p).length;
  for (const line of ['E1.', 'E2.']) {
    p.J().speaker.speak(line);
    await p.tick(0);
    p.synth.finish();
    await p.tick(0);
  }
  const state = p.J().speaker.voiceState();
  t.check('notCounted', state.name === 'elevenlabs' && state.gaveUp === false, JSON.stringify(state));
  t.check('ownNote', voiceNotes(p).length === notesBefore + 1 && voiceNotes(p)[notesBefore] === 'ElevenLabs failed.',
    JSON.stringify(voiceNotes(p)));
  p.J().speaker.speak('E3.');
  await p.tick(0);
  p.synth.finish();
  await p.tick(0);
  const asks = speakAsks(p).filter(ask => ask.voice === 'elevenlabs');
  t.check('third', p.J().speaker.voiceState().gaveUp === true && asks.length === 3, `${asks.length} asks / ${JSON.stringify(p.J().speaker.voiceState())}`);
});

story('a good part of an earlier voice choice does not speak for the new one', {
  played: 'a Deepgram part that plays well after "switch voice to ElevenLabs" still plays, and its line ends once',
  counted: 'ElevenLabs then fails three lines in a row and is given up at the third, as if the old part never played',
}, async (t, open) => {
  const p = await speakingPage(open, { handlers: {
    '/status': statusWith(voiceOffer('deepgram')),
    '/voice/speak': (url, init) => (JSON.parse(init.body).voice === 'elevenlabs'
      ? refusal(503, { ok: false, error: 'voice_error', answer: 'ElevenLabs failed.' }) : audioResponse(1, 'old')),
  } });
  p.J().speaker.speak('Old line.');
  await p.tick(0);
  p.J().speaker.setVoice('elevenlabs');
  p.audios[0].end();                                  // the old part played well, after the switch
  await p.tick(0);
  t.check('played', p.audios.length === 1 && speakingEnds(p) === 1 && p.liveUrls.size === 0, `${speakingEnds(p)} ends`);
  const given = [];
  for (const line of ['E1.', 'E2.', 'E3.']) {
    p.J().speaker.speak(line);
    await p.tick(0);
    p.synth.finish();
    await p.tick(0);
    given.push(p.J().speaker.voiceState().gaveUp);
  }
  t.check('counted', given.join() === 'false,false,true', given.join());
});

story('a muted tab asks for no audio', {
  muted: 'a ?mute=1 tab with the server voice sends no request to /voice/speak, for any line',
  mutedEleven: 'a ?mute=1 tab with the ElevenLabs voice sends no request to /voice/speak either, also after "switch voice to eleven labs"',
  control: 'the same page without ?mute=1 asks for the audio of its line',
}, async (t, open) => {
  const handlers = { '/status': statusWith(voiceOffer('deepgram')), '/voice/speak': () => audioResponse(1), '/chat': () => chatReply('An answer', 5) };
  const p = await open({ search: '?mute=1', synth: true, handlers });
  p.J().speaker.unlock();
  p.J().speaker.speak('A line.');
  await p.J().handleInput('a question');
  await p.tick(1000);
  t.check('muted', speakAsks(p).length === 0 && p.$('voice').textContent === 'voice: Deepgram · aura-2-draco-en',
    `${speakAsks(p).length} asks / ${p.$('voice').textContent}`);
  const e = await open({ search: '?mute=1', synth: true, handlers: Object.assign({}, handlers, {
    '/status': statusWith(voiceOffer('elevenlabs')),
    '/voice': () => ({ ok: true, answer: 'ElevenLabs it is.', nodes: [], graph: meta(5), voice: elevenInfo }),
  }) });
  e.J().speaker.unlock();
  e.J().speaker.speak('A line.');
  await e.J().handleInput('a question');
  await e.J().handleInput('switch voice to eleven labs');
  await e.tick(1000);
  t.check('mutedEleven', speakAsks(e).length === 0 && e.synth.said.length === 0
    && e.$('voice').textContent === 'voice: ElevenLabs · 6WwXjDDEMyNmFG95zycZ', `${speakAsks(e).length} asks / ${e.$('voice').textContent}`);
  const q = await speakingPage(open, { handlers });
  q.J().speaker.speak('A line.');
  await q.tick(0);
  t.check('control', speakAsks(q).length === 1 && speakAsks(q)[0].text === 'A line.', JSON.stringify(speakAsks(q)));
});

story('a stop while a server voice part plays', {
  stopped: '"stop" during a part stops its audio at once and gives its URL back',
  dropped: 'the part that was still on its way never plays, and no later part is asked for',
  ended: 'the "speaking ended" signal comes once',
}, async (t, open) => {
  const late = deferred();
  const p = await speakingPage(open, { handlers: {
    '/status': statusWith(voiceOffer('deepgram')),
    '/voice/speak': (url, init) => (partOf(init) === 0 ? audioResponse(3, 'part0') : late.promise),
  } });
  p.J().speaker.speak('A long line. In three parts. The end.');
  await p.tick(0);
  const playing = p.audios[0];
  await p.J().handleInput('stop');
  await p.tick(0);
  t.check('stopped', Boolean(playing) && playing.paused && p.liveUrls.size === 0, `${playing && playing.paused} / ${p.liveUrls.size} urls`);
  late.resolve(audioResponse(3, 'part1'));
  await p.tick(2000);
  t.check('dropped', p.audios.length === 1 && speakAsks(p).length === 2 && p.liveUrls.size === 0,
    `${p.audios.length} audios / ${speakAsks(p).map(ask => ask.part)} / ${p.liveUrls.size} urls`);
  t.check('ended', speakingEnds(p) === 1 && p.J().status() !== 'speaking', `${speakingEnds(p)} ends`);
});

story('the server voice keeps the queue rule of speak', {
  click: 'a line before the first click asks for no audio; after the click only the last such line is asked for',
  newest: 'a new line while a part plays stops that part at once, gives its URL back, drops its next part, and asks for the new line',
  signals: 'the cut line sends no "speaking ended" of its own; the new line ends once',
}, async (t, open) => {
  const p = await open({ search: '', synth: true, handlers: {
    '/status': statusWith(voiceOffer('deepgram')),
    '/voice/speak': (url, init) => audioResponse(2, JSON.parse(init.body).text + '#' + partOf(init)),
  } });
  p.J().speaker.speak('Early one.');                  // the greeting and these wait for the first click
  p.J().speaker.speak('Early two.');
  await p.tick(0);
  const before = speakAsks(p).length;
  p.J().speaker.unlock();
  await p.tick(0);
  const asked = speakAsks(p);
  t.check('click', before === 0 && asked.length === 2 && asked[0].text === 'Early two.' && asked[0].part === 0 && asked[1].part === 1,
    `${before} before / ${JSON.stringify(asked)}`);
  const old = p.audios[0];
  p.J().speaker.speak('The new line.');
  await p.tick(0);
  const fresh = p.audios[1];
  t.check('newest', Boolean(old) && old.paused && Boolean(fresh) && /The new line\.#0$/.test(fresh.src) && fresh.played
    && p.audios.length === 2 && p.liveUrls.size === 1, `${p.audios.map(audio => audio.src)} / ${p.liveUrls.size} urls`);
  fresh.end();
  await p.tick(0);
  p.audios[p.audios.length - 1].end();
  await p.tick(0);
  t.check('signals', speakingEnds(p) === 1 && p.liveUrls.size === 0 && p.audios.length === 3 && /The new line\.#1$/.test(p.audios[2].src),
    `${speakingEnds(p)} ends / ${p.audios.map(audio => audio.src)} / ${p.liveUrls.size} urls`);
});

story('the voice at start', {
  kept: 'the kept choice is used at start when the status says that voice is there with a key',
  noKey: 'a kept choice without a key any more falls back to the browser voice, without a word',
  blocked: 'with localStorage blocked the page starts with the server\'s default',
  keptBrowser: 'a kept "browser" wins over a server default',
  keptEleven: 'a kept "elevenlabs" wins over a Deepgram default, and its first line asks with voice: "elevenlabs"',
}, async (t, open) => {
  const start = async (storage, offer) => {
    const p = await speakingPage(open, { storage, handlers: { '/status': statusWith(offer), '/voice/speak': () => audioResponse(1) } });
    p.J().speaker.speak('Hi.');
    await p.tick(0);
    return p;
  };
  let p = await start({ jarviz_voice: 'deepgram' }, voiceOffer('browser', true));
  t.check('kept', p.$('voice').textContent === 'voice: Deepgram · aura-2-draco-en' && speakAsks(p).length === 1, p.$('voice').textContent);
  p = await start({ jarviz_voice: 'deepgram' }, voiceOffer('browser', false));
  t.check('noKey', p.$('voice').textContent === 'voice: browser' && speakAsks(p).length === 0 && p.synth.said.includes('Hi.')
    && voiceNotes(p).length === 0 && p.$('answer').textContent === 'Hello.',          // the greeting only
    `${p.$('voice').textContent} / ${speakAsks(p).length} asks / ${p.$('answer').textContent} / ${JSON.stringify(voiceNotes(p))}`);
  p = await start('blocked', voiceOffer('deepgram', true));
  t.check('blocked', p.$('voice').textContent === 'voice: Deepgram · aura-2-draco-en' && speakAsks(p).length === 1, p.$('voice').textContent);
  p = await start({ jarviz_voice: 'browser' }, voiceOffer('deepgram', true));
  t.check('keptBrowser', p.$('voice').textContent === 'voice: browser' && speakAsks(p).length === 0, p.$('voice').textContent);
  p = await start({ jarviz_voice: 'elevenlabs' }, voiceOffer('deepgram', true));
  t.check('keptEleven', p.$('voice').textContent === 'voice: ElevenLabs · 6WwXjDDEMyNmFG95zycZ' && speakAsks(p).length === 1
    && speakAsks(p)[0].voice === 'elevenlabs', `${p.$('voice').textContent} / ${JSON.stringify(speakAsks(p))}`);
});

story('an old server without voices', {
  chip: 'with no "voice" in /status the voice chip stays hidden',
  browser: 'lines are said with the browser voice, and no audio is asked for',
  route: '"switch voice to deepgram" goes to the brain, as before',
}, async (t, open) => {
  const p = await speakingPage(open, { handlers: { '/chat': () => chatReply('As before', 5), '/voice/speak': () => audioResponse(1) } });
  t.check('chip', p.$('voice').hidden === true);
  p.J().speaker.speak('A line.');
  await p.tick(0);
  t.check('browser', speakAsks(p).length === 0 && p.synth.said.includes('A line.'), JSON.stringify(p.synth.said));
  await p.J().handleInput('switch voice to deepgram');
  await p.tick(0);
  t.check('route', count(p.fetchLog, '/chat') === 1 && count(p.fetchLog, '/voice') === 0 && p.$('answer').textContent === 'As before',
    `${count(p.fetchLog, '/chat')} chats / ${count(p.fetchLog, '/voice')} voice posts`);
});

// The Ali Memory line under an answer (showMemory in app.js). The fake page does not clear the children of an element
// when its text is set (a real page does), so each case that must show NO line has a fresh page.
const memoryNotes = p => p.$('answer').children.filter(child => child.className === 'memory-note').map(child => child.textContent);
const memoryReply = (answer, memory, rev = 5) => Object.assign(chatReply(answer, rev), { memory });

story('an answer given with Ali Memory', {
  line: 'an answer given with Ali Memory shows one plain line under it: "From Ali Memory:" and the titles, joined by " · "',
  text: 'a title is text from a stranger: markup in it is shown as it is, and the page makes no HTML of it',
  none: 'an answer with no memory (used false, no memory field, an old server) shows no Ali Memory line',
  odd: 'odd memory fields are cut or refused: at most 3 titles of at most 120 characters, text only, one line each',
  failed: 'a failed answer shows no Ali Memory line, also when it names memories',
  late: 'a late answer shows no Ali Memory line: the newer answer owns the place',
  silent: 'the Ali Memory line is never spoken: only the answer is',
}, async (t, open) => {
  const ask = async (p, text) => { await p.J().handleInput(text); await p.tick(0); };
  const p = await speakingPage(open, { handlers: { '/chat': () => memoryReply('The kettle wants descaling on Fridays.',
    { used: true, titles: ['Kettle descaling', 'Boiler <b>code</b>'] }) } });
  await ask(p, 'when do I descale the kettle?');
  t.check('line', JSON.stringify(memoryNotes(p)) === JSON.stringify(['From Ali Memory: Kettle descaling · Boiler <b>code</b>'])
    && p.$('answer').textContent === 'The kettle wants descaling on Fridays.' && p.$('answer').hidden === false,
    JSON.stringify(memoryNotes(p)));
  const note = p.$('answer').children.find(child => child.className === 'memory-note');
  t.check('text', Boolean(note) && note.tagName === 'SPAN' && !('innerHTML' in note) && note.children.length === 0,
    note ? Object.keys(note).join(',') : 'no line');
  t.check('silent', JSON.stringify(p.synth.said) === JSON.stringify(['The kettle wants descaling on Fridays.']),
    JSON.stringify(p.synth.said));

  const shows = async memory => {                   // a fresh page, one answer: the Ali Memory lines it shows
    const fresh = await open({ handlers: { '/chat': () => (memory === undefined ? chatReply('Plain.', 5) : memoryReply('Plain.', memory)) } });
    await ask(fresh, 'tell me about the boiler');
    return memoryNotes(fresh);
  };
  const none = [];
  for (const memory of [{ used: false, why: 'off_brain' }, { used: false, titles: ['Boiler'] }, undefined, null,
                        { used: true, titles: [] }]) {
    const got = await shows(memory);
    if (got.length) none.push(`${JSON.stringify(memory)} -> ${JSON.stringify(got)}`);
  }
  t.check('none', !none.length, none.join('; '));
  const odd = [];
  for (const [memory, wanted] of [
    [{ used: true, titles: 'Kettle' }, []], [{ used: 'yes', titles: ['Kettle'] }, []], [{ used: true }, []],
    [{ used: true, titles: [7, null, {}, '   ', 'One\nTwo', 'x'.repeat(300), 'B', 'C', 'D'] },
     ['From Ali Memory: One Two · ' + 'x'.repeat(120) + ' · B']]]) {
    const got = await shows(memory);
    if (JSON.stringify(got) !== JSON.stringify(wanted)) odd.push(`${JSON.stringify(memory).slice(0, 60)} -> ${JSON.stringify(got)}`);
  }
  t.check('odd', !odd.length, odd.join('; '));

  const f = await open({ handlers: { '/chat': () => ({ ok: false, answer: 'It went wrong.', error: 'api_error', nodes: [],
    graph: meta(5), memory: { used: true, titles: ['Kettle descaling'] } }) } });
  await ask(f, 'when do I descale the kettle?');
  t.check('failed', f.$('answer').textContent === 'It went wrong.' && memoryNotes(f).length === 0, JSON.stringify(memoryNotes(f)));

  const read = deferred();                          // the first answer waits for a read of the galaxy; the second comes
  const replies = [memoryReply('Old answer.', { used: true, titles: ['Old memory'] }, 7), chatReply('New answer.', 7)];
  const q = await open({ handlers: { '/chat': () => replies.shift(), 'graph-data.js': () => read.promise } });
  const first = q.J().handleInput('tell me about the kettle');
  await q.tick(0);
  const second = q.J().handleInput('tell me about the boiler');
  await q.tick(0);
  read.resolve(graphFile(galaxy(7, ['A', 'B', 'C'])));
  await first; await second;
  await q.tick(2000);
  t.check('late', q.$('answer').textContent === 'New answer.' && memoryNotes(q).length === 0 && count(q.fetchLog, '/chat') === 2,
    `${q.$('answer').textContent} / ${JSON.stringify(memoryNotes(q))}`);
});

story('the harness reports trouble', {
  thrown: 'a story that made all its checks and then threw is reported as a failure',
  falsy: 'a story that made all its checks and then threw null is reported as a failure too',
  bare: 'a story that threw an object with no prototype (it has no String()) is reported as a failure',
  getter: 'a story that threw a value whose message getter throws is reported as a failure',
  droppedBare: 'a dropped promise that failed with an object with no prototype is a failure of its story',
  late:'a story that made all its checks and then never ended is reported as a failure',
  dropped: 'a promise that the page lets fall is a failure of its story',
  handled: 'a rejection that gets its handler later is no failure',
  expected: 'a rejection that the story expects (t.expectRejection) is no failure',
}, async t => {
  const inner = (body, ms) => runStory({ title: 'inner', checks: { one: 'one' }, run: body }, loaded, ms);
  const failed = results => results.filter(result => !result.ok).map(result => result.name);
  const broken = () => ({ build_id: 'b1', rev: 5, nodes: null, links: [] });       // boot() throws, and nothing catches it
  let got = failed(await inner(async t2 => { t2.check('one', true); throw new Error('boom'); }));
  t.check('thrown', got.length === 1 && /ran to its end/.test(got[0]), JSON.stringify(got));
  got = failed(await inner(async t2 => { t2.check('one', true); throw null; }));
  t.check('falsy', got.length === 1 && /ran to its end/.test(got[0]), JSON.stringify(got));
  got = failed(await inner(async t2 => { t2.check('one', true); throw Object.create(null); }));
  t.check('bare', got.length === 1 && /ran to its end/.test(got[0]), JSON.stringify(got));
  got = failed(await inner(async t2 => { t2.check('one', true); throw { get message() { throw new Error('no message'); } }; }));
  t.check('getter', got.length === 1 && /ran to its end/.test(got[0]), JSON.stringify(got));
  got = failed(await inner(async t2 => {
    Promise.reject(Object.create(null));                // dropped: nothing ever handles it
    await drain();
    t2.check('one', true);
  }));
  t.check('droppedBare', got.length === 1 && /no promise of the page failed/.test(got[0]), JSON.stringify(got));
  got = failed(await inner(async t2 => { t2.check('one', true); await new Promise(() => {}); }, 30));
  t.check('late', got.length === 1 && /ran to its end/.test(got[0]), JSON.stringify(got));
  got = failed(await inner(async (t2, open2) => {
    await open2({ graph: broken() }).catch(() => {});
    t2.check('one', true);
  }));
  t.check('dropped', got.length === 1 && /no promise of the page failed/.test(got[0]), JSON.stringify(got));
  got = failed(await inner(async t2 => {
    const later = Promise.reject(new Error('caught later'));
    await drain();
    await later.catch(() => {});
    t2.check('one', true);
  }));
  t.check('handled', got.length === 0, JSON.stringify(got));
  got = failed(await inner(async (t2, open2) => {
    t2.expectRejection(/reading 'map'/);
    await open2({ graph: broken() }).catch(() => {});
    t2.check('one', true);
  }));
  t.check('expected', got.length === 0, JSON.stringify(got));
});

// ------------------------------------------------------------------ the run

const short = text => String(text).replace(/\s+/g, ' ').trim().slice(0, 160);
// What a thrown or rejected value says (its `message`, or its `stack`), for a line. It never throws itself: a story can
// throw anything (an object with no prototype has no String(), a `message` getter can throw), and its failure must
// still be reported. Whether it threw is told apart (`threw`): a value like null or 0 is a failure too.
const told = (value, part = 'message') => {
  try { return short(value && value[part] || value); } catch (error) { return 'a thrown value that cannot be shown'; }
};
const drain = async () => { for (let i = 0; i < 5; i++) await new Promise(done => setImmediate(done)); };

// A promise that fails with no handler at the end of its turn ("dropped") is a failure of the story that runs then.
// One that gets its handler later (Node says 'rejectionHandled') is not. A story that drops one on purpose says so
// first: t.expectRejection(/its message/). What is left at the end of the run is a failure too.
const dropped = new Map();          // promise -> { story, text }
let running = null;                 // the story that runs now (null: none)
process.on('unhandledRejection', (error, promise) => {
  dropped.set(promise, { story: running, text: told(error), stack: told(error, 'stack') });
});
process.on('rejectionHandled', promise => { dropped.delete(promise); });

// The dropped promises of one story (or of none: `story` null takes them all), taken out of the list.
function takeDropped(story) {
  const taken = [];
  for (const [promise, drop] of dropped) {
    if (story === null || drop.story === story) { dropped.delete(promise); taken.push(drop); }
  }
  return taken;
}

async function runStory(entry, sources, limitMs = STORY_MS) {
  const done = new Set();
  const results = [];
  const expected = [];
  const t = {
    check(key, ok, detail = '') {
      if (!(key in entry.checks)) throw new Error(`the story "${entry.title}" checks an unnamed rule: ${key}`);
      done.add(key);
      results.push({ name: entry.checks[key], ok: Boolean(ok), detail: ok ? '' : detail });
    },
    expectRejection(pattern) { expected.push(pattern); },     // one dropped promise whose message matches is no failure
  };
  const me = { title: entry.title };
  const outer = running;
  running = me;
  const open = async options => {                    // a fresh page that has booted
    const p = page(sources, options);
    await p.tick(0);
    if (!p.J()) throw new Error('the page did not boot: ' + p.consoleLines.join(' | '));
    return p;
  };
  let threw = false;                                  // apart from `trouble`: a story can throw null, 0 or ''
  let trouble = null;
  let limit = null;
  try {
    await Promise.race([entry.run(t, open),
      new Promise((_, fail) => { limit = setTimeout(() => fail(new Error('it did not end: an await never settled')), limitMs); })]);
  } catch (error) {
    threw = true;
    trouble = error;
  } finally {
    clearTimeout(limit);
  }
  await drain();                                      // a promise dropped in the story's last step is reported now
  running = outer;
  const why = () => told(trouble);
  for (const key of Object.keys(entry.checks)) {
    if (!done.has(key)) results.push({ name: entry.checks[key], ok: false, detail: threw ? 'not reached: ' + why() : 'not reached' });
  }
  // A story that threw (whatever it threw) or never ended failed, also when every check it made passed.
  if (threw) results.push({ name: `the story "${entry.title}" ran to its end`, ok: false, detail: why() });
  for (const drop of takeDropped(me)) {
    const wanted = expected.findIndex(pattern => pattern.test(drop.text));
    if (wanted >= 0) { expected.splice(wanted, 1); continue; }
    results.push({ name: `in the story "${entry.title}" no promise of the page failed unseen`, ok: false, detail: drop.text, stack: drop.stack });
  }
  return results;
}

async function main() {
  const args = process.argv.slice(2);
  if (args.includes('--list')) {
    for (const entry of STORIES) for (const name of Object.values(entry.checks)) process.stdout.write(name + '\n');
    return 0;
  }
  const [coreFile, appFile] = args;
  if (!coreFile || !appFile) throw new Error('usage: node page_checks.js <core.js> <app.js> [--list]');
  const sources = loaded = { core: fs.readFileSync(coreFile, 'utf8'), app: fs.readFileSync(appFile, 'utf8') };

  const probe = page(sources);                       // the page must load and boot, or nothing can be checked
  await probe.tick(0);
  await drain();
  if (!probe.J()) {
    const reasons = probe.consoleLines.concat(takeDropped(null).map(drop => drop.text));
    throw new Error('the page did not boot: ' + (reasons.join(' | ') || 'no reason given'));
  }

  const results = [];
  for (const entry of STORIES) results.push(...await runStory(entry, sources));
  await drain();
  for (const drop of takeDropped(null)) {             // dropped outside a story, or after its story had ended
    const where = drop.story ? ` (after the story "${drop.story.title}" had ended)` : '';
    results.push({ name: 'no promise of the page failed unseen' + where, ok: false, detail: drop.text, stack: drop.stack });
  }
  for (const result of results) {                     // for people: where a dropped promise came from
    if (!result.ok && result.stack) process.stderr.write('page_checks: a promise of the page failed: ' + result.stack + '\n');
  }
  const failures = results.filter(result => !result.ok).map(result => short(result.name + (result.detail ? ' -- ' + result.detail : '')));
  process.stdout.write(JSON.stringify({ pass: results.length - failures.length, fail: failures.length, failures }) + '\n');
  return 0;
}

main().then(code => { process.exitCode = code; }, error => {
  process.stderr.write('page_checks: could not run: ' + told(error) + '\n');
  process.exitCode = 2;
});
