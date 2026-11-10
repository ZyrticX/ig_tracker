#!/usr/bin/env python3
"""
Instagram Unfollower Tracker — הליבה.
Snapshot של רשימת העוקבים -> diff מול הבדיקה הקודמת -> טקסט התראה.

השירות הקבוע (תזמון + בוט טלגרם) נמצא ב-bot.py.
בדיקה ידנית חד-פעמית מהטרמינל: python tracker.py
"""
import html
import json
import logging
import os
import random
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from instagrapi import Client
from instagrapi.exceptions import (AccountSuspended, BadCredentials, BadPassword, ChallengeError,
                                   ClientThrottledError, FeedbackRequired, LoginRequired,
                                   PleaseWaitFewMinutes, RateLimitError, ReloginAttemptExceeded,
                                   TwoFactorRequired)

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

_missing = [k for k in ("IG_USERNAME", "IG_PASSWORD", "TARGET_USERNAME",
                        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not os.getenv(k)]
if _missing:
    sys.exit(f"Missing in .env: {', '.join(_missing)}")

IG_USERNAME = os.environ["IG_USERNAME"].lstrip("@")   # החשבון שמושך את הנתונים
IG_PASSWORD = os.environ["IG_PASSWORD"]
# מפתח אפליקציית האימות (2FA). אם קיים, השרת מייצר קודים לבד בלי לשאול אותך
TOTP_SECRET = os.getenv("IG_TOTP_SECRET", "").replace(" ", "").upper()
TARGET = os.environ["TARGET_USERNAME"].lstrip("@").lower()  # החשבון שעוקבים אחרי העוקבים שלו
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHATS = [c.strip() for c in os.environ["TELEGRAM_CHAT_ID"].split(",") if c.strip()]
PROXY = os.getenv("IG_PROXY", "").strip()
NOTIFY_NEW = os.getenv("NOTIFY_NEW_FOLLOWERS", "true").lower() == "true"
MIN_RATIO = float(os.getenv("MIN_FETCH_RATIO", "0.9"))
FIRST_RUN_RATIO = float(os.getenv("FIRST_RUN_MIN_RATIO", "0.75"))
# כמה משיכות לאחד בכל בדיקה. 7 משיכות × 5 בדיקות ביום הביאו לחסימת קצב (feedback_required).
# 3 משיכות נותנות ~99%, והאישור בכמה בדיקות ברצף מכסה את מה שהוחמץ.
FETCH_PASSES = max(1, int(os.getenv("FETCH_PASSES", "3")))
PASS_DELAY = (float(os.getenv("PASS_DELAY_MIN_SEC", "120")), float(os.getenv("PASS_DELAY_MAX_SEC", "240")))
# ירידת עוקב מדווחת רק אחרי שהוא חסר ב-N בדיקות ברצף (עוקב חדש: נראה ב-2 ברצף)
LOST_CONFIRM = max(1, int(os.getenv("LOST_CONFIRM_CHECKS", "3")))
# אחרי חסימת קצב של אינסטגרם: הפסקה מלאה (גם לבדיקות ידניות) למשך:
LIMIT_COOLDOWN_H = float(os.getenv("LIMIT_COOLDOWN_HOURS", "24"))
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Jerusalem"))
DB_PATH = BASE / os.getenv("DB_PATH", "tracker.db")
SESSION_PATH = BASE / os.getenv("SESSION_PATH", "session.json")

# לוג לקובץ תמיד (UTF-8, עם רוטציה), ולמסך רק כשיש מסך (pythonw רץ בלי)
_handlers = [RotatingFileHandler(BASE / "tracker.log", maxBytes=1_000_000, backupCount=3,
                                 encoding="utf-8")]
if sys.stderr:
    _handlers.append(logging.StreamHandler())
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=_handlers)
log = logging.getLogger("ig-tracker")

SCHEMA = """
CREATE TABLE IF NOT EXISTS followers(
    pk TEXT PRIMARY KEY, username TEXT, full_name TEXT,
    first_seen TEXT, last_seen TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, pk TEXT, username TEXT, type TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS runs(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, total INTEGER, lost INTEGER, gained INTEGER);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


class PartialFetch(Exception):
    pass


class PasswordChangeRequired(Exception):
    pass


@dataclass
class Result:
    ok: bool
    text: str
    kind: str = "ok"        # ok | auth (צריך פעולה ממך) | retry (תקלה זמנית) | limit (חסימת קצב)
    changed: bool = False


# ---------- עזרים ----------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fmt_ts(iso: str | None) -> str:
    if not iso:
        return "—"
    return datetime.fromisoformat(iso).astimezone(TZ).strftime("%d.%m %H:%M")


def open_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.executescript(SCHEMA)
    if "missed" not in [r[1] for r in db.execute("PRAGMA table_info(followers)")]:
        db.execute("ALTER TABLE followers ADD COLUMN missed INTEGER DEFAULT 0")
        db.commit()
    if "confirmed" not in [r[1] for r in db.execute("PRAGMA table_info(followers)")]:
        db.execute("ALTER TABLE followers ADD COLUMN confirmed INTEGER DEFAULT 1")
        db.commit()
    return db


def meta_get(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(db: sqlite3.Connection, **values) -> None:
    db.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                   [(k, None if v is None else str(v)) for k, v in values.items()])
    db.commit()


# ---------- Telegram ----------
def tg(text: str, chat_id: str | None = None, silent: bool = False,
       markup: dict | None = None) -> None:
    """שולח הודעה (לצ'אט אחד או לכולם), מפוצל לפי שורות כדי לא לחתוך תגיות HTML."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > 3900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for chat in [chat_id] if chat_id else TG_CHATS:
        for i, chunk in enumerate(chunks):
            data = {"chat_id": chat, "text": chunk, "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                    "disable_notification": "true" if silent else "false"}
            if markup and i == len(chunks) - 1:
                data["reply_markup"] = json.dumps(markup)
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                    data=data, timeout=20,
                )
                r.raise_for_status()
            except Exception as e:
                log.error("Telegram send failed: %s", e)


def fmt_user(username: str, full_name: str) -> str:
    link = f'<a href="https://instagram.com/{html.escape(username)}">@{html.escape(username)}</a>'
    return f"• {link}" + (f" — {html.escape(full_name)}" if full_name else "")


# ---------- Instagram ----------
def _password_change_required(username: str) -> str:
    raise PasswordChangeRequired()


def totp_code() -> str:
    """קוד מאפליקציית האימות. אם נשארו פחות מ-10 שניות לחלון הנוכחי, מחכה לחלון הבא."""
    left = 30 - time.time() % 30
    if left < 10:
        time.sleep(left + 1)
    return Client.totp_generate_code(TOTP_SECRET)


def login(ask_code=None) -> Client:
    """ask_code(prompt) -> str | None מביא קוד אימות (בשירות: מהבוט). None = מהמקלדת."""
    ask = ask_code or (lambda prompt: input(f"{prompt}\n> ").strip())
    cl = Client()
    cl.delay_range = [3, 7]
    cl.challenge_code_handler = lambda username, choice: ask(
        f"📩 אינסטגרם שלחה קוד אימות ({getattr(choice, 'name', choice)}) "
        f"לחשבון @{html.escape(username)}.") or ""
    cl.change_password_handler = _password_change_required
    if PROXY:
        cl.set_proxy(PROXY)
    if SESSION_PATH.exists():
        cl.load_settings(SESSION_PATH)
    try:
        # עם session שמור: בודק אותו, ואם פג תוקף מתחבר מחדש עם אותו מכשיר
        cl.login(IG_USERNAME, IG_PASSWORD)
    except TwoFactorRequired:
        if TOTP_SECRET:
            log.info("2FA required, using IG_TOTP_SECRET")
            code = totp_code()
        else:
            code = ask(f"🔐 נדרש קוד אימות דו-שלבי לחשבון @{html.escape(IG_USERNAME)} "
                       "(מאפליקציית האימות או SMS).")
        if not code:
            raise
        # הקוד תקף לזמן קצר: בלי השהיות מלאכותיות בהתחברות עם הקוד
        cl.delay_range = None
        try:
            cl.login(IG_USERNAME, IG_PASSWORD, verification_code=code)
        finally:
            cl.delay_range = [3, 7]
    cl.dump_settings(SESSION_PATH)
    return cl


def target_pk(cl: Client, db: sqlite3.Connection) -> str:
    key = f"pk:{TARGET}"
    pk = meta_get(db, key)
    if not pk:
        pk = str(cl.user_id_from_username(TARGET))
        meta_set(db, **{key: pk})
    return pk


# ---------- Core ----------
def run(cl: Client, db: sqlite3.Connection) -> tuple[str, bool]:
    """מושך עוקבים, שומר, ומחזיר (טקסט דוח, האם היו שינויים)."""
    now = now_iso()
    pk = target_pk(cl, db)

    expected = cl.user_info(pk).follower_count or 0
    # אינסטגרם מחזירה את הרשימה בדפים, והיא זזה תוך כדי הדפדוף: חלק מהעוקבים חוזרים פעמיים וחלק
    # מדולגים (בפועל ~85% בכל משיכה, ובכל פעם אחרים). מאחדים כמה משיכות, עם הפסקות ארוכות ביניהן.
    # מספר העוקבים בפרופיל לא מהימן (מתעדכן באיחור) ולכן לא משפיע על שום החלטה.
    current: dict[str, tuple[str, str]] = {}
    for i in range(FETCH_PASSES):
        order = "date_followed_latest" if i % 2 else None
        raw = cl.user_followers(pk, use_cache=False, amount=0, order=order)
        before = len(current)
        current.update({str(k): (u.username, u.full_name or "") for k, u in raw.items()})
        added = len(current) - before
        log.info("Pass %d: +%d, %d unique (profile shows %d, not used for decisions)",
                 i + 1, added, len(current), expected)
        if added == 0:
            break
        if i < FETCH_PASSES - 1:
            time.sleep(random.uniform(*PASS_DELAY))
    got = len(current)
    log.info("Fetched %d / %d followers", got, expected)

    # הגנה מהתראות שווא: משיכה חלקית לא נשמרת.
    # follower_count כולל חשבונות שאינסטגרם לא מחזירה ברשימה (מושבתים, ספאם), אז הרשימה בדרך כלל
    # 85-95% ממנו. לכן משווים למשיכה המוצלחת הקודמת, ורק בפעם הראשונה למספר המוצג (בסף מקל).
    prev = db.execute("SELECT total FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    base, ratio = (prev[0], MIN_RATIO) if prev else (expected, FIRST_RUN_RATIO)
    if base and got < base * ratio:
        hint = " אם החשבון פרטי, החשבון המחובר חייב לעקוב אחריו." if got == 0 else ""
        what = f"מתוך {base} בבדיקה הקודמת" if prev else f"מתוך {expected} שמוצגים בפרופיל"
        raise PartialFetch(f"נמשכו {got} עוקבים {what}.{hint}")

    # הרשימה היא האמת, אבל כל שינוי (בשני הכיוונים) מדווח רק אחרי שהוא חוזר בכמה בדיקות ברצף:
    #   עוקב חדש  -> נרשם כ"ממתין" (confirmed=0), ומדווח כשהוא נראה גם בבדיקה הבאה.
    #   עוקב שנעלם -> missed עולה ב-1 בכל בדיקה שהוא חסר, ומדווח כירידה אחרי LOST_CONFIRM ברצף.
    first_run = db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    rows = {r[0]: r[1:] for r in db.execute(
        "SELECT pk, username, full_name, active, missed, confirmed FROM followers")}
    lost, gained = [], []
    upserts, state = [], []          # state: (active, missed, confirmed, pk)
    for p, (u, n) in current.items():
        row = rows.get(p)
        if row is None:
            upserts.append((p, u, n, now, now, 1, 0, 1 if first_run else 0))
        else:
            _, _, active, _, confirmed = row
            if active and confirmed:
                state.append((1, 0, 1, p))
            elif active:                         # ממתין מהבדיקה הקודמת, נראה שוב: מאושר
                state.append((1, 0, 1, p))
                gained.append((p, u, n))
            else:                                # חזר לעקוב אחרי שירד: ממתין לאישור
                state.append((1, 0, 0, p))
    for p, (u, n, active, missed, confirmed) in rows.items():
        if p in current or not active:
            continue
        if not confirmed:                        # היה ממתין ונעלם: לא היה אמיתי, בלי דיווח
            state.append((0, 0, 1, p))
        elif missed + 1 >= LOST_CONFIRM:         # חסר ב-LOST_CONFIRM בדיקות ברצף: ירד
            state.append((0, 0, 1, p))
            lost.append((p, u, n))
        else:
            state.append((1, missed + 1, 1, p))
    db.executemany(
        """INSERT INTO followers(pk, username, full_name, first_seen, last_seen, active, missed, confirmed)
           VALUES(?, ?, ?, ?, ?, ?, ?, ?)""", upserts)
    db.executemany("UPDATE followers SET active=?, missed=?, confirmed=? WHERE pk=?", state)
    db.executemany("UPDATE followers SET username=?, full_name=?, last_seen=? WHERE pk=?",
                   [(u, n, now, p) for p, (u, n) in current.items()])

    if not first_run:
        db.executemany("INSERT INTO events(pk, username, type, ts) VALUES(?, ?, 'unfollow', ?)",
                       [(p, u, now) for p, u, _ in lost])
        db.executemany("INSERT INTO events(pk, username, type, ts) VALUES(?, ?, 'follow', ?)",
                       [(p, u, now) for p, u, _ in gained])

    db.execute("INSERT INTO runs(ts, total, lost, gained) VALUES(?, ?, ?, ?)",
               (now, got, len(lost), len(gained)))
    db.commit()

    if first_run:
        return (f"✅ <b>הטראקר הופעל</b> על @{html.escape(TARGET)}\n"
                f"נשמר בסיס של {got} עוקבים."), True

    lines = []
    if lost:
        lines.append(f"📉 <b>הורידו עוקב ({len(lost)})</b>")
        lines += [fmt_user(u, n) for _, u, n in sorted(lost, key=lambda x: x[1])]
    if gained and NOTIFY_NEW:
        lines.append(f"\n📈 <b>עוקבים חדשים ({len(gained)})</b>")
        lines += [fmt_user(u, n) for _, u, n in sorted(gained, key=lambda x: x[1])]
    if not lines:
        return f"✅ בדיקה עברה, אין שינויים. 👥 {got} עוקבים", False
    lines.append(f"\n👥 סה״כ עוקבים: {got}")
    return "\n".join(lines), True


def explain(e: Exception) -> Result:
    """מתרגם חריגה להודעה ברורה + סוג: auth (צריך פעולה ממך) או retry (תקלה זמנית)."""
    who = f"@{html.escape(IG_USERNAME)}"
    if isinstance(e, ChallengeError):
        return Result(False, f"⚠️ אינסטגרם ביקשה אימות לחשבון {who}. היכנס אליו באפליקציה, "
                             "אשר שזה אתה, ואז שלח /check.", "auth")
    if isinstance(e, TwoFactorRequired):
        if TOTP_SECRET:
            return Result(False, f"🔐 קוד ה-2FA שנוצר מ-IG_TOTP_SECRET נדחה עבור {who}. "
                                 "בדוק שהמפתח נכון (אותו מפתח שבאפליקציית האימות).", "auth")
        return Result(False, f"🔐 לא התקבל קוד דו-שלבי לחשבון {who}. שלח /login ואז "
                             "<code>/code 123456</code>.", "auth")
    if isinstance(e, (BadPassword, BadCredentials)):
        return Result(False, f"🔑 הסיסמה של {who} נדחתה. עדכן IG_PASSWORD ב-.env "
                             "והפעל מחדש את השירות.", "auth")
    if isinstance(e, PasswordChangeRequired):
        return Result(False, f"🔑 אינסטגרם דורשת להחליף סיסמה ל-{who}. החלף באפליקציה, "
                             "עדכן ב-.env והפעל מחדש.", "auth")
    if isinstance(e, AccountSuspended):
        return Result(False, f"⛔ החשבון {who} הושעה על ידי אינסטגרם.", "auth")
    if isinstance(e, (LoginRequired, ReloginAttemptExceeded)):
        return Result(False, f"🔌 ההתחברות של {who} נכשלה (session לא תקף). "
                             "נסה /login, ואם לא עובד היכנס לחשבון מהאפליקציה.", "auth")
    if isinstance(e, (FeedbackRequired, RateLimitError, ClientThrottledError)):
        return Result(False, f"⛔ אינסטגרם הגבילה את החשבון {who} בגלל יותר מדי בקשות. "
                             f"הבדיקות מושהות ל-{LIMIT_COOLDOWN_H:g} שעות, גם ידניות. "
                             "הנתונים לא השתנו.", "limit")
    if isinstance(e, PleaseWaitFewMinutes):
        return Result(False, "⏳ אינסטגרם ביקשה לחכות כמה דקות. הנתונים לא השתנו, ננסה שוב "
                             "מאוחר יותר.", "retry")
    if isinstance(e, PartialFetch):
        return Result(False, f"⚠️ משיכה חלקית: {html.escape(str(e))} "
                             "הבדיקה לא נשמרה כדי למנוע התראות שווא.", "retry")
    log.error("Unexpected error", exc_info=e)
    return Result(False, f"❌ שגיאה: <code>{html.escape(str(e))[:500]}</code>", "retry")


def check(ask_code=None) -> Result:
    """בדיקה מלאה: התחברות + משיכה + השוואה. לא זורק חריגות, מתעד סטטוס ב-meta."""
    db = open_db()
    try:
        meta_set(db, last_attempt=now_iso())
        try:
            text, changed = run(login(ask_code), db)
            res = Result(True, text, "ok", changed)
        except Exception as e:
            res = explain(e)
        log.info("Check done: ok=%s kind=%s changed=%s", res.ok, res.kind, res.changed)
        if not res.ok:
            log.warning("Check problem: %s", res.text)
        if res.ok:
            meta_set(db, last_ok=now_iso(), last_error=None, last_error_ts=None, ig_ok=1,
                     fail_streak=0)
        else:
            streak = int(meta_get(db, "fail_streak") or 0) + 1
            meta_set(db, last_error=res.text, last_error_ts=now_iso(), fail_streak=streak,
                     ig_ok=0 if res.kind == "auth" else meta_get(db, "ig_ok"))
            if res.kind == "limit":
                meta_set(db, cooldown_until=time.time() + LIMIT_COOLDOWN_H * 3600)
        return res
    finally:
        db.close()


def cooldown_left() -> float:
    """שניות שנותרו להשהיה אחרי חסימת קצב, או 0."""
    db = open_db()
    try:
        return max(0.0, float(meta_get(db, "cooldown_until") or 0) - time.time())
    finally:
        db.close()


def main() -> None:
    """בדיקה ידנית מהטרמינל. קודי אימות מוקלדים במקלדת. לא להריץ במקביל לשירות."""
    res = check()
    log.info("Result: ok=%s kind=%s", res.ok, res.kind)
    tg(res.text)
    sys.exit(0 if res.ok else 1)


if __name__ == "__main__":
    main()
