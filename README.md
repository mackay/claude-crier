# Crier

**Hear a short spoken summary when each Claude Code session finishes or needs you — named per session — so you can run several at once and just listen.**

Crier is a local, zero-API voice-notification plugin for [Claude Code](https://claude.com/claude-code). When a session completes a turn, it speaks a one-line summary of *what got done and the next step*. When a session is blocked waiting on you, it says so. Each session gets its own name and voice, so with headphones in you always know **which** session is talking without looking.

It is **not** a read-the-whole-reply-aloud tool and **not** a two-way voice chat — it's an *awareness* layer for people juggling multiple agents.

```
"api-server — finished adding the search endpoint; next, run the tests."
"web-app — needs you: it has a question before continuing."
"docs-site, heads up. the test suite is failing on the new endpoint."
```

## Why

Running three or four Claude Code sessions in parallel, you lose track of which one stopped, which is blocked on a permission prompt, and which is quietly done. A terminal bell tells you *something* happened but not *what*. Crier tells you what, in whose voice.

## Requirements

- **macOS** (first-class): uses the built-in `say` command. No install, no API keys, no network.
- **Python 3** (the system `/usr/bin/python3` is fine — standard library only, nothing to `pip install`).
- Optional but recommended: download an **Enhanced** or **Premium** voice (see [Voices](#voices)). The default "compact" voices sound robotic.

Windows and Linux are supported on a best-effort, experimental basis — see [Platform support](#platform-support).

## Install

```text
/plugin marketplace add mackay/claude-crier
/plugin install crier@claude-crier
```

That's it — the hooks and slash commands activate immediately. No editing of your `settings.json`, no shell changes. (Crier ships only local command hooks; as with any plugin, install from sources you trust.)

## Usage

Voice is **off by default** and opt-in **per session** — turn it on only for the sessions you want to monitor:

| Command | What it does |
|---|---|
| `/crier:on` | Turn voice on for **this** session |
| `/crier:off` | Turn it off for this session (stops audio *and* the summary line) |
| `/crier:name <name>` | Set the spoken name for this session, e.g. `/crier:name api-server` |
| `/crier:persona` | List available voices; the current one is marked |
| `/crier:persona <#\|name>` | Pick a voice, e.g. `/crier:persona 3` or `/crier:persona Ava` (speaks a sample) |
| `/crier:rate <wpm>` | Set this session's speech rate, e.g. `/crier:rate 180` (`default` resets) |
| `/crier:focus on\|off` | Stay quiet while you're looking at this session's terminal — see [Focus mode](#focus-mode) |
| `/crier:doctor` | Diagnose your setup (engine, voices, session, on/off, rate, focus) |

> These live under the plugin's `crier:` namespace — type `/crier` in the prompt to see them all and pick one.

If you don't set a name, it defaults to the session's folder name. If you don't pick a voice, Crier assigns a **random Enhanced/Premium voice per session** — stable within a session, different across parallel ones, so they're easy to tell apart by ear.

### Silence everything at once

Create the file `~/.claude/voice/.muted` to instantly silence every session (e.g. you step into a meeting); delete it to resume. Handy aliases:

```bash
alias crier-mute='touch ~/.claude/voice/.muted'
alias crier-unmute='rm -f ~/.claude/voice/.muted'
```

### Focus mode

`/crier:focus on` keeps a session **quiet while you're actively looking at its terminal** — voice is for the sessions you *aren't* watching — and it speaks again once you switch away. It fails open: if it can't tell, it speaks.

Currently this works on **iTerm2 only** (it matches the focused pane via `ITERM_SESSION_ID`); on other terminals it reports unavailable and just keeps speaking. The first use may trigger a one-time macOS **Automation** permission prompt (to read iTerm2's focused session). Run `/crier:doctor` to confirm focus detection shows `available` in your terminal.

## How it works

Crier registers three [Claude Code hooks](https://code.claude.com/docs/en/hooks):

- **`SessionStart`** — when voice is on, injects an instruction asking Claude to end each turn with a hidden one-line summary marker (`🔊 VOICE: …`).
- **`Stop`** — extracts that summary line from the transcript and speaks it, prefixed with the session's name.
- **`Notification`** — speaks an alert when the session needs your input or permission. Repeated idle notifications are de-duplicated (at most once a minute), and a notification is dropped rather than queued if something else is mid-sentence — so it never plays *after* you've already answered the prompt.
- **Error flagging** — if Claude marks a turn as failed or blocked (it's asked to begin the summary with "Problem:"), Crier speaks it as a *"heads up"* alert so problems stand out by ear. This is self-reported, so it's reliable but not guaranteed.

Speech is serialized with a lock so two sessions finishing at once don't talk over each other. Per-session state (on/off, name, voice, rate, focus) lives in `~/.claude/voice/state/<session-id>.json`.

## Voices

On macOS, download better voices in **System Settings → Accessibility → Spoken Content → System Voice → Manage Voices**, and choose the **Enhanced** or **Premium** quality. Good natural picks:

| Voice | Accent |
|---|---|
| Ava (Premium), Zoe (Premium), Tom (Enhanced), Susan (Enhanced) | US |
| Serena (Premium), Jamie (Premium), Stephanie (Enhanced) | UK |
| Fiona (Enhanced) | Scottish |
| Moira (Enhanced) | Irish |

Then `/crier:persona` to browse and pick. Note: Siri voices are **not** available to `say` — only the Enhanced/Premium variants are.

Tip for parallel sessions: give each a different accent so you can identify them instantly.

## Platform support

| OS | Engine | Status |
|---|---|---|
| **macOS** | built-in `say` | ✅ Fully supported |
| **Windows** | PowerShell `System.Speech` | ⚠️ Experimental (built-in voices; no install) |
| **Linux** | `espeak-ng` / `spd-say` | ⚠️ Experimental (robotic; install required) |

**Bring your own voice (any OS):** set `CLAUDE_VOICE_CMD` to any command that reads text on stdin and speaks it — e.g. a [Piper](https://github.com/rhasspy/piper) pipeline for natural offline voices on Linux:

```bash
export CLAUDE_VOICE_CMD='piper --model en_US-amy-medium.onnx --output-raw | aplay -r 22050 -f S16_LE -t raw -'
```

Run `/crier:doctor` on any platform for tailored setup guidance.

## Configuration

All optional; per-session slash commands override these launch-time defaults.

| Variable | Purpose |
|---|---|
| `CLAUDE_VOICE=1` | Start a session with voice already on |
| `CLAUDE_SESSION_LABEL` | Default spoken name for the session |
| `CLAUDE_VOICE_NAME` | Force a specific voice, e.g. `"Ava (Premium)"` |
| `CLAUDE_VOICE_RATE` | Speech rate (words/min on macOS) |
| `CLAUDE_VOICE_CMD` | Custom TTS command (reads text on stdin) |

## Troubleshooting

Run **`/crier:doctor`** — it reports your platform, whether a TTS engine is found, installed voices, the detected session, and whether voice is on or off, then speaks a test line.

- **Silent?** Check voice is on (`/crier:on`), no global mute (`~/.claude/voice/.muted`), and an engine is available (`/crier:doctor`).
- **No summary, just "<name> finished"?** Claude didn't emit the `🔊 VOICE:` line that turn — usually transient; `/crier:on` re-states the instruction.

## Privacy

Fully local. No API keys, no network calls, no telemetry. Audio is synthesized on-device; transcripts are read only from your local Claude Code session files.

## License

MIT
