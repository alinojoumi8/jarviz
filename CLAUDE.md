# Jarviz

A personal "Jarvis": a 3D galaxy of markdown notes with a brain (Anthropic API), a voice in both
directions, and a butler persona. The spec is a 12-prompt pack. `TODO.md` lists every prompt and its state.

## Run

- Start: double-click `start-jarviz.bat` (it runs `python server.py --open`). The page is locked: a browser
  needs the page key once. `--open` gives it to the default browser. For another browser, run
  `python server.py --link` and paste the link.
- Re-index the notes: `python build.py`. The running server picks the new index up by itself.
- Verify: `python preflight.py` while the server runs.
- Verify the teardown kit: `python drill.py` while the server runs. It runs one hidden test teardown and removes its own note.
  `python drill.py --visible` is the same drill with the Chrome window on screen.

## Files

- `build.py` indexer, standard library only. Writes `viewer/graph-data.js`.
- `server.py` server, persona, routes. Port 4700, bound to 127.0.0.1.
- `brains.py` the seven brains behind one `ask()`. Five use a key (Claude, OpenAI, DeepSeek, Venice, OpenRouter):
  Claude uses the `anthropic` package, the others plain HTTPS from the standard library. Two use a sign-in
  (Codex, Antigravity): Jarviz starts the user's own tool (`codex`, `agy`), and the tool uses its own sign-in.
  `python brains.py` runs the offline checks of the sign-in brains.
- `voices.py` the voices behind one `speak()`: the browser's own voice (the server does nothing for it) and
  Deepgram (text to speech over plain HTTPS, standard library only). `python voices.py` runs its offline checks.
- `preflight.py` live end-to-end checks.
- `teardown.py` the teardown bay (Part Two), standard library only. The server soft-imports it and starts without it.
- `drill.py` the live teardown drill. `record()` and `put_back()` make a test leave no trace.
- `viewer/index.html`, `viewer/core.js`, `viewer/app.js` the page. No npm, no build step.
- `page_checks.js` the rules of the page, proved in Node with a fake page (no browser). Preflight runs it.
  A change in `viewer/app.js` gets a story there. It is in the project root: the server serves only `viewer/`.
- `config.json` the active brain, a key and a model for each brain, the notes folder. Git ignores it.
  A sign-in brain has a model line only (empty: the tool's own default model). The voice lines are `voice` (the
  default voice), `deepgram_api_key` and `deepgram_voice`.
- `~/.jarvis-tools/brain-locks.json` the lock proofs of the sign-in brains: the tool version and the lock recipe
  that a real trap proved. It is outside the project. Jarviz writes it.
- `~/.jarvis-tools/page-key` the page key (the lock). It is outside the project. The server makes it at the first start.

## Laws

- "Done" means preflight passed. Run `python preflight.py` and paste its summary line before you say a
  change is done. When something breaks, add a check for it.
- Every API key lives only in `config.json` in the project root. Never in `viewer/`, never in a response,
  never in a log. Never ask the user to paste a key into a chat. They type it into the file.
- The server writes to `config.json` in two places only. `write_starter_config` (from `main()`) makes the file
  when there is none, and never writes over one. `save_brain_choice` changes the text of the `"brain"` entry
  and keeps every other character; it reads the new text back before it replaces the file. `load_config` never
  writes. A new brain is one entry in `PROVIDERS` (`brains.py`) plus its key line and model line in
  `config.json`. Model ids change often: read the provider's documents, do not guess.
- `save_brain_choice` puts the new text in place in one step, so a crash never leaves half a config. For that moment
  the text is in a staging file beside it (`config.json.<pid>.tmp`: made new, never over a file, git ignores it).
  That is the ONE other file that may hold a key. `sweep_config_staging` (from `main()`) removes one that a crash
  left, and never reads it. A file that holds keys is made for its owner only (mode 0600, `_new_private_file`),
  and a brain switch never makes `config.json` readable for more people than before (`_same_mode_as`).
- Preflight proves a brain with one very small real question on the same path as `ask()` (route `"probe"` in
  `EFFORT` and `TIMEOUT_S`). A model list does not prove that a key may ask the model. The answer must have the
  shape of a completion: an empty text is fine for the tiny budget, a missing envelope is a fail. An error text
  from a provider loses the key BEFORE it is cut (`_without_key`); `scrub_key` is the second net.
- A provider call never follows a redirect (the key would go with it). That holds for the `anthropic` package too:
  `get_client` gives it an HTTP client that follows none.
- A sign-in brain (kind `"cli"` in `PROVIDERS`) is TALK-ONLY. Its tool is a coding agent, and Jarviz gives it text
  from strangers (notes, inspected websites). The lock is in ONE place for each tool: `CODEX_LOCK` (read-only
  sandbox, every tool family off, the user's own Codex settings left out), and `AGY_LOCK` with `AGY_SETTINGS` and
  `AGY_AGENT_HEAD` (an agent file with an empty tool list, strict permissions, deny rules). Do not take a flag out
  to make something work. `CLI_FRAME` is only words. It is not the lock.
- Jarviz never reads, copies or prints a sign-in. It does not open `~/.codex` or `~/.gemini`. It starts the tool,
  and the tool signs in.
- A sign-in brain is started with no shell. Never a `.cmd` or `.bat`: Windows starts cmd.exe for those. `find_tool`
  gives None for a tool that is there only as a batch file. Every argument is a constant of `brains.py`, except a
  model id that passed `MODEL_ID_RE`. The prompt goes in on stdin. The working folder is a new empty room (`cwd=`),
  removed after the call. No text from a note, a website or the user ever goes on a command line.
- A sign-in brain answers only after a real trap proved its lock for THIS version of the tool and THIS recipe
  (`ensure_locked`, `prove_lock`, `lock_recipe`). A new version or a changed flag proves again by itself. A trap
  that could not look (`None`) is never a pass and writes no record. The trap uses `TRAP_FRAME`, never `CLI_FRAME`:
  it must test the lock, not the manners of the model.
- A lock proof is STRONG or WEAK (`lock_seen`). Strong: the trap saw a real attempt that the lock refused (Codex:
  the sandbox refused a write, `CODEX_REFUSED_RE`; agy: a permission refusal of a tool step, `AGY_REFUSED_RE`).
  Weak: no word came back, no file was made and no tool ran, but no refused attempt was seen. Both let the brain
  answer. Preflight says PASS for strong and WARN for weak. Never call a weak proof strong.
- The trap looks twice: a run with no evidence is run once more (`_prove_twice`). A weak run never replaces a
  strong proof of the same version and recipe (`_record_proof`). A False verdict always replaces the record.
  `check_brain_locks` tries once more when the trap could not look. Two times "could not look" is a FAIL.
- An agy run that never started (`cli_not_started`: the backend gave no conversation) is tried again, at most
  `CLI_START_TRIES` times, in `_cli_call` only. A run that started is never asked twice.
- agy must say at the start of EVERY run that it took the lock (`AGY_INIT`: permission mode `strict`, agent
  `jarviz-brain`). A run that started without those words is `cli_not_locked`, and its answer is thrown away.
- A tool that ran throws the answer away (`cli_tool_ran`). Codex: any item that is not talk (`CODEX_TALK_ITEMS`).
  agy: a tool step that ends in ERROR is one of three kinds: refused by the lock (a permission message), never
  sent (`AGY_NOT_SENT_RE`: agy threw an invalid call out), or anything else, which may have run. Only the first
  two keep the answer. The one more exception is `HARMLESS_LISTING` (tools that only list). Never add a tool to it
  that can read, write, run a command or reach the web.
- What a tool says (its events, its error text, its version) is data from a stranger. Types are checked, and a text
  goes through `_cli_detail` (no home folder, no e-mail address) before it travels on.
- Do not write that a sign-in brain "cannot" use tools. Write that the trap found none. The limits are in `TODO.md`
  under "Not proven".
- The server binds 127.0.0.1 and serves only `viewer/`.
- The page lock: every request needs the page key. A browser brings it as the cookie `jarviz_key`. A tool signs
  the request with it (header `X-Jarviz-Sign`, made by `server.sign_request`) and never sends the key. ONE
  place decides: `Handler._unlocked` in `server.py`. A new route is locked without more work. Do not add a route
  that answers before that check. The one exception is `/lock/proof`: it proves that the server holds the key and
  shows nothing about it.
- A signature works once: `claim_signature` keeps its nonce until its time is over. The same signed request a
  second time is locked out. Every tool call is signed anew. A signature is claimed only when the request is proven
  whole: a GET in `_unlocked`, a POST after its body matched (`_signed_body_ok`, called from `_read_json`). Every
  POST route takes its payload from `_read_json` only. A signature whose time is over is never claimed, and the
  time of a claim is read inside the lock.
- A signature is never traded for the cookie: `Handler._door` gives the cookie only to a caller that brought the
  key itself. A signature names the start of the server (`INSTANCE`), so it is worth nothing after a restart.
- No tool and no test sends the key itself, in a header, a cookie or an address. `python server.py --link` shows
  the link only after `server.listener_holds` proved that the server holds the key. Only the running server makes
  the key file, after the port is its own.
- A test that must NOT start a teardown sends `preflight.INERT_SITE`, never an empty site. An empty site means
  "the front Chrome tab" on a Mac.
- The page key lives only in `~/.jarvis-tools/page-key`. Never in `viewer/`, never in a response body, never in the
  console, never in a test's output. `python server.py --link` is the one command that shows it, to the user.
- Tests call the server through `preflight.call` (it signs the call). A call with `key=False` is an outsider.
- One function speaks: `createSpeaker().speak` in `viewer/core.js`. `?mute=1` makes a tab silent. Open
  test tabs with `?mute=1`.
- The Deepgram key is a key like the others: only in `config.json`, never in the page. The page asks the server
  for audio (`POST /voice/speak`), the server asks Deepgram (`voices.speak`) and gives the bytes back. A voice call
  never follows a redirect, and an error text loses the key before it is cut (`voices.scrub_key`).
- The voice choice lives in the browser (`localStorage`, `jarviz_voice`). `POST /voice` only judges a wish and
  answers with a line. The server never writes the choice into `config.json`: it writes that file in two places
  only. The `"voice"` line there is the default voice, and only the user types it.
- `createSpeaker().speak` plays the Deepgram audio part by part, or uses the browser voice. A muted tab asks for NO
  audio: every request to Deepgram costs money. When a part fails, the browser voice says the rest, so no line is
  lost. `/voice/speak` takes at most `MAX_SPEAK_CHARS`, and `voices.split_text` cuts the text (one Deepgram
  request takes at most 2000 characters).
- Preflight proves the Deepgram voice like a brain: one very small real request on the same path as `speak()`
  (`voices.probe`), and only when there is a key. Without a key the route must refuse cleanly.
- One owner for focus: only `Focus` in `viewer/app.js` moves the camera or changes which stars are lit.
- One door for new notes: `Galaxy.birth_note` in `server.py` and `addNote` in `viewer/app.js`.
- Every node id equals its index in `GRAPH.nodes`.
- The persona, the control tags and the spoken lines live in the persona block at the top of `server.py`.
  The lines that the page says by itself are `PAGE_LINES` in that block. The page gets them with `/status`.
  `viewer/` holds no butler words: `lines` in `viewer/app.js` has plain words for the time before the first answer.
- Voice routes are checked before the brain. Add a command with `router.add`, do not edit the core.
- A control tag runs only when the user's own sentence asks for that action. Every tag in
  `TAG_HANDLERS` needs an entry in `TAG_INTENT` (`server.py`). Text inside a note can never order an action.
  A sentence that only mentions a website does not ask for a teardown. A sentence about a teardown that is there
  ("what did my teardown of stripe.com say") does not ask for a new one, and "do not tear down stripe.com"
  does not ask for one (`asks_for_teardown`). The page's own voice route follows the same idea: a question starts
  a teardown only when its subject IS the site (`matchTeardown` in `viewer/core.js`), and never when the sentence
  refuses one ("How does example.com work? Do not tear it down.": `refusesTeardown`).
- Note text is never HTML. The viewer uses `textContent` only. Labels given to the 3D library go
  through `Core.escapeHtml`.
- The two outside libraries are pinned in three places that must agree: `GRAPH_LIB_URL` and
  `THREE_LIB_DIR` in `server.py` (the security policy), `viewer/index.html` (with its hash), and
  `THREE_URL` in `viewer/app.js`. three.js must be the same release as the one inside the
  3d-force-graph bundle (`window.__THREE__` in the browser console shows it).
- Claude Opus 5.5: never send a thinking setting and never force a tool choice. Set effort per route
  (`EFFORT` in `brains.py`).
- Chat history is plain text (what was said, what was answered, receipts). So a switch of brains keeps
  the conversation. Receipts stay in the history right after the turn that earned them.
- Tests clean up after themselves. A test note is removed before the test ends.
- Part Two (REA): it observes only. No clicks, no logins, websites the user may look at.
- REA lives outside the project in `~/.jarvis-tools/rea` and is pinned by `REA_PIN` in `teardown.py`. Never install
  "latest". After a version change, run `python drill.py`.
- Jarviz runs four REA commands only (`REA_COMMANDS`) and two DevTools methods only (`DevTools.ALLOWED`): a read
  with fixed text, and the mouse wheel. Do not add a click, a key press or a navigation.
- Every teardown gets a new Chrome profile. `Browser.close()` always runs: Chrome stops and the folder is deleted.
  Jarviz never attaches to a browser that it did not start.
- One place starts a teardown: `start_teardown` in `server.py`. Websites only (`teardown.parse_site`), one at a time.
  Public names and the user's own `localhost` only: no private-network names (`PRIVATE_ENDINGS`,
  `resolves_to_private`). `teardown.check_site` is THE rule for which site may open: the server and the commands
  `python teardown.py browser` and `capture` both use it. The `[TEARDOWN:site|question]` tag runs only for a site whose own name is a word in the
  user's sentence (`named_by_user`), and "this" only when the user said "this site". Both are read in the part of
  the sentence that ASKS (`asking_part`, `tag_site_named`): in "do not tear down stripe.com, then tear down
  example.com" only example.com is named. The site must stand in the clause that requests the teardown. Another
  clause counts only when the request points ("tear it down"). This is a pattern, not an understanding of English:
  the brain decides first, and this gate can only refuse. `check_site` looks up the host that Chrome opens, with
  its "www.".
- During a teardown, `_refuse_local_turn` reads where the page is. A private address, an address that is not a web
  address, or no answer from the browser stops the job. It does not see each connection that Chrome makes: that
  limit is written in `TODO.md` under "Not proven". Do not write that Jarviz blocks private-network requests.
- A note never shows the brain a ready-made control tag: `note_block` turns `[WORD:` into `(WORD:`.
- The REA note links to the Jarviz note. When the notes have no note called Jarviz or Jarvis, the first teardown
  makes `captures/Jarviz.md` first (`owner_note` in `teardown.py`). The births go in this order: Jarviz, REA, the teardown.
- A teardown that fails takes back the notes it made (`Galaxy.unbirth`), newest first. `unbirth` deletes a file only
  when it still holds the bytes that `birth_note` wrote (`Galaxy._born`). A file that someone changed stays.
- A birth, a take-back or a forget that fails puts the index in memory back first (`Galaxy._restore`), then the file.
  A clean-up that fails never hides the error that stopped the work. A file is never put back on top of another file.
- Every evidence line in the digest starts with who captured it (`EVIDENCE_TAGS`). The brain writes the note text.
  Code writes the Evidence section and the facts block on top.
- Everything that comes out of the inspected website is data from a stranger: REA's output, the page reads (they
  run inside the page, so the page can change their answers), and the brain's teardown text. Every number goes
  through `_count`, every text through `_site_text` or `_clean`, every address through `safe_url`. The brain's text
  goes through `tame_note`, and its spoken line through `risky_spoken`, before it is saved or said.
- A check that cannot look says so. `investigation_chromes()` and `leftover_profiles()` return `None` when the
  system could not be asked. `None` is never a pass. In preflight, a probe that got no answer is not a refusal
  (`try_probe`): the check fails and names it.
- `/teardown/cancel` with `{"job": id}` stops only that job. A test always sends an id: the drill its own job,
  preflight a made-up one. Only a MISSING `job` field stops the job that runs now (the voice command). A `job` that
  is null, empty or not a text stops nothing. An answer about a job carries `job_id`, and the page ties its
  "say nothing at the end" flags to that id.
- A teardown receipt goes on the turn that started the job. `Session.draft` makes the turn, and `publish` puts it
  into the history only after the job was accepted. A receipt for a draft is kept. A start that is refused
  publishes nothing, so it leaves no turn behind.
- A teardown binds ONE notes folder (`Galaxy.current_folder`). Its picture and every one of its notes go there.
  `birth_note` refuses when the galaxy shows another folder by then (`NotesMoved`), and the job takes back what it
  made. A note that it made in the folder the galaxy has left is removed by its identity only
  (`Galaxy.unbirth_left`: a plain file in the bound folder whose bytes still have the `born` fingerprint).
  `Galaxy.unbirth` itself never takes a receipt of another folder.
- The REA version is read from the installed files on EVERY call (`rea_reading`): there is no cache, so a new
  install is seen at once. A profile path is compared as it is on a Mac or Linux. Only Windows folds case.
- `run_rea` runs only the pinned REA version. On a Mac or Linux a process is "our Chrome" only when the system
  says that its program is the browser that `find_chrome()` gives, the `chrome` file beside it, one of its Mac
  helpers, or Chrome's crash helper (`ps -o comm=`, joined by pid in `posix_table`; `is_chrome_program`), and it
  names the profile in `--user-data-dir=` (the crash helper in `--database=`). Another program is never stopped.
  A process on the profile whose program is not known, a process list that cannot be read, or no browser found
  is "could not look": nothing is stopped on a guess, and no check says "clean".
- The identity of a note is its notes folder, its path and, for a note of this run, the fingerprint of the bytes
  that were written (`note_id`), not its title. The sources of a reply and its graph numbers come from one locked
  step (`chat_route`, `search_route`). Every capture receipt carries `"build"` and `"born"` (that fingerprint),
  written under the lock. `/forget` needs both, taken from the receipt: it moves a note only when it is the same
  creation, in the same galaxy. Two captures with exactly the same bytes count as the same note.
- Every receipt also carries `"vault"`: a fingerprint of the notes folder (`vault_of`), never its path. `unbirth`
  takes a note back only in the same vault and with the same `born`. A note of another folder is never touched.
- Node numbers belong to one state of the galaxy. The page uses the node numbers of a reply only when the galaxy
  on screen is exactly the reply's snapshot (the same `build_id` and the same `rev`), a receipt only when its
  `build` is the build on screen, and never on a galaxy that it could not read again.
- The page publishes a revision only when the change it describes is installed. A receipt that cannot be applied
  makes the page doubt the galaxy and read it again. The numbers in the last status of a teardown only say if a
  fresh read is needed. A timer that focuses a note checks the snapshot and the note again when it fires.
- The drill counts only an end that is confirmed (`follow_job`). Only a good status answer counts (`job_of`: it
  has the key `job`, and a job has an `id`), and a job ends only on `done`, `failed` or `cancelled`. An end that is not confirmed is a FAIL.
  When another job or no job answers, the end of our job was not seen: that is "not confirmed", with the last view
  that was ours. `put_back` trusts only a good `/status` answer with whole graph numbers (`served_graph`).
- In `page_checks.js` a story that throws, runs out of time or leaves a promise rejection behind is a failure.
- `main()` takes the port first and loads the galaxy after it. A second start that finds the port busy changes
  nothing. `Galaxy.refresh_if_rebuilt` follows the `notes_dir` that `config.json` names now.
- Library fingerprints live in ONE table (`FINGERPRINTS`). A "presence" pattern must be a string that only that
  library has: a name that other libraries also mention proves nothing.
- `/teardown/note` and `/teardown/image` serve only files inside the teardowns folder (`valid_name`). The card and
  the reader show no file paths.
- A test that runs a teardown uses a hidden browser (`"headless": true`, or a `?headless=1` tab) and
  `drill.record()` / `drill.put_back()`. A test never starts a real teardown without them. The one test with a
  window is `python drill.py --visible`. It still records and puts back.
- The drill compares the galaxy by its shape (`drill.shape`: note names and link pairs), not by node numbers. A
  galaxy that is built again can number the same notes in another order.
