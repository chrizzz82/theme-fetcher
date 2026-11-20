import os
import json
import re
import time
import shutil
import threading
import subprocess
import requests
from pathlib import Path
from typing import List, Optional, Tuple, Dict
from fastapi import FastAPI, Form, HTTPException, Response
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from pydantic import BaseModel

app = FastAPI(title="Theme Fetcher & Media Enricher")

DATA_DIR = Path("/data")
COLLECTIONS_DIR = DATA_DIR / "collections"
LOCAL_THEMES_DIR = DATA_DIR / "local_themes"
CONFIG_FILE = DATA_DIR / "config.json"
STATE_FILE = DATA_DIR / "state.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
COLLECTIONS_DIR.mkdir(parents=True, exist_ok=True)
LOCAL_THEMES_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "local_media_paths": os.getenv("LOCAL_MEDIA_DIR", "/media"),
    "local_scan_enabled": os.getenv("LOCAL_SCAN_ENABLED", "true").lower() in ("true", "1", "yes"),
    "jellyfin_url": os.getenv("JELLYFIN_URL", "").rstrip("/"),
    "jellyfin_api_key": os.getenv("JELLYFIN_API_KEY", ""),
    "jf_metadata_dir": os.getenv("JF_METADATA_DIR", "/jellyfin-metadata"),
    "plex_url": os.getenv("PLEX_URL", "").rstrip("/"),
    "plex_token": os.getenv("PLEX_TOKEN", ""),
    "min_duration_sec": int(os.getenv("MIN_DURATION_SEC", "25")),
    "max_duration_sec": int(os.getenv("MAX_DURATION_SEC", "270")),
    "scan_interval_hours": int(os.getenv("SCAN_INTERVAL_HOURS", "12")),
    "audio_fade": True,
    "embed_cover": True,
    "auto_download": False
}

EXCLUDE_KEYWORDS = [
    "scene", "szene", "clip", "movieclip", "movie clip", "dialog", "dialogue",
    "teaser", "trailer", "ausschnitt", "interview", "making of", "featurette",
    "reaction", "reaktion", "review", "kritik", "talk", "hörspiel", "ganzer film",
    "full movie", "parody", "parodie", "gameplay", "walkthrough", "1 hour",
    "10 hours", "loop", "tutorial", "piano", "klavier", "guitar", "gitarre",
    "karaoke", "bass boosted", "nightcore", "slowed", "reverb", "8d audio",
    "extended", "compilation", "suite", "blooper", "deleted scene", "fight scene",
    "ending scene", "death scene"
]

THEME_REGEX = re.compile(r".*themes?.*\.(mp3|m4a|flac|ogg|wav|aac|opus)$", re.IGNORECASE)

state_lock = threading.Lock()
stop_requested = False

scan_progress = {
    "is_running": False,
    "action_type": "",
    "current_step": "Bereit",
    "current_item_id": "",
    "current_item_title": "",
    "item_status": {},  # item_id -> {"status": str, "percent": int, "step": str}
    "processed": 0,
    "total": 0,
    "percent": 0,
    "new_downloaded": 0,
    "failed": 0,
    "queue": [],
    "recent_finished": []
}


def clean_url_input(val: str) -> str:
    val = (val or "").strip().rstrip("/")
    if not val:
        return ""
    if not val.startswith("http://") and not val.startswith("https://"):
        return f"http://{val}"
    return val


def load_config() -> dict:
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists():
        try:
            stored = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            cfg.update(stored)
        except Exception as e:
            print(f"[Config Warnung] {e}")
    return cfg


def save_config(cfg: dict):
    with state_lock:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"items": {}, "blacklist": {}, "folders": []}


def save_state(state: dict):
    with state_lock:
        STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def slugify(text: str) -> str:
    clean = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    return re.sub(r"[-\s]+", "_", clean) or "item"


def normalize_tab(tab_name: str) -> str:
    t = (tab_name or "").lower().strip()
    if "seri" in t or "show" in t:
        return "serien"
    if "col" in t or "box" in t:
        return "collections"
    if "ordn" in t or "fold" in t:
        return "ordner"
    return "filme"


def find_theme_file_in_dir(d: Path) -> Optional[Path]:
    """Findet tolerant jede Theme-Audiodatei im Ordner oder in Staffel-Unterordnern"""
    if not d.exists() or not d.is_dir():
        return None
    try:
        # 1. Direkte Suche im Hauptordner
        for f in d.iterdir():
            if f.is_file() and THEME_REGEX.match(f.name):
                return f
        # 2. Suche in Season/Staffel-Unterordnern
        for sub in d.iterdir():
            if sub.is_dir() and re.search(r"season|staffel|\bs\d+", sub.name, re.IGNORECASE):
                for sf in sub.iterdir():
                    if sf.is_file() and THEME_REGEX.match(sf.name):
                        return sf
    except Exception:
        pass
    return None


def resolve_media_path(raw_path_str: str) -> Path:
    raw_str = raw_path_str.replace("\\", "/")
    p = Path(raw_str)
    if p.exists():
        return p

    parts = [part for part in p.parts if part not in ("/", "\\", "")]
    if not parts:
        return Path("/media")

    media_base = Path("/media")
    if not media_base.exists():
        return p

    cand1 = media_base.joinpath(*parts)
    if cand1.exists() or cand1.parent.exists():
        return cand1

    top_name = parts[0].lower()
    try:
        for entry in media_base.iterdir():
            if entry.is_dir():
                ename = entry.name.lower()
                if ename == top_name or top_name in ename or ename in top_name:
                    cand = entry.joinpath(*parts[1:])
                    if cand.exists() or cand.parent.exists():
                        return cand
    except Exception:
        pass

    if len(parts) > 1:
        cand2 = media_base.joinpath(*parts[1:])
        if cand2.exists() or cand2.parent.exists():
            return cand2

    target_name = parts[-1].lower()
    try:
        for root, dirs, _ in os.walk(media_base):
            for d in dirs:
                if d.lower() == target_name:
                    return Path(root) / d
            if len(Path(root).relative_to(media_base).parts) >= 3:
                dirs.clear()
    except Exception:
        pass

    return cand1


def check_item_theme(it: dict, existing: Optional[dict] = None) -> Tuple[bool, str, Optional[Path]]:
    """Tiefensuche: Findet theme.mp3 auch bei abweichenden Mount-Pfaden und korrigiert it['dir'] dauerhaft"""
    existing = existing or {}
    candidates = []

    if it.get("dir"):
        candidates.append(Path(it["dir"]))
        candidates.append(resolve_media_path(it["dir"]))

    if existing.get("dir") and existing.get("dir") != it.get("dir"):
        candidates.append(Path(existing["dir"]))
        candidates.append(resolve_media_path(existing["dir"]))

    raw_p = it.get("raw_path") or existing.get("raw_path")
    if raw_p:
        res = resolve_media_path(raw_p)
        candidates.append(res if res.is_dir() else res.parent)

    item_id = it.get("id") or existing.get("id")
    if item_id:
        candidates.append(LOCAL_THEMES_DIR / item_id)

    seen = set()
    for d in candidates:
        d_str = str(d)
        if d_str in seen:
            continue
        seen.add(d_str)

        theme_file = find_theme_file_in_dir(d)
        if theme_file:
            if theme_file.name.lower() != "theme.mp3":
                standard_mp3 = d / "theme.mp3"
                if not standard_mp3.exists():
                    try:
                        shutil.copy2(theme_file, standard_mp3)
                        theme_file = standard_mp3
                    except Exception:
                        pass
            return True, d_str, theme_file

    # Robuste Rekursive Suche im gesamten /media Mount nach passendem Ordnernamen (z. B. Die Dinos)
    title_raw = it.get("title", "").strip().lower()
    title_slug = slugify(title_raw)
    media_base = Path("/media")
    if media_base.exists() and len(title_slug) >= 2:
        try:
            for root, dirs, _ in os.walk(media_base):
                for d in dirs:
                    d_lower = d.lower()
                    d_slug = slugify(d)
                    if d_lower == title_raw or d_slug == title_slug or (len(title_slug) > 4 and (title_slug in d_slug or d_slug in title_slug)):
                        cand_dir = Path(root) / d
                        theme_file = find_theme_file_in_dir(cand_dir)
                        if theme_file:
                            it["dir"] = str(cand_dir)
                            return True, str(cand_dir), theme_file
                        # Falls Ordner existiert, aber noch kein Theme hat: Pfad trotzdem auf realen Mount umbiegen!
                        it["dir"] = str(cand_dir)
                if len(Path(root).relative_to(media_base).parts) >= 4:
                    dirs.clear()
        except Exception:
            pass

    default_dir = str(it.get("dir") or (candidates[0] if candidates else (str(LOCAL_THEMES_DIR / item_id) if item_id else "/media")))
    return False, default_dir, None


def find_jellyfin_boxset_dir(title: str, jf_meta_base: str) -> Optional[Path]:
    base = Path(jf_meta_base)
    if not base.exists():
        return None
    target_slug = slugify(title)
    try:
        for p in base.rglob("*"):
            if p.is_dir() and slugify(p.name) == target_slug:
                if any(x in str(p).lower() for x in ("boxset", "collection", "metadata")):
                    return p
    except Exception:
        pass
    return None


def sync_collection_theme(item: dict, mp3_path: Path, jf_meta_base: str):
    if item.get("type") != "Collection" or not mp3_path or not mp3_path.exists():
        return
    jf_dir = find_jellyfin_boxset_dir(item["title"], jf_meta_base)
    if jf_dir:
        try:
            shutil.copy2(mp3_path, jf_dir / "theme.mp3")
        except Exception as e:
            print(f"[Collection Sync Warnung] {e}")


def scan_local_folders(paths_str: str, filter_type: Optional[str] = None, exclude_slugs: set = None) -> tuple[List[dict], List[str]]:
    results = []
    scanned_dirs = []
    if not paths_str:
        return results, scanned_dirs

    exclude_slugs = exclude_slugs or set()
    paths = [p.strip() for p in paths_str.replace(";", ",").split(",") if p.strip()]
    video_exts = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".iso"}

    for base_str in paths:
        base = Path(base_str)
        if not base.exists() or not base.is_dir():
            scanned_dirs.append(f"❌ {base_str} (Pfad existiert nicht im Container)")
            continue

        scanned_dirs.append(f"📁 Lokaler Mount: {base_str}")
        try:
            for root, dirs, files in os.walk(base):
                global stop_requested
                if stop_requested:
                    break

                rpath = Path(root)
                if rpath.name.startswith(".") or rpath.name.lower() in ("@eadir", "$recycle.bin", "system volume information"):
                    continue

                try:
                    rel_parts = rpath.relative_to(base).parts
                except ValueError:
                    rel_parts = ()
                if len(rel_parts) > 3:
                    continue

                has_video = any(Path(f).suffix.lower() in video_exts for f in files)
                has_theme = bool(find_theme_file_in_dir(rpath))
                has_seasons = any(re.search(r"season|staffel|specials", d, re.IGNORECASE) for d in dirs)

                if has_video or has_theme or has_seasons:
                    if re.search(r"^(season|staffel|specials|\bs\d+)", rpath.name, re.IGNORECASE):
                        continue

                    name = rpath.name
                    year = None
                    m = re.search(r"^(.*?)\s*[\(\[](\d{4})[\)\]]", name)
                    if m:
                        title = m.group(1).strip()
                        year = m.group(2)
                    else:
                        title = re.sub(r"[\._]", " ", name).strip()

                    itype = "Serie" if has_seasons or any("s01" in f.lower() or "staffel" in f.lower() for f in files) else "Film"
                    if filter_type and itype != filter_type:
                        continue

                    # TITEL- & JAHRES-BASIERTE DEDUPLIZIERUNG GEGEN JELLYFIN/PLEX
                    item_slug = f"{slugify(title)}_{year or ''}"
                    alt_slug = slugify(title)
                    if item_slug in exclude_slugs or alt_slug in exclude_slugs or str(rpath) in exclude_slugs:
                        continue

                    item_id = f"local_{abs(hash(str(rpath)))}"

                    results.append({
                        "id": item_id,
                        "source": "Lokal",
                        "type": itype,
                        "title": title,
                        "original_title": "",
                        "year": year,
                        "dir": str(rpath)
                    })
        except Exception as e:
            print(f"[Lokaler Scan Fehler bei {base_str}] {e}")

    return results, scanned_dirs


def get_jellyfin_headers(api_key: str) -> dict:
    clean_key = api_key.strip()
    return {
        "X-Emby-Token": clean_key,
        "X-MediaBrowser-Token": clean_key,
        "Authorization": f'MediaBrowser Token="{clean_key}", Client="ThemeFetcher", Device="ZimaOS", DeviceId="theme-fetcher-zimaos", Version="1.2.0"',
        "Accept": "application/json"
    }


def test_jellyfin_connection(url: str, api_key: str) -> dict:
    url = clean_url_input(url)
    api_key = api_key.strip()
    if not url:
        return {"ok": False, "msg": "Bitte Jellyfin IP und Port eingeben (z. B. 192.168.178.50:8096)"}
    if not api_key:
        return {"ok": False, "msg": "Bitte API-Key eintragen"}

    headers = get_jellyfin_headers(api_key)
    try:
        sys_resp = requests.get(f"{url}/System/Info", headers=headers, timeout=6)
        server_name = "Jellyfin"
        version = ""
        if sys_resp.status_code == 200:
            s_data = sys_resp.json()
            server_name = s_data.get("ServerName", "Jellyfin")
            version = s_data.get("Version", "")

        users_resp = requests.get(f"{url}/Users", headers=headers, params={"api_key": api_key}, timeout=6)
        if users_resp.status_code in (401, 403):
            return {"ok": False, "msg": f"URL erreichbar, aber API-Key ungültig (HTTP {users_resp.status_code})"}
        users_resp.raise_for_status()
        users = users_resp.json()

        it_resp = requests.get(
            f"{url}/Items",
            headers=headers,
            params={"IncludeItemTypes": "Movie,Series,BoxSet", "Recursive": "true", "Limit": "1", "api_key": api_key},
            timeout=6
        )
        item_count = it_resp.json().get("TotalRecordCount", 0) if it_resp.status_code == 200 else 0

        info_txt = f"Verbunden mit '{server_name}' (v{version}) • {len(users)} Benutzer • {item_count} Medien gefunden!"
        return {"ok": True, "msg": info_txt}
    except requests.exceptions.ConnectTimeout:
        return {"ok": False, "msg": "Zeitüberschreitung (Timeout). Läuft Jellyfin unter dieser IP und diesem Port?"}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "msg": "Verbindung abgelehnt. Bitte IP und Port prüfen (z. B. 192.168.178.50:8096)!"}
    except Exception as e:
        return {"ok": False, "msg": f"Fehler: {str(e)}"}


def test_plex_connection(url: str, token: str) -> dict:
    url = clean_url_input(url)
    token = token.strip()
    if not url:
        return {"ok": False, "msg": "Bitte Plex IP und Port eintragen (z. B. 192.168.178.50:32400)"}
    if not token:
        return {"ok": False, "msg": "Bitte Plex Token eintragen"}

    headers = {"X-Plex-Token": token, "Accept": "application/json"}
    try:
        resp = requests.get(f"{url}/", headers=headers, timeout=6)
        if resp.status_code in (401, 403):
            return {"ok": False, "msg": "URL erreichbar, aber Plex Token ist ungültig!"}
        resp.raise_for_status()
        data = resp.json().get("MediaContainer", {})
        friendly_name = data.get("friendlyName", "Plex Server")
        version = data.get("version", "")
        return {"ok": True, "msg": f"Verbunden mit '{friendly_name}' (v{version})!"}
    except Exception as e:
        return {"ok": False, "msg": f"Plex Fehler: {str(e)}"}


def test_folder_path(paths_str: str) -> dict:
    paths_str = paths_str.strip()
    if not paths_str:
        return {"ok": False, "msg": "Kein Pfad angegeben"}

    paths = [p.strip() for p in paths_str.replace(";", ",").split(",") if p.strip()]
    details = []

    for p in paths:
        path_obj = Path(p)
        if not path_obj.exists():
            details.append(f"❌ '{p}' existiert nicht im Container")
        elif not path_obj.is_dir():
            details.append(f"❌ '{p}' ist kein Verzeichnis")
        else:
            try:
                subdirs = [d.name for d in path_obj.iterdir() if d.is_dir() and not d.name.startswith(".")]
                files = [f.name for f in path_obj.iterdir() if f.is_file()]
                details.append(f"✓ '{p}': {len(subdirs)} Unterordner, {len(files)} Dateien")
            except Exception as e:
                details.append(f"⚠️ '{p}' Lesefehler: {e}")

    ok = any("✓" in d for d in details)
    return {"ok": ok, "msg": " | ".join(details)}


def fetch_jellyfin_items(url: str, api_key: str, filter_type: Optional[str] = None) -> tuple[List[dict], List[str]]:
    if not url or not api_key:
        return [], []
    headers = get_jellyfin_headers(api_key)
    results = []
    scanned_folders = []
    seen_ids = set()

    type_mapping = {"Film": "Movie", "Serie": "Series", "Collection": "BoxSet"}
    item_types = type_mapping.get(filter_type, "Movie,Series,BoxSet")

    try:
        vf_resp = requests.get(f"{url}/Library/VirtualFolders", headers=headers, params={"api_key": api_key.strip()}, timeout=10)
        if vf_resp.status_code == 200:
            for vf in vf_resp.json():
                name = vf.get("Name", "Unbenannt")
                locations = vf.get("Locations", [])
                loc_txt = ", ".join(locations) if locations else "Standardpfad"
                scanned_folders.append(f"🎬 Jellyfin Mediathek: '{name}' [{loc_txt}]")
    except Exception as e:
        print(f"[Jellyfin VirtualFolders Warnung] {e}")

    try:
        params = {
            "IncludeItemTypes": item_types,
            "Recursive": "true",
            "Fields": "Path,ProductionYear,OriginalTitle,ImageTags,PrimaryImageAspectRatio",
            "EnableTotalRecordCount": "true",
            "api_key": api_key.strip()
        }
        resp = requests.get(f"{url}/Items", headers=headers, params=params, timeout=35)
        resp.raise_for_status()

        for item in resp.json().get("Items", []):
            item_type = item.get("Type")
            title = item.get("Name", "Unbekannt")
            clean_id = item["Id"]
            item_id = f"jf_{clean_id}"

            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)

            has_image = bool(item.get("ImageTags", {}).get("Primary"))

            if item_type == "BoxSet":
                if filter_type and filter_type != "Collection":
                    continue
                col_dir = COLLECTIONS_DIR / f"{item_id}_{slugify(title)}"
                col_dir.mkdir(parents=True, exist_ok=True)
                results.append({
                    "id": item_id,
                    "jf_raw_id": clean_id,
                    "source": "Jellyfin",
                    "type": "Collection",
                    "title": title,
                    "original_title": item.get("OriginalTitle") or "",
                    "year": item.get("ProductionYear"),
                    "dir": str(col_dir),
                    "has_poster": has_image
                })
                continue

            raw_path = item.get("Path")
            if not raw_path:
                continue

            resolved = resolve_media_path(raw_path)
            target_dir = resolved.parent if item_type == "Movie" else resolved
            itype = "Serie" if item_type == "Series" else "Film"

            if filter_type and itype != filter_type:
                continue

            results.append({
                "id": item_id,
                "jf_raw_id": clean_id,
                "source": "Jellyfin",
                "type": itype,
                "title": title,
                "original_title": item.get("OriginalTitle") or "",
                "year": item.get("ProductionYear"),
                "dir": str(target_dir),
                "raw_path": raw_path,
                "has_poster": has_image
            })
    except Exception as e:
        print(f"[Jellyfin Fetch Fehler] {e}")
        scanned_folders.append(f"❌ Jellyfin Fehler beim Einlesen: {e}")

    return results, scanned_folders


def fetch_plex_items(url: str, token: str, filter_type: Optional[str] = None) -> tuple[List[dict], List[str]]:
    if not url or not token:
        return [], []
    headers = {"X-Plex-Token": token, "Accept": "application/json"}
    results = []
    scanned_folders = []
    try:
        sec_resp = requests.get(f"{url}/library/sections", headers=headers, timeout=10)
        sec_resp.raise_for_status()
        data = sec_resp.json().get("MediaContainer", {})

        for directory in data.get("Directory", []):
            sec_id = directory.get("key")
            sec_type = directory.get("type")
            sec_title = directory.get("title", "Mediathek")
            if sec_type not in ("movie", "show"):
                continue

            scanned_folders.append(f"🍿 Plex Mediathek: '{sec_title}' ({sec_type})")
            items_resp = requests.get(f"{url}/library/sections/{sec_id}/all", headers=headers, timeout=25)
            items_data = items_resp.json().get("MediaContainer", {})

            for video in items_data.get("Metadata", []):
                rkey = video.get("ratingKey")
                vtype = "Film" if sec_type == "movie" else "Serie"
                if filter_type and vtype != filter_type:
                    continue

                vtitle = video.get("title", "Unbekannt")
                thumb = video.get("thumb")

                media_parts = video.get("Media", [{}])[0].get("Part", [{}])
                file_path = media_parts[0].get("file") if media_parts else None
                if not file_path:
                    continue

                resolved = resolve_media_path(file_path)
                p = resolved.parent if vtype == "Film" else resolved

                results.append({
                    "id": f"plex_{rkey}",
                    "plex_thumb": thumb,
                    "source": "Plex",
                    "type": vtype,
                    "title": vtitle,
                    "original_title": video.get("originalTitle", ""),
                    "year": video.get("year"),
                    "dir": str(p),
                    "raw_path": file_path,
                    "has_poster": bool(thumb)
                })

            if not filter_type or filter_type == "Collection":
                try:
                    col_resp = requests.get(f"{url}/library/sections/{sec_id}/collections", headers=headers, timeout=15)
                    for col in col_resp.json().get("MediaContainer", {}).get("Metadata", []):
                        rkey = col.get("ratingKey")
                        ctitle = col.get("title", "Collection")
                        cid = f"plex_col_{rkey}"
                        cdir = COLLECTIONS_DIR / f"{cid}_{slugify(ctitle)}"
                        cdir.mkdir(parents=True, exist_ok=True)
                        results.append({
                            "id": cid,
                            "plex_thumb": col.get("thumb"),
                            "source": "Plex",
                            "type": "Collection",
                            "title": ctitle,
                            "original_title": "",
                            "year": col.get("maxYear") or col.get("minYear"),
                            "dir": str(cdir),
                            "has_poster": bool(col.get("thumb"))
                        })
                except Exception:
                    pass

    except Exception as e:
        print(f"[Plex Fehler] {e}")
    return results, scanned_folders


def build_search_queries(item: dict) -> List[str]:
    title = item["title"].strip()
    orig = (item.get("original_title") or "").strip()
    year = item.get("year")
    itype = item["type"]
    queries = []

    if itype == "Serie":
        queries.append(f"ytsearch8:{title} Opening Theme Song Soundtrack")
        queries.append(f"ytsearch6:{title} Titelmelodie Instrumental")
        if orig and orig.lower() != title.lower():
            queries.append(f"ytsearch8:{orig} TV Theme Song Audio")
            queries.append(f"ytsearch6:{orig} Opening Credits Soundtrack")
    elif itype == "Collection":
        queries.append(f"ytsearch8:{title} Soundtrack Main Theme")
        clean_col = re.sub(r"\b(collection|reihe|sammlung|filmreihe)\b", "", title, flags=re.IGNORECASE).strip()
        if clean_col and clean_col.lower() != title.lower():
            queries.append(f"ytsearch8:{clean_col} Main Theme Soundtrack")
        if orig and orig.lower() != title.lower():
            queries.append(f"ytsearch8:{orig} Main Theme Score")
    else:
        if orig and orig.lower() != title.lower():
            queries.append(f"ytsearch8:{orig} {year or ''} Soundtrack Main Title".strip())
            queries.append(f"ytsearch6:{orig} Official Audio Theme")
        queries.append(f"ytsearch8:{title} {year or ''} Soundtrack Main Title".strip())
        queries.append(f"ytsearch6:{title} Titelmusik Instrumental")

    base_name = orig if orig else title
    queries.append(f"scsearch6:{base_name} Main Theme Soundtrack")
    return queries


def find_all_candidates(search_expr: str, blacklist: List[str], min_dur: int, max_dur: int) -> List[dict]:
    """Liefert ALLE passenden Kandidaten einer Suche, um gesperrte Videos automatisch zu überspringen"""
    cmd = [
        "yt-dlp",
        search_expr,
        "--dump-json",
        "--flat-playlist",
        "--no-warnings"
    ]
    candidates = []
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        lines = res.stdout.strip().splitlines()
        for line in lines:
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            vid_id = entry.get("id", "")
            vid_title = entry.get("title", "")
            duration = entry.get("duration") or 0
            url = entry.get("url") or entry.get("webpage_url") or (
                f"https://www.youtube.com/watch?v={vid_id}" if vid_id else ""
            )
            lower_title = vid_title.lower()

            if not url or vid_id in blacklist:
                continue
            if duration and (duration < min_dur or duration > max_dur):
                continue
            if any(bad in lower_title for bad in EXCLUDE_KEYWORDS):
                continue

            candidates.append({"id": vid_id or url, "url": url, "title": vid_title, "duration": duration})
    except Exception:
        pass
    return candidates


def post_process_audio(input_file: Path, output_file: Path, duration: int, fade: bool = True, cover_path: Optional[Path] = None):
    temp_processed = input_file.parent / f"processed_{input_file.name}"
    
    af_filter = []
    if fade:
        af_filter.append("afade=t=in:ss=0:d=1.5")
        if duration and duration > 6:
            out_start = max(1.0, duration - 3.0)
            af_filter.append(f"afade=t=out:st={out_start}:d=3.0")

    af_arg = ",".join(af_filter) if af_filter else "anull"
    cmd = ["ffmpeg", "-y", "-i", str(input_file)]
    
    if cover_path and cover_path.exists():
        cmd.extend(["-i", str(cover_path), "-map", "0:a", "-map", "1:v", "-c:a", "libmp3lame", "-b:a", "192k", "-c:v", "copy", "-id3v2_version", "3", "-metadata:s:v", 'title="Album cover"', "-metadata:s:v", 'comment="Cover (front)"'])
    else:
        cmd.extend(["-c:a", "libmp3lame", "-b:a", "192k"])

    cmd.extend(["-af", af_arg, str(temp_processed)])

    try:
        subprocess.run(cmd, capture_output=True, timeout=30)
        if temp_processed.exists() and temp_processed.stat().st_size > 1024:
            if output_file.exists():
                output_file.unlink()
            temp_processed.rename(output_file)
            input_file.unlink(missing_ok=True)
            return True
    except Exception as e:
        print(f"[FFmpeg Filter Warnung] {e}")

    if input_file.exists():
        if output_file.exists():
            output_file.unlink()
        input_file.rename(output_file)
    return True


def save_poster_to_disk(item: dict) -> bool:
    """TMM-Feature: Lädt das Poster herunter und speichert es als poster.jpg im Medienordner"""
    target_dir = Path(item.get("dir", ""))
    if not target_dir.exists() or not target_dir.is_dir():
        return False

    poster_file = target_dir / "poster.jpg"
    if poster_file.exists():
        return True

    cfg = load_config()
    if item.get("source") == "Jellyfin" and item.get("jf_raw_id") and cfg.get("jellyfin_url"):
        jf_url = cfg["jellyfin_url"]
        raw_id = item["jf_raw_id"]
        api_key = cfg.get("jellyfin_api_key", "").strip()
        headers = get_jellyfin_headers(api_key)
        try:
            r = requests.get(f"{jf_url}/Items/{raw_id}/Images/Primary", headers=headers, timeout=8)
            if r.status_code == 200 and len(r.content) > 1024:
                poster_file.write_bytes(r.content)
                return True
        except Exception:
            pass
    return False


def search_and_download(item: dict, custom_query_or_url: Optional[str] = None, blacklist: List[str] = None, min_dur: int = 25, max_dur: int = 270, jf_meta_base: str = "/jellyfin-metadata", fade: bool = True) -> dict:
    global scan_progress
    item_id = item["id"]
    blacklist = blacklist or []
    target_dir = Path(item["dir"])

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        test_write = target_dir / ".theme_test_write"
        test_write.write_text("ok")
        test_write.unlink()
    except Exception:
        safe_fallback = LOCAL_THEMES_DIR / item_id
        safe_fallback.mkdir(parents=True, exist_ok=True)
        target_dir = safe_fallback
        item["dir"] = str(safe_fallback)

    output_mp3 = target_dir / "theme.mp3"
    temp_template = str(target_dir / "theme_temp.%(ext)s")

    for old_tmp in target_dir.glob("theme_temp.*"):
        old_tmp.unlink(missing_ok=True)

    is_direct_url = custom_query_or_url and (
        custom_query_or_url.startswith("http://") or custom_query_or_url.startswith("https://")
    )

    candidates = []
    if is_direct_url:
        candidates = [{"url": custom_query_or_url, "title": "Manueller Link", "id": custom_query_or_url, "duration": 180}]
    else:
        scan_progress["item_status"][item_id] = {"status": "searching", "percent": 20, "step": "Suche Soundtrack..."}
        search_list = (
            [f"ytsearch8:{custom_query_or_url}", f"scsearch6:{custom_query_or_url}"]
            if custom_query_or_url
            else build_search_queries(item)
        )
        for expr in search_list:
            cands = find_all_candidates(expr, blacklist, min_dur, max_dur)
            if cands:
                candidates.extend(cands)
                if len(candidates) >= 5:
                    break

    if not candidates:
        scan_progress["item_status"][item_id] = {"status": "failed", "percent": 100, "step": "Kein Soundtrack gefunden"}
        return {"ok": False, "msg": "Keine reine Titelmusik gefunden (nur Trailer/Szenen oder keine Treffer)"}

    # MULTI-KANDIDATEN-FALLBACK: Wenn Video gesperrt/gelöscht ist -> Nächstes probieren!
    last_err = ""
    for idx, chosen_video in enumerate(candidates, 1):
        scan_progress["item_status"][item_id] = {
            "status": "downloading",
            "percent": int(30 + (idx / len(candidates)) * 40),
            "step": f"Lade ({idx}/{len(candidates)}): {chosen_video['title'][:35]}..."
        }

        dl_cmd = [
            "yt-dlp",
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "192K",
            "--no-playlist",
            "-o", temp_template,
            chosen_video["url"]
        ]
        try:
            res = subprocess.run(dl_cmd, capture_output=True, text=True, timeout=90)
            if res.returncode != 0:
                err_line = res.stderr.strip().splitlines()[-1] if res.stderr else "Fehler"
                last_err = err_line[:80]
                blacklist.append(chosen_video["id"])
                continue

            downloaded = target_dir / "theme_temp.mp3"
            if downloaded.exists() and downloaded.stat().st_size > 1024:
                scan_progress["item_status"][item_id] = {"status": "processing", "percent": 85, "step": "Schneide Audio (Fade-In/Out)..."}

                # TMM: Poster sicherstellen und in MP3 einbetten
                save_poster_to_disk(item)
                cover_file = target_dir / "poster.jpg" if (target_dir / "poster.jpg").exists() else None

                post_process_audio(downloaded, output_mp3, chosen_video.get("duration", 180), fade=fade, cover_path=cover_file)
                sync_collection_theme(item, output_mp3, jf_meta_base)

                scan_progress["item_status"][item_id] = {"status": "done", "percent": 100, "step": "Erfolgreich geladen!"}
                return {
                    "ok": True,
                    "video_id": chosen_video["id"],
                    "video_title": chosen_video["title"]
                }
        except Exception as e:
            last_err = str(e)
            continue

    scan_progress["item_status"][item_id] = {"status": "failed", "percent": 100, "step": f"Fehler: {last_err[:40]}"}
    return {"ok": False, "msg": f"Alle Treffer fehlgeschlagen ({last_err[:60]})"}


def run_scan(scope: str = "all"):
    global scan_progress, stop_requested
    if scan_progress["is_running"]:
        return

    stop_requested = False
    scan_progress["is_running"] = True
    scan_progress["action_type"] = "scan"
    scope_name = "alle Medien" if scope == "all" else (f"{scope}e" if scope == "Film" else f"{scope}n")
    scan_progress["current_step"] = f"Lese {scope_name} ein..."
    scan_progress["processed"] = 0
    scan_progress["total"] = 0
    scan_progress["percent"] = 0
    scan_progress["new_downloaded"] = 0
    scan_progress["failed"] = 0

    try:
        cfg = load_config()
        state = load_state()

        filter_type = None if scope == "all" else scope

        raw_items = []
        all_scanned_folders = []
        jf_slugs = set()

        if cfg.get("jellyfin_url") and cfg.get("jellyfin_api_key"):
            scan_progress["current_step"] = f"Lese Jellyfin ({scope_name}) ein..."
            jf_items, jf_folders = fetch_jellyfin_items(cfg.get("jellyfin_url", ""), cfg.get("jellyfin_api_key", ""), filter_type=filter_type)
            raw_items.extend(jf_items)
            all_scanned_folders.extend(jf_folders)
            for it in jf_items:
                jf_slugs.add(f"{slugify(it['title'])}_{it.get('year') or ''}")
                jf_slugs.add(slugify(it["title"]))
                if it.get("dir"):
                    jf_slugs.add(it["dir"])

        if cfg.get("plex_url") and cfg.get("plex_token"):
            scan_progress["current_step"] = f"Lese Plex ({scope_name}) ein..."
            plex_items, plex_folders = fetch_plex_items(cfg.get("plex_url", ""), cfg.get("plex_token", ""), filter_type=filter_type)
            raw_items.extend(plex_items)
            all_scanned_folders.extend(plex_folders)
            for it in plex_items:
                jf_slugs.add(f"{slugify(it['title'])}_{it.get('year') or ''}")
                jf_slugs.add(slugify(it["title"]))
                if it.get("dir"):
                    jf_slugs.add(it["dir"])

        if cfg.get("local_scan_enabled", True):
            scan_progress["current_step"] = "Prüfe lokale Medienordner..."
            loc_items, loc_folders = scan_local_folders(
                cfg.get("local_media_paths", "/media"),
                filter_type=filter_type,
                exclude_slugs=jf_slugs
            )
            raw_items.extend(loc_items)
            all_scanned_folders.extend(loc_folders)

        # BEREINIGUNG VON ALTEN DUPLIKATEN AUS STATE
        current_state_items = {}
        for it in raw_items:
            current_state_items[it["id"]] = it

        # Bereinige alte lokale Duplikate aus vorherigen Versionen
        cleaned_existing = {}
        for k, v in state.get("items", {}).items():
            if v.get("source") == "Lokal":
                v_slug = f"{slugify(v.get('title',''))}_{v.get('year') or ''}"
                if v_slug in jf_slugs or slugify(v.get('title','')) in jf_slugs:
                    continue
            cleaned_existing[k] = v
        state["items"] = cleaned_existing

        total_items = len(current_state_items)
        scan_progress["total"] = total_items
        jf_meta = cfg.get("jf_metadata_dir", "/jellyfin-metadata")

        items_list = list(current_state_items.values())

        for idx, it in enumerate(items_list, 1):
            if stop_requested:
                scan_progress["current_step"] = "Scan abgebrochen!"
                break

            item_id = it["id"]
            scan_progress["processed"] = idx
            scan_progress["percent"] = int((idx / max(1, total_items)) * 100)
            scan_progress["current_item_title"] = f"{it['title']}"
            scan_progress["current_item_id"] = item_id

            existing = state["items"].get(item_id, {})
            theme_exists, actual_dir, mp3_file = check_item_theme(it, existing)

            it["dir"] = actual_dir

            if theme_exists and it.get("type") == "Collection" and mp3_file:
                sync_collection_theme(it, mp3_file, jf_meta)

            if theme_exists:
                status_txt = existing.get("video_title") or (mp3_file.name if mp3_file else "Vorhanden")
                scan_progress["item_status"][item_id] = {"status": "ok", "percent": 100, "step": "Vorhanden"}
            else:
                prev_status = existing.get("status", "")
                status_txt = prev_status if prev_status and prev_status != "Vorhanden" else "Fehlt"
                scan_progress["item_status"][item_id] = {"status": "missing", "percent": 0, "step": "Fehlt"}

            record = {
                **existing,
                **it,
                "has_theme": theme_exists,
                "status": status_txt
            }
            state["items"][item_id] = record

        if scope == "all":
            state["folders"] = all_scanned_folders

        save_state(state)
        if not stop_requested:
            scan_progress["current_step"] = f"Scan abgeschlossen: {total_items} {scope_name} erfasst."
    except Exception as e:
        scan_progress["current_step"] = f"Fehler beim Scan: {e}"
    finally:
        scan_progress["is_running"] = False


def run_download_queue(scope: str = "all"):
    global scan_progress, stop_requested
    if scan_progress["is_running"]:
        return

    stop_requested = False
    scan_progress["is_running"] = True
    scan_progress["action_type"] = "download"
    scan_progress["current_step"] = "Initialisiere Download-Warteschlange..."
    scan_progress["processed"] = 0
    scan_progress["percent"] = 0
    scan_progress["new_downloaded"] = 0
    scan_progress["failed"] = 0
    scan_progress["queue"] = []
    scan_progress["recent_finished"] = []

    try:
        cfg = load_config()
        state = load_state()
        min_dur = cfg.get("min_duration_sec", 25)
        max_dur = cfg.get("max_duration_sec", 270)
        fade_active = cfg.get("audio_fade", True)
        jf_meta = cfg.get("jf_metadata_dir", "/jellyfin-metadata")

        all_missing = [it for it in state["items"].values() if not it.get("has_theme")]
        if scope != "all":
            missing_items = [it for it in all_missing if it.get("type") == scope]
        else:
            missing_items = all_missing

        total_missing = len(missing_items)
        scan_progress["total"] = total_missing

        if total_missing == 0:
            scan_progress["current_step"] = "Keine fehlenden Themes in dieser Kategorie!"
            return

        scan_progress["queue"] = [it["title"] for it in missing_items]

        for idx, it in enumerate(missing_items, 1):
            if stop_requested:
                scan_progress["current_step"] = "Download abgebrochen!"
                break

            item_id = it["id"]
            title = it["title"]
            scan_progress["processed"] = idx
            scan_progress["percent"] = int((idx / total_missing) * 100)
            scan_progress["current_item_title"] = f"{title} ({it.get('type')})"
            scan_progress["current_item_id"] = item_id
            scan_progress["current_step"] = f"Lade ({idx}/{total_missing}): {title}..."

            if scan_progress["queue"]:
                scan_progress["queue"].pop(0)

            bl = state["blacklist"].get(item_id, [])
            dl_res = search_and_download(
                it,
                blacklist=bl,
                min_dur=min_dur,
                max_dur=max_dur,
                jf_meta_base=jf_meta,
                fade=fade_active
            )

            if dl_res["ok"]:
                it["has_theme"] = True
                it["video_id"] = dl_res["video_id"]
                it["video_title"] = dl_res["video_title"]
                it["status"] = dl_res["video_title"] or "Heruntergeladen"
                scan_progress["new_downloaded"] += 1
                scan_progress["recent_finished"].insert(0, {"title": title, "ok": True, "msg": dl_res["video_title"] or "Geladen"})
            else:
                it["status"] = dl_res["msg"]
                scan_progress["failed"] += 1
                scan_progress["recent_finished"].insert(0, {"title": title, "ok": False, "msg": dl_res["msg"]})

            scan_progress["recent_finished"] = scan_progress["recent_finished"][:8]

            state["items"][item_id] = it
            save_state(state)

        if not stop_requested:
            scan_progress["current_step"] = f"Download fertig! {scan_progress['new_downloaded']} geladen, {scan_progress['failed']} fehlgeschlagen."
    except Exception as e:
        scan_progress["current_step"] = f"Fehler beim Download: {e}"
    finally:
        scan_progress["is_running"] = False
        scan_progress["current_item_id"] = ""


@app.get("/poster/{item_id}")
def get_poster(item_id: str):
    state = load_state()
    cfg = load_config()
    item = state["items"].get(item_id)
    if not item:
        raise HTTPException(status_code=404)

    p = Path(item.get("dir", ""))
    if p.exists() and p.is_dir():
        for fname in ("poster.jpg", "folder.jpg", "cover.jpg", "show.jpg", "poster.png", "folder.png"):
            img_file = p / fname
            if img_file.exists():
                return FileResponse(img_file)

    if item.get("source") == "Jellyfin" and item.get("jf_raw_id") and cfg.get("jellyfin_url"):
        jf_url = cfg["jellyfin_url"]
        raw_id = item["jf_raw_id"]
        api_key = cfg.get("jellyfin_api_key", "").strip()
        headers = get_jellyfin_headers(api_key)

        for img_type in ("Primary", "Thumb", "Backdrop"):
            img_url = f"{jf_url}/Items/{raw_id}/Images/{img_type}"
            try:
                r = requests.get(
                    img_url,
                    headers=headers,
                    params={"maxWidth": "160", "quality": "85", "api_key": api_key},
                    timeout=4
                )
                if r.status_code == 200 and len(r.content) > 100:
                    return Response(content=r.content, media_type=r.headers.get("Content-Type", "image/jpeg"))
            except Exception:
                pass

    initial = (item.get("title") or "?")[0].upper()
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="60" height="90" viewBox="0 0 60 90">
        <rect width="100%" height="100%" fill="#262a32" rx="4"/>
        <text x="50%" y="54%" font-family="sans-serif" font-size="24" fill="#7d8590" text-anchor="middle" dominant-baseline="middle">{initial}</text>
    </svg>"""
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/audio/{item_id}")
def stream_audio(item_id: str):
    state = load_state()
    item = state["items"].get(item_id)
    if not item:
        raise HTTPException(status_code=404)

    has_theme, real_dir, mp3_file = check_item_theme(item)
    if not has_theme or not mp3_file or not mp3_file.exists():
        raise HTTPException(status_code=404)

    return FileResponse(mp3_file, media_type="audio/mpeg", headers={"Accept-Ranges": "bytes"})


@app.get("/api/progress")
def get_progress():
    return scan_progress


@app.post("/api/cancel")
def cancel_action():
    global stop_requested, scan_progress
    stop_requested = True
    scan_progress["current_step"] = "Abbruch angefordert..."
    return {"ok": True, "msg": "Abbruch angefordert"}


@app.post("/scan")
def trigger_scan(
    scope: str = Form("all"),
    tab: str = Form("filme"),
    filter_status: str = Form("all"),
    q: str = Form("")
):
    threading.Thread(target=run_scan, kwargs={"scope": scope}, daemon=True).start()
    clean_tab = normalize_tab(tab)
    return RedirectResponse(f"/?tab={clean_tab}&filter_status={filter_status}&q={q}", status_code=303)


@app.post("/download")
def trigger_download(
    scope: str = Form("all"),
    tab: str = Form("filme"),
    filter_status: str = Form("all"),
    q: str = Form("")
):
    threading.Thread(target=run_download_queue, kwargs={"scope": scope}, daemon=True).start()
    clean_tab = normalize_tab(tab)
    return RedirectResponse(f"/?tab={clean_tab}&filter_status={filter_status}&q={q}", status_code=303)


@app.post("/scan-single/{item_id}")
def scan_single(
    item_id: str,
    tab: str = Form("filme"),
    filter_status: str = Form("all"),
    q: str = Form("")
):
    state = load_state()
    cfg = load_config()
    item = state["items"].get(item_id)
    if not item:
        raise HTTPException(status_code=404)

    theme_exists, actual_dir, mp3_file = check_item_theme(item)
    item["dir"] = actual_dir
    item["has_theme"] = theme_exists
    if theme_exists:
        item["status"] = item.get("video_title") or (mp3_file.name if mp3_file else "Vorhanden")
        if item.get("type") == "Collection" and mp3_file:
            sync_collection_theme(item, mp3_file, cfg.get("jf_metadata_dir", "/jellyfin-metadata"))
    else:
        item["status"] = "Fehlt"

    state["items"][item_id] = item
    save_state(state)

    clean_tab = normalize_tab(tab)
    return RedirectResponse(f"/?tab={clean_tab}&filter_status={filter_status}&q={q}", status_code=303)


@app.post("/replace/{item_id}")
def replace_theme(
    item_id: str,
    custom_input: str = Form(""),
    tab: str = Form("filme"),
    filter_status: str = Form("all"),
    q: str = Form("")
):
    state = load_state()
    cfg = load_config()
    item = state["items"].get(item_id)
    if not item:
        raise HTTPException(status_code=404)

    old_vid = item.get("video_id")
    if old_vid:
        bl = state["blacklist"].setdefault(item_id, [])
        if old_vid not in bl:
            bl.append(old_vid)

    dl_res = search_and_download(
        item,
        custom_query_or_url=custom_input.strip() or None,
        blacklist=state["blacklist"].get(item_id, []),
        min_dur=cfg.get("min_duration_sec", 25),
        max_dur=cfg.get("max_duration_sec", 270),
        jf_meta_base=cfg.get("jf_metadata_dir", "/jellyfin-metadata"),
        fade=cfg.get("audio_fade", True)
    )
    if dl_res["ok"]:
        item["has_theme"] = True
        item["video_id"] = dl_res["video_id"]
        item["video_title"] = dl_res["video_title"]
        item["status"] = dl_res["video_title"] or "Heruntergeladen"
    else:
        item["status"] = dl_res["msg"]

    state["items"][item_id] = item
    save_state(state)

    clean_tab = normalize_tab(tab)
    return RedirectResponse(f"/?tab={clean_tab}&filter_status={filter_status}&q={q}", status_code=303)


@app.post("/delete/{item_id}")
def delete_theme(
    item_id: str,
    tab: str = Form("filme"),
    filter_status: str = Form("all"),
    q: str = Form("")
):
    state = load_state()
    item = state["items"].get(item_id)
    if item:
        for p in (Path(item.get("dir", "")) / "theme.mp3", LOCAL_THEMES_DIR / item["id"] / "theme.mp3"):
            p.unlink(missing_ok=True)

        old_vid = item.get("video_id")
        if old_vid:
            state["blacklist"].setdefault(item_id, []).append(old_vid)
        item["has_theme"] = False
        item["status"] = "Gelöscht (geblockt)"
        save_state(state)

    clean_tab = normalize_tab(tab)
    return RedirectResponse(f"/?tab={clean_tab}&filter_status={filter_status}&q={q}", status_code=303)


# TMM FEATURE: POSTER MANUELL ZIEHEN
@app.post("/fetch-poster/{item_id}")
def api_fetch_poster(
    item_id: str,
    tab: str = Form("filme"),
    filter_status: str = Form("all"),
    q: str = Form("")
):
    state = load_state()
    item = state["items"].get(item_id)
    if item:
        save_poster_to_disk(item)
    clean_tab = normalize_tab(tab)
    return RedirectResponse(f"/?tab={clean_tab}&filter_status={filter_status}&q={q}", status_code=303)


# TMM FEATURE: RENAMER (VORSCHAU & DATEINAMEN BEREINIGEN)
class RenameReq(BaseModel):
    item_id: str
    target_mode: str = "standard"  # standard: "Titel (Jahr)"

@app.post("/api/rename")
def api_rename_item(data: RenameReq):
    state = load_state()
    item = state["items"].get(data.item_id)
    if not item or not item.get("dir"):
        return {"ok": False, "msg": "Eintrag oder Pfad nicht gefunden"}

    item_dir = Path(item["dir"])
    if not item_dir.exists():
        return {"ok": False, "msg": f"Verzeichnis {item_dir} existiert nicht"}

    title = item.get("title", "").strip()
    year = item.get("year")
    clean_title = re.sub(r'[\\/*?:"<>|]', "", title)
    new_dir_name = f"{clean_title} ({year})" if year else clean_title

    renamed_files = []
    # 1. Benenne Videodateien um
    video_exts = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts"}
    for f in item_dir.iterdir():
        if f.is_file() and f.suffix.lower() in video_exts:
            if item.get("type") == "Film":
                new_file_name = f"{new_dir_name}{f.suffix.lower()}"
                new_file_path = item_dir / new_file_name
                if f != new_file_path and not new_file_path.exists():
                    try:
                        f.rename(new_file_path)
                        renamed_files.append(f"{f.name} ➔ {new_file_name}")
                    except Exception as e:
                        return {"ok": False, "msg": f"Fehler beim Umbenennen: {e}"}

    # 2. Benenne den Ordner selbst um (falls abweichend)
    if item_dir.name != new_dir_name and item_dir.parent.exists():
        new_dir_path = item_dir.parent / new_dir_name
        if not new_dir_path.exists():
            try:
                item_dir.rename(new_dir_path)
                item["dir"] = str(new_dir_path)
                state["items"][data.item_id] = item
                save_state(state)
                renamed_files.append(f"Ordner: {item_dir.name} ➔ {new_dir_name}")
            except Exception as e:
                return {"ok": False, "msg": f"Ordner-Umbenennung fehlgeschlagen: {e}"}

    return {"ok": True, "msg": "Erfolgreich umbenannt: " + (", ".join(renamed_files) or "Bereits sauber benannt!")}


class TestJellyfinReq(BaseModel):
    url: str
    api_key: str

class TestPlexReq(BaseModel):
    url: str
    token: str

class TestFolderReq(BaseModel):
    paths: str

@app.post("/api/test-jellyfin")
def api_test_jellyfin(data: TestJellyfinReq):
    return test_jellyfin_connection(data.url, data.api_key)

@app.post("/api/test-plex")
def api_test_plex(data: TestPlexReq):
    return test_plex_connection(data.url, data.token)

@app.post("/api/test-folder")
def api_test_folder(data: TestFolderReq):
    return test_folder_path(data.paths)


@app.get("/settings", response_class=HTMLResponse)
def get_settings_page():
    cfg = load_config()
    return f"""
    <!doctype html>
    <html lang="de">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Einstellungen • Theme Fetcher</title>
        <style>
            :root {{
                --bg: #121417;
                --card-bg: #181b20;
                --border: #262a32;
                --text: #e4e6eb;
                --text-muted: #9aa0a6;
                --primary: #2b7de9;
                --primary-hover: #1b66c9;
                --success: #28a745;
                --danger: #c93b3b;
            }}
            * {{ box-sizing: border-box; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 24px; max-width: 900px; margin: 0 auto; }}
            .nav-back {{ margin-bottom: 20px; }}
            .nav-back a {{ color: var(--text-muted); text-decoration: none; font-size: 0.95rem; font-weight: 600; display: inline-flex; align-items: center; gap: 6px; }}
            .nav-back a:hover {{ color: #fff; }}
            h1 {{ margin: 0 0 20px 0; font-size: 1.7rem; }}
            .card {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 22px; margin-bottom: 20px; }}
            .card h2 {{ margin-top: 0; font-size: 1.15rem; border-bottom: 1px solid var(--border); padding-bottom: 8px; display: flex; justify-content: space-between; align-items: center; }}
            .form-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-top: 14px; }}
            .form-group {{ display: flex; flex-direction: column; gap: 6px; }}
            .form-group label {{ font-size: 0.85rem; font-weight: 600; color: var(--text-muted); }}
            .form-group small {{ font-size: 0.78rem; color: #7d8590; }}
            input[type="text"], input[type="number"] {{ background: #1e2126; border: 1px solid #343840; color: #fff; padding: 8px 12px; border-radius: 6px; font-size: 0.9rem; }}
            input[type="text"]:focus, input[type="number"]:focus {{ outline: none; border-color: var(--primary); }}
            .checkbox-group {{ display: flex; align-items: center; gap: 10px; font-size: 0.9rem; margin-top: 6px; }}
            .checkbox-group input {{ width: 18px; height: 18px; cursor: pointer; }}
            button, .btn {{ background: var(--primary); color: white; border: none; padding: 10px 18px; border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 0.95rem; text-decoration: none; display: inline-flex; align-items: center; gap: 6px; }}
            button.test {{ background: #2f3e54; color: #7db2ff; border: 1px solid #415575; font-size: 0.8rem; padding: 6px 12px; }}
            .test-result {{ margin-top: 6px; font-size: 0.85rem; padding: 8px 12px; border-radius: 4px; display: none; }}
            .test-result.ok {{ display: block; background: #133824; color: #5ce69a; border: 1px solid #1f5938; }}
            .test-result.err {{ display: block; background: #421c1c; color: #ff8585; border: 1px solid #6b2d2d; }}
        </style>
    </head>
    <body>
        <div class="nav-back">
            <a href="/">← Zurück zur Mediathek</a>
        </div>
        <h1>⚙️ Einstellungen & Quellen</h1>

        <form method="post" action="/settings">
            
            <div class="card">
                <h2>
                    <span>📁 1. Lokale Medienquellen (Festplatte)</span>
                    <button type="button" class="test" onclick="runTestFolder()">📂 Pfad testen</button>
                </h2>
                <div class="form-grid">
                    <div class="form-group" style="grid-column: 1 / -1;">
                        <label>Medienpfad(e) im Container</label>
                        <input type="text" id="cfg_paths" name="local_media_paths" value="{cfg.get('local_media_paths', '/media')}">
                        <small>Standard: <code>/media</code>. Entspricht deinem Docker Volume Mount.</small>
                        <div id="res_folder" class="test-result"></div>
                    </div>
                    <div class="checkbox-group" style="grid-column: 1 / -1;">
                        <input type="checkbox" id="local_scan_enabled" name="local_scan_enabled" {'checked' if cfg.get('local_scan_enabled', True) else ''}>
                        <label for="local_scan_enabled">Lokalen Festplatten-Scan aktivieren (wird automatisch mit Jellyfin dedupliziert)</label>
                    </div>
                </div>
            </div>

            <div class="card">
                <h2>
                    <span>🎬 2. Medienserver (Jellyfin / Plex)</span>
                    <div style="display:flex; gap:6px;">
                        <button type="button" class="test" onclick="runTestJellyfin()">🔌 Jellyfin testen</button>
                        <button type="button" class="test" onclick="runTestPlex()">🍿 Plex testen</button>
                    </div>
                </h2>
                <div class="form-grid">
                    <div class="form-group">
                        <label>Jellyfin Server-IP & Port</label>
                        <input type="text" id="cfg_jf_url" name="jellyfin_url" value="{cfg.get('jellyfin_url', '')}">
                        <small>Standard-Port: <strong>8096</strong> (z. B. <code>192.168.178.50:8096</code>)</small>
                    </div>
                    <div class="form-group">
                        <label>Jellyfin API-Key</label>
                        <input type="text" id="cfg_jf_key" name="jellyfin_api_key" value="{cfg.get('jellyfin_api_key', '')}">
                        <small>Erstellt unter Jellyfin: Dashboard ➔ API-Schlüssel</small>
                    </div>
                    <div id="res_jf" class="test-result" style="grid-column: 1 / -1;"></div>

                    <div class="form-group">
                        <label>Plex Server-IP & Port (Optional)</label>
                        <input type="text" id="cfg_plex_url" name="plex_url" value="{cfg.get('plex_url', '')}">
                        <small>Standard-Port: <strong>32400</strong> (z. B. <code>192.168.178.50:32400</code>)</small>
                    </div>
                    <div class="form-group">
                        <label>Plex Token (Optional)</label>
                        <input type="text" id="cfg_plex_token" name="plex_token" value="{cfg.get('plex_token', '')}">
                        <small>Dein persönlicher X-Plex-Token</small>
                    </div>
                    <div id="res_plex" class="test-result" style="grid-column: 1 / -1;"></div>
                </div>
            </div>

            <div class="card">
                <h2>⚡ 3. Download-Kriterien & Audio-Veredelung</h2>
                <div class="form-grid">
                    <div class="form-group">
                        <label>Min. Titellänge (Sekunden)</label>
                        <input type="number" name="min_duration_sec" value="{cfg.get('min_duration_sec', 25)}">
                        <small>Verhindert Jingles und kurze Soundeffekte (Standard: 25s)</small>
                    </div>
                    <div class="form-group">
                        <label>Max. Titellänge (Sekunden)</label>
                        <input type="number" name="max_duration_sec" value="{cfg.get('max_duration_sec', 270)}">
                        <small>Verhindert ganze Alben und Loops (Standard: 270s)</small>
                    </div>
                    <div class="checkbox-group">
                        <input type="checkbox" id="audio_fade" name="audio_fade" {'checked' if cfg.get('audio_fade', True) else ''}>
                        <label for="audio_fade"><strong>Sanftes Ein- und Ausblenden (Fade-In / Fade-Out):</strong> 1.5s sanft anblenden, 3s ausblenden</label>
                    </div>
                    <div class="checkbox-group">
                        <input type="checkbox" id="embed_cover" name="embed_cover" {'checked' if cfg.get('embed_cover', True) else ''}>
                        <label for="embed_cover"><strong>Cover-Bild direkt in MP3 einbetten (ID3-Tag) & poster.jpg speichern</strong></label>
                    </div>
                </div>
            </div>

            <div style="margin-top:20px; display:flex; gap:12px;">
                <button type="submit" style="background:#28a745; font-size:1.05rem; padding:12px 24px;">💾 Einstellungen speichern & anwenden</button>
                <a href="/" class="btn" style="background:#2a2e36;">Abbrechen</a>
            </div>
        </form>

        <script>
            async function runTestJellyfin() {{
                const resEl = document.getElementById('res_jf');
                resEl.className = 'test-result';
                resEl.style.display = 'block';
                resEl.innerText = 'Prüfe Verbindung zu Jellyfin...';
                
                const url = document.getElementById('cfg_jf_url').value;
                const apiKey = document.getElementById('cfg_jf_key').value;

                try {{
                    const resp = await fetch('/api/test-jellyfin', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ url: url, api_key: apiKey }})
                    }});
                    const data = await resp.json();
                    resEl.className = 'test-result ' + (data.ok ? 'ok' : 'err');
                    resEl.innerText = (data.ok ? '✓ ' : '✕ ') + data.msg;
                }} catch (e) {{
                    resEl.className = 'test-result err';
                    resEl.innerText = '✕ Anfrage fehlgeschlagen: ' + e;
                }}
            }}

            async function runTestPlex() {{
                const resEl = document.getElementById('res_plex');
                resEl.className = 'test-result';
                resEl.style.display = 'block';
                resEl.innerText = 'Prüfe Verbindung zu Plex...';
                
                const url = document.getElementById('cfg_plex_url').value;
                const token = document.getElementById('cfg_plex_token').value;

                try {{
                    const resp = await fetch('/api/test-plex', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ url: url, token: token }})
                    }});
                    const data = await resp.json();
                    resEl.className = 'test-result ' + (data.ok ? 'ok' : 'err');
                    resEl.innerText = (data.ok ? '✓ ' : '✕ ') + data.msg;
                }} catch (e) {{
                    resEl.className = 'test-result err';
                    resEl.innerText = '✕ Anfrage fehlgeschlagen: ' + e;
                }}
            }}

            async function runTestFolder() {{
                const resEl = document.getElementById('res_folder');
                resEl.className = 'test-result';
                resEl.style.display = 'block';
                resEl.innerText = 'Prüfe Medienpfad im Container...';
                
                const paths = document.getElementById('cfg_paths').value;

                try {{
                    const resp = await fetch('/api/test-folder', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ paths: paths }})
                    }});
                    const data = await resp.json();
                    resEl.className = 'test-result ' + (data.ok ? 'ok' : 'err');
                    resEl.innerText = data.msg;
                }} catch (e) {{
                    resEl.className = 'test-result err';
                    resEl.innerText = '✕ Anfrage fehlgeschlagen: ' + e;
                }}
            }}
        </script>
    </body>
    </html>
    """


@app.post("/settings")
def update_settings(
    local_media_paths: str = Form("/media"),
    local_scan_enabled: Optional[str] = Form(None),
    jellyfin_url: str = Form(""),
    jellyfin_api_key: str = Form(""),
    plex_url: str = Form(""),
    plex_token: str = Form(""),
    min_duration_sec: int = Form(25),
    max_duration_sec: int = Form(270),
    audio_fade: Optional[str] = Form(None),
    embed_cover: Optional[str] = Form(None),
    scan_interval_hours: int = Form(12)
):
    cfg = load_config()
    cfg["local_media_paths"] = local_media_paths.strip()
    cfg["local_scan_enabled"] = bool(local_scan_enabled)
    cfg["jellyfin_url"] = clean_url_input(jellyfin_url)
    cfg["jellyfin_api_key"] = jellyfin_api_key.strip()
    cfg["plex_url"] = clean_url_input(plex_url)
    cfg["plex_token"] = plex_token.strip()
    cfg["min_duration_sec"] = min_duration_sec
    cfg["max_duration_sec"] = max_duration_sec
    cfg["audio_fade"] = bool(audio_fade)
    cfg["embed_cover"] = bool(embed_cover)
    cfg["scan_interval_hours"] = scan_interval_hours

    save_config(cfg)
    threading.Thread(target=run_scan, kwargs={"scope": "all"}, daemon=True).start()
    return RedirectResponse("/?settings=saved", status_code=303)


@app.get("/", response_class=HTMLResponse)
def index(q: str = "", filter_status: str = "all", tab: str = "filme", settings: str = ""):
    cfg = load_config()
    state = load_state()
    all_items = list(state["items"].values())

    clean_tab = normalize_tab(tab)

    total_filme = sum(1 for i in all_items if i.get("type") == "Film")
    filme_ok = sum(1 for i in all_items if i.get("type") == "Film" and i.get("has_theme"))
    filme_miss = total_filme - filme_ok

    total_serien = sum(1 for i in all_items if i.get("type") == "Serie")
    serien_ok = sum(1 for i in all_items if i.get("type") == "Serie" and i.get("has_theme"))
    serien_miss = total_serien - serien_ok

    total_cols = sum(1 for i in all_items if i.get("type") == "Collection")
    cols_ok = sum(1 for i in all_items if i.get("type") == "Collection" and i.get("has_theme"))
    cols_miss = total_cols - cols_ok

    scanned_folders = state.get("folders", [])

    type_map = {"filme": "Film", "serien": "Serie", "collections": "Collection"}
    current_scope = type_map.get(clean_tab, "Film")

    tab_items = [i for i in all_items if i.get("type") == current_scope]
    tab_total = len(tab_items)
    tab_ok = sum(1 for i in tab_items if i.get("has_theme"))
    tab_miss = tab_total - tab_ok

    items = tab_items
    if q:
        items = [
            i for i in items
            if q.lower() in i.get("title", "").lower() or q.lower() in (i.get("original_title") or "").lower()
        ]
    if filter_status == "missing":
        items = [i for i in items if not i.get("has_theme")]
    elif filter_status == "ok":
        items = [i for i in items if i.get("has_theme")]

    items.sort(key=lambda x: (x.get("has_theme", False), x.get("title", "").lower()))

    rows = ""
    for it in items:
        iid = it["id"]
        has_theme = bool(it.get("has_theme"))
        orig_hint = (
            f" <small style='color:#7d8590;'>[{it['original_title']}]</small>"
            if it.get("original_title") and it["original_title"].lower() != it["title"].lower()
            else ""
        )

        badge = (
            '<span class="badge ok">✓ Vorhanden</span>'
            if has_theme
            else '<span class="badge miss">✕ Fehlt</span>'
        )
        player = (
            f'<audio controls preload="none" src="/audio/{iid}?t={int(time.time())}"></audio>'
            if has_theme
            else "-"
        )

        if has_theme:
            v_title = it.get("video_title") or it.get("status") or "theme.mp3"
            sub_info = f"<div style='font-size:0.75rem; color:#5ce69a; margin-top:3px;'>🎵 {v_title}</div>"
        else:
            err_msg = it.get("status") or ""
            if err_msg and err_msg != "Fehlt" and err_msg != "Vorhanden":
                sub_info = f"<div style='font-size:0.75rem; color:#ff8585; margin-top:3px;'>⚠️ {err_msg}</div>"
            else:
                sub_info = ""

        # TMM Status: Prüfe ob lokales Poster existiert
        p_dir = Path(it.get("dir", ""))
        has_local_poster = (p_dir / "poster.jpg").exists() or (p_dir / "folder.jpg").exists()
        poster_btn = (
            '<form method="post" action="/fetch-poster/' + iid + '" style="display:inline;" title="Lädt Poster in den Medienordner (TMM)">'
            f'<input type="hidden" name="tab" value="{clean_tab}">'
            f'<input type="hidden" name="filter_status" value="{filter_status}">'
            f'<input type="hidden" name="q" value="{q}">'
            '<button type="submit" class="btn-sm secondary">🖼️ Poster</button>'
            '</form>'
        ) if not has_local_poster else '<span title="poster.jpg vorhanden" style="font-size:0.75rem; color:#7d8590;">✓ Poster</span>'

        rows += f"""
        <tr id="row_{iid}">
            <td style="width: 50px; padding: 6px;">
                <img src="/poster/{iid}" style="width: 44px; height: 64px; object-fit: cover; border-radius: 4px; display: block;" loading="lazy" alt="Cover">
            </td>
            <td>
                <strong>{it['title']}</strong>{orig_hint} <small>({it.get('year') or '-'})</small><br>
                <span class="meta">{it['source']} • <code>{it.get('dir')}</code></span>
            </td>
            <td>
                <div id="status_box_{iid}">
                    {badge}
                    {sub_info}
                </div>
            </td>
            <td>{player}</td>
            <td style="white-space: nowrap;">
                <div style="display:inline-flex; gap:4px; align-items:center;">
                    <form method="post" action="/replace/{iid}" class="inline-form" onsubmit="this.querySelector('button').innerText='⏳';">
                        <input type="hidden" name="tab" value="{clean_tab}">
                        <input type="hidden" name="filter_status" value="{filter_status}">
                        <input type="hidden" name="q" value="{q}">
                        <input type="text" name="custom_input" placeholder="YouTube URL / Begriff..." style="width:130px; font-size:0.8rem; padding:4px 6px;">
                        <button type="submit" class="btn-sm" title="Jetzt suchen & laden">▶️ Laden</button>
                    </form>
                    <form method="post" action="/scan-single/{iid}" style="display:inline;" onsubmit="this.querySelector('button').innerText='⏳';">
                        <input type="hidden" name="tab" value="{clean_tab}">
                        <input type="hidden" name="filter_status" value="{filter_status}">
                        <input type="hidden" name="q" value="{q}">
                        <button type="submit" class="btn-sm secondary" title="Nur diesen Eintrag neu prüfen">🔄</button>
                    </form>
                    {poster_btn}
                    <button type="button" class="btn-sm secondary" title="TMM-Renamer: Datei/Ordner nach Standard umbenennen" onclick="triggerRename('{iid}', '{it['title']}')">🏷️</button>
                    <form method="post" action="/delete/{iid}" style="display:inline;" onsubmit="return confirm('Theme löschen und sperren?');">
                        <input type="hidden" name="tab" value="{clean_tab}">
                        <input type="hidden" name="filter_status" value="{filter_status}">
                        <input type="hidden" name="q" value="{q}">
                        <button type="submit" class="btn-sm danger" title="Löschen & Blockieren">✕</button>
                    </form>
                </div>
            </td>
            <!-- NEUE FORTSCHRITTS-SPALTE HINTER AKTIONEN -->
            <td style="min-width: 140px; font-size: 0.8rem;" id="prog_col_{iid}">
                <div id="prog_bar_box_{iid}" style="display:none;">
                    <div style="background:#262a32; height:8px; border-radius:4px; overflow:hidden; margin-bottom:4px;">
                        <div id="prog_bar_fill_{iid}" style="background:#28a745; height:100%; width:0%; transition:width 0.3s;"></div>
                    </div>
                    <span id="prog_bar_txt_{iid}" style="color:#7db2ff; font-size:0.75rem;">Verarbeite...</span>
                </div>
                <span id="prog_static_{iid}" style="color:#7d8590;">{'✓ Bereit' if has_theme else 'Fehlt'}</span>
            </td>
        </tr>
        """

    saved_banner = '<div class="banner">✓ Einstellungen gespeichert!</div>' if settings == "saved" else ""
    folder_list_html = "".join(f"<li style='margin-bottom:6px;'><code>{f}</code></li>" for f in scanned_folders) or "<li>Bisher keine Bibliotheken erfasst. Klicke auf 'Mediathek scannen'!</li>"

    scope_label = "Filme" if current_scope == "Film" else ("Serien" if current_scope == "Serie" else "Collections")

    return f"""
    <!doctype html>
    <html lang="de">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Theme Fetcher & Media Enricher</title>
        <style>
            :root {{
                --bg: #121417;
                --card-bg: #181b20;
                --border: #262a32;
                --text: #e4e6eb;
                --text-muted: #9aa0a6;
                --primary: #2b7de9;
                --primary-hover: #1b66c9;
                --danger: #c93b3b;
                --success: #28a745;
                --success-bg: #1d4d32;
                --success-text: #5ce69a;
                --miss-bg: #4d2323;
                --miss-text: #ff7b7b;
            }}
            * {{ box-sizing: border-box; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 20px; }}
            .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; flex-wrap: wrap; gap: 14px; }}
            h1 {{ margin: 0; font-size: 1.6rem; letter-spacing: -0.5px; display: flex; align-items: center; gap: 8px; }}
            .stats {{ color: var(--text-muted); font-size: 0.95rem; margin-top: 4px; }}
            button, .btn {{ background: var(--primary); color: white; border: none; padding: 8px 14px; border-radius: 6px; cursor: pointer; font-weight: 500; font-size: 0.9rem; text-decoration: none; display: inline-flex; align-items: center; gap: 6px; }}
            button:hover, .btn:hover {{ background: var(--primary-hover); }}
            button.secondary, .btn.secondary {{ background: #2a2e36; color: var(--text); }}
            button.secondary:hover, .btn.secondary:hover {{ background: #353b45; }}
            button.success {{ background: var(--success); }}
            button.success:hover {{ background: #218838; }}
            button.danger {{ background: var(--danger); }}
            button.danger:hover {{ background: #a82e2e; }}
            .btn-sm {{ padding: 4px 8px; font-size: 0.8rem; border-radius: 4px; }}
            input[type="text"], input[type="number"], select {{ background: #1e2126; border: 1px solid #343840; color: #fff; padding: 7px 10px; border-radius: 6px; font-size: 0.85rem; }}
            input[type="text"]:focus, select:focus {{ outline: none; border-color: var(--primary); }}
            table {{ width: 100%; border-collapse: collapse; background: var(--card-bg); border-radius: 8px; overflow: hidden; margin-top: 10px; }}
            th, td {{ padding: 8px 10px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: middle; }}
            th {{ background: #1f2329; font-size: 0.8rem; text-transform: uppercase; color: var(--text-muted); letter-spacing: 0.5px; }}
            .meta {{ font-size: 0.75rem; color: #8b929a; }}
            .badge {{ padding: 3px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; display:inline-block; }}
            .badge.ok {{ background: var(--success-bg); color: var(--success-text); }}
            .badge.miss {{ background: var(--miss-bg); color: var(--miss-text); }}
            .badge.active-dl {{ background: #1c3b66; color: #7db2ff; animation: pulse 1.2s infinite; }}
            @keyframes pulse {{ 0% {{ opacity: 0.6; }} 50% {{ opacity: 1; }} 100% {{ opacity: 0.6; }} }}
            .inline-form {{ display: inline-flex; gap: 4px; }}
            audio {{ height: 32px; width: 190px; }}
            .banner {{ background: #1d4d32; color: #5ce69a; padding: 10px 16px; border-radius: 6px; margin-bottom: 16px; font-weight: 500; }}
            
            /* PROGRESS & QUEUE CARD */
            .progress-card {{ background: var(--card-bg); border: 1px solid #344256; border-radius: 8px; padding: 16px; margin-bottom: 18px; box-shadow: 0 4px 12px rgba(0,0,0,0.3); }}
            .progress-bar-bg {{ background: #262a32; height: 14px; border-radius: 7px; overflow: hidden; margin: 10px 0; }}
            .progress-bar-fill {{ background: #28a745; height: 100%; width: 0%; transition: width 0.3s ease; }}
            .queue-box {{ background: #131519; border: 1px solid #232730; border-radius: 6px; padding: 10px; margin-top: 12px; font-size: 0.85rem; }}
            
            /* TABS */
            .tab-nav {{ display: flex; gap: 8px; margin-bottom: 14px; border-bottom: 1px solid var(--border); padding-bottom: 8px; flex-wrap: wrap; }}
            .tab-btn {{ background: transparent; color: var(--text-muted); border: 1px solid transparent; padding: 8px 16px; border-radius: 6px; font-size: 0.95rem; cursor: pointer; text-decoration: none; font-weight: 600; }}
            .tab-btn:hover {{ background: #20242b; color: #fff; }}
            .tab-btn.active {{ background: #2b7de9; color: #fff; }}
            
            /* FILTER PILLS */
            .pill-filter {{ display: flex; gap: 8px; align-items: center; flex-wrap:wrap; }}
            .pill {{ background: #20242b; color: var(--text-muted); border: 1px solid #2d323b; padding: 5px 12px; border-radius: 20px; font-size: 0.85rem; text-decoration: none; font-weight: 500; cursor: pointer; }}
            .pill:hover {{ color: #fff; border-color: #3f4552; }}
            .pill.active {{ background: #2b7de9; color: #fff; border-color: #2b7de9; }}
            .pill.miss.active {{ background: #c93b3b; color: #fff; border-color: #c93b3b; }}
            .pill.ok.active {{ background: #28a745; color: #fff; border-color: #28a745; }}
        </style>
    </head>
    <body>
        <div class="header">
            <div>
                <h1>🎵 Theme Fetcher & Media Enricher</h1>
                <div class="stats">Filme: {filme_ok}/{total_filme} • Serien: {serien_ok}/{total_serien} • Collections: {cols_ok}/{total_cols}</div>
            </div>
            <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
                <a href="/settings" class="btn secondary">⚙️ Einstellungen</a>

                <form method="post" action="/scan" style="display:inline;">
                    <input type="hidden" name="scope" value="{current_scope}">
                    <input type="hidden" name="tab" value="{clean_tab}">
                    <input type="hidden" name="filter_status" value="{filter_status}">
                    <input type="hidden" name="q" value="{q}">
                    <button type="submit" title="Scannt nur die aktuelle Kategorie ({scope_label})">🔍 Nur {scope_label} scannen</button>
                </form>

                <form method="post" action="/scan" style="display:inline;">
                    <input type="hidden" name="scope" value="all">
                    <input type="hidden" name="tab" value="{clean_tab}">
                    <input type="hidden" name="filter_status" value="{filter_status}">
                    <input type="hidden" name="q" value="{q}">
                    <button type="submit" class="secondary" title="Kompletten Scan über alle Kategorien ausführen">🌐 Alles scannen</button>
                </form>

                <form method="post" action="/download" style="display:inline;">
                    <input type="hidden" name="scope" value="{current_scope}">
                    <input type="hidden" name="tab" value="{clean_tab}">
                    <input type="hidden" name="filter_status" value="{filter_status}">
                    <input type="hidden" name="q" value="{q}">
                    <button type="submit" class="success" title="Lädt fehlende Themes für {scope_label}">⬇️ {scope_label} laden ({tab_miss})</button>
                </form>

                <button type="button" id="btnCancel" class="danger" style="display:none;" onclick="cancelRunningAction()">🛑 Abbrechen</button>
            </div>
        </div>

        {saved_banner}

        <!-- LIVE FORTSCHRITTS- & WARTESCHLANGEN-KARTE -->
        <div id="progressCard" class="progress-card" style="display:none;">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <strong id="progStep" style="font-size:1.05rem;">Aktion läuft...</strong>
                <span id="progCount" style="color:var(--text-muted); font-size:0.95rem; font-weight:600;">0 / 0</span>
            </div>
            <div class="progress-bar-bg">
                <div id="progFill" class="progress-bar-fill"></div>
            </div>
            <div style="display:flex; justify-content:space-between; font-size:0.85rem; color:var(--text-muted);">
                <span id="progItem">Verarbeite...</span>
                <span>Erfolgreich geladen: <strong id="progDown" style="color:#5ce69a;">0</strong> | Fehler: <strong id="progFail" style="color:#ff8585;">0</strong></span>
            </div>

            <div id="queueBox" class="queue-box" style="display:none;">
                <div style="font-weight:600; margin-bottom:6px; color:#7db2ff;">📋 Nächste in der Download-Warteschlange:</div>
                <div id="queueList" style="color:var(--text-muted); font-size:0.8rem; margin-bottom:10px;">-</div>
                <div style="font-weight:600; margin-bottom:4px; color:#5ce69a;">✓ Zuletzt abgeschlossen:</div>
                <div id="recentList" style="font-size:0.8rem;">-</div>
            </div>
        </div>

        <!-- REITER / TABS -->
        <div class="tab-nav">
            <a href="/?tab=filme&filter_status={filter_status}{f'&q={q}' if q else ''}" class="tab-btn {'active' if clean_tab=='filme' else ''}">🎬 Filme ({filme_ok}/{total_filme})</a>
            <a href="/?tab=serien&filter_status={filter_status}{f'&q={q}' if q else ''}" class="tab-btn {'active' if clean_tab=='serien' else ''}">📺 Serien ({serien_ok}/{total_serien})</a>
            <a href="/?tab=collections&filter_status={filter_status}{f'&q={q}' if q else ''}" class="tab-btn {'active' if clean_tab=='collections' else ''}">📁 Collections ({cols_ok}/{total_cols})</a>
            <a href="/?tab=ordner" class="tab-btn {'active' if clean_tab=='ordner' else ''}">📂 Eingelesene Mediatheken ({len(scanned_folders)})</a>
        </div>

        {'<!-- ORDNER TAB -->' if clean_tab == 'ordner' else ''}
        <div style="display: {'block' if clean_tab == 'ordner' else 'none'}; background:var(--card-bg); padding:20px; border-radius:8px;">
            <h3 style="margin-top:0;">Gefundene Mediatheken & Verzeichnisse:</h3>
            <p style="color:var(--text-muted); font-size:0.9rem;">Hier siehst du alle Mediatheken und Pfade, die Jellyfin und deine Festplatte gemeldet haben:</p>
            <ul style="line-height:1.8; font-size:0.95rem;">
                {folder_list_html}
            </ul>
        </div>

        <!-- FILTER-LEISTE & TABELLE -->
        <div style="display: {'none' if clean_tab == 'ordner' else 'block'};">
            <div style="background:var(--card-bg); padding:12px; border-radius:8px; margin-bottom:14px; display:flex; gap:14px; align-items:center; justify-content:space-between; flex-wrap:wrap;">
                
                <div class="pill-filter">
                    <span style="font-size:0.85rem; color:var(--text-muted); margin-right:4px;">Filter:</span>
                    <a href="/?tab={clean_tab}&filter_status=all{f'&q={q}' if q else ''}" class="pill {'active' if filter_status=='all' else ''}">Alle ({tab_total})</a>
                    <a href="/?tab={clean_tab}&filter_status=missing{f'&q={q}' if q else ''}" class="pill miss {'active' if filter_status=='missing' else ''}">❌ Fehlt ({tab_miss})</a>
                    <a href="/?tab={clean_tab}&filter_status=ok{f'&q={q}' if q else ''}" class="pill ok {'active' if filter_status=='ok' else ''}">✓ Vorhanden ({tab_ok})</a>
                </div>

                <form method="get" action="/" style="display:flex; gap:6px; align-items:center;">
                    <input type="hidden" name="tab" value="{clean_tab}">
                    <input type="hidden" name="filter_status" value="{filter_status}">
                    <input type="text" name="q" value="{q}" placeholder="{scope_label} durchsuchen..." style="width:200px;">
                    <button type="submit" class="btn-sm">Suchen</button>
                    {f'<a href="/?tab={clean_tab}&filter_status={filter_status}" class="btn-sm secondary">✕</a>' if q else ''}
                </form>
            </div>

            <table>
                <thead>
                    <tr>
                        <th style="width:50px;">Cover</th>
                        <th>Titel & Pfad</th>
                        <th>Status</th>
                        <th>Vorschau</th>
                        <th>Aktionen</th>
                        <th>Fortschritt</th>
                    </tr>
                </thead>
                <tbody>{rows or f'<tr><td colspan="6" style="text-align:center; padding:30px; color:var(--text-muted);">Keine Einträge unter {scope_label} gefunden.</td></tr>'}</tbody>
            </table>
        </div>

        <script>
            // AUDIO-AUTOSTOPP: Wenn ein Player startet, stoppen alle anderen sofort!
            document.addEventListener('play', function(e){{
                var audios = document.getElementsByTagName('audio');
                for(var i = 0, len = audios.length; i < len; i++){{
                    if(audios[i] != e.target){{
                        audios[i].pause();
                    }}
                }}
            }}, true);

            async function triggerRename(itemId, title) {{
                if (!confirm('TMM-Renamer: Möchtest du die Mediendateien und den Ordner für "' + title + '" nach Jellyfin/Plex-Standard umbenennen?')) {{
                    return;
                }}
                try {{
                    const resp = await fetch('/api/rename', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ item_id: itemId }})
                    }});
                    const data = await resp.json();
                    alert(data.msg);
                    if (data.ok) location.reload();
                }} catch (e) {{
                    alert('Fehler beim Umbenennen: ' + e);
                }}
            }}

            async function cancelRunningAction() {{
                try {{
                    await fetch('/api/cancel', {{ method: 'POST' }});
                    document.getElementById('progStep').innerText = 'Abbruch angefordert...';
                }} catch (e) {{}}
            }}

            async function pollProgress() {{
                try {{
                    const resp = await fetch('/api/progress');
                    const data = await resp.json();
                    const card = document.getElementById('progressCard');
                    const cancelBtn = document.getElementById('btnCancel');
                    const qBox = document.getElementById('queueBox');
                    
                    if (data.is_running) {{
                        card.style.display = 'block';
                        cancelBtn.style.display = 'inline-flex';
                        document.getElementById('progStep').innerText = (data.action_type === 'download' ? '⬇️ ' : '🔍 ') + data.current_step;
                        document.getElementById('progCount').innerText = data.processed + ' / ' + data.total + ' (' + data.percent + '%)';
                        document.getElementById('progFill').style.width = data.percent + '%';
                        document.getElementById('progItem').innerText = data.current_item_title || '';
                        document.getElementById('progDown').innerText = data.new_downloaded;
                        document.getElementById('progFail').innerText = data.failed;

                        // TABELLEN-FORTSCHRITTSBALKEN PRO ZEILE AKTUALISIEREN
                        if (data.current_item_id) {{
                            const barBox = document.getElementById('prog_bar_box_' + data.current_item_id);
                            const barFill = document.getElementById('prog_bar_fill_' + data.current_item_id);
                            const barTxt = document.getElementById('prog_bar_txt_' + data.current_item_id);
                            const barStatic = document.getElementById('prog_static_' + data.current_item_id);
                            
                            if (barBox && barFill && barTxt) {{
                                barBox.style.display = 'block';
                                if (barStatic) barStatic.style.display = 'none';
                                const itemInfo = (data.item_status && data.item_status[data.current_item_id]) || {{}};
                                const pct = itemInfo.percent || 50;
                                barFill.style.width = pct + '%';
                                barTxt.innerText = itemInfo.step || 'Lädt Theme...';
                            }}
                        }}

                        if (data.action_type === 'download') {{
                            qBox.style.display = 'block';
                            if (data.queue && data.queue.length > 0) {{
                                document.getElementById('queueList').innerText = data.queue.slice(0, 6).join(' • ') + (data.queue.length > 6 ? ' ... (+' + (data.queue.length - 6) + ' weitere)' : '');
                            }} else {{
                                document.getElementById('queueList').innerText = 'Keine weiteren Titel in der Warteschlange.';
                            }}

                            if (data.recent_finished && data.recent_finished.length > 0) {{
                                document.getElementById('recentList').innerHTML = data.recent_finished.slice(0, 4).map(r => 
                                    '<div style=\"color:' + (r.ok ? '#5ce69a' : '#ff8585') + '; margin-bottom:2px;\">' + (r.ok ? '✓ ' : '✕ ') + '<strong>' + r.title + '</strong>: ' + r.msg + '</div>'
                                ).join('');
                            }}
                        }} else {{
                            qBox.style.display = 'none';
                        }}
                    }} else {{
                        cancelBtn.style.display = 'none';
                        if (data.processed > 0 && data.processed === data.total) {{
                            card.style.display = 'block';
                            document.getElementById('progStep').innerText = data.current_step;
                            document.getElementById('progFill').style.width = '100%';
                            document.getElementById('progDown').innerText = data.new_downloaded;
                            document.getElementById('progFail').innerText = data.failed;
                        }}
                    }}
                }} catch (e) {{}}
            }}

            setInterval(pollProgress, 1200);
            pollProgress();
        </script>
    </body>
    </html>
    """
