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

  // A command that still needs its name ("switch brain to", "change your voice to", "switch to") waits this long for
  // the rest: people pause before a name, and speech recognition ends the phrase at that pause. Nothing else waits it.
  const HOLD_MS = 2500;

  // A lone "switch brain" is answered with "which brain?". Only the next sentence, and only this soon, may answer it
  // with a name alone ("Claude"). The time covers the question being said aloud.
  const NAME_WAIT_MS = 15000;

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
                          fetchAudio(text, part, voice) -> Promise<{ blob, parts }>, Audio, createObjectURL,
                          revokeObjectURL, onTrouble({ answer, tell, gaveUp }) }
     Two kinds of voice behind one speak(): "browser" (speechSynthesis) and a server voice ("deepgram", "elevenlabs"):
     the server turns the text into audio, part by part, and the page plays the parts in order. A line keeps the voice
     it started with: every part of it is asked for with that voice, also after a switch in the middle of the line. */
  function createSpeaker(env) {
    let unlocked = false;
    let waiting = null;     // a line asked for before the first click
    let current = null;     // the line being said: an utterance (browser voice) or a job (server voice)
    let voice = 'browser';
    let failures = 0;       // server-voice lines that failed in a row (only a line that played to its end resets it)
    let told = false;       // the trouble line was shown since the last part that played well
    let gaveUp = false;     // VOICE_GIVE_UP failures in a row: the server voice is not asked until setVoice
    let generation = 0;     // a new number for every setVoice: a job counts only for the choice it started with
    let owed = false;       // the page heard "speaking" (onStart) and not yet "ended" (onEnd)

    const serverVoice = () => voice !== 'browser' && !gaveUp && Boolean(env.fetchAudio && env.Audio && env.createObjectURL);
    const tellStart = line => { owed = true; if (env.onStart) env.onStart(line); };
    const tellEnd = line => { owed = false; if (env.onEnd) env.onEnd(line); };

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
      utterance.onstart = () => { if (current === utterance && !started) tellStart(line); };
      utterance.onend = utterance.onerror = () => {
        if (current !== utterance) return;                // an older line that was cut off
        current = null;
        tellEnd(line);
      };
      current = utterance;
      env.synth.speak(utterance);
      watch(utterance);
      return true;
    }

    // The server voice: part 0 first. While a part plays, the next one is asked for, so there is no gap. The job
    // keeps the voice of this moment (job.voice): a later setVoice does not change the parts of this line. It keeps
    // its generation too: after a setVoice its parts no longer count for the failures of the new choice.
    function speakAudio(line) {
      const job = { audioJob: true, line, voice, generation, parts: 1, started: false, audio: null, url: null, ahead: null };
      current = job;
      fetchPart(job, 0).then(outcome => playPart(job, 0, outcome));
      return true;
    }

    // One part's audio as an outcome that never fails: { ok: { blob, parts } } or { error }.
    function fetchPart(job, part) {
      let asked;
      try {
        asked = Promise.resolve(env.fetchAudio(job.line, part, job.voice));
      } catch (error) {
        asked = Promise.reject(error);
      }
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
        const last = part + 1 >= job.parts;
        if (job.generation === generation) {              // a part that played well, of the voice chosen now
          told = false;                                   // a later failure shows its line again
          if (last) failures = 0;                         // a line counts as played well only when its last part did
        }
        if (last) return finish(job);
        askAhead();
        job.ahead.outcome.then(next => playPart(job, part + 1, next));
      };
      audio.onerror = () => { if (mine()) fail(job, null); };
      let playing;
      try { playing = Promise.resolve(audio.play()); } catch (error) { playing = Promise.reject(error); }
      playing.then(() => {
        if (!mine()) return;
        if (!job.started) { job.started = true; tellStart(job.line); }
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

    // The end of a line. A line cut off by this one sent no "ended" (the new line goes on speaking). When this line
    // never started (it could not be played at all), that "ended" is still owed: it is sent now, so the page is
    // never left "speaking" and deaf.
    function finish(job) {
      current = null;
      if (owed) tellEnd(job.line);
    }

    // A part could not be fetched or played: the browser voice says the line at once, so nothing is lost. The page
    // does not know where the server cut the text, so after a part that played, the line is said from its start.
    // A job of an earlier choice (setVoice came after it started) still hands its line to the browser voice and
    // shows its line, but it counts no failure for the voice chosen now, and it never gives that voice up.
    function fail(job, error) {
      release(job);
      const ours = job.generation === generation;
      let gaveUpNow = false;
      let tell = true;
      if (ours) {
        failures += 1;
        gaveUpNow = failures >= VOICE_GIVE_UP && !gaveUp;
        if (gaveUpNow) gaveUp = true;
        tell = !told;
        told = true;
      }
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

    function setVoice(name) {                             // "browser", or a server voice: "deepgram", "elevenlabs"
      voice = typeof name === 'string' && name && name !== 'browser' ? name : 'browser';
      generation += 1;
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
      if (wasSpeaking) tellEnd('');
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
     sends the whole sentence. A switch that still needs its name ("switch brain to") is held longer
     (holdMs): the name may follow after a pause.
     options = { finishMs, holdMs, onSentence, onInterrupt, setTimer, clearTimer } */
  function createFinishBuffer(options) {
    const finishMs = options.finishMs == null ? FINISH_MS : options.finishMs;
    const holdMs = options.holdMs == null ? HOLD_MS : options.holdMs;
    const setTimer = options.setTimer || setTimeout;
    const clearTimer = options.clearTimer || clearTimeout;
    let parts = [];
    let timer = null;

    function disarm() { if (timer !== null) { clearTimer(timer); timer = null; } }
    function arm() { disarm(); timer = setTimer(flush, awaitsName(parts.join(' ')) ? holdMs : finishMs); }

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

  /* A switch, the way speech recognition writes it. The switch routes read a sentence in ONE plain form (switchText):
     no wake word ("Jarvis,"), no polite start ("can you", "could you", "would you", "will you", "I want you to",
     "please"), no polite end ("please", "now", "thanks", "thank you", "for me"), and no marks inside: "switch-brain"
     and "switch, brain" are "switch brain". Speech writes "to" as "2", "two" or "too" too (TO). This is a pattern, not
     an understanding of English: the server judges every name (brains.resolve_name, voices.resolve_name). */
  const TO = '(?:to|2|two|too)';
  const WAKE_RE = /^(?:(?:hey|ok|okay)\s+)?(?:jarviz|jarvis)\s+/i;
  const POLITE_START_RE = /^(?:please|(?:can|could|would|will)\s+you|i\s+want\s+you\s+to|i(?:'d|\s+would)\s+like\s+you\s+to)\s+/i;
  const POLITE_END_RE = /\s+(?:please|now|thanks|thank\s+you|for\s+me|jarviz|jarvis)$/i;

  function switchText(sentence) {
    let text = String(sentence || '').replace(/’/g, "'")
      .replace(/[,;:!?"()\u2013\u2014-]/g, ' ')       // marks are spaces: "switch-brain", "anti-gravity"
      .replace(/\.(?=\s|$)/g, ' ')                     // a period after a word: "Jarvis. Switch", "Claude."
      .replace(/\s+/g, ' ').trim();
    for (let before = null; before !== text;) {       // "Jarvis, can you please ... now, thanks"
      before = text;
      text = text.replace(WAKE_RE, '').replace(POLITE_START_RE, '').replace(POLITE_END_RE, '').trim();
    }
    return text;
  }

  // Names as people say them, one entry per way of writing: "deep seek" and "deepseek". A space stands for any space.
  const anyName = names => names.slice().sort((a, b) => b.length - a.length)
    .map(name => name.split(' ').map(word => word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('\\s+')).join('|');

  // The brains that the short forms take without the word "brain" ("switch to Claude", "use DeepSeek"). brains.py
  // checks that the server knows every name in BRAIN_NAMES and BRAIN_HEARD.
  const BRAIN_NAMES = ['claude', 'anthropic', 'openai', 'open ai', 'chatgpt', 'chat gpt', 'gpt', 'deepseek', 'deep seek',
    'venice', 'openrouter', 'open router', 'codex', 'codex cli', 'openai codex', 'open ai codex', 'antigravity',
    'anti gravity', 'antigravity cli', 'anti gravity cli', 'agy'];
  // What speech recognition writes for some of them. "switch to cloud" takes these, "use the cloud" does not.
  const BRAIN_HEARD = ['cloud', 'clawed', 'clod', 'deep sea', 'codecs'];

  // "Switch brain to DeepSeek", "switch my brain 2 Claude", "switch to brain DeepSeek", "use the OpenAI brain",
  // "switch to Claude", "use Claude" -> the name that was said. With the word "brain" any name is taken. The short
  // forms without it take only the names above, and the sentence must end there: never "use the force".
  const BRAIN_SWITCH = [
    new RegExp('^(?:switch|change|swap)\\s+(?:the\\s+|your\\s+|my\\s+)?brains?\\s+' + TO + '\\s+(?:the\\s+)?(.+)$', 'i'),
    new RegExp('^(?:switch|change|swap)\\s+' + TO + '\\s+(?:the\\s+)?brain\\s+(.+)$', 'i'),
    new RegExp('^(?:switch|change|swap)\\s+' + TO + '\\s+(?:the\\s+)?(.+?)\\s+brain$', 'i'),
    /^use\s+(?:the\s+)?(.+?)\s+brain$/i,
    new RegExp('^(?:switch|change|swap)\\s+' + TO + '\\s+(?:the\\s+)?(' + anyName(BRAIN_NAMES.concat(BRAIN_HEARD)) + ')$', 'i'),
    new RegExp('^use\\s+(?:the\\s+)?(' + anyName(BRAIN_NAMES) + ')$', 'i'),
  ];
  const NO_BRAIN_NAME = /^(?:the|a|an)$/i;            // "switch brain to the": the name has not come yet

  function matchBrainSwitch(sentence) {
    const text = switchText(sentence);
    for (const pattern of BRAIN_SWITCH) {
      const match = text.match(pattern);
      const name = match ? match[1].trim() : '';
      if (name && !NO_BRAIN_NAME.test(name)) return name;
    }
    return null;
  }

  // The server voices without the word "voice" ("switch to ElevenLabs"), and what speech writes for them. voices.py
  // checks that the server knows every name in VOICE_NAMES, VOICE_HEARD and BROWSER_NAMES.
  const VOICE_NAMES = ['deepgram', 'deep gram', 'deepgram aura', 'deep gram aura', 'aura', 'elevenlabs', 'eleven labs',
    '11labs', '11 labs'];
  const VOICE_HEARD = ['eleven lab', '11 lab', 'level labs'];
  // The browser's own voice. Without the word "voice" only "speak with ..." takes these names.
  const BROWSER_NAMES = ['browser', "browser's", 'built in', 'builtin', 'normal', 'default', 'system', 'chrome'];

  // "Switch voice to Deepgram", "use the browser voice", "speak with Deepgram", "switch to ElevenLabs" -> the name that
  // was said. With the word "voice" any name is taken. Without it only the names above, and the sentence must end
  // there. A sentence that matchBrainSwitch takes is never a voice switch. The server resolves the name.
  const VOICE_SWITCH = [
    new RegExp('^(?:switch|change|swap|set)\\s+(?:the\\s+|your\\s+|my\\s+)?voices?\\s+(?:back\\s+)?' + TO
      + '\\s+(?:the\\s+)?(.+?)(?:\\s+voice)?$', 'i'),
    new RegExp('^(?:switch|change|swap)\\s+(?:back\\s+)?' + TO + '\\s+(?:the\\s+|your\\s+)?(.+?)\\s+voice$', 'i'),
    /^use\s+(?:the\s+|your\s+)?(.+?)\s+voice$/i,
    new RegExp('^(?:speak|talk)\\s+(?:with|in|using)\\s+(?:the\\s+|your\\s+|a\\s+)?('
      + anyName(VOICE_NAMES.concat(VOICE_HEARD, BROWSER_NAMES)) + ')(?:\\s+voice)?$', 'i'),
    new RegExp('^(?:switch|change|swap)\\s+(?:back\\s+)?' + TO + '\\s+(?:the\\s+)?(' + anyName(VOICE_NAMES.concat(VOICE_HEARD)) + ')$', 'i'),
  ];
  const NO_VOICE_NAME = /^(?:the|your|my|a|an|own|his|her|its|our|their|this|that|same|new|other)$/i;   // "use your voice"

  function matchVoiceSwitch(sentence) {
    if (matchBrainSwitch(sentence)) return null;        // a brain switch first: the two never take the same sentence
    const text = switchText(sentence);
    for (const pattern of VOICE_SWITCH) {
      const match = text.match(pattern);
      const name = match ? match[1].trim() : '';
      if (name && !NO_VOICE_NAME.test(name)) return name;
    }
    return null;
  }

  // A switch that has not said its name yet. "switch brain", "change your voice", "switch voice to": the page asks
  // which one (matchSwitchAlone). Those and a bare "switch to" are held by the finish buffer for HOLD_MS (awaitsName),
  // because the name often follows after a pause.
  const BRAIN_ALONE = new RegExp('^(?:switch|change|swap)\\s+(?:the\\s+|your\\s+|my\\s+)?brains?(?:\\s+' + TO + '(?:\\s+the)?)?$', 'i');
  const VOICE_ALONE = new RegExp('^(?:switch|change|swap|set)\\s+(?:the\\s+|your\\s+|my\\s+)?voices?(?:\\s+(?:back\\s+)?'
    + TO + '(?:\\s+the)?)?$', 'i');
  const SWITCH_TO_ALONE = new RegExp('^(?:switch|change|swap)\\s+(?:back\\s+)?' + TO + '(?:\\s+the)?(?:\\s+brain)?$', 'i');

  function matchSwitchAlone(sentence) {                // -> 'brain', 'voice' or null
    const text = switchText(sentence);
    return BRAIN_ALONE.test(text) ? 'brain' : VOICE_ALONE.test(text) ? 'voice' : null;
  }

  function awaitsName(sentence) {
    const text = switchText(sentence);
    return BRAIN_ALONE.test(text) || VOICE_ALONE.test(text) || SWITCH_TO_ALONE.test(text);
  }

  // The answer to "which brain?" or "which voice?": a known name alone ("Claude", "to DeepSeek", "the Eleven Labs
  // voice", "Claude please"). Anything else (a question, "hello") is no answer: it goes its own way.
  const BRAIN_ANSWER = new RegExp('^(?:' + TO + '\\s+)?(?:the\\s+)?(' + anyName(BRAIN_NAMES.concat(BRAIN_HEARD)) + ')(?:\\s+brain)?$', 'i');
  const VOICE_ANSWER = new RegExp('^(?:' + TO + '\\s+)?(?:the\\s+)?(' + anyName(VOICE_NAMES.concat(VOICE_HEARD, BROWSER_NAMES))
    + ')(?:\\s+voice)?$', 'i');

  function matchSwitchAnswer(sentence, kind) {         // -> { kind, name } or null
    const pattern = kind === 'brain' ? BRAIN_ANSWER : kind === 'voice' ? VOICE_ANSWER : null;
    const match = pattern ? switchText(sentence).match(pattern) : null;
    return match ? { kind, name: match[1].trim() } : null;
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
    FINISH_MS, HOLD_MS, NAME_WAIT_MS, INTERRUPT_WORDS, REMEMBER_TRIGGERS, CLUSTER_MIN,
    isMuted, pickVoice, createSpeaker, interruptWord, createFinishBuffer,
    createRouter, matchRemember, matchBrainSwitch, matchVoiceSwitch, switchText, matchSwitchAlone, awaitsName,
    matchSwitchAnswer, planFocus, escapeHtml,
    TEARDOWN_COMMANDS, spokenToAddress, findAddress, matchTeardown,
  };
});
