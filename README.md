# Schulblick

Private family dashboard on top of **Schulportal Hessen** (parent account). A worker logs in with the parent
credentials, reads homework, exams, substitutions, school calendar, timetables, attendance and recent
messages for all children, and stores them in SQLite. A small web app shows each family member their slice:
parents see everything, each child sees only their own items in the same layout.

```
Schulportal Hessen ──► worker (APScheduler, httpx) ──► SQLite (/data) ◄── web (FastAPI, Jinja2) ◄── nginx (TLS) ◄── phones
```

## Components

| Path | Purpose |
|---|---|
| `src/schulblick/sph/client.py` | Login flow (login → connect → start), session renewal, page/AJAX fetches |
| `src/schulblick/sph/cryptor.py` | RSA handshake + AES ("Salted__") for encrypted message payloads |
| `src/schulblick/sph/parsers.py` | Pure parsers for the parent views (tested against anonymised fixtures) |
| `src/schulblick/sync/assign.py` | Maps items to children: course groups, classes, grade audiences, message recipients |
| `src/schulblick/sync/runner.py` | One sync pass per area with change detection (`first_seen` / `changed_at`) |
| `src/schulblick/sync/worker.py` | Schedule, credential blocking after rejected logins, nightly backup |
| `src/schulblick/web/` | Accounts (argon2, TOTP for parents), sessions, overview per role |
| `config/family.toml` | School id, children, display names, accounts, display rules |
| `deploy/` | nginx vhost for `family.verstehst.de`, setup and update scripts |

Login and encryption follow liblanis (lanis-mobile, Aug 2026). Mapping rules:

* **Exams/tests**: calendar events from the "Klausurplaner" carry their course group (`Französisch g8a (081F01-GYM)`),
  matched against the child's courses from *Mein Unterricht*.
* **Other events**: grade/class audiences parsed from text (`JG5-7`, `G9+R9`, `5. und 6. Klassen`, `Unterstufe`);
  no audience = whole family. Keywords in `parent_only_keywords` hide events from children.
* **Substitutions**: rows naming the child's class; `sure` when the subject is in the child's timetable slot, otherwise
  shown as "falls belegt".
* **Messages** (last 5 days): groups addressed to *Eltern …* are parents-only; *Lernende/SuS* groups are also shown
  to the matching children. Loading a body marks it read in the portal (`fetch_message_bodies`).

## Development

```bash
pip install -e .[dev]
pytest
# preview with anonymised fixtures
export SCHULBLICK_CONFIG=tests/fixtures/family_test.toml SCHULBLICK_DB=/tmp/sb.db SCHULBLICK_COOKIE_SECURE=false
schulblick sync --fixtures tests/fixtures
schulblick set-password papa
uvicorn schulblick.web.app:create_app --factory --port 8765
```

## Deployment

See `deploy/ANLEITUNG.md` (German, step by step). Short version on the server:

```bash
sudo mkdir -p /opt/schulblick && sudo chown $USER /opt/schulblick   # then copy the project there
cd /opt/schulblick && sudo bash deploy/setup.sh
```

## Security notes

* The parent password for the portal lives only in `secrets/sph_credentials` (owner uid 10001, mode 0400), mounted
  as a Docker secret into the worker. The web container never sees it.
* Containers are read-only, drop all capabilities, run as uid 10001; the web port is bound to 127.0.0.1.
* Authorisation is enforced when the overview is built (`OverviewBuilder`), not only in templates.
* After a rejected portal login the worker stops trying until the secret file changes (avoids account lockout).

## Roadmap: family calendar

Planned next: a shared family calendar in the same app. Design sketch:

* table `family_events(id, title, start, end, all_day, location, note, audience JSON, created_by, updated_at)` where
  `audience` is a list of child ids, `["*"]` for everyone, or `["parents"]`;
* everyone can add entries; children see their own and family-wide entries, parents see all;
  children may edit only what they created, parents may edit everything;
* school items (exams, holidays, substitutions) appear read-only in the same calendar view;
* per-person ICS feed (`/ics/<token>.ics`) so phones can subscribe.
