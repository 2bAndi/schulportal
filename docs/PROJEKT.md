# Schulblick – Projektakte

Stand: 2. November 2026

Diese Datei bündelt alles Wissen zum Projekt an einem Ort: Ziele, Entscheidungen, Betrieb, Verlauf und offene
Punkte. Die technische Beschreibung steht im [README](../README.md), die Server-Einrichtung in
[deploy/ANLEITUNG.md](../deploy/ANLEITUNG.md).

---

## 1. Worum es geht

Schulblick ist eine private Familien-Web-App. Sie meldet sich mit dem **Elternkonto des Schulportals Hessen** an
(Schule am Ried, Frankfurt, Schulnummer 6055) und zeigt jedem Familienmitglied eine eigene Übersicht.

- **Eltern** (Papa, Mama) sehen alle drei Kinder.
- **Kinder** (Ben G8a, Boje G5b, Carlotta „Lotte“ G5c) sehen nur ihre eigenen Einträge, im gleichen Aufbau.
- **Nachhilfe** (Melissa) sieht nur Hausaufgaben, Arbeiten und Kalender von Boje und Lotte.
- Dazu kommt ein gemeinsamer **Familienkalender**, in den alle eintragen können.

Gestartet im Oktober 2026 mit der Frage, ob das Schulportal eine Schnittstelle hat. Es gibt keine offizielle, nur
die Community-Projekte lanisapi und liblanis/Lanis-Mobile. Auf dieser Grundlage ist Schulblick entstanden.

## 2. Wo was liegt

| Was | Wo |
|---|---|
| Live-App | https://family.verstehst.de |
| Code lokal | `D:\Code\schulportal` (Rechner BRO-WORK-03) |
| GitHub | https://github.com/2bAndi/schulportal (Branch `main`) |
| Server | Tangoserver, IONOS Ubuntu 24.04, `212.227.82.248`, SSH-Benutzer `hidance` |
| Installation auf dem Server | `/opt/schulblick` (eigener Compose-Stack, getrennt von `/opt/wordpress`) |
| Port | Web-Container an `127.0.0.1:8130`, nginx auf dem Host macht TLS |
| Konfiguration | `config/family.toml` (keine Zugangsdaten) |
| Geheimnisse | `secrets/sph_credentials` (Elternkonto), `secrets/anthropic_key` (KI) – nicht in Git |
| Datenbank | SQLite im Docker-Volume `schulblick-data` (`/data/schulblick.db`), nächtliche Sicherung 03:30, 7 Tage |
| Entwicklungs-Chat | https://claude.ai/chat/c14f067f-7021-4679-a2b3-270b4b55547a („Schulportal Hessen Schnittstellen“) |

## 3. Architektur in Kürze

```
Schulportal Hessen ──(Elternkonto, httpx)──► worker ──► SQLite ◄── web (FastAPI) ◄── nginx/TLS ◄── Browser/Handy
                                               │
                                               └──► Anthropic-API (optional, nur worker)
```

- **worker**: meldet sich am Portal an, liest Mein Unterricht, Klassenarbeiten, Vertretungsplan, Kalender,
  Stundenpläne, Anwesenheit und die verschlüsselten Nachrichten (RSA/AES-Handshake nach liblanis), ordnet alles
  den Kindern zu (`sync/assign.py`), Zeitplan per APScheduler. Hat als einziger die Secrets.
- **web**: FastAPI mit Rollen, argon2-Passwörtern, Sitzungs-Tokens (90 Tage), CSP ohne Inline-Skripte,
  installierbar als Web-App (PWA).
- Beide Container laufen als Benutzer 10001, read-only, ohne Capabilities, `no-new-privileges`.
- **Abrufzeiten**: Schultage 06:15–16:45 alle 30 min, abends 19:00 und 21:00 vor Schultagen, Wochenende
  08:05/18:05; Kalender und Anwesenheit 05:30; Stundenpläne sonntags; Outlook-Abos stündlich.

## 4. Funktionsumfang

1. **Übersicht**: „Die nächsten Tage“ (7 Tage, Farbe pro Kind), Hausaufgaben, Klassenarbeiten (10 Wochen),
   Vertretungsplan, Nachrichten der letzten 5 Tage, Anwesenheit, Stundenplan, „neu“-Markierung, Abrufstatus.
2. **Familienkalender**: Einträge anlegen, ändern, löschen; Zielgruppe pro Eintrag (einzelne Kinder, alle,
   nur Eltern); Wiederholungen täglich/wöchentlich/14-tägig/monatlich/jährlich, mit Enddatum und
   „nicht in den Ferien“ (Ferien aus dem Schulkalender); einzelne Termine einer Serie absagen; ICS-Abo pro Person.
3. **Outlook-Import**: einmaliger Datei-Import (.ics, einfache Wiederholungen bleiben erhalten) oder Abo eines
   veröffentlichten Outlook-Kalenders (stündlich, nur lesend). Berücksichtigt Windows-Zeitzonen, Sommerzeit,
   EXDATE und „privat“.
4. **KI-Zusatz** (Anthropic-API, `claude-haiku-5-5`):
   - Kurzfassung jeder neuen Nachricht (2–3 Sätze)
   - Aufgaben und Fristen aus Nachrichten, mit Datum auch in „Die nächsten Tage“
   - Zuordnung zu einem Kind als Rückfall, wenn die Regeln nichts finden
   - Morgenübersicht „Heute früh“ um 06:15: zugeklappte Karte, aufgeklappt Stichpunkte pro Kind, „Alle“ und
     „Familie“; Kinder bekommen ihre eigene Version in der Du-Form
   - Alles über erzwungene Werkzeugaufrufe (`record_message`, `record_digest`), Ergebnisse in SQLite
5. **Nachhilfe-Konto** (`role = "tutor"`, `children = [...]`): Hausaufgaben, Arbeiten, „Die nächsten Tage“,
   Stundenpläne und Kalender der zugeordneten Kinder; sieht Familieneinträge dieser Kinder und für die ganze Familie, aber nichts
   „Nur Mama und Papa“; keine Nachrichten, Vertretungen, Anwesenheit, KI-Übersicht. Kann eigene
   Termine für ihre Kinder anlegen und nur diese ändern.
6. **Farbe umschalten**: Knopf neben den Reitern, automatisch → hell → dunkel, pro Gerät per Cookie.

## 5. Konten

| Schulblick-Benutzer | Person | Rolle |
|---|---|---|
| `papa` | Andreas | Eltern |
| `mama` | Wiebke | Eltern |
| `ben` | Ben | Kind |
| `boje` | Boje | Kind |
| `lotte` | Carlotta | Kind |
| `melissa` | Melissa (Nachhilfe) | Nachhilfe, nur Boje und Carlotta |

Passwörter liegen nur als argon2-Hash vor und lassen sich **nicht** auslesen. Ein vergessenes Passwort wird neu
gesetzt (siehe unten). 2FA ist für alle abgeschaltet (`require_totp_for_parents = false`).

Für das Schulportal selbst gilt: Benutzername so, wie er auf der Anmeldeseite eingetippt wird, **klein
geschrieben und ohne Schulnummer**.

## 6. Spickzettel Betrieb

Alle Befehle auf dem Server in `/opt/schulblick` mit `sudo`.

| Aufgabe | Befehl |
|---|---|
| Konten anzeigen | `docker compose run --rm web schulblick users` |
| Passwort neu setzen | `docker compose run --rm web schulblick set-password <name>` |
| Portal-Login testen | `docker compose run --rm worker schulblick check-login` |
| Sofort abrufen | `docker compose run --rm worker schulblick sync` |
| Log des Abrufs | `docker compose logs -f worker` |
| Letzte Abrufe | `docker compose exec worker sqlite3 -column /data/schulblick.db "SELECT area, ok, message, finished_at FROM sync_runs ORDER BY id DESC LIMIT 6"` |
| Portal-Passwort geändert | `secrets/sph_credentials` anpassen, `docker compose restart worker` |
| KI-Schlüssel setzen | `bash deploy/ki-schluessel.sh` |
| KI sofort laufen lassen | `docker compose exec worker schulblick ai-run` |
| Morgenübersicht neu schreiben | `docker compose exec worker schulblick ai-digest --neu` |
| Neue Version einspielen | Dateien nach `/opt/schulblick` kopieren (ohne `config/`), dann `bash deploy/update.sh` |

Update vom PC aus (PowerShell):

```powershell
cd D:\Code\schulportal
git push
scp -r pyproject.toml docker-compose.yml src deploy hidance@212.227.82.248:/tmp/schulblick-neu/
```

Auf dem Server: `sudo cp -r /tmp/schulblick-neu/* /opt/schulblick/ && cd /opt/schulblick && sudo bash deploy/update.sh`

## 7. Entscheidungen

| Datum | Entscheidung |
|---|---|
| Okt. 2026 | Eigene App auf dem Tangoserver statt fremder App; Abruf über das Elternkonto |
| Okt. 2026 | Fünf Konten, Kinder sehen nur Eigenes im gleichen Format, Eltern alles |
| Okt. 2026 | Nachrichten nur neue und höchstens 5 Tage alt; Inhalte werden geladen (markiert sie im Portal als gelesen) |
| Okt. 2026 | Kinder sehen ihre eigene Anwesenheit; Anzeigename „Lotte“ für Carlotta |
| Okt. 2026 | Subdomain `family.verstehst.de` |
| 23.10.2026 | Abschnitt „Schultermine“ entfällt, abgedeckt durch „Die nächsten Tage“ |
| 23.10.2026 | 2FA vorerst für alle aus, auch für die Eltern |
| 23.10.2026 | Familienkalender mit wiederkehrenden Terminen (Pflicht) und Outlook-Import |
| 23.10.2026 | README und Doku auf Deutsch (Ausnahme von der sonst englischen Projektsprache) |
| 23.10.2026 | Für den PC reicht die installierbare Web-App; keine eigene .exe |
| 24.10.2026 | KI-Zusatz über die Anthropic-API, bezahlt aus dem API-Guthaben; alle vier KI-Funktionen |
| 24.10.2026 | Anwesenheit geht nie an die KI (`include_attendance = false`) |
| 24.10.2026 | Schlüssel nur im worker; eigener Workspace „schulblick“ mit 20 $ Limit, zusätzlich max. 150 Aufrufe/Tag |
| 24.10.2026 | Morgenübersicht eingeklappt beim Aufruf, als Stichpunkte statt Fließtext |
| 02.11.2026 | Hintergrund hell/dunkel umschaltbar, pro Gerät |
| 02.11.2026 | Nachhilfe-Zugang für Melissa: Boje und Lotte; Hausaufgaben, Arbeiten, Stundenpläne, Kalender inkl. Familienterminen dieser Kinder; darf eigene Termine eintragen |

## 8. Verlauf

| Commit | Datum | Inhalt |
|---|---|---|
| `f39b7de` | 23.10. | Erste Version: Abrufdienst, Parser, Web-App mit Rollen, Deploy-Paket |
| `951b941` | 23.10. | README mit Screenshots |
| `a8790f1` | 23.10. | README auf Deutsch |
| `5caac3d` | 23.10. | Familienkalender mit Rechten pro Person und ICS-Abo |
| `8818352` | 23.10. | `update.sh` wartet bis zu 60 s auf die Web-App |
| `792be61` | 23.10. | Wiederkehrende Familientermine |
| `f43f530` | 23.10. | Outlook-Import: Datei und verbundene Kalender |
| `54f4bef` | 24.10. | KI-Zusatz: Kurzfassung, Aufgaben, Zuordnung, Morgenübersicht |
| `a8d0602` | 24.10. | Morgenübersicht über erzwungenen Werkzeugaufruf (vorher leere Antworten) |
| `47b0176` | 24.10. | Morgenübersicht eingeklappt, Stichpunkte pro Kind |
| `7720d2d` | 24.10. | README und Anleitung zur Morgenübersicht |

Erster erfolgreicher Abruf am 23.10.: alle fünf Bereiche OK, 3 Kinder erkannt, 34 Unterrichtseinträge,
164 Termine, 3 Nachrichten.

## 9. Gelernt beim Einrichten

- Ubuntu 24.04 hat nginx 1.24: `http2 on;` gibt es dort nicht, stattdessen `listen 443 ssl http2;`.
  Eine kaputte vHost-Datei gefährdet auch Tangosummer, weil beide am selben nginx hängen.
- Container laufen als uid 10001: `config/` braucht `755`, `family.toml` `644`.
- Portal-Benutzername ist groß-/kleinschreibungsempfindlich: klein schreiben, ohne Schulnummer.
- SQLite war anfangs nicht thread-sicher eingebunden; behoben.
- Bei abgelehnten Portal-Zugangsdaten hört der worker auf (Schutz vor Kontosperre), bis sich die Secret-Datei ändert.
- Docker Compose startet nicht, wenn `secrets/anthropic_key` fehlt; `update.sh` legt sie leer an.
- Für `family` muss ein expliziter A-Record auf `212.227.82.248` zeigen, sonst greift der Wildcard-Eintrag.
- Windows-Zeilenenden in Skripten vor dem Ausführen entfernen: `sed -i 's/\r$//' deploy/*.sh`.
- Die Morgenübersicht als freie Textantwort kam leer zurück; erzwungener Werkzeugaufruf löst das.

## 10. Offene Punkte

1. **API-Schlüssel erneuern.** Der Anthropic-Schlüssel wurde am 24.10. einmal im Chat eingefügt. Sicherer ist ein
   neuer Schlüssel im Workspace „schulblick“, eingespielt mit `deploy/ki-schluessel.sh`, und den alten widerrufen.
2. **Server-Stand prüfen.** Ob die letzte `ai.py` (eingeklappte Stichpunkt-Übersicht, `47b0176`) auf dem Server
   läuft, ist nicht bestätigt.
3. **Ordner `_to_delete`** im Projekt enthält nur Übertragungsdateien und kann gelöscht werden.
4. **Android-App für Bens Freunde** (ohne Familienteil, Android Studio, Play Store). Offene Fragen:
   - Verteilung: öffentlich im Play Store oder erst per Testlink?
   - Umfang Version 1: Vertretungsplan, Hausaufgaben, Arbeiten, Stundenplan, Nachrichten, eigener Kalender mit
     Wiederholungen, Benachrichtigungen?
   - Darf Ben mit seinem Schülerkonto helfen, damit die Schülerseiten ausgelesen werden können?

   Rahmen: alles läuft auf dem Handy (eigenes Schülerkonto, kein Server dazwischen), Neuschreiben in Kotlin;
   Play Store verlangt bei neuen Privatkonten 14 Tage geschlossenen Test mit 12 Testern, 25 $ Entwicklerkonto,
   Datenschutzerklärung, Altersfreigabe (Vorschlag 13+), Name klar als inoffiziell. Lanis-Mobile gibt es bereits;
   Schulblick müsste sich durch „Die nächsten Tage“, eigenen Kalender, ICS-Import und Oberfläche abheben.
5. **Ausblick aus dem README**: Push-Benachrichtigung bei Ausfall am nächsten Tag oder neuen Nachrichten;
   Morgenübersicht zusätzlich per Push oder Mail.
