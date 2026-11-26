/* Page checks: the rules of the Jarviz page (viewer/app.js), proved in Node without a browser.

   It loads the real core.js and app.js into a fake page: a few fake page elements, a fake 3D graph
   library, a fake microphone, fake timers and a scripted server. Then it plays short stories (a reply
   that comes late, a read of the galaxy that fails, a reader closed too early ...) and checks what the
   page did. Nothing goes to the network, and no file is read except the two that are named.

   preflight runs it:   node page_checks.js viewer/core.js viewer/app.js
   It prints ONE line on stdout, JSON: {"pass": 89, "fail": 0, "failures": []}. The exit code is 0 when it
   ran to its end, also when a rule failed. Any other exit code means it could not run (a file is
   missing, or the page code threw while it loaded); the reason is on stderr.
   For people:          node page_checks.js viewer/core.js viewer/app.js --list   (the name of every check)

   To add a case: add a story(...) below, with a short plain name for each check it makes (the name
   says the rule), and call t.check(key, ok, detail) once for each key. A check that is never reached
   fails. Node's standard library only. */
'use strict';
const fs = require('fs');
const vm = require('vm');

const STORY_MS = 3000;                  // a story that has not ended by then failed (an await never settled)

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
                         'td-shot', 'brain', 'muted'];

// One fresh page. options: graph (the GRAPH on the page), handlers (path -> answer), reduced (reduced motion).
function page(sources, options = {}) {
  let now = 0, nextId = 1, timers = [];
  const setTimer = (fn, ms = 0) => { const id = nextId++; timers.push({ id, at: now + (ms || 0), fn }); return id; };
  const clearTimer = id => { timers = timers.filter(timer => timer.id !== id); };
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
      Promise.resolve().then(() => handler(String(url), init)).then(body => resolve({
        ok: true, status: 200,
        json: async () => (typeof body === 'string' ? JSON.parse(body) : body),
        text: async () => (typeof body === 'string' ? body : JSON.stringify(body)),
      }), reject);
    });
  }

  const consoleLines = [];             // the page's console goes here, never to stdout
  const keep = (...parts) => consoleLines.push(parts.map(String).join(' '));
  const sandbox = {
    console: { log: keep, warn: keep, error: keep, info: keep, debug: keep },
    document, setTimeout: setTimer, clearTimeout: clearTimer, requestAnimationFrame: () => 0,
    performance: { now: () => now }, fetch: fakeFetch, AbortController, URLSearchParams, URL, crypto: globalThis.crypto,
    sessionStorage: { items: {}, getItem(k) { return this.items[k] || null; }, setItem(k, v) { this.items[k] = String(v); } },
    navigator: { language: 'en-GB' }, location: { search: '?mute=1' },       // a muted tab: the page never speaks
    matchMedia: () => ({ matches: Boolean(options.reduced) }), addEventListener() {}, innerWidth: 1400, innerHeight: 900,
    SpeechRecognition: FakeRecognition, ForceGraph3D,
    GRAPH: options.graph || galaxy(5, ['A', 'B', 'C']),
  };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(sources.core, sandbox, { filename: 'core.js' });
  vm.runInContext(sources.app, sandbox, { filename: 'app.js' });
  return { $: id => document.getElementById(id), J: () => sandbox.Jarviz, tick, fetchLog, handlers, recognizers,
           controls, graphLog, consoleLines };
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

// ------------------------------------------------------------------ the run

const short = text => String(text).replace(/\s+/g, ' ').trim().slice(0, 160);

async function runStory(entry, sources) {
  const done = new Set();
  const results = [];
  const t = {
    check(key, ok, detail = '') {
      if (!(key in entry.checks)) throw new Error(`the story "${entry.title}" checks an unnamed rule: ${key}`);
      done.add(key);
      results.push({ name: entry.checks[key], ok: Boolean(ok), detail: ok ? '' : detail });
    },
  };
  const open = async options => {                    // a fresh page that has booted
    const p = page(sources, options);
    await p.tick(0);
    if (!p.J()) throw new Error('the page did not boot: ' + p.consoleLines.join(' | '));
    return p;
  };
  let trouble = null;
  let limit = null;
  try {
    await Promise.race([entry.run(t, open),
      new Promise((_, fail) => { limit = setTimeout(() => fail(new Error('it did not end: an await never settled')), STORY_MS); })]);
  } catch (error) {
    trouble = error;
  } finally {
    clearTimeout(limit);
  }
  for (const key of Object.keys(entry.checks)) {
    if (!done.has(key)) results.push({ name: entry.checks[key], ok: false, detail: trouble ? 'not reached: ' + (trouble.message || trouble) : 'not reached' });
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
  const sources = { core: fs.readFileSync(coreFile, 'utf8'), app: fs.readFileSync(appFile, 'utf8') };

  const probe = page(sources);                       // the page must load and boot, or nothing can be checked
  await probe.tick(0);
  if (!probe.J()) throw new Error('the page did not boot: ' + (probe.consoleLines.concat(rejections).join(' | ') || 'no reason given'));

  const results = [];
  for (const entry of STORIES) results.push(...await runStory(entry, sources));
  const failures = results.filter(result => !result.ok).map(result => short(result.name + (result.detail ? ' -- ' + result.detail : '')));
  process.stdout.write(JSON.stringify({ pass: results.length - failures.length, fail: failures.length, failures }) + '\n');
  return 0;
}

// A promise that the page lets fall must not end the run. It is noted on stderr.
const rejections = [];
process.on('unhandledRejection', error => {
  rejections.push(short(error && error.message || error));
  process.stderr.write('page_checks: a promise of the page failed: ' + short(error && error.stack || error) + '\n');
});

main().then(code => { process.exitCode = code; }, error => {
  process.stderr.write('page_checks: could not run: ' + short(error && error.message || error) + '\n');
  process.exitCode = 2;
});
