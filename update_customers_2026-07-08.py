"""Customer data update, 8 July 2026.

Source: Customer data.xlsx (954 rows). Applies:
  1. Six new customer categories (Stockist, Walkin, Distributor, Kiosk,
     Direct sale, Church).
  2. Category assignment for every customer per the file (523 fills,
     1 change).
  3. account_status='on_hold' for customers marked "Not active" (kept
     visible for win-back follow-up, not archived).
  4. Rename: SIRIUS LIMITED -> SIRIUS LIMITED - CANARY HOTEL BUKOTO.
  5. Rep user accounts (role='rep', locked until admin activates) and
     customer_reps links, with spelling variants merged.

Backs up the database first. Idempotent: safe to re-run.

Run:  python3 update_customers_2026-07-08.py path/to/Customer\ data.xlsx
"""
import os
import re
import secrets
import shutil
import sqlite3
import sys
import unicodedata
from datetime import datetime

import bcrypt
import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "instance", "pricing.db")
XLSX = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "imports", "Customer data.xlsx")

NEW_CATEGORIES = ["Stockist", "Walkin", "Distributor", "Kiosk", "Direct sale", "Church"]

# Category label in file -> canonical category name (case/space variants).
CAT_ALIASES = {
    "school/ institution": "School / Institution",
    "school / institution": "School / Institution",
}

# Rep spelling variants -> canonical name.
REP_ALIASES = {
    "Eunice Kussima": "Eunice Kusiima",
    "Eunice KUsiima": "Eunice Kusiima",
    "Harriet AKello": "Harriet Akello",
    "Tonny Mpajji": "Tonny Mpagi",
    "Tony Mpagi": "Tonny Mpagi",
    "Tonny Mpaggi": "Tonny Mpagi",
    "Lillian Nakaweesi": "Lilian Nakaweesi",
    "Lilian Nakawesi": "Lilian Nakaweesi",
    "Lilian Nakwesi": "Lilian Nakaweesi",
    "LilIian Nakawesi": "Lilian Nakaweesi",
    "Namungolo Rebecca": "Namungholo Rebecca",
    "Namungholo": "Namungholo Rebecca",
    "NAMUNGHOLO REBECCA": "Namungholo Rebecca",
    "Whitney Achaki": "Achaki Whitney",
    "Derrick Mugisa": "Mugisa Derrick",
    "Mugisa Derick": "Mugisa Derrick",
    "Barugahre Gilbert": "Barugahare Gilbert",
    "Baruhagare Gilbert": "Barugahare Gilbert",
    "Beatrice": "Beatrice Kagoro",
    "Tonny Mboga": "Tonny Mbogga",
    "Gift": "Gift Atwiine",
    "Diana": "Diana Kalinte",
    "Dianah Kalinte": "Diana Kalinte",
    "Tayebwa Daphine": "Daphine Tayebwa",
    "Niwaga Hillary": "Niwagaba Hillary",
}

# Rep names that are existing app users: rep label -> username.
REP_EXISTING_USERS = {"Jngari": "Sokoni", "Rep1": "Rep1", "Angela": "Zanika"}

# Not resolvable to a person; reported, not linked.
REP_SKIP = {"Marketing", "Birdnest Bunyonyi Resort", "John Mpagi"}

RENAMES = {"SIRIUS LIMITED": "SIRIUS LIMITED - CANARY HOTEL BUKOTO"}


def norm(s):
    s = unicodedata.normalize("NFKC", str(s)).strip().upper()
    return re.sub(r"\s+", " ", s)


def clean(v):
    if v is None:
        return None
    v = str(v).strip()
    return None if v in ("", "None") else v


def main():
    ts = datetime.now().strftime("%Y-%m-%d_%H%M")
    backup = os.path.join(HERE, "instance", f"pricing_backup_{ts}_pre_customer_update.db")
    src = sqlite3.connect(DB)
    try:
        dst = sqlite3.connect(backup)
        src.backup(dst)
        dst.close()
    except sqlite3.OperationalError:
        src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        shutil.copy2(DB, backup)
    print(f"Backup: {backup}")

    cur = src.cursor()
    report = {"cats_added": 0, "cat_set": 0, "on_hold": 0, "reactivated": 0,
              "renamed": 0, "users_created": 0, "rep_links": 0,
              "unmatched": [], "skipped_reps": {}}

    # 1. Categories.
    existing = {r[1].lower(): r[0] for r in cur.execute("SELECT id, name FROM customer_category")}
    order = cur.execute("SELECT COALESCE(MAX(sort_order),0) FROM customer_category").fetchone()[0] or 0
    for name in NEW_CATEGORIES:
        if name.lower() not in existing:
            order += 1
            cur.execute("INSERT INTO customer_category (name, sort_order) VALUES (?,?)", (name, order))
            existing[name.lower()] = cur.lastrowid
            report["cats_added"] += 1

    def cat_id(label):
        label = clean(label)
        if not label:
            return None
        canonical = CAT_ALIASES.get(label.lower(), label)
        return existing.get(canonical.lower())

    # 2. Load file and index DB customers by normalized name.
    wb = openpyxl.load_workbook(XLSX, data_only=True)
    rows = [r for r in wb["Customers"].iter_rows(values_only=True) if r[0] and r[0] != "Customer"]
    db_cust = {norm(r[1]): r[0] for r in cur.execute("SELECT id, name FROM customer")}

    # 3. Rename first so the new file row matches.
    for old, new in RENAMES.items():
        if norm(old) in db_cust and norm(new) not in db_cust:
            cur.execute("UPDATE customer SET name=? WHERE id=?", (new, db_cust[norm(old)]))
            db_cust[norm(new)] = db_cust.pop(norm(old))
            report["renamed"] += 1

    # 4. Rep users.
    users = {r[0]: r[1] for r in cur.execute("SELECT full_name, id FROM user")}
    usernames = {r[0].lower() for r in cur.execute("SELECT username FROM user")}
    existing_by_username = {r[1].lower(): r[0] for r in cur.execute("SELECT id, username FROM user")}

    def rep_user_id(label):
        label = re.sub(r"\s+", " ", label.strip())
        if label in REP_SKIP:
            report["skipped_reps"][label] = report["skipped_reps"].get(label, 0) + 1
            return None
        if label in REP_EXISTING_USERS:
            return existing_by_username.get(REP_EXISTING_USERS[label].lower())
        canonical = REP_ALIASES.get(label, label)
        if canonical in users:
            return users[canonical]
        parts = canonical.split()
        base = (parts[0][0] + parts[-1]).lower() if len(parts) > 1 else parts[0].lower()
        uname = base
        n = 2
        while uname in usernames:
            uname = f"{base}{n}"
            n += 1
        pw_hash = bcrypt.hashpw(secrets.token_urlsafe(16).encode(), bcrypt.gensalt()).decode()
        cur.execute(
            "INSERT INTO user (username, full_name, password_hash, role, can_edit,"
            " is_active, created_at, failed_attempts) VALUES (?,?,?,?,0,0,?,0)",
            (uname, canonical, pw_hash, "rep", datetime.utcnow().isoformat(sep=" ")))
        users[canonical] = cur.lastrowid
        usernames.add(uname)
        report["users_created"] += 1
        return users[canonical]

    # 5. Row-by-row updates.
    for r in rows:
        cid = db_cust.get(norm(r[0]))
        if cid is None:
            report["unmatched"].append(r[0])
            continue

        status = "on_hold" if clean(r[1]) == "Not active" else "ok"
        prev = cur.execute("SELECT account_status FROM customer WHERE id=?", (cid,)).fetchone()[0]
        if prev != status:
            cur.execute("UPDATE customer SET account_status=? WHERE id=?", (status, cid))
            report["on_hold" if status == "on_hold" else "reactivated"] += 1

        ct = cat_id(r[3])
        if ct is not None:
            prev_ct = cur.execute("SELECT category_id FROM customer WHERE id=?", (cid,)).fetchone()[0]
            if prev_ct != ct:
                cur.execute("UPDATE customer SET category_id=? WHERE id=?", (ct, cid))
                report["cat_set"] += 1

        reps_raw = clean(r[4])
        if reps_raw:
            uids = {rep_user_id(p) for p in reps_raw.split(",") if p.strip()}
            uids.discard(None)
            for uid in uids:
                done = cur.execute(
                    "SELECT 1 FROM customer_reps WHERE customer_id=? AND user_id=?",
                    (cid, uid)).fetchone()
                if not done:
                    cur.execute("INSERT INTO customer_reps (customer_id, user_id) VALUES (?,?)", (cid, uid))
                    report["rep_links"] += 1

    src.commit()
    src.close()
    print(report)


if __name__ == "__main__":
    main()
