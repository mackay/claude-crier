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
  on | off | name <name> | persona [#|name] | rate <wpm> | focus on|off | doctor

State is per session id under ~/.claude/voice/state/<id>.json and OVERRIDES the
launch-time env defaults (so `claude-voice` still works, but you can also flip a
plain session on/off from inside it).

Modes: sessionstart | stop | notification | _speak | on | off | name | persona | rate | focus | doctor
"""
import sys
import os
import re
import json
import glob
import fcntl
import shutil
import time
import hashlib
import subprocess

HOME = os.path.expanduser("~")
VOICE_DIR = os.path.join(HOME, ".claude", "voice")
STATE_DIR = os.path.join(VOICE_DIR, "state")
LOCK = os.path.join(VOICE_DIR, ".speak.lock")
MUTE = os.path.join(VOICE_DIR, ".muted")  # global kill-switch for ALL sessions
LAST_SPOKEN = os.path.join(VOICE_DIR, ".last_spoken")  # de-dup guard
DEDUP_WINDOW = 10  # seconds — drop an identical line spoken again within this window
MARKER = "\U0001f50a VOICE:"
# A summary that begins with one of these (Claude is asked to flag failures/blocks
# this way) is spoken as an alert ("heads up") instead of a normal completion.
_ALERT_RE = re.compile(r"^\s*(?:⚠️?\s*)?(problem|error|blocked|failed|stuck)\b[:,\-\s]*", re.I)
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
        # Drop an identical line spoken in the last few seconds — guards against a
        # hook that fired twice (e.g. the plugin enabled twice). The lock makes this
        # check race-free: the prior utterance has finished and recorded itself.
        try:
            with open(LAST_SPOKEN) as f:
                last = json.load(f)
        except Exception:
            last = {}
        if last.get("text") == text and (time.time() - float(last.get("ts") or 0)) < DEDUP_WINDOW:
            return
        tts_speak(text, voice, rate)
        try:
            with open(LAST_SPOKEN, "w") as f:
                json.dump({"text": text, "ts": time.time()}, f)
        except Exception:
            pass
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


# ------------------------------ focus detection ------------------------------
# "Focus mode": stay quiet for a session while the user is actively looking at
# its terminal pane (voice is for the sessions you AREN'T watching). macOS +
# iTerm2 only for now; everywhere else this reports unsupported and never
# suppresses (fail-open — better a redundant announcement than a missed one).

def _frontmost_app():
    """Name of the frontmost app — uses lsappinfo, no Automation permission."""
    try:
        asn = subprocess.run(["lsappinfo", "front"],
                             capture_output=True, text=True, timeout=2).stdout.strip()
        if not asn:
            return None
        out = subprocess.run(["lsappinfo", "info", "-only", "name", asn],
                             capture_output=True, text=True, timeout=2).stdout
        m = re.search(r'"LSDisplayName"\s*=\s*"([^"]+)"', out)
        return m.group(1) if m else None
    except Exception:
        return None


def _iterm_focused_session_id():
    """Unique id of iTerm2's focused session (needs Automation permission once)."""
    try:
        out = subprocess.run(
            ["osascript", "-e",
             'tell application "iTerm2" to tell current window '
             'to tell current session to get id'],
            capture_output=True, text=True, timeout=3).stdout.strip()
        return out or None
    except Exception:
        return None


def focus_supported():
    """(bool, detail) — whether per-pane focus detection can work here."""
    if platform_kind() != "macos":
        return (False, "macOS only")
    if os.environ.get("ITERM_SESSION_ID"):
        return (True, "iTerm2")
    return (False, "only iTerm2 is supported")


def is_user_looking():
    """True only when we can confirm the user is looking at THIS session's pane.
    Returns False whenever unsure, so focus mode never silences by mistake."""
    ok, _ = focus_supported()
    if not ok:
        return False
    if _frontmost_app() != "iTerm2":
        return False                       # a different app is in front
    env_id = os.environ.get("ITERM_SESSION_ID", "")
    focused = _iterm_focused_session_id()
    return bool(env_id and focused and focused in env_id)


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
        "need from the user. If the work failed, errored, or you are blocked, begin "
        "that sentence with the word 'Problem:' so it can be flagged as an alert. "
        "Do not name the session (the system prepends that). Output nothing after "
        "that line."
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
    if read_state(sid).get("focus") and is_user_looking():
        return  # you're looking at this session — no need to announce it
    lab = resolve_label(sid, p)
    txt = last_assistant_text(p.get("transcript_path", ""))
    phrase = None
    if txt:
        for ln in reversed(txt.splitlines()):
            i = ln.find(MARKER)
            if i != -1:
                phrase = ln[i + len(MARKER):].strip()
                break
    if not phrase:
        spoken = f"{lab} finished."
    elif _ALERT_RE.match(phrase):
        clean = _ALERT_RE.sub("", phrase, count=1).strip()
        spoken = f"{lab}, heads up. {clean}"
    else:
        spoken = f"{lab}. {phrase}"
    speak_async(spoken, resolve_voice(sid), resolve_rate(sid))


def notification():
    p = payload()
    sid = p.get("session_id") or current_session_id(p.get("cwd"))
    if not is_enabled(sid) or os.path.exists(MUTE):
        return
    if read_state(sid).get("focus") and is_user_looking():
        return  # you're already looking at this session
    lab = resolve_label(sid, p)
    msg = (p.get("message") or "needs your attention").strip()
    # Claude Code re-fires the idle "waiting for input" notification every few
    # seconds until you interact; speak a given message at most once per minute
    # so it doesn't nag on repeat.
    st = read_state(sid)
    now = time.time()
    if st.get("_notif_msg") == msg and (now - float(st.get("_notif_ts") or 0)) < 60:
        return
    write_state(sid, _notif_msg=msg, _notif_ts=now)
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


def cmd_rate(args):
    sid = current_session_id()
    arg = " ".join(args).strip().lower()
    if not arg:
        print("Current speech rate: %s" % (resolve_rate(sid) or "default"))
        return
    if arg in ("default", "reset", "off", "none"):
        write_state(sid, rate=None)
        print("Speech rate reset to the default.")
        return
    if not arg.isdigit():
        print("Rate must be a whole number of words per minute, e.g. 180.")
        return
    write_state(sid, rate=int(arg))
    print("Speech rate set to %s words per minute." % arg)


def cmd_focus(args):
    sid = current_session_id()
    arg = " ".join(args).strip().lower()
    cur = bool(read_state(sid).get("focus"))
    if arg in ("on", "true", "1", "yes"):
        new = True
    elif arg in ("off", "false", "0", "no"):
        new = False
    elif arg in ("", "toggle"):
        new = not cur
    else:
        print("Usage: focus on | off")
        return
    write_state(sid, focus=new)
    if not new:
        print("Focus mode OFF — this session always speaks.")
        return
    ok, detail = focus_supported()
    if ok:
        print("Focus mode ON — this session stays quiet while you're looking at its "
              "terminal (%s) and speaks when you're elsewhere. (macOS may ask once "
              "for Automation permission.)" % detail)
    else:
        print("Focus mode ON, but focus detection isn't available here (%s), so it "
              "will keep speaking normally." % detail)


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
    fok, fdetail = focus_supported()
    out.append("  focus detection: %s (%s)"
               % ("available" if fok else "unavailable", fdetail))
    st = read_state(sid)
    out.append("  this session: %s | name='%s' | voice=%s | rate=%s | focus=%s"
               % ("ON" if is_enabled(sid) else "OFF",
                  resolve_label(sid), resolve_voice(sid) or "platform default",
                  resolve_rate(sid) or "default",
                  "on" if st.get("focus") else "off"))
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
    elif mode == "rate":
        cmd_rate(rest)
    elif mode == "focus":
        cmd_focus(rest)
    elif mode == "doctor":
        cmd_doctor()


if __name__ == "__main__":
    main()
