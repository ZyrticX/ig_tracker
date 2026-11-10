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
# מתחת לחלק הזה מבסיס התצפיות ההיסטורי הסריקה מסומנת "נראית חלקית" - אזהרה בלבד בדוח,
# לא חסימה: ממילא לא מסיקים מסריקה חיה דבר על מי שלא נצפה בה.
MIN_RATIO = float(os.getenv("MIN_FETCH_RATIO", "0.9"))
# כמה משיכות לאחד בכל בדיקה. 7 משיכות × 5 בדיקות ביום הביאו לחסימת קצב (feedback_required).
# איחוד משיכות מצמצם החמצות אקראיות בין דפים, אבל אין אחוז כיסוי מובטח: קיטום שיטתי של
# אינסטגרם אינו ניתן לתיקון בעוד משיכות, רק לזיהוי ולדיווח כ"לא חד-משמעי".
FETCH_PASSES = max(1, int(os.getenv("FETCH_PASSES", "3")))
PASS_DELAY = (float(os.getenv("PASS_DELAY_MIN_SEC", "120")), float(os.getenv("PASS_DELAY_MAX_SEC", "240")))
# ירידת עוקב מדווחת רק אחרי שהוא חסר ב-N בדיקות ברצף (עוקב חדש: נראה ב-2 ברצף)
LOST_CONFIRM = max(1, int(os.getenv("LOST_CONFIRM_CHECKS", "3")))
# אחרי חסימת קצב של אינסטגרם: הפסקה מלאה (גם לבדיקות ידניות) למשך:
LIMIT_COOLDOWN_H = float(os.getenv("LIMIT_COOLDOWN_HOURS", "24"))
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Jerusalem"))
DB_PATH = BASE / os.getenv("DB_PATH", "tracker.db")
SESSION_PATH = BASE / os.getenv("SESSION_PATH", "session.json")

class _RedactFormatter(logging.Formatter):
    """מסווה את טוקן הבוט בכל רשומת לוג, כולל traceback (requests כוללת את ה-URL המלא בשגיאות)."""

    def format(self, record: logging.LogRecord) -> str:
        return super().format(record).replace(TG_TOKEN, "***")


# לוג לקובץ תמיד (UTF-8, עם רוטציה), ולמסך רק כשיש מסך (pythonw רץ בלי)
_handlers = [RotatingFileHandler(BASE / "tracker.log", maxBytes=1_000_000, backupCount=3,
                                 encoding="utf-8")]
if sys.stderr:
    _handlers.append(logging.StreamHandler())
_fmt = _RedactFormatter("%(asctime)s %(levelname)s %(message)s")
for _h in _handlers:
    _h.setFormatter(_fmt)
logging.basicConfig(level=logging.INFO, handlers=_handlers)
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
    # בסיס אמון ל-DB קיים: המשיכה הגדולה ביותר שהצליחה בעבר
    if meta_get(db, "baseline_total") is None:
        row = db.execute("SELECT MAX(total) FROM runs").fetchone()
        if row and row[0]:
            db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('baseline_total', ?)",
                       (str(row[0]),))
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
                log.error("Telegram send failed: %s", e)  # ה-formatter מסווה את הטוקן


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
def run(cl: Client, db: sqlite3.Connection) -> tuple[str, bool, bool]:
    """מושך עוקבים ומעדכן נוכחות בלבד. מחזיר (טקסט דוח, נמצאו עוקבים חדשים, הסריקה נראית חלקית).

    סריקה חיה היא תצפית בלבד: הופעה של עוקב ברשימה מוכיחה שהוא עוקב, אבל היעדרות ממנה
    אינה מוכיחה שהוא הפסיק - אינסטגרם מקצצת ומדלגת בלי להודיע. לכן מהסריקות החיות לא
    מסיקים ירידות, לא מזיזים מוני היעדרות ולא מסמנים עוקבים כלא פעילים. התראת "הורידו
    עוקב" דורשת מקור שלם חיצוני (ייצוא רשמי), והיא לא קיימת בכלי כרגע.
    """
    now = now_iso()
    pk = target_pk(cl, db)

    expected = cl.user_info(pk).follower_count or 0
    # אינסטגרם מחזירה את הרשימה בדפים, והיא זזה תוך כדי הדפדוף: חלק מהעוקבים חוזרים פעמיים וחלק
    # מדולגים. מאחדים כמה משיכות, עם הפסקות ארוכות ביניהן. האיחוד מצמצם החמצות אקראיות, אבל לא
    # יכול לתקן קיטום שיטתי: חלק מהרשימה שאינסטגרם לא מחזירה בעקביות נשאר בלתי נראה.
    current: dict[str, tuple[str, str]] = {}
    limited = False
    for i in range(FETCH_PASSES):
        order = "date_followed_latest" if i % 2 else None
        raw = cl.user_followers(pk, use_cache=False, amount=0, order=order)
        # אינסטגרם יכולה לסמן שהיא מקצצת את הרשימה (should_limit_list_of_followers). הבדיקה כאן
        # היא best-effort בלבד: last_json מחזיק את התשובה האחרונה, שעלולה להידרש על ידי נתיב גיבוי
        # של instagrapi, ועמודים קודמים אינם נגישים. דגל כבוי אינו ראיה שהרשימה שלמה.
        if (cl.last_json or {}).get("should_limit_list_of_followers"):
            limited = True
        before = len(current)
        current.update({str(k): (u.username, u.full_name or "") for k, u in raw.items()})
        added = len(current) - before
        log.info("Pass %d: +%d, %d unique (profile shows %d)", i + 1, added, len(current), expected)
        if added == 0:
            break
        if i < FETCH_PASSES - 1:
            time.sleep(random.uniform(*PASS_DELAY))
    got = len(current)
    missing_profile = max(0, expected - got)
    coverage = (f"נצפו {got} מתוך {expected} שמוצגים בפרופיל (~{round(100 * got / expected)}%, "
                f"{missing_profile} לא נצפו בסריקה)" if expected else f"נצפו {got} עוקבים")

    # בסיס תצפיות: המשיכה הגדולה ביותר שאי פעם נצפתה. סימן עזר לזיהוי סריקה חלקית בלבד,
    # לא בסיס להוכחת נכונות - אין כזו מסריקה חיה.
    baseline = int(meta_get(db, "baseline_total") or 0)
    partial = limited or bool(baseline and got < baseline * MIN_RATIO)
    if got > baseline:
        meta_set(db, baseline_total=got)
    log.info("Fetched %d / %d followers%s", got, expected,
             " [looks partial]" if partial else "")

    # נוכחות בלבד: תצפית חיובית מאושרת לאחר שתי ראיות (כמו קודם); שורות של עוקבים שלא נצפו
    # בסריקה לא נוגעות בהן בכלל - לא missed, לא active, לא אירועים.
    first_run = db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    rows = {r[0]: r[1:] for r in db.execute(
        "SELECT pk, username, full_name, active, missed, confirmed FROM followers")}
    gained, upserts, state = [], [], []     # state: (active, missed, confirmed, pk)
    for p, (u, n) in current.items():
        row = rows.get(p)
        if row is None:
            upserts.append((p, u, n, now, now, 1, 0, 1 if first_run else 0))
        else:
            _, _, active, _, confirmed = row
            if active and confirmed:
                state.append((1, 0, 1, p))          # נצפה שוב: מרענן נוכחות
            elif active:                            # ממתין, נצפה עוד פעם: מאושר (שתי תצפיות, לא בהכרח ברצף)
                state.append((1, 0, 1, p))
                gained.append((p, u, n))
            else:                                   # סומן בעבר כ"ירד" (היסק ישן) ונצפה: ממתין לאישור
                state.append((1, 0, 0, p))
    not_observed = sum(1 for p, r in rows.items() if r[2] and p not in current)
    db.executemany(
        """INSERT INTO followers(pk, username, full_name, first_seen, last_seen, active, missed, confirmed)
           VALUES(?, ?, ?, ?, ?, ?, ?, ?)""", upserts)
    db.executemany("UPDATE followers SET active=?, missed=?, confirmed=? WHERE pk=?", state)
    db.executemany("UPDATE followers SET username=?, full_name=?, last_seen=? WHERE pk=?",
                   [(u, n, now, p) for p, (u, n) in current.items()])

    if not first_run:
        db.executemany("INSERT INTO events(pk, username, type, ts) VALUES(?, ?, 'follow', ?)",
                       [(p, u, now) for p, u, _ in gained])

    db.execute("INSERT INTO runs(ts, total, lost, gained) VALUES(?, ?, 0, ?)",
               (now, got, len(gained)))
    db.commit()

    if first_run:
        # גם התצפית הראשונה מתוייגת במפורש כחלקית כשהיא לא כיסתה את המספר בפרופיל
        partial = partial or bool(expected and got < expected * MIN_RATIO)
        head = (f"⚠️ <b>הטראקר הופעל על @{html.escape(TARGET)}, התצפית הראשונה נראית חלקית</b>"
                if partial else f"✅ <b>הטראקר הופעל</b> על @{html.escape(TARGET)}")
        return (f"{head}\n"
                f"נשמר בסיס תצפיות. {coverage}.\n"
                "שים לב: תצפית חיה מוכיחה מי עוקב, לא מי הפסיק - שינויים אצל מי שלא נצפה "
                "בסריקות לא יתגלו."), True, partial

    unknown = (f"\n❓ {not_observed} עוקבים ידועים לא נצפו בסריקה (סטטוס לא ידוע)"
               if not_observed else "")
    note = ("\n⚠️ הסריקה נראית חלקית, כנראה בגלל קיטום של אינסטגרם. הנוכחות שנצפתה עודכנה, "
            "אבל אי אפשר ללמוד ממנה על מי שלא נצפה.") if partial else ""
    lines = []
    if gained and NOTIFY_NEW:
        # "נצפו לראשונה" ולא "עוקבים חדשים": עוקב ותיק שלא נצפה בעבר יופיע כאן בפעם הראשונה
        lines.append(f"📈 <b>נצפו לראשונה ({len(gained)})</b>")
        lines += [fmt_user(u, n) for _, u, n in sorted(gained, key=lambda x: x[1])]
    if not lines:
        return f"✅ בדיקה עברה, לא נצפו עוקבים חדשים. 👥 {coverage}{unknown}{note}", False, partial
    lines.append(f"\n👥 {coverage}")
    if unknown:
        lines.append(unknown.strip())
    if note:
        lines.append(note.strip())
    return "\n".join(lines), True, partial

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
    log.error("Unexpected error", exc_info=e)
    return Result(False, f"❌ שגיאה: <code>{html.escape(str(e))[:500]}</code>", "retry")


def check(ask_code=None) -> Result:
    """בדיקה מלאה: התחברות + משיכה + השוואה. לא זורק חריגות, מתעד סטטוס ב-meta."""
    db = open_db()
    try:
        meta_set(db, last_attempt=now_iso())
        try:
            text, changed, uncertain = run(login(ask_code), db)
            res = Result(True, text, "uncertain" if uncertain else "ok", changed)
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
