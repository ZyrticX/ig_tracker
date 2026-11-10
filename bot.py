#!/usr/bin/env python3
"""
שירות 24/7: בדיקה כל CHECK_INTERVAL_HOURS + בוט טלגרם לשליטה ומעקב.
הבוט עונה רק לצ'אטים שב-TELEGRAM_CHAT_ID. קודי אימות של אינסטגרם מגיעים דרכו (/code).
הרצה: python bot.py   (על שרת Linux רץ כ-systemd service, ראה deploy/install.sh)
"""
import html
import os
import queue
import random
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone

import requests

import tracker as t
from tracker import log

INTERVAL = float(os.getenv("CHECK_INTERVAL_HOURS", "6")) * 3600
RETRY = float(os.getenv("RETRY_MINUTES", "60")) * 60
RETRY_MAX = float(os.getenv("RETRY_MAX_HOURS", "6")) * 3600
JITTER = int(os.getenv("JITTER_MAX_SEC", "600"))
REPORT_NO_CHANGES = os.getenv("REPORT_NO_CHANGES", "true").lower() == "true"
CODE_TIMEOUT = 600

API = f"https://api.telegram.org/bot{t.TG_TOKEN}"
COMMANDS = [
    ("status", "מצב המערכת"),
    ("check", "בדיקת עוקבים עכשיו"),
    ("login", "בדיקת התחברות לאינסטגרם בלבד"),
    ("lost", "מי הוריד עוקב (30 ימים אחרונים)"),
    ("gained", "עוקבים חדשים (30 ימים אחרונים)"),
    ("history", "כל השינויים האחרונים"),
    ("code", "שליחת קוד אימות: /code 123456"),
    ("help", "עזרה"),
]
KEYBOARD = {"inline_keyboard": [
    [{"text": "🔄 בדיקה עכשיו", "callback_data": "check"}],
    [{"text": "📉 מי הוריד עוקב", "callback_data": "lost"},
     {"text": "📈 עוקבים חדשים", "callback_data": "gained"}],
]}
HELP = "🤖 <b>IG Tracker</b>\n" + "\n".join(f"/{c} — {d}" for c, d in COMMANDS)

busy = threading.Lock()          # פעולה אחת מול אינסטגרם בכל רגע (session אחד)
task = ""                        # שם הפעולה שרצה עכשיו, להצגה ב-/status
codes: queue.Queue = queue.Queue()
waiting_code = threading.Event()
started = time.time()


# ---------- עזרים ----------
def human(sec: float) -> str:
    sec = abs(sec)
    if sec < 3600:
        return f"{int(sec // 60)} דק׳"
    if sec < 86400:
        return f"{sec / 3600:.0f} שע׳"
    return f"{sec / 86400:.1f} ימים"


def epoch_ts(epoch: float) -> str:
    return t.fmt_ts(datetime.fromtimestamp(epoch, timezone.utc).isoformat())


def get_next_due() -> float:
    with closing(t.open_db()) as db:
        return float(t.meta_get(db, "next_due") or 0)


def set_next_due(delay: float) -> None:
    with closing(t.open_db()) as db:
        t.meta_set(db, next_due=time.time() + delay + random.randint(0, JITTER))


def ask_code(prompt: str) -> str | None:
    """נקרא מתוך ההתחברות כשאינסטגרם מבקשת קוד. מחכה שיגיע /code מהטלגרם."""
    while not codes.empty():
        codes.get_nowait()
    waiting_code.set()
    log.info("Waiting for verification code via Telegram")
    t.tg(f"{prompt}\nשלח כאן: <code>/code 123456</code> (יש {CODE_TIMEOUT // 60} דקות)")
    try:
        return codes.get(timeout=CODE_TIMEOUT)
    except queue.Empty:
        return None
    finally:
        waiting_code.clear()


def start(label: str, fn, *args) -> bool:
    """מריץ פעולה מול אינסטגרם ב-thread נפרד, כדי שהבוט ימשיך לקבל הודעות (למשל /code)."""
    global task
    if not busy.acquire(blocking=False):
        return False
    task = label

    def runner():
        global task
        try:
            fn(*args)
        except Exception:
            log.exception("%s failed", label)
        finally:
            task = ""
            busy.release()

    threading.Thread(target=runner, daemon=True).start()
    return True


# ---------- פעולות ----------
def next_delay(res) -> float:
    """כמה לחכות אחרי בדיקה. תקלה זמנית: ניסיון חוזר עם backoff (שעה, שעתיים, 4...).
    חסימת קצב: ההשהיה המלאה. בעיית התחברות: מחזור רגיל, לא מציקים לאינסטגרם."""
    if res is None or res.kind == "retry":
        with closing(t.open_db()) as db:
            streak = int(t.meta_get(db, "fail_streak") or 1)
        return min(RETRY * 2 ** (streak - 1), RETRY_MAX)
    if res.kind == "limit":
        return t.cooldown_left()
    return INTERVAL


def do_check(manual: bool) -> None:
    res = None
    try:
        res = t.check(ask_code=ask_code)
        if res.ok and not res.changed:
            if manual or REPORT_NO_CHANGES:
                t.tg(res.text, silent=True, markup=KEYBOARD)
        else:
            t.tg(res.text, markup=KEYBOARD)
    finally:
        set_next_due(next_delay(res))


def cooldown_msg() -> str | None:
    left = t.cooldown_left()
    if not left:
        return None
    return (f"⛔ אינסטגרם הגבילה את החשבון. הבדיקות מושהות עוד {human(left)} "
            f"(עד {epoch_ts(time.time() + left)}). בדיקה עכשיו רק תאריך את החסימה.")


def do_login(chat: str) -> None:
    try:
        t.login(ask_code)
        text, ok = f"🟢 ההתחברות לאינסטגרם תקינה (@{html.escape(t.IG_USERNAME)}).", 1
    except Exception as e:
        res = t.explain(e)
        text, ok = res.text, 0 if res.kind == "auth" else None
    if ok is not None:
        with closing(t.open_db()) as db:
            t.meta_set(db, ig_ok=ok)
    t.tg(text, chat, markup=KEYBOARD)


def status_text() -> str:
    with closing(t.open_db()) as db:
        m = {k: t.meta_get(db, k) for k in
             ("last_ok", "last_attempt", "last_error", "last_error_ts", "ig_ok", "next_due")}
        last = db.execute("SELECT total, ts FROM runs ORDER BY id DESC LIMIT 1").fetchone()

    if waiting_code.is_set():
        head = "📩 <b>ממתין לקוד אימות</b> — שלח <code>/code 123456</code>"
    elif task:
        head = f"🔄 <b>{task} רצה כרגע</b>"
    elif t.cooldown_left():
        head = "⛔ <b>מושהה: אינסטגרם הגבילה את החשבון</b>"
    elif not m["last_attempt"]:
        head = "⚪ <b>עוד לא בוצעה בדיקה</b>"
    elif m["last_error"]:
        head = "🔴 <b>יש בעיה</b>"
    else:
        head = "🟢 <b>הכול תקין</b>"

    ig = {"1": "🟢 מחובר", "0": "🔴 לא מחובר"}.get(m["ig_ok"] or "", "⚪ לא ידוע")
    lines = [head, "",
             f"👁 עוקב אחרי: @{html.escape(t.TARGET)}",
             f"🔑 מחובר דרך: @{html.escape(t.IG_USERNAME)} · {ig}"]
    if last:
        ago = time.time() - datetime.fromisoformat(last[1]).timestamp()
        lines.append(f"✅ בדיקה מוצלחת אחרונה: {t.fmt_ts(last[1])} (לפני {human(ago)}) · "
                     f"👥 {last[0]}")
    if m["last_error"]:
        lines.append(f"\n❗ תקלה אחרונה ({t.fmt_ts(m['last_error_ts'])}):\n{m['last_error']}\n")
    due = float(m["next_due"] or 0)
    if due > time.time():
        lines.append(f"⏭ בדיקה הבאה: {epoch_ts(due)} (בעוד {human(due - time.time())})")
    lines.append(f"⏱ השירות רץ {human(time.time() - started)}")
    return "\n".join(lines)


def history_text(kind: str | None = None, days: int = 30) -> str:
    """kind: 'unfollow' | 'follow' | None (הכול). מציג שינויים מה-days ימים האחרונים."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    q, args = "SELECT username, type, ts FROM events WHERE ts >= ?", [since]
    if kind:
        q, args = q + " AND type = ?", args + [kind]
    with closing(t.open_db()) as db:
        rows = db.execute(q + " ORDER BY id DESC LIMIT 40", args).fetchall()
    title = {"unfollow": "📉 <b>הורידו עוקב</b>", "follow": "📈 <b>עוקבים חדשים</b>"}.get(
        kind, "🗂 <b>שינויים אחרונים</b>")
    if not rows:
        return f"{title}\nאין שינויים ב-{days} הימים האחרונים."
    icon = {"unfollow": "📉", "follow": "📈"}
    return f"{title} · {days} ימים\n" + "\n".join(
        f"{icon.get(k, '•')} {t.fmt_ts(ts)} · "
        f'<a href="https://instagram.com/{html.escape(u)}">@{html.escape(u)}</a>'
        for u, k, ts in rows)


# ---------- טלגרם ----------
def handle(msg: dict) -> None:
    chat = str(msg.get("chat", {}).get("id", ""))
    text = (msg.get("text") or "").strip()
    if not chat or not text:
        return
    if chat not in t.TG_CHATS:
        log.warning("Ignored message from unknown chat %s", chat)
        return

    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    if waiting_code.is_set() and text.replace(" ", "").isdigit():
        cmd, arg = "/code", text           # אפשר לשלוח את הקוד גם בלי /code

    if cmd == "/status":
        t.tg(status_text(), chat, markup=KEYBOARD)
    elif cmd == "/check":
        if cooldown_msg():
            t.tg(cooldown_msg(), chat)
        elif start("בדיקה", do_check, True):
            t.tg("🔄 מתחיל בדיקה, זה לוקח כמה דקות...", chat)
        else:
            t.tg(f"⏳ {task or 'פעולה'} כבר רצה כרגע.", chat)
    elif cmd == "/login":
        if start("התחברות", do_login, chat):
            t.tg("🔄 בודק התחברות לאינסטגרם...", chat)
        else:
            t.tg(f"⏳ {task or 'פעולה'} כבר רצה כרגע.", chat)
    elif cmd == "/lost":
        t.tg(history_text("unfollow"), chat, markup=KEYBOARD)
    elif cmd == "/gained":
        t.tg(history_text("follow"), chat, markup=KEYBOARD)
    elif cmd == "/history":
        t.tg(history_text(), chat, markup=KEYBOARD)
    elif cmd == "/code":
        code = arg.replace(" ", "")
        if not waiting_code.is_set():
            t.tg("אין כרגע בקשת קוד פתוחה.", chat)
        elif not code.isdigit():
            t.tg("שלח כך: <code>/code 123456</code>", chat)
        else:
            codes.put(code)
            log.info("Verification code received")
            t.tg("👍 הקוד התקבל, ממשיך בהתחברות...", chat)
    else:
        t.tg(HELP, chat, markup=KEYBOARD)


def handle_callback(cb: dict) -> None:
    """לחיצה על כפתור = אותה פקודה. תמיד עונים ללחיצה כדי שהכפתור לא ייתקע על 'טוען'."""
    msg = cb.get("message") or {}
    try:
        requests.post(f"{API}/answerCallbackQuery", data={"callback_query_id": cb["id"]}, timeout=20)
    except requests.RequestException as e:
        log.warning("answerCallbackQuery failed: %s", e)
    data = cb.get("data") or ""
    if data in ("check", "lost", "gained"):
        handle({"chat": msg.get("chat", {}), "text": "/" + data})


def poll() -> None:
    offset = None
    while True:
        try:
            r = requests.get(f"{API}/getUpdates", params={"timeout": 50, "offset": offset},
                             timeout=60)
            if r.status_code == 409:
                log.error("Telegram 409: another copy of the bot is running (only one allowed)")
                time.sleep(30)
                continue
            r.raise_for_status()
            for upd in r.json()["result"]:
                offset = upd["update_id"] + 1
                try:
                    if "callback_query" in upd:
                        handle_callback(upd["callback_query"])
                    else:
                        handle(upd.get("message") or {})
                except Exception:
                    log.exception("Handler failed")
        except requests.RequestException as e:
            log.warning("Telegram poll failed: %s", e)
            time.sleep(10)


def scheduler() -> None:
    while True:
        try:
            if time.time() >= get_next_due():
                if t.cooldown_left():
                    set_next_due(t.cooldown_left())
                else:
                    start("בדיקה מתוזמנת", do_check, False)
        except Exception:
            log.exception("Scheduler error")
        time.sleep(30)


def main() -> None:
    try:
        requests.post(f"{API}/setMyCommands", timeout=20, json={
            "commands": [{"command": c, "description": d} for c, d in COMMANDS]})
    except requests.RequestException as e:
        log.warning("setMyCommands failed: %s", e)

    due = get_next_due()
    when = (f"בדיקה הבאה: {epoch_ts(due)}" if due > time.time()
            else "בדיקה מתחילה עכשיו")
    log.info("Service started")
    t.tg(f"🤖 השירות עלה. {when}.\n/status למצב, /help לפקודות.", silent=True, markup=KEYBOARD)

    threading.Thread(target=scheduler, daemon=True).start()
    poll()


if __name__ == "__main__":
    main()
