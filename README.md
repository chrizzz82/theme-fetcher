# Theme Fetcher – Alpha Version

Theme Fetcher ist ein leichtgewichtiger Container für **ZimaOS / CasaOS / Docker**, der automatisch passende Titelmelodien (`theme.mp3`) für Filme, Serien und Sammlungen herunterlädt.

Unterstützt werden:
- **Plex Media Server**
- **Jellyfin** (inkl. BoxSets / Sammlungen)
- **Emby Media Server**
- **Lokaler Ordner-Scan** (für Kodi, Infuse, Zidoo oder dateibasierte Sammlungen)

---

## 🚀 Schnellstart

### 1. Dateien auf ZimaOS / deinen Server kopieren
Erstelle einen Ordner für die App (z. B. unter `/DATA/AppData/theme-fetcher-build`):
```bash
mkdir -p /DATA/AppData/theme-fetcher-build
```
Kopiere `Dockerfile`, `docker-compose.yml` und `server.py` dorthin.

### 2. Konfiguration anpassen (`docker-compose.yml`)
Passe die IPs und API-Keys der Medienserver an, die du im Einsatz hast. Du musst nicht alle aktivieren; ungenutzte Server können leer bleiben.
* Für **Emby**: Trage `EMBY_URL` und `EMBY_API_KEY` ein.
* Für **Kodi / Infuse / Lokale Ordner**: Setze `LOCAL_SCAN_ENABLED=true`.

### 3. Container bauen & starten
Im Ordner ausführen:
```bash
docker compose up -d --build
```

### 4. Web-Oberfläche öffnen
Rufe im Browser die Weboberfläche auf:
```text
http://<DEINE-SERVER-IP>:8080
```

---

## 🎯 Funktionen der Web-Oberfläche
* **Übersicht & Statistik:** Zeigt vorhandene vs. fehlende Themes an.
* **Filter:** Nach Quelle (Jellyfin, Plex, Emby, Lokal), Medientyp (Film, Serie, Collection) und Status filtern.
* **Audio-Player:** Vorhandene `theme.mp3` direkt im Browser vorhören.
* **Neu laden / Überschreiben:** Bei unpassendem Song einfach einen alternativen YouTube-/SoundCloud-Link oder Suchbegriff eingeben und per Klick ersetzen. Der alte Track wird automatisch auf die Blacklist gesetzt.
* **Löschen (✕):** Entfernt die `theme.mp3` und blockiert den fehlerhaften Track dauerhaft.
