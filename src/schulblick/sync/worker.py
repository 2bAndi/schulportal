"""Long-running worker: schedules portal syncs, backups and housekeeping."""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .. import db
from ..config import Settings, get_settings, read_sph_credentials
from ..sph.client import LoginLocked, PortalDown, SPHClient, WrongCredentials
from .runner import Syncer

log = logging.getLogger("schulblick.worker")

FREQUENT = ("substitutions", "lessons", "messages")
DAILY = ("lessons", "calendar", "messages")
FULL = ("lessons", "timetables", "substitutions", "calendar", "messages")


class Worker:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        db.init_db(settings.db_path)
        self.conn = db.connect(settings.db_path)
        self.client: SPHClient | None = None
        self._creds_mtime: float | None = None

    # -- blocking after auth problems -----------------------------------------------------

    def _meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key: str, value: str | None) -> None:
        if value is None:
            self.conn.execute("DELETE FROM meta WHERE key=?", (key,))
        else:
            self.conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (key, value))

    def _blocked(self) -> bool:
        block = self._meta("portal_block")
        if not block:
            return False
        kind, _, rest = block.partition("|")
        if kind == "credentials":
            mtime = self.s.sph_secret_path.stat().st_mtime if self.s.sph_secret_path.exists() else 0
            if str(mtime) != rest.split("|")[0]:
                log.info("Credentials file changed, trying again")
                self._set_meta("portal_block", None)
                return False
            return True
        if kind == "locked":
            until = datetime.fromisoformat(rest.split("|")[0])
            if datetime.now(timezone.utc) >= until:
                self._set_meta("portal_block", None)
                return False
            return True
        return False

    def _client(self) -> SPHClient:
        mtime = self.s.sph_secret_path.stat().st_mtime
        if self.client is None or mtime != self._creds_mtime:
            username, password = read_sph_credentials(self.s.sph_secret_path)
            if self.client:
                self.client.close()
            self.client = SPHClient(self.s.school_id, username, password)
            self._creds_mtime = mtime
        return self.client

    # -- jobs ---------------------------------------------------------------------------

    def sync(self, areas: tuple[str, ...]) -> None:
        if self._blocked():
            log.warning("Portal access blocked (%s) – skipping %s", self._meta("portal_block"), ",".join(areas))
            return
        try:
            results = Syncer(self.s, self.conn, self._client()).run(areas)
            log.info("sync %s", results)
        except WrongCredentials as exc:
            mtime = self.s.sph_secret_path.stat().st_mtime
            self._set_meta("portal_block", f"credentials|{mtime}|{exc}")
            log.error("Parent credentials rejected. Fix the secret file; no further login attempts until then.")
        except LoginLocked as exc:
            until = datetime.now(timezone.utc) + timedelta(hours=1)
            self._set_meta("portal_block", f"locked|{until.isoformat()}|{exc}")
            log.error("Portal login locked: %s", exc)
        except PortalDown as exc:
            log.warning("Portal down: %s", exc)

    def backup(self) -> None:
        target_dir = self.s.db_path.parent / "backups"
        target_dir.mkdir(exist_ok=True)
        target = target_dir / f"schulblick-{datetime.now(self.s.tz):%Y%m%d}.db"
        dest = sqlite3.connect(target)
        try:
            self.conn.backup(dest)
        finally:
            dest.close()
        backups = sorted(target_dir.glob("schulblick-*.db"))
        for old in backups[:-7]:
            old.unlink()
        log.info("Backup written to %s", target)

    def housekeeping(self) -> None:
        now = db.utcnow()
        self.conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
        self.conn.execute("DELETE FROM login_failures WHERE at < ?", (day_ago,))
        month_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
        self.conn.execute("DELETE FROM sync_runs WHERE started_at < ?", (month_ago,))

    # -- schedule -----------------------------------------------------------------------

    def run_forever(self) -> None:
        tz = self.s.tz
        # one worker thread: jobs share the DB connection and the portal session, so run them one after another
        sched = BlockingScheduler(timezone=tz, executors={"default": ThreadPoolExecutor(1)}, job_defaults={"coalesce": True, "max_instances": 1,
                                                              "misfire_grace_time": 900})
        add = sched.add_job
        # school days: every 30 minutes 06:15–16:45
        add(self.sync, CronTrigger(day_of_week="mon-fri", hour="6-16", minute="15,45", timezone=tz),
            args=[FREQUENT], id="school-day")
        # evenings before a school day: tomorrow's plan, homework, messages
        add(self.sync, CronTrigger(day_of_week="mon-thu,sun", hour="19,21", minute="0", timezone=tz),
            args=[FREQUENT], id="evening")
        # weekends / holidays
        add(self.sync, CronTrigger(day_of_week="sat,sun", hour="8,18", minute="5", timezone=tz),
            args=[FREQUENT], id="weekend")
        # calendar and attendance once a day, timetables once a week
        add(self.sync, CronTrigger(hour="5", minute="30", timezone=tz), args=[DAILY], id="daily")
        add(self.sync, CronTrigger(day_of_week="sun", hour="17", minute="40", timezone=tz),
            args=[("timetables",)], id="weekly")
        add(self.backup, CronTrigger(hour="3", minute="30", timezone=tz), id="backup")
        add(self.housekeeping, CronTrigger(hour="3", minute="45", timezone=tz), id="housekeeping")

        log.info("Initial full sync")
        self.sync(FULL)
        sched.start()


def main() -> None:
    logging.basicConfig(force=True, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    while not settings.sph_secret_path.exists():
        log.error("Secret file %s missing – waiting", settings.sph_secret_path)
        time.sleep(60)
    Worker(settings).run_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
