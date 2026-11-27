/* Jarviz core: the small rules that must never break.

   The page loads this file, and preflight runs it in Node to prove the rules.
   So nothing in here touches the DOM. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.JarvizCore = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // How long a pause must last before a spoken sentence counts as finished.
  // The most personal number in this build. Too low and it cuts you off mid-thought,
  // too high and it feels slow. Tune it after a day of real use, never in one sitting.
  const FINISH_MS = 900;

  // Words that skip the finish window and act at once.
  const INTERRUPT_WORDS = ['stop', 'wait', 'cancel', 'quiet', 'hold on', 'never mind', 'shut up'];

  // A sentence that starts with one of these is saved as a note. Add the phrases you really use.
  const REMEMBER_TRIGGERS = ['remember that', 'note to self', "don't let me forget that", "don't let me forget"];

  // An answer drawn from this many notes lights a cluster. Flying to one of them would be a lie.
  const CLUSTER_MIN = 4;

  // British first, as a butler should be.
  const VOICE_PREFERENCE = [/Google UK English Male/i, /\bRyan\b/i, /\bGeorge\b/i, /\bDaniel\b/i, /\bArthur\b/i, /\bThomas\b/i];

  function isMuted(search) {
    return new URLSearchParams(String(search || '')).get('mute') === '1';
  }

  function pickVoice(voices) {
    const list = Array.from(voices || []);
    const british = list.filter(voice => /^en[-_]GB/i.test(voice.lang));
    for (const pattern of VOICE_PREFERENCE) {
      const hit = british.find(voice => pattern.test(voice.name));
      if (hit) return hit;
    }
    return british[0] || list.find(voice => /^en/i.test(voice.lang)) || list[0] || null;
  }

  // A server voice that failed this many lines in a row is not asked again until it is chosen again.
  const VOICE_GIVE_UP = 3;
  const MAX_PARTS = 64;     // a guard against a strange X-Jarviz-Parts header

  /* The speaker. env = { synth, Utterance, getSearch, pickVoice, onStart, onEnd,
                          fetchAudio(text, part) -> Promise<{ blob, parts }>, Audio, createObjectURL, revokeObjectURL,
                          onTrouble({ answer, tell, gaveUp }) }
     Two voices behind one speak(): "browser" (speechSynthesis) and a server voice ("deepgram"): the server turns the
     text into audio, part by part, and the page plays the parts in order. */
  function createSpeaker(env) {
    let unlocked = false;
    let waiting = null;     // a line asked for before the first click
    let current = null;     // the line being said: an utterance (browser voice) or a job (server voice)
    let voice = 'browser';
    let failures = 0;       // server-voice lines that failed in a row
    let told = false;       // the trouble line was shown since the last part that played well
    let gaveUp = false;     // VOICE_GIVE_UP failures in a row: the server voice is not asked until setVoice

    const serverVoice = () => voice !== 'browser' && !gaveUp && Boolean(env.fetchAudio && env.Audio && env.createObjectURL);

    // THE ONE FUNCTION THAT SPEAKS. Nothing else may make sound.
    function speak(text) {
      if (isMuted(env.getSearch())) return false;         // ?mute=1: this tab never speaks, and never asks for audio
      const line = String(text || '').trim();
      if (!line) return false;
      if (!serverVoice() && (!env.synth || !env.Utterance)) return false;
      if (!unlocked) { waiting = line; return false; }    // browsers block audio before the first click
      cut();                                              // the newest line wins: the one being said stops now
      return serverVoice() ? speakAudio(line) : speakBrowser(line, false);
    }

    // The browser voice. `started`: the line already began in the server voice, so its start is not told twice.
    function speakBrowser(line, started) {
      if (!env.synth || !env.Utterance) return false;
      const utterance = new env.Utterance(line);
      const chosen = env.pickVoice ? env.pickVoice() : null;
      if (chosen) { utterance.voice = chosen; utterance.lang = chosen.lang; }
      utterance.rate = 1.03;
      utterance.pitch = 0.95;
      utterance.onstart = () => { if (current === utterance && !started && env.onStart) env.onStart(line); };
      utterance.onend = utterance.onerror = () => {
        if (current !== utterance) return;                // an older line that was cut off
        current = null;
        if (env.onEnd) env.onEnd(line);
      };
      current = utterance;
      env.synth.speak(utterance);
      watch(utterance);
      return true;
    }

    // The server voice: part 0 first. While a part plays, the next one is asked for, so there is no gap.
    function speakAudio(line) {
      const job = { audioJob: true, line, parts: 1, started: false, audio: null, url: null, ahead: null };
      current = job;
      fetchPart(job, 0).then(outcome => playPart(job, 0, outcome));
      return true;
    }

    // One part's audio as an outcome that never fails: { ok: { blob, parts } } or { error }.
    function fetchPart(job, part) {
      let asked;
      try { asked = Promise.resolve(env.fetchAudio(job.line, part)); } catch (error) { asked = Promise.reject(error); }
      return asked.then(ok => ({ ok }), error => ({ error }));
    }

    function playPart(job, part, outcome) {
      if (current !== job) return;                        // cut off or stopped: a part still on its way is dropped
      if (!outcome.ok || !outcome.ok.blob) return fail(job, outcome.error);
      if (part === 0) {
        const parts = Number(outcome.ok.parts);
        job.parts = Number.isInteger(parts) && parts >= 1 ? Math.min(parts, MAX_PARTS) : 1;
      }
      let audio;
      try {
        job.url = env.createObjectURL(outcome.ok.blob);
        audio = job.audio = new env.Audio(job.url);
      } catch (error) {
        return fail(job, error);                          // no element to play it: the browser voice says the line
      }
      const mine = () => current === job && job.audio === audio;
      const askAhead = () => {                            // the next part, asked for once
        if (part + 1 >= job.parts || (job.ahead && job.ahead.part === part + 1)) return;
        job.ahead = { part: part + 1, outcome: fetchPart(job, part + 1) };
      };
      audio.onended = () => {
        if (!mine()) return;
        release(job);
        failures = 0;                                     // a part that played well
        told = false;
        if (part + 1 >= job.parts) return finish(job);
        askAhead();
        job.ahead.outcome.then(next => playPart(job, part + 1, next));
      };
      audio.onerror = () => { if (mine()) fail(job, null); };
      let playing;
      try { playing = Promise.resolve(audio.play()); } catch (error) { playing = Promise.reject(error); }
      playing.then(() => {
        if (!mine()) return;
        if (!job.started) { job.started = true; if (env.onStart) env.onStart(job.line); }
        askAhead();
      }, error => { if (mine()) fail(job, error); });
      return undefined;
    }

    function release(job) {                               // the audio stops, and its object URL is given back
      const { audio, url } = job;
      job.audio = null;
      job.url = null;
      if (audio) {
        audio.onended = audio.onerror = null;
        try { audio.pause(); } catch (error) { /* already stopped */ }
      }
      if (url && env.revokeObjectURL) env.revokeObjectURL(url);
    }

    function finish(job) {
      current = null;
      if (job.started && env.onEnd) env.onEnd(job.line);
    }

    // A part could not be fetched or played: the browser voice says the line at once, so nothing is lost. The page
    // does not know where the server cut the text, so after a part that played, the line is said from its start.
    function fail(job, error) {
      release(job);
      failures += 1;
      const gaveUpNow = failures >= VOICE_GIVE_UP && !gaveUp;
      if (gaveUpNow) gaveUp = true;
      const tell = !told;
      told = true;
      if ((tell || gaveUpNow) && env.onTrouble) {
        env.onTrouble({ answer: error && typeof error.answer === 'string' ? error.answer : '', tell, gaveUp: gaveUpNow });
      }
      current = null;
      if (!speakBrowser(job.line, job.started)) finish(job);
    }

    function cut() {                                      // stop the line being said, with no signal: a new one follows
      const was = current;
      current = null;
      if (was && was.audioJob === true) release(was);
      if (env.synth && (env.synth.speaking || env.synth.pending)) env.synth.cancel();
    }

    function setVoice(name) {                             // "browser", or a server voice such as "deepgram"
      voice = typeof name === 'string' && name && name !== 'browser' ? name : 'browser';
      failures = 0;
      told = false;
      gaveUp = false;
    }

    // Chrome sometimes never reports that a line has ended. Then the page would think Jarviz is
    // still talking and stay deaf. If the voice has gone quiet for a moment, call the line ended.
    function watch(utterance) {
      if (typeof env.synth.speaking !== 'boolean') return;
      let quiet = 0;
      const timer = setInterval(() => {
        if (current !== utterance) return clearInterval(timer);
        quiet = env.synth.speaking || env.synth.pending ? 0 : quiet + 1;
        if (quiet < 3) return undefined;
        clearInterval(timer);
        return utterance.onend();
      }, 400);
    }

    function unlock() {
      if (unlocked) return;
      unlocked = true;
      if (waiting) { const line = waiting; waiting = null; speak(line); }
    }

    function stop() {
      waiting = null;
      const wasSpeaking = current !== null;
      cut();                                              // the audio stops at once; parts still on their way are dropped
      if (env.synth) env.synth.cancel();
      if (wasSpeaking && env.onEnd) env.onEnd('');
    }

    return { speak, unlock, stop, setVoice, isUnlocked: () => unlocked, isSpeaking: () => current !== null,
             voiceState: () => ({ name: voice, gaveUp }) };
  }

  function interruptWord(sentence) {
    const text = String(sentence || '').toLowerCase().replace(/[^a-z\s]/g, ' ').replace(/\s+/g, ' ').trim();
    const bare = text.replace(/\s+(?:jarviz|jarvis|please|now|talking)$/g, '').replace(/^(?:jarviz|jarvis)\s+/, '');
    return INTERRUPT_WORDS.includes(bare) ? bare : null;
  }

  /* The finish window. Speech recognition ends a phrase at every pause, and people pause in the
     middle of sentences. So a final phrase is held, and only the pause that truly ends the thought
     sends the whole sentence.  options = { finishMs, onSentence, onInterrupt, setTimer, clearTimer } */
  function createFinishBuffer(options) {
    const finishMs = options.finishMs == null ? FINISH_MS : options.finishMs;
    const setTimer = options.setTimer || setTimeout;
    const clearTimer = options.clearTimer || clearTimeout;
    let parts = [];
    let timer = null;

    function disarm() { if (timer !== null) { clearTimer(timer); timer = null; } }
    function arm() { disarm(); timer = setTimer(flush, finishMs); }

    function flush() {
      disarm();
      const sentence = parts.join(' ').replace(/\s+/g, ' ').trim();
      parts = [];
      if (sentence) options.onSentence(sentence);
    }

    function push(finalText) {
      const text = String(finalText || '').trim();
      if (!text) return;
      const word = interruptWord(text);
      if (word) { cancel(); options.onInterrupt(word); return; }   // "stop" and "wait" never wait
      parts.push(text);
      arm();
    }

    function touch() { if (parts.length) arm(); }     // more speech is on its way: keep waiting
    function cancel() { parts = []; disarm(); }

    return { push, touch, flush, cancel, pending: () => parts.join(' ') };
  }

  /* Voice routes: checked BEFORE a sentence goes to the brain. A pattern is a RegExp or a
     function that returns something truthy. Later features add routes here, not in the core. */
  function createRouter() {
    const routes = [];
    return {
      add(name, pattern, handler) { routes.push({ name, pattern, handler }); return this; },
      match(sentence) {
        const text = String(sentence || '').trim();
        for (const route of routes) {
          const hit = typeof route.pattern === 'function' ? route.pattern(text) : text.match(route.pattern);
          if (hit) return { name: route.name, handler: route.handler, hit, text };
        }
        return null;
      },
      names: () => routes.map(route => route.name),
    };
  }

  // "Remember that the till was short" -> "the till was short". Anything else -> null.
  function matchRemember(sentence) {
    const text = String(sentence || '').trim()
      .replace(/^(?:(?:hey|ok|okay)\s+)?(?:jarviz|jarvis)[,.!]?\s+/i, '')
      .replace(/^please\s+/i, '');
    const lower = text.toLowerCase().replace(/’/g, "'");
    for (const trigger of REMEMBER_TRIGGERS) {
      if (!lower.startsWith(trigger)) continue;
      const rest = text.slice(trigger.length);
      if (rest && !/^[\s,:;-]/.test(rest)) continue;      // "remember thatcher" is not a capture
      return rest.replace(/^[\s,:;-]+/, '').trim() || null;
    }
    return null;
  }

  // "Switch brain to DeepSeek", "use the OpenAI brain", "switch to Claude", "switch to Codex" -> the name that was said.
  // With the word "brain" any name is taken. The short form without it ("switch to agy") takes only the
  // names in the last pattern: Claude, OpenAI, DeepSeek, Venice, OpenRouter, Codex (CLI) and Antigravity (CLI).
  // The server resolves the name.
  const BRAIN_SWITCH = [
    /^(?:switch|change|swap)\s+(?:the\s+|your\s+)?brains?\s+to\s+(?:the\s+)?(.+)$/i,
    /^(?:switch|change|swap)\s+to\s+(?:the\s+)?(.+?)\s+brain$/i,
    /^use\s+(?:the\s+)?(.+?)\s+brain$/i,
    /^(?:switch|change|swap)\s+to\s+(?:the\s+)?(claude|anthropic|open\s?ai\s+codex|open\s?ai|chat\s?gpt|gpt|deep\s?seek|venice|open\s?router|codex(?:\s+cli)?|anti\s?gravity(?:\s+cli)?|agy)$/i,
  ];

  function matchBrainSwitch(sentence) {
    const text = String(sentence || '').trim().replace(/[.!?]+$/, '')
      .replace(/^(?:(?:hey|ok|okay)\s+)?(?:jarviz|jarvis)[,.!]?\s+/i, '')
      .replace(/^please\s+/i, '');
    for (const pattern of BRAIN_SWITCH) {
      const match = text.match(pattern);
      if (match) return match[1].trim();
    }
    return null;
  }

  // "Switch voice to Deepgram", "use the browser voice", "speak with Deepgram" -> the name that was said.
  // With the word "voice" any name is taken. Without it only the names in VOICE_NAMES, and the sentence must end
  // there. A sentence that matchBrainSwitch takes is never a voice switch. The server resolves the name.
  const VOICE_NAMES = '(deep\\s?gram(?:\\s+aura)?|aura|browser(?:[\'’]s)?|built[\\s-]?in|normal|default|system|chrome)';
  const VOICE_SWITCH = [
    /^(?:switch|change|swap|set)\s+(?:the\s+|your\s+|my\s+)?voices?\s+(?:back\s+)?to\s+(?:the\s+)?(.+?)(?:\s+voice)?$/i,
    /^(?:switch|change|swap)\s+(?:back\s+)?to\s+(?:the\s+|your\s+)?(.+?)\s+voice$/i,
    /^use\s+(?:the\s+|your\s+)?(.+?)\s+voice$/i,
    new RegExp('^(?:speak|talk)\\s+(?:with|in|using)\\s+(?:the\\s+|your\\s+|a\\s+)?' + VOICE_NAMES + '(?:\\s+voice)?$', 'i'),
    /^(?:switch|change|swap)\s+(?:back\s+)?to\s+(?:the\s+)?(deep\s?gram(?:\s+aura)?|aura)$/i,
  ];
  const NO_VOICE_NAME = /^(?:the|your|my|a|an|own|his|her|its|our|their|this|that|same|new|other)$/i;   // "use your voice"

  function matchVoiceSwitch(sentence) {
    if (matchBrainSwitch(sentence)) return null;        // a brain switch first: the two never take the same sentence
    const text = String(sentence || '').trim().replace(/[.!?]+$/, '')
      .replace(/^(?:(?:hey|ok|okay)\s+)?(?:jarviz|jarvis)[,.!]?\s+/i, '')
      .replace(/^please\s+/i, '').replace(/,?\s+please$/i, '');
    for (const pattern of VOICE_SWITCH) {
      const match = text.match(pattern);
      const name = match ? match[1].trim() : '';
      if (name && !NO_VOICE_NAME.test(name)) return name;
    }
    return null;
  }

  /* Teardowns. A sentence starts one ONLY when it names a site: "reverse engineer igloo dot inc",
     "tear down linear.app", "how does stripe.com do that", "what is example.com built with",
     or "this site" / "this page". "Tear down the old shed" names no site, so it goes to the brain. */
  const SPOKEN_SIGNS = [[/\s+dot\s+/gi, '.'], [/\s+slash\s+/gi, '/'], [/\s+colon\s+/gi, ':']];
  const ADDRESS_RE = /^(https?:\/\/)?((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,23}|localhost|127\.0\.0\.1)(?::[0-9]{1,5})?(?:\/[^\s#]*)?$/i;
  // "How does server.py work" is about a file, not about the website server.py. Without "https://"
  // in front, a name that ends like a file is not an address. (teardown.py has the same list.)
  const FILE_ENDINGS = new Set(('py js mjs cjs ts tsx jsx json md txt sh css html htm yml yaml toml bat exe png jpg jpeg ' +
    'gif svg pdf zip csv log ini cfg xml php rb lock').split(' '));
  const THIS_SITE_RE = /^(?:this|the current)\s+(?:site|page|website|web\s?page|tab)$/;
  const POLITE = '(?:(?:hey|ok|okay)\\s+)?(?:(?:jarviz|jarvis)[,.!]?\\s+)?(?:(?:please|can you|could you|would you)\\s+)*';
  const TEARDOWN_ASKS = [
    new RegExp('^' + POLITE + '(?:reverse[\\s-]*engineer|tear\\s*down|take\\s+apart|x-?ray)\\s+(.+)$', 'i'),
    new RegExp('^' + POLITE + 'take\\s+(.+?)\\s+apart$', 'i'),
    new RegExp('^' + POLITE + 'tear\\s+(.+?)\\s+down$', 'i'),
  ];
  // Questions start one only when their subject IS the site: "how does the stripe.com homepage work". In
  // "how did my teardown of stripe.com work" the subject is a teardown, so the sentence goes to the brain.
  const TEARDOWN_QUESTIONS = [
    /^how\s+(?:does|do|did|is|was)\s+(.+?)\s+(?:do|does|did|work|works|built|made|make|makes|done)\b.*$/i,
    /^what(?:\s+is|'s)\s+(.+?)\s+(?:built|made|written|running)\s+(?:with|on|in|of|from)\b.*$/i,
    /^what\s+(?:does|is)\s+(.+?)\s+(?:use|using|run\s+on)\b.*$/i,
  ];
  // The small words that may stand around the address in a question's subject.
  const SITE_WORDS_RE = /^(?:(?:the|a|site|website|web\s?page|home\s?page|landing\s+page|page|web\s?app|app|of|at|['’]s)(?:\s+|$))*$/i;
  // A refusal with a teardown word after it, anywhere in the sentence: "how does example.com work? Do not tear it
  // down." Such a sentence never starts one here; it goes to the brain as a question. The same idea as REFUSAL_RE
  // in server.py, smaller: "don't forget to tear down X" and "don't hesitate to ..." are no refusal.
  const REFUSAL_RE = /\b(?:do\s+not|don['’]?t|never|not\s+to|no\s+need\s+to|(?:should|must|will|can)\s*not|(?:shouldn|mustn|won|can)['’]?t|without|avoid|no(?=\s+tear\s*downs?\b))\b(?!\s+(?:forget|hesitate)\b)/i;
  const TEARDOWN_WORD_RE = /reverse[\s-]*engineer|tear(?:ing)?[\s-]*(?:\S+\s+){0,4}?down|tak(?:e|ing)\s+(?:\S+\s+){0,4}?apart|x-?ray/i;
  function refusesTeardown(text) {
    const refusal = text.search(REFUSAL_RE);          // a teardown word after a later refusal is after this one too
    return refusal >= 0 && TEARDOWN_WORD_RE.test(text.slice(refusal));
  }
  const TEARDOWN_COMMANDS = {
    last: /^(?:show|open|bring up)\s+(?:me\s+)?(?:the\s+)?(?:last|latest|previous|newest)\s+tear\s?down[.!]?$/i,
    open: /^(?:open|read|show)\s+(?:me\s+)?(?:the\s+)?(?:full|whole|entire)\s+tear\s?down[.!]?$/i,
    close: /^(?:close|hide|dismiss)\s+(?:the\s+)?tear\s?down(?:\s+(?:panel|card|reader))?[.!]?$/i,
    cancel: /^(?:cancel|stop|abort)\s+(?:the\s+)?tear\s?down[.!]?$/i,
  };

  // "igloo dot inc" -> "igloo.inc". "stripe.com slash pricing" -> "stripe.com/pricing".
  function spokenToAddress(text) {
    let address = String(text || '').replace(/\s+/g, ' ').trim();      // the path keeps its capital letters
    for (const [pattern, sign] of SPOKEN_SIGNS) address = address.replace(pattern, sign);
    return address.replace(/^[\s"'.,;!?]+|[\s"'.,;!?]+$/g, '');
  }

  const bareWord = word => word.replace(/^["'(]+|["').,;!?]+$/g, '').replace(/['’]s$/, '');

  // The word itself when it is a website address, else null. "server.py" is a file, not an address.
  function addressOf(bare) {
    const match = bare.match(ADDRESS_RE);
    return match && (match[1] || !FILE_ENDINGS.has(match[2].split('.').pop().toLowerCase())) ? bare : null;
  }

  // The first website address in a piece of speech, or null. "make it pop" -> null.
  function findAddress(text) {
    for (const word of spokenToAddress(text).split(' ')) {
      const site = addressOf(bareWord(word));
      if (site) return site;
    }
    return null;
  }

  // The site that a question's subject IS: one address with only small site words around it ("the stripe.com
  // homepage"), or "this" for "this site". Anything else ("my teardown of stripe.com") -> null.
  function siteOfSubject(subject) {
    if (THIS_SITE_RE.test(subject.toLowerCase())) return 'this';
    let site = null;
    const rest = [];
    for (const word of spokenToAddress(subject).split(' ')) {
      const bare = bareWord(word);
      const found = addressOf(bare);
      if (found && site) return null;                 // two addresses: not one site
      if (found) site = found;
      else if (bare) rest.push(bare);
    }
    return site && SITE_WORDS_RE.test(rest.join(' ')) ? site : null;
  }

  // -> { site: "igloo.inc" or "this", question: the whole sentence }, or null when no site is named, or when
  // the sentence refuses a teardown.
  function matchTeardown(sentence) {
    const text = String(sentence || '').replace(/\s+/g, ' ').trim().replace(/[.!?]+$/, '');
    if (refusesTeardown(text)) return null;
    for (const pattern of TEARDOWN_ASKS) {
      const match = text.match(pattern);
      if (!match) continue;
      const named = match[1].trim();
      if (THIS_SITE_RE.test(named.toLowerCase())) return { site: 'this', question: text };
      const site = findAddress(named);
      if (site) return { site, question: text };
    }
    for (const pattern of TEARDOWN_QUESTIONS) {
      const match = text.match(pattern);
      const site = match ? siteOfSubject(match[1].trim()) : null;
      if (site) return { site, question: text };
    }
    return null;
  }

  // Decide what the galaxy does with the notes an answer used. Decided before the camera moves.
  function planFocus(nodes) {
    const list = [...new Set((Array.isArray(nodes) ? nodes : []).filter(Number.isInteger))];
    if (!list.length) return { mode: 'still', lit: [] };                  // small talk: hold still
    if (list.length >= CLUSTER_MIN) return { mode: 'cluster', lit: list };
    return { mode: 'fly', target: list[0], lit: list };
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  }

  return {
    FINISH_MS, INTERRUPT_WORDS, REMEMBER_TRIGGERS, CLUSTER_MIN,
    isMuted, pickVoice, createSpeaker, interruptWord, createFinishBuffer,
    createRouter, matchRemember, matchBrainSwitch, matchVoiceSwitch, planFocus, escapeHtml,
    TEARDOWN_COMMANDS, spokenToAddress, findAddress, matchTeardown,
  };
});
