#!/usr/bin/env python3
"""בדיקות אופליין: סמנטיקת תצפית-בלבד, עוקבים חדשים, מיגרציה, לוגים ו-offset.
מריצים משורש הריפו:  python3 tests/offline_test.py
משתמש בלקוח מזויף - בלי אינסטגרם, בלי רשת, בלי סיסמאות אמיתיות."""
import io
import logging
import os
import sys
import types

os.environ.update({
    "IG_USERNAME": "dummy_ig", "IG_PASSWORD": "dummy_pw", "TARGET_USERNAME": "target_acc",
    "TELEGRAM_BOT_TOKEN": "123:SECRETTOKEN", "TELEGRAM_CHAT_ID": "999",
    "DB_PATH": "/tmp/test_tracker.db", "SESSION_PATH": "/tmp/test_session.json",
    "FETCH_PASSES": "1", "LOST_CONFIRM_CHECKS": "3", "MIN_FETCH_RATIO": "0.9",
})
for i in range(1, 9):
    f = f"/tmp/test_tracker{i}.db"
    if os.path.exists(f):
        os.remove(f)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tracker as t

import requests


class FakeUser:
    def __init__(self, username, full_name=""):
        self.username = username
        self.full_name = full_name


class FakeClient:
    def __init__(self, followers, profile_count, limited=False):
        self.followers = followers
        self.profile_count = profile_count
        self.last_json = {"should_limit_list_of_followers": limited}

    def user_id_from_username(self, u):
        return "PK_TARGET"

    def user_info(self, pk):
        return types.SimpleNamespace(follower_count=self.profile_count)

    def user_followers(self, pk, use_cache=False, amount=0, order=None):
        assert order in (None, "date_followed_latest", "date_followed_earliest"), order
        return {p: FakeUser(u, n) for p, (u, n) in self.followers.items()}


BASE_F = {f"pk{i}": (f"user{i}", f"Name {i}") for i in range(1, 21)}


def do_run(followers, profile_count=None, limited=False):
    cl = FakeClient(followers, profile_count if profile_count is not None else len(followers),
                    limited)
    db = t.open_db()
    try:
        return t.run(cl, db)
    finally:
        db.close()


def q(sql, args=()):
    db = t.open_db()
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()


def events():
    return q("SELECT username, type FROM events ORDER BY id")


def missed_of(pk):
    row = q("SELECT missed FROM followers WHERE pk=?", (pk,))
    return row[0][0] if row else None


def active_of(pk):
    row = q("SELECT active FROM followers WHERE pk=?", (pk,))
    return row[0][0] if row else None


def baseline():
    db = t.open_db()
    v = t.meta_get(db, "baseline_total")
    db.close()
    return v


fails = []


def expect(label, cond):
    print(("PASS" if cond else "FAIL"), label)
    if not cond:
        fails.append(label)


# --- חלק 1: נוכחות ועוקבים חדשים ---
t.DB_PATH = "/tmp/test_tracker1.db"
txt, ch, partial = do_run(BASE_F)
expect("1 first run: observation baseline seeded, no events, no completeness claim",
       ch and not partial and not events() and baseline() == "20"
       and "בסיס תצפיות" in txt and "לא מי הפסיק" in txt)

f_new = dict(BASE_F)
f_new["pk99"] = ("newbie", "New Bee")
_, ch2, _ = do_run(f_new)
txt, ch3, _ = do_run(f_new)
expect("2 new follower: silent on 1st sighting, follow event on 2nd",
       not ch2 and ch3 and events() == [("newbie", "follow")])
expect("3 report carries observed coverage, not follower total",
       "נצפו 21 מתוך 21" in txt)

# --- חלק 2: היעדרות לא מוכיחה כלום ---
t.DB_PATH = "/tmp/test_tracker2.db"
do_run(BASE_F)  # בסיס תצפיות 20
subset16 = dict(list(BASE_F.items())[:16])
partials, txt_last = True, ""
for _ in range(5):
    txt_last, _, p = do_run(subset16, profile_count=20)
    partials = partials and p
expect("4 constant 80% subset x5: no unfollow events, missed frozen, unknown status shown",
       partials and not events() and all(missed_of(f"pk{i}") == 0 for i in range(17, 21))
       and all(active_of(f"pk{i}") == 1 for i in range(17, 21))
       and "4 עוקבים ידועים לא נצפו" in txt_last and "סטטוס לא ידוע" in txt_last)

t.DB_PATH = "/tmp/test_tracker3.db"
do_run(BASE_F)
drop12 = {k: v for k, v in BASE_F.items() if k not in ("pk1", "pk2")}
drop34 = {k: v for k, v in BASE_F.items() if k not in ("pk3", "pk4")}
do_run(drop12)   # 90%: pk1,pk2 לא נצפו
do_run(drop34)   # 90%: pk3,pk4 לא נצפו, pk1,pk2 חזרו
expect("5 rotating 90% subsets: zero negative inference, presence refreshed",
       not events() and all(missed_of(f"pk{i}") == 0 for i in range(1, 5))
       and all(active_of(f"pk{i}") == 1 for i in range(1, 5)))

t.DB_PATH = "/tmp/test_tracker4.db"
do_run(BASE_F)
swap = {k: v for k, v in BASE_F.items() if k != "pk1"}
swap["pk21"] = ("swapper", "Swap Per")
txt, ch, partial = do_run(swap, profile_count=20)  # אותו מספר בדיוק, הרכב שונה
_, _, _ = do_run(swap, profile_count=20)
ev = events()
expect("6 equal-count replacement: pk1 not declared lost, pk21 confirmed as new only",
       not partial and missed_of("pk1") == 0 and active_of("pk1") == 1
       and ev == [("swapper", "follow")] and "1 עוקבים ידועים לא נצפו" in txt)

t.DB_PATH = "/tmp/test_tracker5.db"
txt, ch, partial = do_run(BASE_F, profile_count=0)  # מספר פרופיל לא ידוע
expect("7 zero/unknown profile count: no crash, count-only coverage text",
       ch and not partial and "נצפו 20 עוקבים" in txt and "%" not in txt)

# --- חלק 3: התחלה חלקית וסימן מים ---
t.DB_PATH = "/tmp/test_tracker6.db"
txt, ch, partial = do_run(subset16, profile_count=20)  # התחלה על 80%
expect("8 initial 80%: visibly labeled partial observation, not reliable completeness",
       partial and "התצפית הראשונה נראית חלקית" in txt
       and "נצפו 16 מתוך 20" in txt and "4 לא נצפו" in txt and "לא מי הפסיק" in txt
       and baseline() == "16")
do_run(BASE_F, profile_count=20)   # הסריקה הראשונה שרואה את pk17-20: ממתינים
txt, _, _ = do_run(BASE_F, profile_count=20)  # ראיה שניה: מאושרים כ"חדשים"
ev = events()
expect("9 watermark rises; previously-invisible followers surface as new (documented tradeoff)",
       baseline() == "20" and len(ev) == 4 and all(k == "follow" for _, k in ev))

# --- חלק 4: DB מדור קודם ---
t.DB_PATH = "/tmp/test_tracker7.db"
db = t.open_db()  # יוצר סכמה
db.execute("INSERT INTO followers VALUES('pk_old','oldie','Old User','2026-01-01','2026-01-01',1,2,1)")
db.execute("INSERT INTO followers VALUES('pk_gone','gone','Gone User','2026-01-01','2026-01-01',0,0,1)")
db.execute("INSERT INTO events(pk, username, type, ts) VALUES('pk_gone','gone','unfollow','2026-01-02')")
for total in (10, 25, 20):
    db.execute("INSERT INTO runs(ts, total, lost, gained) VALUES('2026-01-03', ?, 0, 0)", (total,))
db.commit()
db.close()
db = t.open_db()  # מיגרציה: בסיס תצפיות מההיסטוריה
migrated = t.meta_get(db, "baseline_total")
db.close()
f_legacy = dict(BASE_F)
f_legacy["pk_gone"] = ("gone", "Gone User")  # pk_old לא נצפה; pk_gone מופיע מחדש
do_run(f_legacy, profile_count=21)
ev = events()
expect("10 legacy DB: missed=2 preserved, absent pk untouched, old unfollow kept as legacy",
       migrated == "25" and missed_of("pk_old") == 2 and active_of("pk_old") == 1
       and ev == [("gone", "unfollow")])
do_run(f_legacy, profile_count=21)  # ראיה שניה: pk_gone ו-pk1-20 מאושרים כעוקבים
ev = events()
expect("11 legacy inactive row reappearing: re-confirmed as follow, still no new unfollow",
       ev.count(("gone", "unfollow")) == 1 and ("gone", "follow") in ev
       and all(k == "follow" for _, k in ev[1:]) and active_of("pk_gone") == 1)

# --- חלק 5: דגל קיטום, לוגים, offset ---
t.DB_PATH = "/tmp/test_tracker8.db"
do_run(BASE_F)
f_lim = dict(BASE_F)
f_lim["pk88"] = ("late", "Late Comer")
txt, _, partial = do_run(f_lim, profile_count=21, limited=True)
expect("12 limited flag: partial warning shown, positive observations still recorded",
       partial and "נראית חלקית" in txt and missed_of("pk88") == 0)

stream = io.StringIO()
h = logging.StreamHandler(stream)
h.setFormatter(t._RedactFormatter("%(levelname)s %(message)s"))
lg = logging.getLogger("test-redact")
lg.handlers = [h]
lg.propagate = False
try:
    raise requests.exceptions.HTTPError(
        "400 Client Error for url: https://api.telegram.org/bot123:SECRETTOKEN/sendMessage")
except Exception:
    lg.exception("Handler failed")
out = stream.getvalue()
expect("13 redacting formatter masks token in message AND traceback",
       "SECRETTOKEN" not in out and "bot***/sendMessage" in out)

import bot as b
t.DB_PATH = "/tmp/test_tracker8.db"
fresh_offset = b.get_offset() is None
b.set_offset(100)
handled = []
orig_handle = b.handle
b.handle = lambda msg: handled.append(msg.get("text") or "")
updates = [
    {"update_id": 50, "message": {"chat": {"id": 999}, "text": "/check"}},
    {"update_id": 100, "message": {"chat": {"id": 999}, "text": "/status"}},
]
new_offset = b.process_updates(updates, b.get_offset())
b.handle = orig_handle
expect("14 restart: stale updates skipped, new processed once, offset persisted",
       fresh_offset and handled == ["/status"] and new_offset == 101 and b.get_offset() == 101)

print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
