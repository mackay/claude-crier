#!/usr/bin/env python3
"""Claude Code local voice notifications.

Speaks a short, session-named summary when a turn completes, and an alert when a
session is blocked waiting on you — for monitoring several parallel sessions by
ear.

  - macOS: works out of the box via built-in `say` (use Enhanced/Premium voices).
  - Windows: built-in PowerShell System.Speech (no install).
  - Linux: espeak-ng / spd-say if present, or set CLAUDE_VOICE_CMD to any command
    that reads text on stdin (e.g. a Piper pipeline) for natural voices.

Per-session control is via slash commands (named /voice-* or /crier:* depending
on how it is installed), which call this script with modes:
  on | off | name <name> | persona [#|name] | doctor

State is per session id under ~/.claude/voice/state/<id>.json and OVERRIDES the
launch-time env defaults (so `claude-voice` still works, but you can also flip a
plain session on/off from inside it).

Modes: sessionstart | stop | notification | _speak | on | off | name | persona | doctor
"""
import sys
import os
import re
import json
import glob
import fcntl
import shutil
import hashlib
import subprocess

HOME = os.path.expanduser("~")
VOICE_DIR = os.path.join(HOME, ".claude", "voice")
STATE_DIR = os.path.join(VOICE_DIR, "state")
LOCK = os.path.join(VOICE_DIR, ".speak.lock")
MUTE = os.path.join(VOICE_DIR, ".muted")  # global kill-switch for ALL sessions
MARKER = "\U0001f50a VOICE:"
SELF = os.path.abspath(__file__)

# Launch-time defaults; per-session state (set via slash commands) overrides these.
ENV_ON = os.environ.get("CLAUDE_VOICE") == "1"
ENV_LABEL = os.environ.get("CLAUDE_SESSION_LABEL", "").strip()
ENV_VOICE = os.environ.get("CLAUDE_VOICE_NAME", "").strip()
ENV_RATE = os.environ.get("CLAUDE_VOICE_RATE", "").strip()
BYO_CMD = os.environ.get("CLAUDE_VOICE_CMD", "").strip()  # reads text on stdin


# ----------------------------- platform / TTS -----------------------------

def platform_kind():
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt" or sys.platform.startswith("win"):
        return "windows"
    return "linux"


def _mac_say_list():
    try:
        return subprocess.run(["/usr/bin/say", "-v", "?"],
                              capture_output=True, text=True).stdout.splitlines()
    except Exception:
        return []


# Name column width varies, so split on the locale token (en_US, en_GB_U_SD@...).
_NAME_RE = re.compile(r"^(.+?)\s+[a-z]{2}_[A-Z]{2}")


def _mac_name(line):
    m = _NAME_RE.match(line)
    return m.group(1).strip() if m else None


def installed_voices():
    if platform_kind() != "macos":
        return set()
    return {n for ln in _mac_say_list() if (n := _mac_name(ln))}


def premium_pool():
    """macOS: installed Enhanced/Premium voices (the natural-sounding ones)."""
    if platform_kind() != "macos":
        return []
    pool = []
    for ln in _mac_say_list():
        low = ln.lower()
        if "(premium)" in low or "(enhanced)" in low:
            n = _mac_name(ln)
            if n:
                pool.append(n)
    return sorted(set(pool))


def tts_engine():
    """(name, available?) for the backend that will actually be used."""
    if BYO_CMD:
        return ("custom (CLAUDE_VOICE_CMD)", True)
    k = platform_kind()
    if k == "macos":
        return ("say", os.path.exists("/usr/bin/say"))
    if k == "windows":
        return ("PowerShell System.Speech", bool(shutil.which("powershell")))
    for e in ("spd-say", "espeak-ng", "espeak"):
        if shutil.which(e):
            return (e, True)
    return ("espeak-ng", False)


def tts_speak(text, voice=None, rate=None):
    """Blocking: speak `text` once using the platform backend."""
    if not text:
        return
    if BYO_CMD:
        try:
            subprocess.run(BYO_CMD, shell=True, input=text, text=True)
        except Exception:
            pass
        return
    k = platform_kind()
    try:
        if k == "macos":
            cmd = ["/usr/bin/say"]
            if voice and voice in installed_voices():
                cmd += ["-v", voice]
            if rate:
                cmd += ["-r", str(rate)]
            cmd.append(text)
            subprocess.run(cmd)
        elif k == "windows":
            ps = ("Add-Type -AssemblyName System.Speech;"
                  "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;")
            if voice:
                ps += "try{$s.SelectVoice('%s')}catch{};" % voice.replace("'", "''")
            ps += "$s.Speak([Console]::In.ReadToEnd());"
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           input=text, text=True)
        else:
            eng, avail = tts_engine()
            if not avail:
                return
            if eng == "spd-say":
                subprocess.run(["spd-say", "-w"] +
                               (["-r", str(rate)] if rate else []) + [text])
            else:
                subprocess.run([eng] +
                               (["-s", str(rate)] if rate else []) + [text])
    except Exception:
        pass


# ------------------------------- session state -------------------------------

def _project_dir(cwd):
    return os.path.join(HOME, ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", cwd))


def current_session_id(cwd=None):
    """Best-effort session id when invoked interactively (slash commands): the
    newest transcript for this cwd is the live session."""
    cwd = cwd or os.getcwd()
    try:
        files = glob.glob(os.path.join(_project_dir(cwd), "*.jsonl"))
        if not files:
            return None
        return os.path.splitext(os.path.basename(max(files, key=os.path.getmtime)))[0]
    except Exception:
        return None


def _state_path(sid):
    return os.path.join(STATE_DIR, "%s.json" % sid)


def read_state(sid):
    if not sid:
        return {}
    try:
        with open(_state_path(sid)) as f:
            return json.load(f)
    except Exception:
        return {}


def write_state(sid, **kw):
    if not sid:
        return {}
    os.makedirs(STATE_DIR, exist_ok=True)
    st = read_state(sid)
    st.update(kw)
    with open(_state_path(sid), "w") as f:
        json.dump(st, f)
    return st


def is_enabled(sid):
    st = read_state(sid)
    if "on" in st:
        return bool(st["on"])
    return ENV_ON


def resolve_label(sid, p=None):
    st = read_state(sid)
    if st.get("label"):
        return st["label"]
    if ENV_LABEL:
        return ENV_LABEL
    cwd = (p or {}).get("cwd") or os.getcwd()
    return os.path.basename(cwd.rstrip("/")) or "session"


def resolve_voice(sid):
    st = read_state(sid)
    if st.get("voice"):
        return st["voice"]
    if ENV_VOICE:
        return ENV_VOICE
    pool = premium_pool()
    if not pool:
        return None  # let the platform pick its default voice
    h = int(hashlib.md5((sid or "x").encode()).hexdigest(), 16)
    return pool[h % len(pool)]


def resolve_rate(sid):
    return read_state(sid).get("rate") or (ENV_RATE or None)


def match_persona(arg, pool):
    arg = (arg or "").strip()
    if not pool or not arg:
        return None
    if arg.isdigit():
        i = int(arg) - 1
        return pool[i] if 0 <= i < len(pool) else None
    for n in pool:               # exact (case-insensitive)
        if arg.lower() == n.lower():
            return n
    for n in pool:               # substring
        if arg.lower() in n.lower():
            return n
    return None


# --------------------------------- speaking ---------------------------------

def _locked_speak(text, voice, rate):
    os.makedirs(VOICE_DIR, exist_ok=True)
    fh = open(LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)  # serialize so parallel sessions don't overlap
        tts_speak(text, voice, rate)
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def speak_async(text, voice=None, rate=None):
    """Fire-and-forget: detach a child that speaks under the lock, so the hook
    returns instantly."""
    if not text:
        return
    arg = json.dumps({"text": text, "voice": voice, "rate": rate})
    subprocess.Popen([sys.executable, SELF, "_speak", arg],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


# ----------------------------------- hooks -----------------------------------

def payload():
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def _summary_instruction():
    return (
        "Voice notifications are ON for this session. At the very end of every "
        "response, output one final line starting with '\U0001f50a VOICE:' "
        "followed by a single concise spoken-style sentence (under ~25 words) "
        "stating what you just completed and the immediate next step or what you "
        "need from the user. Do not name the session (the system prepends that). "
        "Output nothing after that line."
    )


def session_start():
    p = payload()
    sid = p.get("session_id") or current_session_id(p.get("cwd"))
    if not is_enabled(sid):
        return
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": _summary_instruction(),
    }}))


def last_assistant_text(path):
    text = None
    try:
        with open(path) as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") == "assistant":
                    for b in o.get("message", {}).get("content", []):
                        if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                            text = b["text"]
    except Exception:
        pass
    return text


def stop():
    p = payload()
    sid = p.get("session_id") or current_session_id(p.get("cwd"))
    if not is_enabled(sid) or os.path.exists(MUTE):
        return
    lab = resolve_label(sid, p)
    txt = last_assistant_text(p.get("transcript_path", ""))
    phrase = None
    if txt:
        for ln in reversed(txt.splitlines()):
            i = ln.find(MARKER)
            if i != -1:
                phrase = ln[i + len(MARKER):].strip()
                break
    speak_async(f"{lab}. {phrase}" if phrase else f"{lab} finished.",
                resolve_voice(sid), resolve_rate(sid))


def notification():
    p = payload()
    sid = p.get("session_id") or current_session_id(p.get("cwd"))
    if not is_enabled(sid) or os.path.exists(MUTE):
        return
    lab = resolve_label(sid, p)
    msg = (p.get("message") or "needs your attention").strip()
    speak_async(f"{lab} needs you. {msg}", resolve_voice(sid), resolve_rate(sid))


# ------------------------- interactive (slash commands) -------------------------

def cmd_on():
    sid = current_session_id()
    write_state(sid, on=True)
    print("Voice ON for this session (%s)." % (resolve_label(sid)))


def cmd_off():
    sid = current_session_id()
    write_state(sid, on=False)
    print("Voice OFF for this session (%s)." % (resolve_label(sid)))


def cmd_name(args):
    sid = current_session_id()
    name = " ".join(args).strip()
    if not name:
        print("Current session name: %s" % resolve_label(sid))
        return
    write_state(sid, label=name)
    print("Session name set to: %s" % name)


def cmd_persona(args):
    sid = current_session_id()
    arg = " ".join(args).strip()
    pool = premium_pool()
    if not arg:
        if platform_kind() != "macos":
            print("Voice personas are macOS-only. Run the doctor command for this platform.")
            return
        if not pool:
            print("No Enhanced/Premium voices installed. Run the doctor command for setup.")
            return
        print("Available voice personas:")
        cur = resolve_voice(sid)
        for i, n in enumerate(pool, 1):
            print("  %2d. %s%s" % (i, n, "  <- current" if n == cur else ""))
        print("Choose one by number or name, e.g. 3 or Ava.")
        return
    chosen = match_persona(arg, pool)
    if not chosen:
        print("No voice matches '%s'. Run the persona command with no argument to see the list." % arg)
        return
    write_state(sid, voice=chosen)
    print("Voice persona set to: %s" % chosen)
    tts_speak("This is %s." % chosen.split(" (")[0], chosen, resolve_rate(sid))


def cmd_doctor():
    sid = current_session_id()
    k = platform_kind()
    eng, avail = tts_engine()
    out = ["Claude voice — diagnostics", "  platform: %s" % k,
           "  engine:   %s (%s)" % (eng, "available" if avail else "NOT FOUND")]
    if k == "macos":
        pool = premium_pool()
        out.append("  Enhanced/Premium voices installed: %d" % len(pool))
        if pool:
            out.append("    " + ", ".join(pool))
        else:
            out.append("    none — System Settings > Accessibility > Spoken Content >")
            out.append("    System Voice > Manage Voices; download an Enhanced/Premium voice.")
    elif k == "windows":
        out.append("  Uses built-in PowerShell System.Speech (no install needed).")
    else:
        if not avail:
            out.append("  No Linux TTS found. Install one:")
            out.append("    Debian/Ubuntu: sudo apt install espeak-ng   (or speech-dispatcher)")
            out.append("    Natural voices: install Piper, then set CLAUDE_VOICE_CMD to a")
            out.append("    pipeline that reads text on stdin, e.g. 'piper -m model.onnx | aplay'.")
        if BYO_CMD:
            out.append("  CLAUDE_VOICE_CMD override is active.")
    out.append("  session detected: %s" % (sid or "NO — could not find a transcript for this cwd"))
    out.append("  this session: %s | name='%s' | voice=%s"
               % ("ON" if is_enabled(sid) else "OFF",
                  resolve_label(sid), resolve_voice(sid) or "platform default"))
    if os.path.exists(MUTE):
        out.append("  NOTE: global mute is active (run `voice-unmute`).")
    print("\n".join(out))
    if avail and not os.path.exists(MUTE):
        tts_speak("Voice check. This is %s." % (resolve_voice(sid) or "the default voice").split(" (")[0],
                  resolve_voice(sid), resolve_rate(sid))


# ----------------------------------- main -----------------------------------

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    rest = sys.argv[2:]
    if mode == "sessionstart":
        session_start()
    elif mode == "stop":
        stop()
    elif mode == "notification":
        notification()
    elif mode == "_speak":
        try:
            d = json.loads(rest[0]) if rest else {}
        except Exception:
            d = {}
        _locked_speak(d.get("text", ""), d.get("voice"), d.get("rate"))
    elif mode == "on":
        cmd_on()
    elif mode == "off":
        cmd_off()
    elif mode == "name":
        cmd_name(rest)
    elif mode == "persona":
        cmd_persona(rest)
    elif mode == "doctor":
        cmd_doctor()


if __name__ == "__main__":
    main()
