# Jarviz build list

Source: `%USERPROFILE%\Downloads\jarviz.pdf` (12 prompts in 2 parts).
Rule: a job is done only when `python preflight.py` passes.

## What Jarviz needs from you

- [x] **Anthropic API key.** It is in `config.json` and it works.
- [ ] **Keys for the other brains (optional).** See "Brains" below.
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

Jarviz has five brains. One is active. The screen shows it at the top (`brain: ...`).

| Brain | Key line in `config.json` | Model line | Model now |
| --- | --- | --- | --- |
| Claude (`anthropic`) | `anthropic_api_key` | `model` | `claude-opus-5-5` |
| OpenAI (`openai`) | `openai_api_key` | `openai_model` | `gpt-6.2-sol` |
| DeepSeek (`deepseek`) | `deepseek_api_key` | `deepseek_model` | `deepseek-flash` |
| Venice (`venice`) | `venice_api_key` | `venice_model` | `venice-uncensored-1-2` |
| OpenRouter (`openrouter`) | `openrouter_api_key` | `openrouter_model` | `openrouter/auto` |

- **Add a key:** open `config.json`, replace `PUT-YOUR-KEY-HERE` on that brain's key line, save. No restart.
- **Switch:** say or type "switch brain to DeepSeek". Or change the `"brain"` line in `config.json`.
- **Ask:** "which brain are you using?"
- A brain without a key cannot be chosen. Jarviz tells you.
- Your notes go to the company of the brain that you choose.
- After you add a key, run `python preflight.py`. It makes one real call to every brain that has a key.
- [x] Claude: tested with the real key.
- [ ] OpenAI, DeepSeek, Venice, OpenRouter: tested with a fake provider only. Not proven until a real key is in.

## Part One: the foundation

- [x] **01 Galaxy and brain.** `build.py`, `server.py`, `viewer/`. The galaxy shows 30 notes. `/chat` gives a clean "no key" answer, not a crash.
- [x] **02 Voice, both directions.** Speaking, listening, the finish window (`FINISH_MS = 900` at the top of `viewer/core.js`), voice routes, `?mute=1`, status line.
- [x] **03 Prove where the answer came from.** One note: the camera flies to it. Four or more notes: the cluster lights and the camera stays. Small talk: nothing moves.
- [x] **04 The butler.** The persona block is at the top of `server.py`. Greeting "Hello, sir." The note count on screen comes from the graph data.
- [x] **05 Total Recall.** "Remember that ..." writes a real file in `notes/captures/`. A new star is born live. "Forget that" takes the last one back.
- [x] **06 Preflight.** `preflight.py`. It had 12 live checks at the end of Part One. Now it has 17.

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

- [x] Last run: `PREFLIGHT: 17 pass, 0 fail, 0 warn` and `DRILL: 51 pass, 0 fail`.
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

The page lock (it was the known gap, it is closed now):

- [x] Port 4700 answers every program and every account on this computer. Now a caller without your page key gets nothing: not the page, not the notes, not the brain, not a teardown. Preflight tries 24 ways without the key.
- [x] The test tools never send the key. They sign each request with it. `python server.py --link` shows the link only after the server proved that it holds the key. So a program that sits on port 4700 in place of Jarviz gets nothing.
- [x] The key is in `%USERPROFILE%\.jarvis-tools\page-key`, outside the project. Other accounts on this computer cannot read your home folder.
- [ ] What no lock can stop: a program that runs as you. It can read the key file. It can read `config.json` too.
- New key: delete the file `page-key` and start Jarviz again with `start-jarviz.bat`. Every browser must be unlocked again.

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
- [ ] A teardown written by a brain other than Claude.

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
