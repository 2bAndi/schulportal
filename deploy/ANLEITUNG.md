# Schulblick auf dem Tangoserver einrichten

Ziel: `https://family.verstehst.de`, Server IONOS Ubuntu 24.04 (212.227.82.248), Benutzer `hidance`.
Der Schulblick läuft als eigener Docker-Compose-Stack in `/opt/schulblick`, getrennt von `/opt/wordpress`.
Der nginx auf dem Host reicht `family.verstehst.de` an `127.0.0.1:8130` weiter, genau wie beim WordPress-Container.

## 0. DNS

Beim DNS-Anbieter von verstehst.de einen A-Record anlegen:

| Name | Typ | Wert |
|---|---|---|
| `family` | A | `212.227.82.248` |

Prüfen (dauert je nach TTL ein paar Minuten): `nslookup family.verstehst.de`

## 1. Projekt auf den Server kopieren

Auf dem Windows-Rechner (PowerShell), aus dem Ordner, in dem `schulblick` liegt:

```powershell
scp -r schulblick hidance@212.227.82.248:/tmp/
```

Auf dem Server:

```bash
sudo mv /tmp/schulblick /opt/schulblick
sudo chown -R hidance:hidance /opt/schulblick
cd /opt/schulblick
sed -i 's/\r$//' deploy/*.sh      # nur nötig, falls Windows-Zeilenenden hineingeraten sind
```

## 2. Einrichtung starten

```bash
cd /opt/schulblick
sudo bash deploy/setup.sh
```

Das Skript
1. prüft, ob `family.verstehst.de` schon auf den Server zeigt,
2. fragt Benutzername und Passwort des **Elternkontos im Schulportal** ab und legt sie als Docker-Secret ab
   (`secrets/sph_credentials`, nur für den Abrufdienst lesbar),
3. baut und startet die zwei Container `web` und `worker`,
4. testet die Anmeldung am Schulportal,
5. holt ein Let's-Encrypt-Zertifikat und richtet den nginx-vHost ein,
6. zeigt die Befehle für die Passwörter.

Benutzername im Schulportal: **klein geschrieben** und **ohne** Schulnummer, z. B. `andreas.broicher`
(die Schulnummer 6055 steht in `config/family.toml`). Mit großem Anfangsbuchstaben lehnt das Portal die Anmeldung ab.

Alle `docker compose`-Befehle brauchen `sudo` (oder vorher `sudo -s`).

## 3. Passwörter und zweiter Faktor

```bash
cd /opt/schulblick
docker compose run --rm web schulblick set-password papa
docker compose run --rm web schulblick set-password mama
docker compose run --rm web schulblick set-password ben
docker compose run --rm web schulblick set-password boje
docker compose run --rm web schulblick set-password lotte

# optional, nur wenn in config/family.toml require_totp_for_parents = true:
# docker compose run --rm web schulblick totp-setup papa

docker compose run --rm web schulblick users             # Kontrolle
```

Passwörter brauchen mindestens 8 Zeichen. Alle melden sich mit Name und Passwort an (Zwei-Faktor ist abschaltbar, derzeit aus). Jedes Gerät bleibt 90 Tage angemeldet.

## 4. Auf den Handys

`https://family.verstehst.de` öffnen, anmelden, dann „Zum Startbildschirm hinzufügen“ (iPhone: Teilen-Menü,
Android: Browser-Menü). Danach startet Schulblick wie eine App.

## Betrieb

| Was | Befehl |
|---|---|
| Logs des Abrufs | `docker compose logs -f worker` |
| Sofort abrufen | `docker compose run --rm worker schulblick sync` |
| Login testen | `docker compose run --rm worker schulblick check-login` |
| Neue Version einspielen | Dateien nach `/opt/schulblick` kopieren, dann `bash deploy/update.sh` |
| Schulportal-Passwort geändert | `secrets/sph_credentials` anpassen, dann `docker compose restart worker` |

**Abrufzeiten:** Schultage 06:15–16:45 alle 30 Minuten, abends 19:00 und 21:00 vor Schultagen,
am Wochenende 08:05 und 18:05; Kalender und Anwesenheit täglich 05:30, Stundenpläne sonntags.

**Wenn das Schulportal das Passwort ablehnt**, versucht der Abrufdienst es nicht weiter (sonst droht eine
Kontosperre). Die Eltern-Ansicht zeigt dann oben einen roten Hinweis. Nach dem Korrigieren der Secret-Datei
läuft er automatisch wieder an.

**Sicherung:** Die Datenbank wird jede Nacht um 03:30 in das Docker-Volume gesichert (7 Tage).
Sie enthält nur Daten, die sich jederzeit neu aus dem Schulportal holen lassen, plus die Konten.

## Einstellungen (`config/family.toml`)

* `display_name` der Kinder, z. B. „Lotte“ für Carlotta
* `kids_see_attendance` – Kinder sehen ihre eigene Anwesenheit
* `fetch_message_bodies` – Nachrichteninhalte laden (markiert sie im Portal als gelesen)
* `parent_only_keywords` – Termine mit diesen Wörtern sehen nur die Eltern

Nach Änderungen: `docker compose restart`
