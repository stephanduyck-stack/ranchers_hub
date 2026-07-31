"""One-way data migration: SQLite (instance/pricing.db) -> PostgreSQL.

Usage (rehearse on a COPY first, always):

    SECRET_KEY=x \
    SOURCE_SQLITE=instance/pricing.db \
    TARGET_DATABASE_URL=postgresql+psycopg2://user:pass@host/dbname \
    python3 migrate_to_postgres.py

What it does, in order:
  1. Builds the full current schema on the empty target (create_all).
  2. Copies every table in foreign-key dependency order, converting SQLite's
     0/1 integers to real booleans.
  3. Resets the PostgreSQL sequences so new rows continue after the copied ids.
  4. Verifies row counts per table and prints a summary.

Deliberately does NOT install the accounting triggers: posted journal entries
could not be copied under them (the append-only guards would fire). The app's
first boot on the new DATABASE_URL installs and verifies all 25 triggers
before serving anything.

The target database must be EMPTY. The script refuses to run against a target
that already contains rows.
"""
import os
import sys

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy import Boolean


def main():
    src_path = os.environ.get("SOURCE_SQLITE", "instance/pricing.db")
    target_url = os.environ.get("TARGET_DATABASE_URL")
    if not target_url:
        print("Set TARGET_DATABASE_URL (postgresql+psycopg2://...)")
        sys.exit(2)
    if not target_url.startswith("postgresql"):
        print("TARGET_DATABASE_URL must be a postgresql:// URL")
        sys.exit(2)
    if not os.path.exists(src_path):
        print(f"Source SQLite file not found: {src_path}")
        sys.exit(2)

    # Import the models WITHOUT creating the real Flask app (create_app would
    # install the accounting triggers, which must come only after the copy).
    # A minimal shim app carries the SQLAlchemy metadata instead.
    os.environ.setdefault("SECRET_KEY", "migration-only")
    os.environ["DATABASE_URL"] = "sqlite:///" + os.path.abspath(src_path)
    from extensions import db          # noqa: E402
    import flask
    shim = flask.Flask(__name__)
    shim.config["SQLALCHEMY_DATABASE_URI"] = os.environ["DATABASE_URL"]
    shim.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(shim)
    with shim.app_context():
        import models  # noqa: F401 — registers every table on db.metadata

        src = db.engine
        dst = create_engine(target_url)

        # target must be empty
        dst_insp = inspect(dst)
        existing = dst_insp.get_table_names()
        if existing:
            with dst.connect() as c:
                for t in existing:
                    n = c.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar()
                    if n:
                        print(f"Target is not empty ({t} has {n} rows). "
                              "Refusing to overwrite.")
                        sys.exit(1)

        print("Creating schema on target…")
        db.metadata.create_all(dst)

        src_insp = inspect(src)
        src_tables = set(src_insp.get_table_names())
        report, failures = [], 0
        with src.connect() as sc, dst.begin() as dc:
            for table in db.metadata.sorted_tables:
                if table.name not in src_tables:
                    report.append((table.name, 0, 0, "not in source, skipped"))
                    continue
                bool_cols = [c.name for c in table.columns
                             if isinstance(c.type, Boolean)]
                rows = sc.execute(select(table)).mappings().all()
                if rows:
                    payload = []
                    for r in rows:
                        d = dict(r)
                        for b in bool_cols:
                            if d.get(b) is not None:
                                d[b] = bool(d[b])
                        payload.append(d)
                    dc.execute(table.insert(), payload)
                n_dst = dc.execute(
                    text(f'SELECT COUNT(*) FROM "{table.name}"')).scalar()
                ok = "OK" if n_dst == len(rows) else "MISMATCH"
                if ok != "OK":
                    failures += 1
                report.append((table.name, len(rows), n_dst, ok))
                # continue after the copied ids
                if "id" in table.columns and rows:
                    dc.execute(text(
                        f"SELECT setval(pg_get_serial_sequence('\"{table.name}\"', 'id'), "
                        f"(SELECT COALESCE(MAX(id), 1) FROM \"{table.name}\"))"))

        width = max(len(t) for t, *_ in report)
        for t, n_src, n_dst, ok in report:
            print(f"{t:<{width}}  {n_src:>8} -> {n_dst:>8}  {ok}")
        total = sum(n for _, n, _, _ in report)
        print(f"\n{total} rows copied across {len(report)} tables.")
        if failures:
            print(f"{failures} table(s) MISMATCHED — do not go live on this copy.")
            sys.exit(1)
        print("Done. Point DATABASE_URL at the target and boot the app: the "
              "first boot installs and verifies the accounting triggers.")


if __name__ == "__main__":
    main()
