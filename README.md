# Crier

**Hear a short spoken summary when each Claude Code session finishes or needs you — named per session — so you can run several at once and just listen.**

Crier is a local, zero-API voice-notification plugin for [Claude Code](https://claude.com/claude-code). When a session completes a turn, it speaks a one-line summary of *what got done and the next step*. When a session is blocked waiting on you, it says so. Each session gets its own name and voice, so with headphones in you always know **which** session is talking without looking.

It is **not** a read-the-whole-reply-aloud tool and **not** a two-way voice chat — it's an *awareness* layer for people juggling multiple agents.

```
"Flock backend — finished wiring the Stop hook; next, run the test suite."
"Signals — needs you: permission to run a database migration."
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
/plugin marketplace add <owner>/claude-crier
/plugin install claude-crier@claude-crier
```

That's it — the hooks and slash commands activate immediately. No editing of your `settings.json`, no shell changes. (Crier ships only local command hooks; as with any plugin, install from sources you trust.)

## Usage

Voice is **off by default** and opt-in **per session** — turn it on only for the sessions you want to monitor:

| Command | What it does |
|---|---|
| `/voice-on` | Turn voice on for **this** session |
| `/voice-off` | Turn it off for this session (stops audio *and* the summary line) |
| `/voice-name <name>` | Set the spoken name for this session, e.g. `/voice-name Flock backend` |
| `/voice-persona` | List available voices; the current one is marked |
| `/voice-persona <#\|name>` | Pick a voice, e.g. `/voice-persona 3` or `/voice-persona Ava` (speaks a sample) |
| `/voice-doctor` | Diagnose your setup (engine, voices, session, on/off state) |

> Installed as a plugin, these may appear namespaced (e.g. `/claude-crier:voice-on`) depending on your Claude Code version.

If you don't set a name, it defaults to the session's folder name. If you don't pick a voice, Crier assigns a **random Enhanced/Premium voice per session** — stable within a session, different across parallel ones, so they're easy to tell apart by ear.

### Silence everything at once

Create the file `~/.claude/voice/.muted` to instantly silence every session (e.g. you step into a meeting); delete it to resume. Handy aliases:

```bash
alias voice-mute='touch ~/.claude/voice/.muted'
alias voice-unmute='rm -f ~/.claude/voice/.muted'
```

## How it works

Crier registers three [Claude Code hooks](https://code.claude.com/docs/en/hooks):

- **`SessionStart`** — when voice is on, injects an instruction asking Claude to end each turn with a hidden one-line summary marker (`🔊 VOICE: …`).
- **`Stop`** — extracts that summary line from the transcript and speaks it, prefixed with the session's name.
- **`Notification`** — speaks an alert when the session needs your input or permission.

Speech is serialized with a lock so two sessions finishing at once don't talk over each other. Per-session state (on/off, name, voice) lives in `~/.claude/voice/state/<session-id>.json`.

## Voices

On macOS, download better voices in **System Settings → Accessibility → Spoken Content → System Voice → Manage Voices**, and choose the **Enhanced** or **Premium** quality. Good natural picks:

| Voice | Accent |
|---|---|
| Ava (Premium), Zoe (Premium), Tom (Enhanced), Susan (Enhanced) | US |
| Serena (Premium), Jamie (Premium), Stephanie (Enhanced) | UK |
| Fiona (Enhanced) | Scottish |
| Moira (Enhanced) | Irish |

Then `/voice-persona` to browse and pick. Note: Siri voices are **not** available to `say` — only the Enhanced/Premium variants are.

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

Run `/voice-doctor` on any platform for tailored setup guidance.

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

Run **`/voice-doctor`** — it reports your platform, whether a TTS engine is found, installed voices, the detected session, and whether voice is on or off, then speaks a test line.

- **Silent?** Check voice is on (`/voice-on`), no global mute (`~/.claude/voice/.muted`), and an engine is available (`/voice-doctor`).
- **No summary, just "<name> finished"?** Claude didn't emit the `🔊 VOICE:` line that turn — usually transient; `/voice-on` re-states the instruction.

## Privacy

Fully local. No API keys, no network calls, no telemetry. Audio is synthesized on-device; transcripts are read only from your local Claude Code session files.

## License

MIT
