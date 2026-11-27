# 🎵 Theme Fetcher

> **Autonomous background theme music downloader and web preview manager for Jellyfin, Plex, and local media servers.**

---

## 📖 Overview

Both **Jellyfin** and **Plex** feature background audio playback while browsing movies, TV shows, and box sets. However, existing plugins often fail on international titles, bundled collections, or pull incorrect audio files without verification.

**Theme Fetcher** is a lightweight, self-hosted Docker companion service that automatically identifies missing theme songs, verifies official scores via **MusicBrainz** and **Wikidata**, allows instant **in-browser streaming previews**, and downloads studio-mastered `theme.mp3` files directly into your media folders.

---

## ✨ Features

- 🎯 **Automated Soundtrack Identification:** Cross-references media items against MusicBrainz and Wikidata to pinpoint exact composers and official track titles before querying audio sources.
- 🎧 **In-Browser Audio Preview:** Built-in web audio player with instant stream playback, scrubbing sliders, track duration, and a **`⏩ Mid` (50%)** quick-seek button to verify the musical hook.
- 🎚️ **Audio Mastering Pipeline:** Automatically normalizes audio through `ffmpeg` with a 1.5-second fade-in and a 3.0-second fade-out curve for seamless background playback.
- 📺 **Strict Folder Architecture:** Accurately isolates television show roots—preventing unwanted scans of season directories (`Season 01`, `Staffel 1`), individual episodes, or metadata/fanart folders (`extrafanart/`).
- 🌐 **Modern Dark-Mode WebUI:** Slate dashboard featuring responsive media cards, cover art caching, real-time download queues, and dynamic bilingual localization (**🇬🇧 EN / 🇩🇪 DE**).
- 🔗 **Direct Jellyfin & Plex Integration:** Pulls library metadata and posters directly over official REST APIs or performs autonomous scans across local host mounts.
- 🛡️ **Blacklist Management:** Delete unsatisfactory themes with a single click to ban their source URLs and trigger an automatic fallback search.

---

## 📸 Screenshots

| Media Dashboard | Theme Selector & Audio Player |
| :---: | :---: |
| *(Insert your dashboard screenshot here)* | *(Insert your audio modal preview screenshot here)* |

---

## 🚀 Quick Start

### 1. Docker Compose / Dockge (Recommended)

Create a `docker-compose.yml` file (or paste into Dockge, Portainer, or OMV):

```yaml
version: "3.8"

services:
  theme-fetcher:
    image: ghcr.io/chrizzz82/theme-fetcher:latest
    container_name: theme-fetcher
    restart: unless-stopped
    ports:
      - "8088:8088"
    environment:
      - PUID=1000
      - PGID=100
      - TZ=Europe/Berlin
      - LOCAL_MEDIA_DIR=/media
      - LOCAL_SCAN_ENABLED=true
    volumes:
      # Map your host media root (contains Movies, TV Shows, etc.)
      - /srv/dev-disk-by-uuid-YOUR-DISK/Media:/media
      # Application data directory (config, state, cache)
      - ./data:/data
    pull_policy: always
```

Start the container:
```bash
docker compose up -d
```

Access the web interface at `http://<your-server-ip>:8088`.

---

## ⚙️ Configuration & Settings

Navigate to **⚙️ Settings** in the top right corner of the web interface to configure your integrations:

### Jellyfin
* **Server URL:** Your Jellyfin address (e.g. `http://jellyfin:8096`)
* **API Key:** Created under *Jellyfin Dashboard ➔ Advanced ➔ API Keys*.

### Plex Media Server
* **Server URL:** Your Plex address (e.g. `http://plex:32400`)
* **Token:** Your `X-Plex-Token`.

### Local Filesystem Scanner
* Enable or disable local storage indexing.
* Specify path mounts (e.g. `/media` or comma-separated `/media/Movies,/media/TV`).

### Audio Preferences
* **Minimum Duration:** 25 seconds *(filters out short bumpers)*.
* **Maximum Duration:** 270 seconds *(filters out full OST albums)*.
* **Audio Fade Curves:** Enable or disable automated fade-in & fade-out mastering.

---

## 📂 File Structure Conventions

Theme Fetcher places audio files according to standard media center conventions:

```text
/media/
├── Movies/
│   └── Inception (2010)/
│       ├── Inception (2010).mkv
│       └── theme.mp3               <-- Placed here
│
└── TV Shows/
    └── Breaking Bad/
        ├── theme.mp3               <-- Placed in show root
        ├── Season 01/
        │   └── S01E01.mkv
        └── Season 02/
            └── S02E01.mkv
```

Once downloaded, Jellyfin and Plex will detect and stream the `theme.mp3` file automatically when navigating the show or movie page.

---

## 🛠️ Technology Stack

- **Backend:** Python 3.11+, FastAPI, Uvicorn
- **Audio Extraction:** yt-dlp
- **Audio Processing:** ffmpeg (audio normalization & curve filtering)
- **Metadata Discovery:** MusicBrainz API, Wikidata API
- **Frontend:** Vanilla HTML5, CSS3 (Modern Slate Dark Theme), Audio Web API
- **Deployment:** Docker, GitHub Container Registry (`ghcr.io`)

---

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
