/* Jarviz viewer. One page, no build step.

   House rules:
   - Only Focus moves the camera or changes which stars are lit.
   - Only speaker.speak (core.js) makes sound, and ?mute=1 silences it.
   - New notes are born through addNote, nowhere else.
   - Voice routes are checked before the brain. Add a command with router.add. */
(function () {
  'use strict';

  const Core = window.JarvizCore;

  // Pinned on purpose. It MUST be the same three.js release as the one inside the 3d-force-graph
  // bundle in index.html (read window.__THREE__ in the console: "186" means 0.186.0).
  const THREE_URL = 'https://cdn.jsdelivr.net/npm/three@0.186.0/build/three.module.js';

  const FLY_MS = 2200;            // camera flight to a note
  const FLY_DISTANCE = 170;       // how close the camera stops
  const IDLE_SPIN = 0.5;          // slow idle rotation: about one turn in two minutes
  const IDLE_RESUME_MS = 7000;    // rotation starts again this long after the user lets go
  const BIRTH_SETTLE_MS = 900;    // a new star drifts clear of its parent before the camera goes
  const PULSE_MS = 1900;          // the glow pulse of a newborn star
  const ECHO_GUARD_MS = 600;      // ignore the microphone this long after Jarviz stops talking
  const LABEL_CHARS = 44;
  const PALETTE = ['#6ec6ff', '#ffb86b', '#b388ff', '#7dffb5', '#ff7eb6', '#ffe66d',
                   '#64ffda', '#ff8a65', '#9fa8da', '#aed581', '#f48fb1', '#80deea'];
  const LOOKS = {                 // how a star looks in each state
    normal:  { core: 1,    glow: 0.55, halo: 8 },
    lit:     { core: 1,    glow: 0.85, halo: 10 },
    primary: { core: 1,    glow: 1,    halo: 13 },
    dim:     { core: 0.22, glow: 0.07, halo: 6 },
  };
  const REDUCED_MOTION = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const $ = id => document.getElementById(id);
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  };

  let THREE = null;
  let Graph = null;
  let data = { nodes: [], links: [] };
  let graphState = { build_id: '', rev: 0 };
  let neighbours = new Map();
  const groupColours = new Map();
  const state = { speaking: false, thinking: false };

  // ------------------------------------------------------------------ data

  function colourOf(group) {
    if (!groupColours.has(group)) groupColours.set(group, PALETTE[groupColours.size % PALETTE.length]);
    return groupColours.get(group);
  }

  const endId = end => (typeof end === 'object' && end !== null ? end.id : end);

  function reindex() {
    neighbours = new Map(data.nodes.map(node => [node.id, new Set()]));
    for (const link of data.links) {
      const a = endId(link.source), b = endId(link.target);
      if (neighbours.has(a) && neighbours.has(b)) { neighbours.get(a).add(b); neighbours.get(b).add(a); }
    }
  }

  function setData(nodes, links) {
    data = { nodes, links };
    reindex();
    Graph.graphData(data);
    $('count').textContent = `${data.nodes.length} notes indexed`;   // read from the graph, never hardcoded
    renderLegend();
  }

  function renderLegend() {
    const counts = new Map();
    for (const node of data.nodes) counts.set(node.group, (counts.get(node.group) || 0) + 1);
    const rows = [...counts.keys()].sort().map(group => {
      const row = el('span');
      const dot = el('i', 'dot');
      dot.style.color = colourOf(group);
      row.append(dot, group + ' ', el('b', null, String(counts.get(group))));
      return row;
    });
    $('legend').replaceChildren(...rows);
  }

  // ----------------------------------------------------------------- stars

  let glowTexture = null;
  let sphereGeometry = null;

  function makeGlowTexture() {
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 128;
    const ctx = canvas.getContext('2d');
    const gradient = ctx.createRadialGradient(64, 64, 0, 64, 64, 64);
    gradient.addColorStop(0, 'rgba(255,255,255,1)');
    gradient.addColorStop(0.18, 'rgba(255,255,255,0.55)');
    gradient.addColorStop(0.45, 'rgba(255,255,255,0.14)');
    gradient.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, 128, 128);
    return new THREE.CanvasTexture(canvas);
  }

  function makeLabel(text) {
    const shown = text.length > LABEL_CHARS ? text.slice(0, LABEL_CHARS - 1) + '…' : text;
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d');
    const font = '600 40px "Segoe UI", system-ui, sans-serif';
    ctx.font = font;
    canvas.width = Math.ceil(ctx.measureText(shown).width) + 36;
    canvas.height = 64;
    ctx.font = font;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.shadowColor = 'rgba(0,0,0,0.95)';
    ctx.shadowBlur = 9;
    ctx.fillStyle = '#f3f7ff';
    ctx.fillText(shown, canvas.width / 2, 34);
    const texture = new THREE.CanvasTexture(canvas);
    texture.colorSpace = THREE.SRGBColorSpace;
    const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
      map: texture, transparent: true, depthTest: false, depthWrite: false, sizeAttenuation: false,
    }));
    const height = 0.034;                               // a share of the screen, so it reads at any distance
    sprite.scale.set(height * canvas.width / canvas.height, height, 1);
    sprite.center.set(0.5, -0.9);                       // sits just above its star
    sprite.renderOrder = 10;
    sprite.raycast = () => {};
    return sprite;
  }

  function makeStar(node) {
    const colour = colourOf(node.group);
    const size = 2.4 + Math.min((neighbours.get(node.id) || new Set()).size, 12) * 0.32;
    const core = new THREE.Mesh(sphereGeometry, new THREE.MeshBasicMaterial({ color: colour, transparent: true }));
    core.scale.setScalar(size);
    const glow = new THREE.Sprite(new THREE.SpriteMaterial({
      map: glowTexture, color: colour, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    }));
    glow.raycast = () => {};                            // only the core answers clicks
    const group = new THREE.Group();
    group.add(core, glow);
    node.__star = { group, core, glow, size, label: null, pulse: 1 };
    styleStar(node);
    return group;
  }

  function styleStar(node) {
    const star = node.__star;
    if (!star) return;
    const mode = Focus.stateOf(node.id);
    const look = LOOKS[mode];
    star.core.material.opacity = look.core;
    star.glow.material.opacity = Math.min(1, look.glow * (star.pulse > 1 ? 1.6 : 1));
    star.glow.scale.setScalar(star.size * look.halo * star.pulse);
    const named = mode === 'primary' || mode === 'lit';
    if (named && !star.label) { star.label = makeLabel(node.label); star.group.add(star.label); }
    if (star.label) star.label.visible = named;
  }

  function pulse(node) {
    if (REDUCED_MOTION) return;
    const started = performance.now();
    (function frame(now) {
      const t = Math.min(1, (now - started) / PULSE_MS);
      if (node.__star) {
        node.__star.pulse = 1 + 2.6 * Math.pow(1 - t, 2) * (0.75 + 0.25 * Math.cos(t * 18));
        styleStar(node);
      }
      if (t < 1) requestAnimationFrame(frame);
    })(started);
  }

  function addStarfield() {
    const count = 4200;
    const positions = new Float32Array(count * 3);
    const colours = new Float32Array(count * 3);
    for (let i = 0; i < count; i++) {
      const radius = 1800 + Math.random() * 5200;
      const u = Math.random() * 2 - 1, angle = Math.random() * Math.PI * 2, ring = Math.sqrt(1 - u * u);
      positions.set([radius * ring * Math.cos(angle), radius * u, radius * ring * Math.sin(angle)], i * 3);
      const warm = Math.random(), bright = 0.4 + Math.random() * 0.6;
      colours.set([bright * (0.82 + 0.18 * warm), bright * 0.88, bright * (1 - 0.25 * warm)], i * 3);
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    geometry.setAttribute('color', new THREE.BufferAttribute(colours, 3));
    const stars = new THREE.Points(geometry, new THREE.PointsMaterial({
      size: 1.7, sizeAttenuation: false, vertexColors: true, transparent: true, opacity: 0.9, depthWrite: false,
    }));
    stars.raycast = () => {};
    Graph.scene().add(stars);
  }

  // ----------------------------------------------------------------- panel

  // Note text -> page elements, for the note panel and for the teardown reader.
  // Only text nodes and a few plain elements are made: no HTML from a note is ever run.
  const Markdown = (() => {
    function findByLabel(label) {
      const wanted = label.trim().toLowerCase();
      return data.nodes.find(node => node.label.toLowerCase() === wanted) || null;
    }

    // [[wikilinks]] become buttons. Everything else stays plain text.
    function inline(parent, text, onLink) {
      let last = 0;
      for (const match of text.matchAll(/\[\[([^\[\]|#]+)(?:#[^\[\]|]*)?(?:\|([^\[\]]*))?\]\]/g)) {
        parent.append(text.slice(last, match.index));
        const target = findByLabel(match[1]);
        const shown = (match[2] || match[1]).trim();
        if (target) {
          const link = el('button', 'wikilink', shown);
          link.type = 'button';
          link.addEventListener('click', () => { if (onLink) onLink(target); Focus.focusNode(target.id); });
          parent.append(link);
        } else {
          parent.append(shown);
        }
        last = match.index + match[0].length;
      }
      parent.append(text.slice(last));
    }

    // options: heading(level) -> tag name, image(src, alt) -> element or null, onLink(node)
    function render(body, text, options = {}) {
      const headingTag = options.heading || (() => 'h4');
      body.replaceChildren();
      let list = null;
      for (const raw of String(text || '').split('\n')) {
        const line = raw.trim().replace(/\*\*/g, '').replace(/`/g, '');
        if (!line || /^-{3,}$/.test(line)) { list = null; continue; }
        const image = line.match(/^!\[([^\]]*)\]\(([^)\s]+)\)$/);
        if (image) {                                    // a picture line: shown only where a reader asks for it
          list = null;
          const picture = options.image ? options.image(image[2], image[1]) : null;
          if (picture) body.append(picture);
          continue;
        }
        const bullet = line.match(/^[-*]\s+(.*)$/);
        const numbered = line.match(/^\d+[.)]\s+(.*)$/);
        const heading = line.match(/^(#{1,6})\s+(.*)$/);
        const quote = line.match(/^>\s?(.*)$/);
        if (bullet || numbered) {
          const tag = bullet ? 'ul' : 'ol';
          if (!list || list.tagName.toLowerCase() !== tag) { list = el(tag); body.append(list); }
          const item = el('li');
          inline(item, (bullet || numbered)[1], options.onLink);
          list.append(item);
          continue;
        }
        list = null;
        const block = el(heading ? headingTag(heading[1].length) : quote ? 'blockquote' : 'p');
        inline(block, heading ? heading[2] : quote ? quote[1] : line, options.onLink);
        body.append(block);
      }
    }

    return { render };
  })();

  const Panel = (() => {
    const renderBody = text => Markdown.render($('panel-body'), text);

    function chips(nodes) {
      $('panel-links').replaceChildren(...nodes.map(node => {
        const dot = el('i', 'dot');
        dot.style.color = colourOf(node.group);
        const button = el('button');
        button.type = 'button';
        button.append(dot, node.label);
        button.addEventListener('click', () => Focus.focusNode(node.id));
        const item = el('li');
        item.append(button);
        return item;
      }));
    }

    function head(group, colour, title) {
      const dot = el('i', 'dot');
      dot.style.color = colour;
      $('panel-group').replaceChildren(dot, group);
      $('panel-title').textContent = title;
      $('panel').hidden = false;
      $('panel').scrollTop = 0;
    }

    function showNote(node) {
      head(node.group, colourOf(node.group), node.label);
      renderBody(node.excerpt);
      const linked = [...(neighbours.get(node.id) || [])].map(id => data.nodes[id]).filter(Boolean)
        .sort((a, b) => a.label.localeCompare(b.label));
      $('panel-links-title').textContent = 'Linked notes';
      $('panel-links-title').hidden = !linked.length;
      chips(linked);
    }

    function showCluster(nodes) {
      head('cluster', '#ffd98a', `${nodes.length} notes in this answer`);
      $('panel-body').replaceChildren(el('p', null, 'The answer came from several notes. They are all lit. Pick one to fly to it.'));
      $('panel-links-title').textContent = 'Sources';
      $('panel-links-title').hidden = false;
      chips(nodes);
    }

    return { showNote, showCluster, hide: () => { $('panel').hidden = true; } };
  })();

  // ----------------------------------------------------------------- focus
  // THE ONE OWNER of the camera and of which stars are lit. Every feature that wants
  // attention on a note calls these functions. Nothing else touches the camera.

  const Focus = (() => {
    let lit = new Set();
    let primary = null;
    let pinned = null;
    let resumeTimer = null;

    function stateOf(id) {
      if (!lit.size) return 'normal';
      if (id === primary) return 'primary';
      return lit.has(id) ? 'lit' : 'dim';
    }

    const linkLit = link => lit.size > 0 && lit.has(endId(link.source)) && lit.has(endId(link.target));

    function paint() {
      for (const node of data.nodes) styleStar(node);
      Graph.linkColor(Graph.linkColor()).linkWidth(Graph.linkWidth())
        .linkDirectionalParticles(Graph.linkDirectionalParticles());
    }

    function spin(on) {
      const controls = Graph.controls();
      if (controls && 'autoRotate' in controls) controls.autoRotate = on && !REDUCED_MOTION;
    }

    function rest(ms) {                       // hold the idle rotation still for a while
      clearTimeout(resumeTimer);
      spin(false);
      resumeTimer = setTimeout(() => spin(true), ms);
    }

    function pin(node) {
      unpin();
      if (!node || !Number.isFinite(node.x)) return;
      node.fx = node.x; node.fy = node.y; node.fz = node.z;
      pinned = node;
    }

    function unpin() {
      if (pinned) { delete pinned.fx; delete pinned.fy; delete pinned.fz; }
      pinned = null;
    }

    function flyTo(node) {
      if (!Number.isFinite(node.x)) return;   // the galaxy has not placed this star yet
      const camera = Graph.camera().position;
      const dx = camera.x - node.x, dy = camera.y - node.y, dz = camera.z - node.z;
      const scale = FLY_DISTANCE / (Math.hypot(dx, dy, dz) || 1);
      const ms = REDUCED_MOTION ? 0 : FLY_MS;
      rest(ms + IDLE_RESUME_MS);
      Graph.cameraPosition(
        { x: node.x + dx * scale, y: node.y + dy * scale, z: node.z + dz * scale },
        { x: node.x, y: node.y, z: node.z }, ms);
    }

    // Fly to one note, light it and its direct neighbours (and any other sources), open its panel.
    function focusNode(id, also = []) {
      const node = data.nodes[id];
      if (!node) return false;
      primary = id;
      lit = new Set([id, ...(neighbours.get(id) || []), ...also.filter(other => data.nodes[other])]);
      pin(node);
      paint();
      flyTo(node);
      Panel.showNote(node);
      return true;
    }

    // Four or more sources: light them all and fly nowhere.
    function lightCluster(ids) {
      const valid = ids.filter(id => data.nodes[id]);
      if (!valid.length) return false;
      primary = null;
      lit = new Set(valid);
      unpin();
      paint();
      Panel.showCluster(valid.map(id => data.nodes[id]));
      return true;
    }

    function clear() {
      primary = null;
      lit = new Set();
      unpin();
      paint();
      Panel.hide();
    }

    function apply(plan) {                    // the answer path: see Core.planFocus
      if (plan.mode === 'fly') return focusNode(plan.target, plan.lit);
      if (plan.mode === 'cluster') return lightCluster(plan.lit);
      return false;                           // small talk: the galaxy holds still
    }

    function start() {                        // where the camera starts, and the slow idle rotation
      Graph.cameraPosition({ x: 0, y: 70, z: 560 });
      Graph.controls().autoRotateSpeed = IDLE_SPIN;
      spin(true);
    }

    function frame() {                        // the opening shot
      rest(1600 + IDLE_RESUME_MS / 2);
      Graph.zoomToFit(REDUCED_MOTION ? 0 : 1600, 110);
    }

    return {
      stateOf, linkLit, focusNode, lightCluster, clear, apply, start, frame,
      grabbed: () => { clearTimeout(resumeTimer); spin(false); },
      released: () => rest(IDLE_RESUME_MS),
      current: () => ({ primary, lit: [...lit] }),
    };
  })();

  // ----------------------------------------------------------------- graph

  function initGraph() {
    Graph = new ForceGraph3D($('graph'), { controlType: 'orbit' })
      .backgroundColor('#01020a')
      .showNavInfo(false)
      .nodeLabel(node => Core.escapeHtml(node.label))
      .nodeRelSize(4)
      .nodeColor(node => colourOf(node.group))
      .nodeOpacity(0.95)
      .linkColor(link => (Focus.linkLit(link) ? '#ffe7a8' : '#5d7fc4'))
      .linkOpacity(0.24)
      .linkWidth(link => (Focus.linkLit(link) ? 0.5 : 0))
      .linkDirectionalParticles(link => (Focus.linkLit(link) ? 2 : 0))
      .linkDirectionalParticleWidth(1.3)
      .linkDirectionalParticleSpeed(0.007)
      .onNodeClick(node => Focus.focusNode(node.id))
      .onBackgroundClick(() => Focus.clear());

    if (THREE) {
      glowTexture = makeGlowTexture();
      sphereGeometry = new THREE.SphereGeometry(1, 20, 14);
      Graph.nodeThreeObject(makeStar);
      addStarfield();
    }
    Graph.d3Force('charge').strength(-75);
    Graph.d3Force('link').distance(40);

    Focus.start();
    const controls = Graph.controls();
    controls.addEventListener('start', Focus.grabbed);
    controls.addEventListener('end', Focus.released);

    let framed = false;
    Graph.onEngineStop(() => { if (!framed && !Focus.current().lit.length) Focus.frame(); framed = true; });
    window.addEventListener('resize', () => Graph.width(window.innerWidth).height(window.innerHeight));
  }

  // ONE DOOR for new notes on the viewer side: birth, link, pulse, fly.
  // Captures and teardowns both use it.
  function addNote(receipt, options = {}) {
    let node = data.nodes[receipt.index];
    if (!node) {
      if (receipt.index !== data.nodes.length) return null;      // a new star takes the next place: its id is its index
      const parent = data.nodes[receipt.related];
      const origin = parent && Number.isFinite(parent.x) ? parent : { x: 0, y: 0, z: 0 };
      const nudge = () => (Math.random() - 0.5) * 6;
      node = { ...receipt.node, x: origin.x + nudge(), y: origin.y + nudge(), z: origin.z + nudge() };
      setData([...data.nodes, node], [...data.links, ...receipt.links.map(link => ({ ...link }))]);
    }
    if (options.quiet) return node;
    pulse(node);
    setTimeout(() => Focus.focusNode(node.id), REDUCED_MOTION ? 0 : BIRTH_SETTLE_MS);
    return node;
  }

  function removeNote(index) {
    if (index !== data.nodes.length - 1) return;      // only the newest star can be taken away
    const current = Focus.current();
    if (current.primary === index || current.lit.includes(index)) Focus.clear();
    setData(data.nodes.slice(0, -1),
            data.links.filter(link => endId(link.source) !== index && endId(link.target) !== index));
  }

  // Another tab (or build.py) changed the galaxy: read the file again and keep the stars in place.
  async function readGraph() {
    const response = await fetch('graph-data.js?fresh=' + Date.now(), { cache: 'no-store' });
    const text = await response.text();
    const fresh = JSON.parse(text.slice(text.indexOf('{'), text.lastIndexOf('}') + 1));
    const nodes = fresh.nodes.map(node => {
      const was = data.nodes[node.id];
      return was && was.label === node.label ? Object.assign(was, node) : { ...node };
    });
    Focus.clear();
    setData(nodes, fresh.links.map(link => ({ ...link })));
    graphState = { build_id: fresh.build_id, rev: fresh.rev };      // the numbers of the file now on screen
  }

  // One read at a time: a caller that comes while a read is in the air gets that read.
  // So an older read can never land after a newer one, and clear the focus of a newer answer.
  let reading = null;
  function reloadGraph() {
    if (!reading) reading = readGraph().finally(() => { reading = null; });
    return reading;
  }

  // `births` = how many notes this reply added or took away (the server counts one step for each).
  // It says what it found:
  //   'base'      the page was right before the reply: the reply's change is applied here
  //   'reloaded'  the file was read again: it already holds the truth, nothing is replayed on top
  //   'failed'    the read failed: change nothing, and keep the old numbers so the next reply reads again
  async function syncGraph(meta, births = 0) {
    if (!meta) return 'base';
    while (reading) await reading.catch(() => {});     // a read in the air first: its numbers decide
    if (meta.build_id === graphState.build_id && meta.rev === graphState.rev + births) {
      graphState = { build_id: meta.build_id, rev: meta.rev };
      return 'base';
    }
    try {
      await reloadGraph();
    } catch (error) {
      console.warn('Could not refresh the galaxy', error);
      return 'failed';
    }
    return 'reloaded';
  }

  // A reply's new notes, by what syncGraph found. 'base': they are born here, in order. 'reloaded': the
  // file already holds them, so only the newest is pulsed and focused, and only when its place holds the
  // same note. 'failed': the galaxy stays as it is.
  function placeBirths(receipts, found, quiet = false) {
    if (found === 'base') {
      receipts.forEach((receipt, index) => addNote(receipt, { quiet: quiet || index < receipts.length - 1 }));
      return;
    }
    const newest = receipts[receipts.length - 1];
    const there = found === 'reloaded' && newest ? data.nodes[newest.index] : null;
    if (there && there.label === newest.node.label) addNote(newest, { quiet });
  }

  // ------------------------------------------------------- status and text

  function setStatus(name) {
    $('status').dataset.state = name;
    $('status').lastElementChild.textContent = name;
  }

  function settle() {
    if (state.speaking) return setStatus('speaking');
    if (state.thinking) return setStatus('thinking');
    setStatus(mic.on ? 'listening' : 'idle');
  }

  function showHeard(text) {
    $('heard').textContent = text ? '“' + text + '”' : '';
    $('heard').hidden = !text;
  }

  function showBrain(brain) {                 // which brain answers, on screen at all times
    if (!brain || !brain.label) return;
    $('brain').textContent = `brain: ${brain.label} · ${brain.model}`;
    $('brain').classList.toggle('warn', brain.has_key === false);
    $('brain').hidden = false;
  }

  // What the page says by itself. The words belong to the persona block in server.py (PAGE_LINES)
  // and come with /status. These plain ones have no character: they are used only until the server
  // has answered once.
  const lines = {
    hello: 'Hello.',
    unreachable: 'I cannot reach the server. Is server.py still running?',
    remember_unreachable: 'That note did not save. I could not reach the server.',
    nothing_to_forget: 'There is no new note of yours to forget.',
    forget_unreachable: 'I could not remove that note. I could not reach the server.',
    brain_unreachable: 'I could not switch brains. I could not reach the server.',
    teardown_unreachable: 'I could not start the teardown. I could not reach the server.',
    cancel_unreachable: 'I could not cancel it. I could not reach the server.',
    teardown_missing: 'I cannot find that teardown on file.',
  };

  async function readStatus() {
    const response = await fetch('/status', { cache: 'no-store' });
    const status = await response.json();
    if (status.ok === false) return status;         // the page has lost its key: the caller shows status.answer
    syncGraph(status.graph);                        // the file on screen may be older than the galaxy: then it is read again
    showBrain(status.active);
    const spoken = status.lines || {};
    for (const name of Object.keys(lines)) {
      if (typeof spoken[name] === 'string' && spoken[name]) lines[name] = spoken[name];
    }
    return status;
  }

  function showAnswer(text, trouble) {
    $('answer').textContent = text;
    $('answer').hidden = !text;
    $('answer').classList.toggle('trouble', Boolean(trouble));
  }

  function say(text, trouble) {
    showAnswer(text, trouble);
    speaker.speak(text);
    settle();
  }

  // ----------------------------------------------------------------- voice

  const speaker = Core.createSpeaker({
    synth: window.speechSynthesis,
    Utterance: window.SpeechSynthesisUtterance,
    getSearch: () => window.location.search,
    pickVoice: () => Core.pickVoice(window.speechSynthesis ? window.speechSynthesis.getVoices() : []),
    onStart: () => { state.speaking = true; settle(); },
    onEnd: () => { state.speaking = false; mic.deafUntil = performance.now() + ECHO_GUARD_MS; settle(); },
  });

  const mic = { on: false, rec: null, deafUntil: 0, failures: 0 };
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;

  // FINISH_MS (top of core.js) is the pause that ends a spoken sentence.
  const buffer = Core.createFinishBuffer({
    onSentence: sentence => handleInput(sentence),
    onInterrupt: () => interrupt(),
  });

  // While Jarviz talks the microphone hears him too. Only "stop" and "wait" get through.
  const deaf = () => state.speaking || performance.now() < mic.deafUntil;

  function onSpeech(event) {
    mic.failures = 0;
    let interim = '';
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const text = event.results[i][0].transcript.trim();
      if (!text) continue;
      if (!event.results[i].isFinal) { interim += text + ' '; continue; }
      if (deaf()) { if (Core.interruptWord(text)) interrupt(); continue; }
      buffer.push(text);
      showHeard(buffer.pending());
    }
    interim = interim.trim();
    if (!interim) return;
    if (deaf()) { if (Core.interruptWord(interim)) interrupt(); return; }
    buffer.touch();
    showHeard((buffer.pending() + ' ' + interim).trim() + ' …');
  }

  // Only the live session acts. A stopped or replaced recognizer can still send a result from audio it
  // already had: it is dropped, so "remember that ..." never saves a note after the microphone is off.
  const live = rec => mic.on && mic.rec === rec;

  function micStart() {
    if (!Recognition) return;
    const rec = new Recognition();
    rec.continuous = true;
    rec.interimResults = true;
    rec.lang = /^en/i.test(navigator.language || '') ? navigator.language : 'en-GB';
    rec.onresult = event => { if (live(rec)) onSpeech(event); };
    rec.onerror = event => {
      if (!live(rec)) return;
      if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
        micStop();
        showAnswer('The microphone is blocked. Allow it with the icon in the address bar, then press the button again.', true);
      } else if (event.error !== 'no-speech' && event.error !== 'aborted') {
        mic.failures += 1;
      }
    };
    rec.onend = () => {                               // Chrome ends a session after some silence: start again
      if (!live(rec)) return;
      if (mic.failures >= 5) {
        micStop();
        return showAnswer('Speech recognition keeps failing. Check the internet connection, then press the button again.', true);
      }
      setTimeout(() => { if (live(rec)) { try { rec.start(); } catch (error) { micStart(); } } }, 250);
    };
    mic.rec = rec;
    mic.on = true;
    try { rec.start(); } catch (error) { /* already started */ }
    $('mic').setAttribute('aria-pressed', 'true');
    settle();
  }

  function micStop() {
    const rec = mic.rec;
    mic.on = false;                                   // not live any more BEFORE it stops: a late result is dropped
    mic.rec = null;
    mic.failures = 0;
    buffer.cancel();
    if (rec) { try { rec.stop(); } catch (error) { /* not running */ } }
    $('mic').setAttribute('aria-pressed', 'false');
    showHeard('');
    settle();
  }

  // ----------------------------------------------------------------- brain

  const SESSION = (() => {
    const fresh = () => (window.crypto && crypto.randomUUID ? crypto.randomUUID() : 'tab-' + Math.random().toString(36).slice(2));
    try {
      let id = sessionStorage.getItem('jarviz-session');
      if (!id) { id = fresh(); sessionStorage.setItem('jarviz-session', id); }
      return id;
    } catch (error) {
      return fresh();
    }
  })();

  let inFlight = null;
  let lastCapture = null;
  let touched = false;          // the user has said or typed something: a late greeting must not cover the answer

  async function post(path, body) {
    if (inFlight) inFlight.abort();                   // a new sentence replaces the one still in the air
    const controller = inFlight = new AbortController();
    state.thinking = true;
    settle();
    try {
      const response = await fetch(path, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body), signal: controller.signal,
      });
      return await response.json();
    } finally {
      if (inFlight === controller) { inFlight = null; state.thinking = false; settle(); }
    }
  }

  let sends = 0;                // every send gets a number: only the newest one owns the answer, the focus and the card

  async function send(path, body, unreachable) {
    const mine = ++sends;
    let reply;
    try {
      reply = await post(path, body);
    } catch (error) {
      if (error.name !== 'AbortError') say(unreachable, true);    // never fail silently
      return;
    }
    await handleReply(reply, mine);
  }

  async function handleReply(reply, mine) {
    const action = reply.ok === false ? null : reply.action || null;
    const type = action ? action.type : null;
    const found = await syncGraph(reply.graph, type === 'remember' || type === 'forget' ? 1 : 0);
    // A newer sentence started while this one waited: it owns the answer. This reply only mends the galaxy.
    const owner = mine === sends;
    if (owner) say(reply.answer || '', reply.ok === false);   // also after a failed read: the save did happen
    if (owner && reply.error === 'failed') Teardown.failureTold();   // the cancel reply said why the job failed
    if (reply.brain && reply.ok !== false) showBrain(reply.brain);
    if (type === 'remember') {
      lastCapture = { ...action.receipt, build: reply.graph && reply.graph.build_id };   // the galaxy it came from
      placeBirths([action.receipt], found, !owner);
    } else if (type === 'forget') {
      if (found === 'base') removeNote(action.index);  // after a new read the file already holds the truth
      lastCapture = null;
    } else if (type === 'teardown') {
      Teardown.follow(action.job, { track: !owner });  // a teardown started: the card shows its steps (late: at its end)
    } else if (type === 'teardown_cancel') {
      Teardown.cancelled();                           // noted: the job's end stays quiet, the card is not touched
    } else if (!owner) {
      return;                     // a late answer moves no camera and changes no card; a teardown above is only tracked
    } else {
      Focus.apply(Core.planFocus(reply.nodes));
    }
  }

  // A test tab with ?headless=1 runs the teardown browser with no window, so a test never pops up on screen.
  const HIDDEN_BROWSER = /[?&]headless=1(?:&|$)/.test(window.location.search);

  const askBrain = text => send('/chat', { message: text, session: SESSION, headless: HIDDEN_BROWSER },
    lines.unreachable);

  const remember = fact => send('/remember', { text: fact, session: SESSION }, lines.remember_unreachable);

  function forgetLast() {
    if (!lastCapture) return say(lines.nothing_to_forget);
    const body = { index: lastCapture.index, label: lastCapture.title, session: SESSION };
    if (typeof lastCapture.build === 'string' && lastCapture.build) body.build = lastCapture.build;
    return send('/forget', body, lines.forget_unreachable);
  }

  const switchBrain = name => send('/brain', { name }, lines.brain_unreachable);

  async function whichBrain() {
    try {
      const status = await readStatus();
      if (status.ok === false) return say(status.answer || lines.unreachable, true);
      say(status.say);
    } catch (error) {
      say(lines.unreachable, true);
    }
  }

  function interrupt() {
    buffer.cancel();
    speaker.stop();
    if (inFlight) inFlight.abort();
    showHeard('');
    settle();
  }

  // -------------------------------------------------------------- teardowns
  // The card (top left) shows one teardown: its steps while it runs, then what was found.
  // The reader shows the whole note. The server does the work (teardown.py); this only watches.

  const Teardown = (() => {
    const POLL_MS = 700;
    const born = new Set();     // jobs whose notes this tab already put in the galaxy
    let following = null;       // the id of the job this tab watches
    let timer = null;
    let misses = 0;
    let card = null;            // the finished teardown on the card
    let quietEnd = false;       // the user cancelled: Jarviz already said so
    let failTold = false;       // the cancel reply already said why the followed job failed
    let round = 0;              // goes up at each follow(): a status answer from an older round is dropped
    let tracked = false;        // the job is only tracked (a late reply): the card shows it at its end, not before
    let opening = 0;            // the newest request to open the reader: closeReader drops it

    const fetchJson = async path => (await fetch(path, { cache: 'no-store' })).json();
    const imageUrl = name => '/teardown/image?name=' + encodeURIComponent(name);

    function paintSteps(job) {
      const list = $('td-steps');
      if (list.children.length !== job.steps.length) list.replaceChildren(...job.steps.map(text => el('li', null, text)));
      [...list.children].forEach((item, index) => {
        const number = index + 1;
        item.dataset.state = number < job.step || job.state === 'done' ? 'done'
          : number > job.step ? 'todo' : job.state === 'running' ? 'now' : 'stopped';
      });
    }

    function paintJob(job, line) {                    // running, or stopped before the end
      card = null;
      $('td-title').textContent = job.label;
      paintSteps(job);
      $('td-steps').hidden = false;
      $('td-done').hidden = true;
      $('td-line').textContent = line || '';
      $('td-line').hidden = !line;
      $('td-line').classList.toggle('trouble', Boolean(line));
      $('teardown').hidden = false;
      fit();
    }

    function paintCard(found, line) {                 // done: one line, the snapshot, the stack, the totals
      card = found;
      $('td-title').textContent = found.label;
      $('td-steps').hidden = true;
      $('td-line').textContent = line;
      $('td-line').hidden = false;
      $('td-line').classList.remove('trouble');
      const shot = $('td-shot');
      shot.dataset.name = found.snapshot || '';
      if (found.snapshot) { shot.src = imageUrl(found.snapshot); shot.alt = 'Snapshot of ' + found.host; } else { shot.removeAttribute('src'); }
      $('td-chips').replaceChildren(...(found.stack || []).slice(0, 10).map(name => el('span', 'chip', name)));
      $('td-totals').textContent = found.totals || '';
      $('td-done').hidden = false;
      $('teardown').hidden = false;
      fit();
    }

    // Never cover the answer, the note panel or the folder list: measure them and shrink to fit.
    // The snapshot is the first thing to go.
    function fit() {
      const box = $('teardown');
      if (!box.hidden) {
        const top = Math.round($('hud').getBoundingClientRect().bottom + 10);
        box.style.top = top + 'px';
        const mine = box.getBoundingClientRect();
        let floor = window.innerHeight - 12;
        for (const id of ['legend', 'heard', 'answer', 'ask', 'hint', 'panel']) {
          const other = $(id);
          if (!other || other.hidden) continue;
          const rect = other.getBoundingClientRect();
          if (!rect.width || !rect.height || rect.bottom <= top) continue;
          if (rect.left < mine.right && rect.right > mine.left) floor = Math.min(floor, Math.max(top, rect.top) - 10);
        }
        const room = Math.max(96, floor - top);
        const shot = $('td-shot');
        shot.hidden = !shot.dataset.name;
        box.style.maxHeight = room + 'px';
        if (!shot.hidden && box.scrollHeight > room + 1) shot.hidden = true;
      }
      const reader = $('reader');
      if (!reader.hidden) {
        const panel = $('panel');
        let left = box.hidden ? 18 : Math.round(box.getBoundingClientRect().right + 12);
        let right = panel.hidden ? 18 : Math.round(window.innerWidth - panel.getBoundingClientRect().left + 12);
        if (window.innerWidth - left - right < 420) { left = 12; right = 12; }     // a small screen: use the width
        const hud = $('hud').getBoundingClientRect();
        reader.style.top = (hud.right > left ? Math.round(hud.bottom + 10) : 16) + 'px';      // not over the name line
        reader.style.left = left + 'px';
        reader.style.right = right + 'px';
        reader.style.bottom = Math.max(12, Math.round(window.innerHeight - $('dock').getBoundingClientRect().top + 10)) + 'px';
      }
    }

    async function finish(job, meta) {
      following = null;
      tracked = false;                                // the end is shown, also for a job that was only tracked
      const told = (quietEnd && job.state === 'cancelled') || (failTold && job.state === 'failed');
      failTold = false;
      if (job.state !== 'done') {
        paintJob(job, job.state === 'cancelled' ? 'Cancelled.' : 'It stopped at this step.');
        if (!told) say(job.say || '', true);
        quietEnd = false;
        // A note the server could not take back is still in the file. When the numbers moved, read the
        // galaxy again. The receipts of a job that failed are never replayed as births.
        await syncGraph(meta);
        return;
      }
      const result = job.result;
      if (!born.has(job.id)) {
        born.add(job.id);
        // The same door as a capture: REA first if it is new, then the teardown, and the camera flies to it.
        placeBirths(result.births, await syncGraph(meta, result.births.length));
      }
      paintCard(result, `Taken apart in ${result.seconds} s`);
      say(result.spoken);
    }

    async function poll() {
      timer = null;
      const mine = round;                             // the follow() this request belongs to
      let status;
      try {
        status = await fetchJson('/teardown/status');
      } catch (error) {
        if (mine !== round) return;                   // follow() started a new watch meanwhile, with its own poll
        misses += 1;
        if (misses > 8) { following = null; return say(lines.unreachable, true); }
        timer = setTimeout(poll, POLL_MS * 3);
        return;
      }
      if (mine !== round) return;                     // an answer about the job this tab watched before
      misses = 0;
      if (status.ok === false) { following = null; return say(status.answer || lines.unreachable, true); }
      const job = status.job;
      if (!job || job.id !== following) { following = null; return; }
      if (job.state === 'running') {
        if (!tracked) paintJob(job);
        timer = setTimeout(poll, POLL_MS);
        return;
      }
      await finish(job, status.graph);
    }

    // Watch a job: its end, its new notes and its result arrive. `track` (a late reply): the reader and
    // the card stay as they are, so the newer sentence keeps the screen. The card shows the job at its end.
    function follow(job, options = {}) {
      if (!job || !job.id) return;
      round += 1;
      failTold = false;
      following = job.id;
      misses = 0;
      tracked = Boolean(options.track);
      if (!tracked) { closeReader(); paintJob(job); }
      clearTimeout(timer);
      timer = setTimeout(poll, POLL_MS);
    }

    async function resume() {                         // the page was reloaded while a teardown was running
      try {
        const status = await fetchJson('/teardown/status');
        if (status.job && status.job.state === 'running') follow(status.job);
      } catch (error) { /* the card stays away */ }
    }

    const start = hit => send('/teardown/start',
      { site: hit.site, question: hit.question, session: SESSION, headless: HIDDEN_BROWSER },
      lines.teardown_unreachable);

    const cancel = () => send('/teardown/cancel', { session: SESSION }, lines.cancel_unreachable);

    async function loadLast(wanted = () => true) {    // `wanted` false: the request was dropped, the card stays shut
      const reply = await fetchJson('/teardown/last');
      if (reply.card && wanted()) paintCard(reply.card, reply.card.seconds ? `Taken apart in ${reply.card.seconds} s` : 'On file');
      return reply;
    }

    async function showLast() {                       // "show me the last teardown": the newest note on disk
      try {
        say((await loadLast()).answer || '');
      } catch (error) {
        say(lines.unreachable, true);
      }
    }

    function closeReader() {
      opening += 1;                                   // a note still on its way does not open the reader again
      $('reader').hidden = true;
      $('reader-body').replaceChildren();
    }

    function picture(source, alt) {                   // only a snapshot from the teardowns folder
      const match = String(source).match(/^assets\/([a-z0-9][a-z0-9.-]*\.png)$/);
      if (!match) return null;
      const image = el('img');
      image.alt = alt || '';
      image.src = imageUrl(match[1]);
      return image;
    }

    async function openReader() {                     // "open the full teardown"
      const mine = ++opening;
      const wanted = () => mine === opening;          // false once the reader was closed (or opened again) meanwhile
      try {
        if (!card) {
          const reply = await loadLast(wanted);
          if (!wanted()) return;
          if (!reply.card) return say(reply.answer || '');
        }
        const note = await fetchJson('/teardown/note?name=' + encodeURIComponent(card.name));
        if (!wanted()) return;
        if (!note.ok) return say(note.answer || lines.teardown_missing, true);
        $('reader-title').textContent = note.label;
        const text = String(note.text).replace(/^#\s+.*\n+/, '');         // the title is already on top
        Markdown.render($('reader-body'), text, {
          heading: level => (level <= 2 ? 'h3' : 'h4'), image: picture, onLink: () => closeReader(),
        });
        const link = $('reader-obsidian');
        const usable = typeof note.obsidian === 'string' && note.obsidian.startsWith('obsidian://open?path=');
        link.hidden = !usable;
        if (usable) link.href = note.obsidian; else link.removeAttribute('href');
        $('reader').hidden = false;
        $('reader-body').scrollTop = 0;
        fit();
      } catch (error) {
        if (wanted()) say(lines.unreachable, true);
      }
    }

    function close() {                                // "close the teardown": the reader first, then the card
      if (!$('reader').hidden) return closeReader();
      closeReader();                                  // a reader still on its way stays shut too
      $('teardown').hidden = true;
    }

    return { start, cancel, follow, resume, showLast, openReader, closeReader, close, fit,
             cancelled: () => { quietEnd = true; }, failureTold: () => { if (following) failTold = true; },
             current: () => ({ following, card }) };
  })();

  // Voice routes: checked in this order BEFORE a sentence goes to the brain.
  const router = Core.createRouter()
    .add('interrupt', text => Core.interruptWord(text), () => interrupt())
    .add('remember', text => Core.matchRemember(text), fact => remember(fact))
    .add('switch-brain', text => Core.matchBrainSwitch(text), name => switchBrain(name))
    .add('which-brain', /^(?:which|what)\s+brain\b/i, () => whichBrain())
    .add('forget-last', /^(?:forget|undo|scratch|delete)\s+(?:that|the last (?:note|one|capture))[.!]?$/i, () => forgetLast())
    .add('close-panel', /^(?:close|hide)\s+(?:the\s+)?(?:panel|note)[.!]?$/i, () => Focus.clear())
    .add('teardown-last', Core.TEARDOWN_COMMANDS.last, () => Teardown.showLast())
    .add('teardown-open', Core.TEARDOWN_COMMANDS.open, () => Teardown.openReader())
    .add('teardown-close', Core.TEARDOWN_COMMANDS.close, () => Teardown.close())
    .add('teardown-cancel', Core.TEARDOWN_COMMANDS.cancel, () => Teardown.cancel())
    .add('teardown', text => Core.matchTeardown(text), hit => Teardown.start(hit));     // only a sentence that names a site

  function handleInput(raw) {
    const text = String(raw || '').replace(/\s+/g, ' ').trim();
    if (!text) return null;
    touched = true;
    showHeard(text);
    const route = router.match(text);
    return route ? route.handler(route.hit, text) : askBrain(text);
  }

  // ------------------------------------------------------------------ boot

  function wire() {
    $('ask').addEventListener('submit', event => {
      event.preventDefault();
      const text = $('q').value;
      $('q').value = '';
      handleInput(text);
    });
    $('mic').addEventListener('click', () => (mic.on ? micStop() : micStart()));
    if (!Recognition) {
      $('mic').disabled = true;
      $('mic').title = 'Voice input needs Chrome or Edge';
    }
    $('panel-close').addEventListener('click', () => Focus.clear());
    $('td-close').addEventListener('click', () => { Teardown.closeReader(); $('teardown').hidden = true; });
    $('td-read').addEventListener('click', () => Teardown.openReader());
    $('reader-close').addEventListener('click', () => Teardown.closeReader());
    window.addEventListener('keydown', event => { if (event.key === 'Escape' && !$('reader').hidden) Teardown.closeReader(); });
    // The teardown card and the reader make room when the answer, the note panel or the window changes.
    window.addEventListener('resize', () => Teardown.fit());
    $('td-shot').addEventListener('load', () => Teardown.fit());
    if (window.ResizeObserver) {
      const watcher = new ResizeObserver(() => Teardown.fit());
      ['hud', 'dock', 'panel', 'legend'].forEach(id => watcher.observe($(id)));
    }

    // Browsers block audio until the first click or key press. The greeting waits for it.
    const wake = () => { speaker.unlock(); $('hint').hidden = true; };
    window.addEventListener('pointerdown', wake, { capture: true, once: true });
    window.addEventListener('keydown', wake, { capture: true, once: true });
    if (window.speechSynthesis) window.speechSynthesis.getVoices();      // start loading the voices now
  }

  function fatal(message) {
    $('hint').hidden = true;
    showAnswer(message, true);
  }

  async function boot() {
    if (typeof GRAPH === 'undefined') return fatal('No graph data yet. Run "python build.py", then reload this page.');
    if (typeof ForceGraph3D === 'undefined') return fatal('The 3D library did not load. Check the internet connection, then reload this page.');
    try {
      THREE = await import(THREE_URL);
    } catch (error) {
      console.warn('three.js did not load. Using plain stars.', error);
    }
    graphState = { build_id: GRAPH.build_id, rev: GRAPH.rev };
    [...new Set(GRAPH.nodes.map(node => node.group))].sort().forEach(colourOf);
    initGraph();
    setData(GRAPH.nodes.map(node => ({ ...node })), GRAPH.links.map(link => ({ ...link })));
    wire();
    const muted = Core.isMuted(window.location.search);
    $('muted').hidden = !muted;
    if (muted || !window.speechSynthesis) $('hint').hidden = true;
    try {
      await readStatus();               // the greeting is one of the persona's lines: it comes with the status
    } catch (error) {
      console.warn('Could not read the server status', error);
    }
    if (!touched) say(lines.hello);     // the user was faster than the status: their answer stays
    Teardown.resume();

    // For tests.
    window.Jarviz = { Core, Graph, Focus, Teardown, router, speaker, addNote, removeNote, handleInput, reloadGraph,
                      data: () => data, status: () => $('status').dataset.state };
  }

  boot();
})();
