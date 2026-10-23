"""Offline stand-in for SPHClient that reads saved pages from a directory.

Used for tests and for previewing the web app without touching the portal:
    schulblick sync --fixtures tests/fixtures
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path


class FixtureClient:
    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)

    def _read(self, name: str) -> str:
        return (self.dir / name).read_text(encoding="utf-8")

    def lessons_html(self) -> str:
        return self._read("meinunterricht_parent.html")

    def substitutions_html(self) -> str:
        return self._read("vertretungsplan.html")

    def timetable_overview_html(self) -> str:
        return self._read("stundenplan_overview.html")

    def timetable_class_html(self, href: str) -> str:
        klass = href.split("k=")[-1].split("&")[0]
        specific = self.dir / f"stundenplan_klasse_{klass}.html"
        return specific.read_text(encoding="utf-8") if specific.exists() else self._read("stundenplan_klasse.html")

    def calendar_events(self, start: date, end: date) -> list[dict]:
        return json.loads(self._read("kalender_events.json"))

    def message_headers(self) -> list[dict]:
        return json.loads(self._read("nachrichten_headers.json"))

    def message(self, uniquid: str) -> dict:
        bodies = json.loads(self._read("nachrichten_bodies.json")) if (self.dir / "nachrichten_bodies.json").exists() else {}
        return bodies.get(uniquid, {"Inhalt": f"(Beispielinhalt für {uniquid})", "reply": []})

    def close(self) -> None:
        pass
