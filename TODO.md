# Jarviz build list

Source: `%USERPROFILE%\Downloads\jarviz.pdf` (12 prompts in 2 parts).
Rule: a job is done only when `python preflight.py` passes.

## What Jarviz needs from you

- [x] **Anthropic API key.** It is in `config.json` and it works.
- [ ] **Keys for the other brains (optional).** See "Brains" below.
- [x] **Codex and Antigravity (no key).** Both tools are on this computer and you are signed in to both. See "Brains" below.
- [x] **An Ali Memory key.** Jarviz has its own read-only token of your Ali Memory hub (the entry `jarviz:ro` in the hub's `.env`, and `ali_memory_api_key` in `config.json`), so the Claude brain can use your knowledge base. See "Ali Memory" below.
- [ ] **Your own notes (optional).** Set `notes_dir` in `config.json` to the full path of your notes folder. Then run `python build.py`. Now Jarviz uses 30 sample notes about a coffee business in `notes/`.
- [x] **A yes for REA (Part Two).** REA 6.7.0 is installed in `%USERPROFILE%\.jarvis-tools\rea`, outside the project.
- [ ] **A newer Node.js (optional).** You have Node.js 24.15.0. The newest REA (6.10.1) needs 24.22.1 or later. So Jarviz is pinned to REA 6.7.0, the newest one that runs on your Node.js. See "Upgrade REA" below.
- [x] Google Chrome: found. The microphone works in Chrome and Edge only.
- [x] Python 3.14, Node.js 24, the `anthropic` package: found.

## How to run

1. Double-click `start-jarviz.bat`. It starts Jarviz, opens the page and unlocks your browser.
2. The page is locked for everyone else. Your browser keeps its key for one year. After the first time, http://127.0.0.1:4700 opens by itself.
3. Another browser shows "Jarviz is locked"? Open a terminal in this folder. Run `python server.py --link`. Paste the link into that browser.
4. Check everything: `python preflight.py`
5. Check the teardown kit end to end: `python drill.py` (one hidden test teardown, then it removes its own note)
6. The same drill with the Chrome window on screen: `python drill.py --visible`
7. Silent test tab: http://127.0.0.1:4700/?mute=1

## Brains

Jarviz has seven brains. One is active. The screen shows it at the top (`brain: ...`). Five use a key. Two use a sign-in and need no key.

| Brain | Key line in `config.json` | Model line | Model now |
| --- | --- | --- | --- |
| Claude (`anthropic`) | `anthropic_api_key` | `model` | `claude-opus-5-5` |
| OpenAI (`openai`) | `openai_api_key` | `openai_model` | `gpt-6.2-sol` |
| DeepSeek (`deepseek`) | `deepseek_api_key` | `deepseek_model` | `deepseek-flash` |
| Venice (`venice`) | `venice_api_key` | `venice_model` | `deepseek-v4-1-flash-1017-uncensored` |
| OpenRouter (`openrouter`) | `openrouter_api_key` | `openrouter_model` | `openrouter/auto` |

- **Add a key:** open `config.json`, replace `PUT-YOUR-KEY-HERE` on that brain's key line, save. No restart.
- **Switch:** say or type "switch brain to DeepSeek". Or change the `"brain"` line in `config.json`.
- **Ask:** "which brain are you using?"
- A brain without a key cannot be chosen. Jarviz tells you.
- Your notes go to the company of the brain that you choose.
- After you add a key, run `python preflight.py`. It makes one real call to every brain that has a key.
- [x] Claude: tested with the real key.
- [x] DeepSeek, Venice, OpenRouter: tested with real keys. Each one answered one real question in preflight.
- [ ] OpenAI: the key is accepted, but the account has no credit, so OpenAI cannot answer ("You have no credits remaining"). Add credit at OpenAI, then run `python preflight.py`. An older test only read the model's page and said "ready". The test now asks one real, very small question, so it tells the truth.
- Venice refused its key in one earlier run ("Authentication failed"). In the last runs it answered. If it refuses again, make a new key at Venice and type it into `venice_api_key`.

### The two sign-in brains (no key)

| Brain | Its tool | Sign in once | Model line in `config.json` |
| --- | --- | --- | --- |
| Codex (`codex`) | `codex` (OpenAI Codex CLI) | `codex login` in a terminal | `codex_model` (empty: the tool's own model) |
| Antigravity (`antigravity`) | `agy` (Google Antigravity CLI) | start `agy` once and sign in | `antigravity_model` (empty: the tool's own model) |

- **Switch:** say or type "switch to Codex" or "switch to Antigravity". To go back: "switch to Claude".
- **How it works:** Jarviz starts the tool on your computer for each question. The tool uses its own sign-in. Jarviz never reads or copies the sign-in.
- **The lock:** these tools are coding agents. They can run commands and read files. Jarviz starts them locked, so they can only talk. A real trap test proves the lock: Jarviz asks the brain to read a trap file, to run a command and to write a file. All three must fail. Jarviz does this test by itself for each new version of a tool, before the first question. The proof is kept in `%USERPROFILE%\.jarvis-tools\brain-locks.json`.
- **If a brain reaches for a tool,** Jarviz throws that answer away and tells you.
- [x] Codex (`codex-cli 0.171.0`): a strong proof. In the trap it tried to write a file, and the sandbox refused. It answered real questions about the notes on the page.
- [x] Antigravity (`agy 1.5.18`): a strong proof. In the trap it tried to call one of your MCP tools, and the deny rule refused. It answered real questions about the notes on the page.
- **Strong and weak proof:** a strong proof saw a real attempt that the lock refused. A weak proof saw no word come back, no file and no tool, but also no attempt. Jarviz works with both. Preflight shows a weak proof as a warning. (Antigravity gives a strong proof only when you have an MCP server, because that is the attempt it can make.)
- **Slower than a key brain.** Codex needs about 6 to 10 seconds for an answer. Antigravity needs about 15 to 30 seconds.
- **Each question uses your plan.** The tool sends its own long instructions with every question: about 23,000 tokens for Codex and about 31,000 for Antigravity. This counts against the allowance of your ChatGPT plan or Google plan.
- **History:** Codex keeps nothing (`--ephemeral`). Antigravity keeps each question as one conversation in its own history (`agy`, then `/resume` shows them). Jarviz cannot turn that off and does not delete it.
- **The first question after a tool update** takes about half a minute longer: Jarviz proves the lock again first. `python preflight.py` does the same proof.
- **A model of your choice:** put its id on the model line. For Antigravity, `agy models` in a terminal shows the ids.
- **Google's side sometimes does not start.** On 26 Nov about 1 start in 3 failed ("empty conversation id"), also with `agy` alone. Jarviz tries again by itself, up to 3 times. It costs about 10 seconds more.
- **A strong proof is not seen in every run.** Sometimes the model says "cannot" and does not try. Then Jarviz runs the trap once more. A weak run never replaces a strong proof of the same version.

## Voice

Jarviz can speak with three voices. The screen shows the one in use at the top (`voice: ...`).

| Voice | What it is | What it needs |
| --- | --- | --- |
| browser | The voice of your browser. It is free. | Nothing. |
| Deepgram | A recorded-quality voice from Deepgram. The start voice is `aura-2-draco-en` (a man, British, calm). | A Deepgram key in `config.json`. |
| ElevenLabs | A recorded-quality voice from ElevenLabs. The start voice is `6WwXjDDEMyNmFG95zycZ` (Eldrin, a crisp British baritone). The start model is `eleven_flash_v2_5` (fast). | An ElevenLabs key in `config.json`. |

- **One line for each name in `config.json`.** When the line is there already (for example with `PUT-YOUR-KEY-HERE`), change only the text between its quotes. Add a line only when it is not there. When a name is there two times, the last line wins: then Jarviz can miss your key. This is true for every line below.
- **Add the Deepgram key:** make a key at Deepgram (a new account gets free credit). Open `config.json`. Find the line `"deepgram_api_key"` and put your key between its quotes, in place of `PUT-YOUR-KEY-HERE`. No such line? Add this line right after the first `{`, with your key between the quotes: `"deepgram_api_key": "YOUR-KEY-HERE",`. Keep the comma at its end only when another line follows it (a file that holds only `{}` gets the line without the comma). Save. No restart.
- **Add the ElevenLabs key:** make a key at ElevenLabs. A restricted key needs the permission for Text to Speech. Open `config.json`. Find the line `"elevenlabs_api_key"` and put your key between its quotes, in place of `PUT-YOUR-KEY-HERE`. No such line? Add this line right after the first `{`, with your key between the quotes: `"elevenlabs_api_key": "YOUR-KEY-HERE",`. Keep the comma at its end only when another line follows it (a file that holds only `{}` gets the line without the comma). Save. No restart.
- **The names must be exact.** `elevenlabs_api_key`, not `elevenlabs_labs_api_key` or `eleven labs api key`. A line with another name is not read. Every line but the last one ends with a comma.
- **Switch:** say or type "switch voice to Deepgram" or "switch voice to ElevenLabs" ("eleven labs" and "use the ElevenLabs voice" work too). To go back: "switch voice to browser".
- **The choice stays in that browser.** Another browser starts with the default voice. To make Deepgram the default, set the line `"voice": "deepgram",` in `config.json` (for ElevenLabs: `"voice": "elevenlabs",`).
- **Another Deepgram voice:** set the line `"deepgram_voice": "aura-2-pandora-en",` (a woman, British, calm). Other names: `aura-2-odysseus-en`, `aura-2-thalia-en`.
- **Another ElevenLabs voice:** set the line `"elevenlabs_voice": "fnYMz3F5gMEDGMWcH1ex",` (Finley). Other ids: `gOupLcAkjEnguROwi4oS` (Darian), `jSuBIjxMKhqIfb0wCK1F` (Baxter). Or copy the id of a voice from "My Voices" in ElevenLabs. Without the line Jarviz uses Eldrin.
- **Another ElevenLabs model:** set the line `"elevenlabs_model": "eleven_multilingual_v2",`. Other ids: `eleven_v3`, `eleven_v4`. Without the line Jarviz uses `eleven_flash_v2_5`.
- **Why Eldrin and not George:** ElevenLabs ends its Default voices (George is one) on 31 December 2026. Eldrin is an Infinity voice: it has no end date.
- **What goes where.** The page never gets your key. The page asks Jarviz for the sound. Jarviz sends two things to the voice you chose (Deepgram or ElevenLabs), over HTTPS: your key for that voice, and the text to speak. The other voice gets nothing.
- **It costs money per letter.** Deepgram counts the letters that Jarviz says (about 3 cents for 1,000 letters). ElevenLabs counts them against the credits of your plan. A muted test tab asks for no sound, so it costs nothing.
- **If the server voice does not answer,** the browser voice says the line. After three failures in a row Jarviz stops asking it until you switch again.
- **Listening is still the browser's job** (Chrome and Edge only). Deepgram can also listen. That is not built.
- [x] Deepgram with a real key: tested on 2026-11-27. `python preflight.py` sent tiny real requests. Real mp3 sound came back from Deepgram (`aura-2-draco-en`), also through `/voice/speak` in 1 and 2 pieces.
- [x] ElevenLabs with a real key: tested on 2026-11-27. `python preflight.py` sent tiny real requests. Real mp3 sound came back from ElevenLabs (Eldrin, `eleven_flash_v2_5`), also through `/voice/speak` in 1 and 2 pieces.
- [ ] The two server voices in the page: NOT tested. Nobody listened to them in a browser yet. The page rules are proved in Node with a fake page (`page_checks.js`).
- **Your ElevenLabs voice now:** `wiF43F1vngB3iRFsbWhJ` (Nathaniel, a sharp British voice with a dry wit, made for AI assistants), on the line `elevenlabs_voice`. It has the style of a film AI butler. It is not a copy of an actor's voice.

## Ali Memory

Ali Memory is your own knowledge base: the hub on this computer, http://127.0.0.1:8791. When the brain is Claude, Jarviz asks it for the best 3 results of each question and gives them to Claude with the question. Jarviz only reads: it never saves anything in Ali Memory. With any other brain, Jarviz answers without Ali Memory.

- **The key:** Jarviz has its own read-only token: the entry `jarviz:ro:...` in `CEREBRAS_MEMORY_HTTP_TOKENS` in the hub's `.env`, and the same secret on the line `"ali_memory_api_key"` in `config.json`. To take it away, remove both and restart the hub (the hub's README, "Recall for hooks"). A backup of the `.env` from before is in the hub's `backups` folder (`.env.bak-jarviz-...`).
- **Another address:** only when the hub runs on another port, for example `"ali_memory_url": "http://127.0.0.1:8792",`. Only 127.0.0.1 and localhost are taken.
- **On screen:** under an answer that used Ali Memory, one line names the memories: "From Ali Memory: ...". The start window says `Ali Memory: on (Claude only)`, or why it is off.
- **What goes where.** Jarviz sends your question and the read-only token to the hub on this computer, and nothing else. The hub's results go to Claude (Anthropic) with the question: at most 3 snippets of 1,200 characters. The page gets the titles only. Another brain gets none of it, also not after a switch: the history it reads leaves out every answer that used Ali Memory.
- **It waits at most 3 seconds.** A hub that is down, slow or still starting changes nothing: Jarviz answers without it, and says nothing.
- [x] The token with the real hub: tested on 2026-11-27. One real recall with the test question "Jarviz" gave HTTP 200 and 3 results.
- [ ] Ali Memory in the page: NOT tested in a browser. The page rule is proved in Node (`page_checks.js`).

## Part One: the foundation

- [x] **01 Galaxy and brain.** `build.py`, `server.py`, `viewer/`. The galaxy shows 30 notes. `/chat` gives a clean "no key" answer, not a crash.
- [x] **02 Voice, both directions.** Speaking, listening, the finish window (`FINISH_MS = 900` at the top of `viewer/core.js`), voice routes, `?mute=1`, status line.
- [x] **03 Prove where the answer came from.** One note: the camera flies to it. Four or more notes: the cluster lights and the camera stays. Small talk: nothing moves.
- [x] **04 The butler.** The persona block is at the top of `server.py`. Greeting "Hello, sir." The note count on screen comes from the graph data.
- [x] **05 Total Recall.** "Remember that ..." writes a real file in `notes/captures/`. A new star is born live. "Forget that" takes the last one back.
- [x] **06 Preflight.** `preflight.py`. It had 12 live checks at the end of Part One. Now it has 19, and one more for each extra brain that has a key.

Proven with the real brain:

- [x] Real answers in character, in 2 to 4 seconds.
- [x] One source: the camera flies to the note. Six sources: the cluster lights. Small talk: nothing moves.
- [x] "Please make a note that ..." saves a real note. "Forget that" takes it back.

Still to prove:

- [ ] Do the voice test below with a real microphone and real sound. Only you can do this one.

## Part Two: the X-ray

- [x] **07 Install REA the safe way.** REA 6.7.0 in `~/.jarvis-tools/rea`, pinned (`REA_PIN` in `teardown.py`). The npm package points to github.com/morluto/rea, and npm holds a build proof from that repository. `teardown.py`, `/teardown/diag`.
  The commands that were run, in `%USERPROFILE%\.jarvis-tools\rea`:
  1. `npm view rea-agents repository.url` (it must name github.com/morluto/rea)
  2. `npm install rea-agents@6.7.0 --save-exact --ignore-scripts`
  3. `npm audit signatures` (174 packages verified)
- [x] **08 A clean browser, every time.** A new Chrome profile for each job on port 9333. It is closed and its folder is deleted after the job, also after a cancel or an error.
- [x] **09 The capture.** REA watches while Jarviz turns the wheel. REA reads the code. REA takes the snapshot. Jarviz adds the file log and the version fingerprints.
- [x] **10 The teardown.** Evidence in, a note out: `notes/captures/teardowns/HOST-DATE.md` with its snapshot. Born in the galaxy through the same door as captures, linked to a `REA` note. The `REA` note is linked to a `Jarviz` note. The first teardown makes both.
- [x] **11 One sentence, out loud.** Voice routes for sites, the live card (top left), the reader.
- [x] **12 The drill.** `python drill.py`. Offline checks, a live drill that leaves no trace, three more preflight checks. The drill also cancels one teardown and runs one against an address where nothing answers. Each time it proves that no Chrome is left.

Proven with the real brain and a real public site (igloo.inc):

- [x] Last run: `PREFLIGHT: 49 pass, 1 fail, 0 warn` and `DRILL: 115 pass, 0 fail`. The one fail is the OpenAI account: it has no credit (see "Brains"). Preflight has 50 checks now.
- [ ] The page in the test browser was not opened in the last run: the test browser lost its unlock. The page rules (`page_checks.js`, 108 rules) ran against the files that the server gives out.
- [x] The drill passed with a hidden browser, and also with the Chrome window on screen (`python drill.py --visible`). The snapshot from the visible window is a real picture.
- [x] A full teardown in about one minute: 96 files, 14.7 MB, Three.js r165, GSAP 3.12.5, postprocessing 6.35.5.
- [x] The card shows the 8 steps live, then the snapshot, the stack and the totals. The reader shows the whole note.
- [x] New stars are born and the camera flies to the teardown. The first time there are three: Jarviz, REA, the teardown. After that, one for each teardown.
- [x] An address where nothing answers: "The site did not answer, so there is nothing to take apart." No note is written.
- [x] "Tear down the old shed" starts nothing. "Reverse engineer Stripe's homepage" starts stripe.com through the brain.
- [x] "Cancel the teardown" closes the browser. No Chrome and no profile folder is left.

Review:

- [x] Two reviewers checked the Part Two code (Python and security). Their findings are fixed and the tests ran again.
- [x] The whole PDF was checked against the code, prompt by prompt. Five gaps were found and fixed (see "The PDF check" below).
- [x] CodeRabbit ran on the first commit: 44 files, 0 findings.
- [x] CodeRabbit ran three times on the new work (the lock, the drill, the page lines): 4 small findings, then 2 in the lock tests, then 1 in the lock. All 7 are fixed.
- [x] The last two CodeRabbit reviews found nothing: the last commit, and then the whole project in one deep review.
- [x] One more security review read the page lock. Nothing had to be fixed. Three of its ideas made the lock stronger.
- The six reviews above ran from this computer with CodeRabbit's free review. The pull request on GitHub gets CodeRabbit's full review when CodeRabbit is installed for the repository.
- [x] The code is on GitHub, in the public repository `alinojoumi8/jarviz`. The work is on the branch `first-build`, with a pull request to `master`.
- [x] The Codex review on the GitHub pull request found 4 problems. All 4 are fixed:
  - When a public site sends the clean browser to a private IP address, Jarviz sees it and stops the job (`is_private_address`). Chrome has already sent that one request: see "Not proven" below.
  - After a note is born or taken back, the live links are the same as after a full rebuild (`build.relink`).
  - A teardown tag from the brain runs only for a sentence that asks for a teardown, not one that only mentions a site.
  - A teardown that fails takes back the Jarviz and REA notes it made (`Galaxy.unbirth`).
- [x] CodeRabbit then read those 4 fixes and gave 8 findings. 6 are fixed:
  - The teardown stops when the browser cannot say where the page is, or when the page is not on a web address. Before, it went on.
  - A take-back deletes a note for good only when the file still holds what Jarviz wrote. A file that someone changed stays.
  - A save that fails puts the index in memory back first, then the file. If the file cannot be put back either, the error says where the file is now. Jarviz never puts it back on top of another file.
  - A picture that cannot be deleted no longer stops the take-back of the notes.
  - A `[[link]]` that thousands of notes use can no longer make the server freeze when a note with that name is born.
  - "What did my teardown of stripe.com say?" does not ask for a new teardown.
- The other 2 findings are one limit that is not closed. It is written under "Not proven" below.
- [x] CodeRabbit read those fixes again and gave 13 findings. 10 are fixed (a put-back never writes over another file, a take-back checks the file that it will delete, "can you summarize my teardown" starts nothing, and 7 smaller ones). 3 were not real: a trace and a small test showed it.
- [x] CodeRabbit is now installed on GitHub. Its first review of the whole pull request gave 18 major and 37 minor findings. 44 are fixed. The ones that matter most:
  - "Do not tear down stripe.com" no longer passes as a request. "How did my teardown of stripe.com work?" no longer starts a new teardown from the page.
  - A signed request from a tool works one time only.
  - The brain test asks a real question. (It found that the OpenAI account has no credit.)
  - No provider error can show a key. A change of brain keeps every other character of `config.json`.
  - A second start of Jarviz no longer rebuilds the galaxy under the Jarviz that runs. A new `notes_dir` is followed without a restart.
  - The microphone: a late result after "off" saves nothing. The page never adds a note on top of an old picture of the galaxy.
  - The teardown receipt goes to the turn that started it. A cancel from a test can stop only its own job.
- [x] CodeRabbit read the fixes again and wrote 14 replies. 1 took a finding back, 5 asked only for the test result, and 8 had a real remaining point. All 8 are fixed:
  - In "do not tear down stripe.com, then tear down example.com", only example.com can be torn down.
  - A signed POST uses up its signature only after its body was checked.
  - The address check looks up the name that Chrome opens, with its "www.".
  - On a Mac or Linux, only a real Chrome that names the test profile is stopped.
  - "Forget that" does not touch a note from a galaxy that was built again. The page compares its galaxy with the server at the start.
  - A note is known by its path, not by its title, so two notes with one title are never mixed up.
  - The drill says "put back" only when the end of its job is confirmed.
  - A late teardown answer no longer closes the reader or opens the card.
- [x] The rules of the page now have their own test file, `page_checks.js`. Preflight runs it.
- [x] CodeRabbit wrote 11 more replies. 6 had a real remaining point, and all 6 are fixed:
  - On a Mac or Linux, Jarviz asks the system which program a process is. A script with "chrome" in its name is never stopped.
  - A signature whose time is over can never be used, also when its content comes late.
  - The drill takes only a good status answer. An error page is not "no job".
  - Each saved note carries the number of its galaxy. "Forget that" needs that number.
  - In "tell me about stripe.com, then tear down example.com", only example.com can be torn down. "I like igloo.inc. Take it apart." still works.
  - A note is known by its notes folder and its path.
- [x] A fourth set of 5 replies: 1 closed its thread, 4 had a small point, all 4 fixed. The time of a signature is read inside the lock. On a Mac or Linux only the exact Chrome that Jarviz starts counts, and a process that nobody can name stops nothing. The drill takes only the end states it knows. The page uses a node number only for the galaxy it belongs to. `page_checks.js` has 79 rules now.
- [x] A fifth set of 4 replies: 2 tiny points fixed (the drill wants a job with an id; the page lights a note only on the exact state of the galaxy that the answer was made from). 1 point is not changed, on purpose: on a Mac or Linux, Jarviz knows "its" Chrome by the program's name and by the test profile in its command line, not by the full file path of the program. A program with Chrome's exact name that runs on Jarviz's own test profile is treated as that Chrome. This part runs only on a Mac or Linux, and this computer has Windows, so more changes there cannot be tested here.
- [x] A sixth set of 3 replies: 1 line fixed (on a Mac or Linux, "no browser found" now means "could not look", not "clean").
- [x] One more reply corrected me, and it was right: two notes can have the same title and another text, because a title takes only the first 7 words. Now every saved note carries a fingerprint of its own text. "Forget that" works only for the note with that fingerprint, so an old receipt can never move another note to the trash.
- The other 5 replies say one thing: preflight must be all green. Its one fail is the OpenAI account with no credit. Add credit, or take the OpenAI key out of `config.json` (then that brain is not tested), and run `python preflight.py`.
- 11 findings are not changed, on purpose:
  - The drill deletes its own test note also when a program changed it. A test note is not your work.
  - A cancel that comes while the note is saved: Jarviz already answers "too late, the note is saved".
  - A link file (symlink) inside your notes folder is read. The folder is yours.
  - The lock keeps `_door` (the key in the address) next to `_unlocked`. A security review read this design.
  - A tag inside the brain's sentence still runs. The brain writes its line and the tag together, and the tag runs only when you asked.
  - When a note is saved during a teardown that fails, the Jarviz note can stay. Jarviz says which notes stay.
  - Preflight does not download the two libraries to check their hash. The browser does that at each start.
  - The file sizes in a teardown note count a file from the cache with its body size.
  - An address with a `?` part is refused. Jarviz opens a host and a path only.
  - three.js stays at 0.186.0. The library inside 3d-force-graph is the same release (r186).
  - The sample note "Wholesale Price List" is a sample.
- [x] CodeRabbit reviewed the pull request again after the sign-in brains: 42 findings (24 in threads, 18 small ones). All 42 were real. All 42 are fixed. The ones that matter most:
  - A lock proof of a sign-in brain is now STRONG or WEAK. Strong means: the trap saw a real attempt, and the lock refused it. Both brains give a strong proof on this computer.
  - Antigravity must say at the start of every run that it took the lock. If it does not, its answer is thrown away.
  - A sign-in tool is never started through a batch file (`.cmd`, `.bat`), because Windows starts a shell for those.
  - A key is taken out of an error text before the text is cut. The Claude package no longer follows a redirect.
  - A brain test fails when the answer is not a real answer (an empty reply with no envelope).
  - "Cancel" with an empty job id stops nothing. Two refused teardown starts at the same moment leave the chat memory as it was.
  - A teardown keeps its picture and its notes in one notes folder. A note of another notes folder is never taken back.
  - Jarviz reads the REA version again when the installed files change.
  - The page: a sentence that refuses a teardown starts none. Two saves at the same moment no longer hide a note.
  - The drill fails when it did not see the end of its own job, and when the server does not confirm the clean-up.
- [x] CodeRabbit answered those fixes with 7 replies. 5 were small real points, and all 5 are fixed:
  - Jarviz reads the REA version at every call (a new install with the same file size and time is seen too).
  - On a Mac or Linux a brain switch no longer makes `config.json` readable for other users of the computer.
  - When the notes folder changes in the middle of a teardown, the first note of that teardown is removed from the old folder too.
  - A chat brain whose answer has no message inside fails the test.
  - One more rare case where a piece of a key could stay at the end of a cut error text.
- The other 2 replies need no code: CodeRabbit accepts that a weak lock proof is allowed, and it asks for the preflight result (it is above).

The page lock (it was the known gap, it is closed now):

- [x] Port 4700 answers every program and every account on this computer. Now a caller without your page key gets nothing: not the page, not the notes, not the brain, not a teardown. Preflight tries 24 ways without the key.
- [x] The test tools never send the key. They sign each request with it. `python server.py --link` shows the link only after the server proved that it holds the key. So a program that sits on port 4700 in place of Jarviz gets nothing.
- [x] The key is in `%USERPROFILE%\.jarvis-tools\page-key`, outside the project. Other accounts on this computer cannot read your home folder.
- [ ] What no lock can stop: a program that runs as you. It can read the key file. It can read `config.json` too.
- New key: close Jarviz first (close its window). Then delete the file `page-key`. Then start Jarviz again with `start-jarviz.bat`. Every browser must be unlocked again. A Jarviz that still runs keeps the old key.

The PDF check (what was different from the PDF):

- [x] Fixed: the `REA` note is now linked to a `Jarviz` note (prompt 10).
- [x] Fixed: the butler's lines that were inside `viewer/app.js` are now in the persona block of `server.py` (prompt 04). A new character is one block again.
- [x] Fixed: the drill now compares every note and every link before and after, and a note that you dictated before the drill does not make it fail (prompt 12).
- [x] Fixed: the drill now runs a teardown that fails, and proves that no Chrome is left (prompt 12).
- [x] Fixed: the install commands are written in this file (prompt 07).
- Kept on purpose: a title shorter than 4 letters does not link notes by mention, and a `[[wikilink]]` that more than 10 notes use does not link them. Without these two rules the galaxy is one ball of links.
- Kept on purpose: REA 6.7.0 does not give the strings for the graphics card, so Jarviz finds them in the code that REA captured (`CODE_SIGNALS`). The evidence line says who found them.
- Kept on purpose: the drill removes only the files that its own test made. It does not copy an old snapshot back, because that can delete a real teardown that you made in the same minute.
- Jarviz has no log on disk of what was said. So the drill has no log to restore.

Not proven:

- [ ] "This site" on a Mac (it reads the front Chrome tab). This computer has Windows, so Jarviz asks for the name.
- [ ] The rule for "which process is our Chrome" on a Mac or Linux. It is tested with made-up process lists only. Run `python drill.py` on such a computer before you trust it there.
- [ ] A teardown written by a brain other than Claude.
- [ ] The lock of a sign-in brain is proven by a trap, not by a law of nature. The limits:
  - The trap shows what the model can do when it is asked to try. It reads a file, runs a command and writes a file. A tool that the trap does not ask for is not proven absent.
  - Codex keeps one inner tool (`exec`, a small JavaScript box). In real tests it had no file access, no network and no command. But the event stream of Codex does not show its calls, so Jarviz cannot see them. The read-only sandbox and the trap are the proof there.
  - Antigravity lists about 60 tools at its start. With Jarviz's agent file (an empty tool list) the model could call none of them in the tests. One tool stayed that only lists (`list_resources`): it showed the names of your own MCP servers to the model. Jarviz lets that pass. `read_resource` (it reads from your MCP servers) could not be tested, because your MCP server has nothing to read. If it runs, Jarviz throws the answer away.
  - Codex and `agy` update themselves. Jarviz proves the lock again for each new version. Between an update and that proof no question is answered. But a new version can add a tool that the trap does not know.
  - A teardown with a sign-in brain gives the text of a strange website to a coding agent. The lock is what stands between them. If you want the smallest risk, write teardowns with a key brain.
- [ ] The sign-in brains on a Mac or Linux. The code has the paths for it, but it ran only on this Windows computer.
- [ ] The ElevenLabs start voice on a free plan. Eldrin is a voice of the Voice Library, and ElevenLabs says that some voices there need a paid plan. Whether a free plan may use it through the API is not known. If ElevenLabs refuses it, Jarviz says that your plan does not allow that voice, and `python preflight.py` names the line to fix. Then copy the id of a voice from "My Voices" into `elevenlabs_voice`.
- [ ] Spoken switches are patterns over what Chrome's speech recognition writes. The sentences in `SPOKEN_SWITCHES` (preflight.py) are proven. Another mishearing ("deep seat", "claud") still goes to the brain as a question until it becomes an alias. Look at the words Jarviz shows that it heard, and tell me a new one.
- [ ] `HOLD_MS` (2500, the wait for a name after "switch brain to") and `NAME_WAIT_MS` (15000, the time to answer "Which brain, sir?") are first guesses. Tune them after a day of real use, like `FINISH_MS`.
- [ ] While Jarviz says "Which brain, sir?" the microphone is deaf, so a name said over him is lost. Say it again after him.
- [ ] The persona tells the brain not to judge keys and not to pretend it can switch itself. A brain can still say otherwise. The page routes catch a switch before the brain sees it.
- [ ] Ali Memory and the brain's own words. Jarviz tames memory text (no ready-made control tag, no forged block) and tells Claude that it is evidence, never instructions. A memory can still mislead Claude in what it says. It can never start an action: a tag runs only when your own sentence asks for it.
- [ ] Ali Memory text that leaves the chat. In a chat where Claude saw Ali Memory (fresh results, or an earlier answer that used it), a note that Claude words may hold only your own words: the words of the fact must stand in your sentence. Otherwise nothing is saved, and Jarviz asks you to say "remember that ..." yourself. A teardown in such a chat opens only an address you said in full, and its question is your own sentence. Not proven: this is a word match, so your own sentence can still repeat what Claude told you a moment before, and a fact you said in other words is refused too. Every later Claude answer in that chat counts as one that used Ali Memory, so another brain's history leaves those answers out.
- [ ] A spoken answer that used Ali Memory. Claude's answer is spoken with your chosen voice. With ElevenLabs or Deepgram, the answer text goes to that company to be spoken, and some Chrome browser voices are network voices (Google gets the text). So words from Ali Memory can reach them through Claude's answer. Your choice was "only Claude sees Ali Memory": tell me if such answers should be spoken only with a voice on this computer.
- [ ] The hub on port 8791. Jarviz cannot tell the real hub from another program that listens on that port while the hub is down. That program would get the read-only token and your question. Only programs on this computer can listen there.
- [ ] The drill's clean-up on a Mac or Linux. It checks a test note's bytes again after moving it aside. An edit through a file handle that was opened before that move, in the instant before the delete, is not seen.
- [ ] A spoken line that holds a key. The server voices refuse such a line and send nothing. Then the browser voice says the line, and some Chrome voices are network voices (Google gets the text).
- [ ] An REA install at the very moment of a teardown. `run_rea` checks the pinned REA version just before it starts REA. An install that someone runs at that moment can swap the files between the check and the start, and Node reads REA's files only as it runs. Jarviz never installs REA itself.
- [ ] The removal of the test note on Mac and Linux. Preflight deletes it through folders opened without following links, checks that no folder was swapped, and moves the note to a random hidden name before it deletes it. That was tested in WSL on a native Linux folder (`/tmp`) and on a Windows drive (there it never deletes by hand: it puts the note back and names it). A real Mac was not tested. One gap remains: a program that watches the folder could see the hidden name and swap it in the instant before the delete. On Windows the delete goes through one handle on the file itself, and that was tested.
- [ ] The clean Chrome and your private network. Jarviz checks the address of the PAGE. It does not see the connections that Chrome makes. There are two cases:
  - The page itself moves to a private address. Jarviz sees that and stops the job. But Chrome has already sent the request that went there. Jarviz reads nothing from that page.
  - The page stays on its public address and asks for things on your home network (a picture, a script, a data call). Jarviz does not see these requests at all, and the job goes on. The same is true when a public name points at a private address only for Chrome.
  - The clean Chrome has no logins. An ordinary browser has the same weak point with such a site. To stop these requests, Jarviz needs a filter between Chrome and the network (a small proxy). That is not built: it is your decision.

## Try it

Voice test (Chrome, sound on):

1. Open the page. You see "Hello, sir." You hear nothing yet.
2. Click anywhere. You hear "Hello, sir."
3. Click the microphone button. Allow the microphone. The status shows `listening`.
4. Say: "What did we agree" ... wait half a second ... "with Hotel Marlowe". Jarviz sends ONE whole sentence. That proves the finish window.
5. While he talks, say "stop". He stops at once.
6. Open http://127.0.0.1:4700/?mute=1 and ask a question. You see the answer. You hear nothing.

Three questions from your notes:

- "What do we charge Hotel Marlowe?" (flies to one note)
- "What is planned for January?" (several notes: the cluster lights)
- "Good morning." (nothing moves)

Five lines for the butler:

- "Good morning, Jarviz."
- "How much is left on the loan?"
- "Who can roast besides Tom?"
- "Tell me a joke about decaf."
- "What is the Wi-Fi password?" (not in the notes: he must say so)

Your first teardown (four steps):

1. The site: `igloo.inc`. It is a public 3D site.
2. The sentence: "Reverse engineer igloo dot inc." Or type: `how does igloo.inc do that`
3. What you see: a Chrome window opens, scrolls by itself, and closes. The card at the top left shows 8 steps. After about one minute it shows "Taken apart in ... s", a snapshot, the stack and the totals. Three new stars appear the first time (Jarviz, REA, the teardown) and the camera flies to the new note.
4. What you hear: "Taking igloo.inc apart now, sir." Then, when it is done, two or three sentences with the answer.

More teardown lines:

- "Show me the last teardown." (the newest one on disk, also after a restart)
- "Open the full teardown." (the reader, with Open in Obsidian and Close)
- "Close the teardown."
- "Cancel the teardown." (stops a running one and closes the browser)
- "Tear down the old shed." (names no site: nothing starts)

Total Recall:

- Say "remember that the finish window should be 900 milliseconds". A new star is born.
- Then ask "what should the finish window be?"
- Say "forget that" to take it back.

## Make it yours

- The pause that ends a sentence: `FINISH_MS` in `viewer/core.js`.
- The character: the persona block at the top of `server.py`.
- Capture phrases: `REMEMBER_TRIGGERS` in `viewer/core.js`.
- Chat speed against depth for Claude: `EFFORT` in `brains.py`.
- A new brain: one entry in `PROVIDERS` in `brains.py` and two lines in `config.json`.
- The libraries Jarviz can name: the `FINGERPRINTS` table in `teardown.py`. Add a line for each library you care about.
- The effect names Jarviz explains: the `CODE_SIGNALS` table in `teardown.py`.
- The shape of a teardown note: `WRITER_RULES` in `teardown.py`.

## Upgrade REA (do it on purpose)

1. If the new REA needs a newer Node.js, install that Node.js first.
2. Change `REA_PIN` in `teardown.py` to the new version.
3. Install that version (the commands are in `/teardown/diag` when the pin and the install differ).
4. Run `python drill.py`. Use the new version only when the drill passes.
