import os
import json
import re
import time
import shutil
import threading
import subprocess
import requests
from pathlib import Path
from typing import List, Optional
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel
import xml.etree.ElementTree as ET

app = FastAPI(title="Theme Fetcher")

DATA_DIR = Path("/data")
COLLECTIONS_DIR = DATA_DIR / "collections"
CONFIG_FILE = DATA_DIR / "config.json"
STATE_FILE = DATA_DIR / "state.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
COLLECTIONS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "local_media_paths": os.getenv("LOCAL_MEDIA_DIR", "/media"),
    "local_scan_enabled": os.getenv("LOCAL_SCAN_ENABLED", "true").lower() in ("true", "1", "yes"),
    "jellyfin_url": os.getenv("JELLYFIN_URL", "").rstrip("/"),
    "jellyfin_api_key": os.getenv("JELLYFIN_API_KEY", ""),
    "jf_metadata_dir": os.getenv("JF_METADATA_DIR", "/jellyfin-metadata"),
    "plex_url": os.getenv("PLEX_URL", "").rstrip("/"),
    "plex_token": os.getenv("PLEX_TOKEN", ""),
    "max_duration_sec": int(os.getenv("MAX_DURATION_SEC", "270")),
    "scan_interval_hours": int(os.getenv("SCAN_INTERVAL_HOURS", "12")),
    "auto_download": True
}

EXCLUDE_KEYWORDS = [
    "cover", "remix", "reaction", "reaktion", "1 hour", "10 hours", "loop",
    "tutorial", "piano", "klavier", "guitar", "gitarre", "karaoke",
    "full soundtrack", "full ost", "trailer", "ganzer film", "hörspiel",
    "review", "kritik", "analys"
]

state_lock = threading.Lock()

# PROGRESS & SCAN STATE
scan_progress = {
    "is_running": False,
    "current_step": "Bereit",
    "current_item": "",
    "processed": 0,
    "total": 0,
    "percent": 0,
    "new_downloaded": 0,
    "failed": 0,
    "folders_scanned": []
}


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
    return re.sub(r"[-\s]+", "_", clean) or "collection"


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
    if item.get("type") != "Collection" or not mp3_path.exists():
        return
    jf_dir = find_jellyfin_boxset_dir(item["title"], jf_meta_base)
    if jf_dir:
        try:
            shutil.copy2(mp3_path, jf_dir / "theme.mp3")
        except Exception as e:
            print(f"[Collection Sync Warnung] {e}")


def resolve_media_path(raw_path_str: str) -> Path:
    raw_str = raw_path_str.replace("\\", "/")
    p = Path(raw_str)
    if p.exists():
        return p

    # Remove root slash parts
    parts = [part for part in p.parts if part not in ("/", "\\", "")]
    
    # 1. Try directly mapping under /media: /Film/... -> /media/Film/...
    cand1 = Path("/media", *parts)
    if cand1.exists() or cand1.parent.exists():
        return cand1

    # 2. Try stripping the first top-level folder if it matches common mount names (e.g. /Film/... -> /media/...)
    if len(parts) > 1:
        cand2 = Path("/media", *parts[1:])
        if cand2.exists() or cand2.parent.exists():
            return cand2

    # 3. Check for keywords
    for i, part in enumerate(parts):
        if part.lower() in ("media", "data", "movies", "filme", "film", "serien", "series", "shows"):
            candidate = Path("/media", *parts[i:])
            if candidate.exists() or candidate.parent.exists():
                return candidate
            candidate_inner = Path("/media", *parts[i+1:])
            if candidate_inner.exists() or candidate_inner.parent.exists():
                return candidate_inner

    # 4. If all fails, map cleanly under /media
    return cand1


def scan_local_folders(paths_str: str) -> tuple[List[dict], List[str]]:
    results = []
    scanned_dirs = []
    if not paths_str:
        return results, scanned_dirs

    paths = [p.strip() for p in paths_str.replace(";", ",").split(",") if p.strip()]
    video_exts = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".iso"}

    for base_str in paths:
        base = Path(base_str)
        if not base.exists() or not base.is_dir():
            scanned_dirs.append(f"❌ {base_str} (Pfad existiert nicht im Container)")
            continue

        scanned_dirs.append(f"📁 {base_str} (Aktiv)")
        try:
            for root, dirs, files in os.walk(base):
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
                has_theme = (rpath / "theme.mp3").exists()
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
        "Authorization": f'MediaBrowser Token="{clean_key}", Client="ThemeFetcher", Device="ZimaOS", DeviceId="theme-fetcher-zimaos", Version="1.0.0"',
        "Accept": "application/json"
    }


def test_jellyfin_connection(url: str, api_key: str) -> dict:
    url = url.strip().rstrip("/")
    api_key = api_key.strip()
    if not url:
        return {"ok": False, "msg": "Bitte zuerst eine Server-URL eintragen (z. B. http://192.168.178.X:8096)"}
    if not api_key:
        return {"ok": False, "msg": "Bitte einen API-Key eintragen"}

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

        user_id = users[0]["Id"] if users else None
        item_count = 0
        if user_id:
            it_resp = requests.get(
                f"{url}/Users/{user_id}/Items",
                headers=headers,
                params={"IncludeItemTypes": "Movie,Series,BoxSet", "Recursive": "true", "Limit": "1", "api_key": api_key},
                timeout=6
            )
            if it_resp.status_code == 200:
                item_count = it_resp.json().get("TotalRecordCount", 0)

        info_txt = f"Verbunden mit '{server_name}' (v{version}) • {len(users)} Benutzer • {item_count} Medien gefunden!"
        return {"ok": True, "msg": info_txt}
    except requests.exceptions.ConnectTimeout:
        return {"ok": False, "msg": "Zeitüberschreitung (Timeout). Läuft Jellyfin unter dieser IP und diesem Port?"}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "msg": "Verbindung abgelehnt. Falls 'localhost'/'127.0.0.1' eingetragen ist: Bitte die echte ZimaOS-IP (z. B. http://192.168.178.X:8096) verwenden!"}
    except Exception as e:
        return {"ok": False, "msg": f"Fehler: {str(e)}"}


def test_plex_connection(url: str, token: str) -> dict:
    url = url.strip().rstrip("/")
    token = token.strip()
    if not url:
        return {"ok": False, "msg": "Bitte Plex Server-URL eintragen"}
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
            details.append(f"❌ '{p}' existiert nicht im Container (Mount in Docker Compose prüfen!)")
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


def fetch_jellyfin_items(url: str, api_key: str) -> tuple[List[dict], List[str]]:
    if not url or not api_key:
        return [], []
    headers = get_jellyfin_headers(api_key)
    results = []
    scanned_folders = []
    try:
        users_resp = requests.get(f"{url}/Users", headers=headers, params={"api_key": api_key.strip()}, timeout=10)
        users_resp.raise_for_status()
        users = users_resp.json()
        if not users:
            return [], []
        user_id = users[0]["Id"]

        params = {
            "IncludeItemTypes": "Movie,Series,BoxSet",
            "Recursive": "true",
            "Fields": "Path,ProductionYear,OriginalTitle",
            "api_key": api_key.strip()
        }
        resp = requests.get(f"{url}/Users/{user_id}/Items", headers=headers, params=params, timeout=25)
        resp.raise_for_status()

        seen_dirs = set()
        for item in resp.json().get("Items", []):
            item_type = item.get("Type")
            title = item.get("Name", "Unbekannt")
            item_id = f"jf_{item['Id']}"

            if item_type == "BoxSet":
                col_dir = COLLECTIONS_DIR / f"{item_id}_{slugify(title)}"
                col_dir.mkdir(parents=True, exist_ok=True)
                results.append({
                    "id": item_id,
                    "source": "Jellyfin",
                    "type": "Collection",
                    "title": title,
                    "original_title": item.get("OriginalTitle") or "",
                    "year": item.get("ProductionYear"),
                    "dir": str(col_dir)
                })
                continue

            raw_path = item.get("Path")
            if not raw_path:
                continue
            resolved = resolve_media_path(raw_path)
            target_dir = resolved.parent if item_type == "Movie" else resolved

            parent_folder = str(target_dir.parent)
            if parent_folder not in seen_dirs:
                seen_dirs.add(parent_folder)
                scanned_folders.append(f"🎬 Jellyfin Pfad: {parent_folder}")

            results.append({
                "id": item_id,
                "source": "Jellyfin",
                "type": "Serie" if item_type == "Series" else "Film",
                "title": title,
                "original_title": item.get("OriginalTitle") or "",
                "year": item.get("ProductionYear"),
                "dir": str(target_dir)
            })
    except Exception as e:
        print(f"[Jellyfin Fetch Fehler] {e}")
    return results, scanned_folders


def fetch_plex_items(url: str, token: str) -> tuple[List[dict], List[str]]:
    if not url or not token:
        return [], []
    headers = {"X-Plex-Token": token, "Accept": "application/xml"}
    results = []
    scanned_folders = []
    try:
        sec_resp = requests.get(f"{url}/library/sections", headers=headers, timeout=10)
        sec_resp.raise_for_status()
        root = ET.fromstring(sec_resp.text)

        for directory in root.findall("Directory"):
            sec_id = directory.attrib.get("key")
            sec_type = directory.attrib.get("type")
            sec_title = directory.attrib.get("title", "Mediathek")
            if sec_type not in ("movie", "show"):
                continue

            scanned_folders.append(f"🍿 Plex Sektion: {sec_title} ({sec_type})")
            items_resp = requests.get(f"{url}/library/sections/{sec_id}/all", headers=headers, timeout=20)
            items_root = ET.fromstring(items_resp.text)

            if sec_type == "movie":
                for video in items_root.findall("Video"):
                    part = video.find(".//Part")
                    if part is not None and "file" in part.attrib:
                        resolved = resolve_media_path(part.attrib["file"])
                        p = resolved.parent
                        results.append({
                            "id": f"plex_{video.attrib.get('ratingKey')}",
                            "source": "Plex",
                            "type": "Film",
                            "title": video.attrib.get("title", "Unbekannt"),
                            "original_title": video.attrib.get("originalTitle", ""),
                            "year": video.attrib.get("year"),
                            "dir": str(p)
                        })
            elif sec_type == "show":
                for show in items_root.findall("Directory"):
                    loc = show.find("Location")
                    if loc is not None and "path" in loc.attrib:
                        resolved = resolve_media_path(loc.attrib["path"])
                        results.append({
                            "id": f"plex_{show.attrib.get('ratingKey')}",
                            "source": "Plex",
                            "type": "Serie",
                            "title": show.attrib.get("title", "Unbekannt"),
                            "original_title": show.attrib.get("originalTitle", ""),
                            "year": show.attrib.get("year"),
                            "dir": str(resolved)
                        })

            try:
                col_resp = requests.get(f"{url}/library/sections/{sec_id}/collections", headers=headers, timeout=15)
                col_root = ET.fromstring(col_resp.text)
                for col in col_root.findall("Directory"):
                    rkey = col.attrib.get("ratingKey", "")
                    ctitle = col.attrib.get("title", "Collection")
                    cid = f"plex_col_{rkey}"
                    cdir = COLLECTIONS_DIR / f"{cid}_{slugify(ctitle)}"
                    cdir.mkdir(parents=True, exist_ok=True)
                    results.append({
                        "id": cid,
                        "source": "Plex",
                        "type": "Collection",
                        "title": ctitle,
                        "original_title": "",
                        "year": col.attrib.get("maxYear") or col.attrib.get("minYear"),
                        "dir": str(cdir)
                    })
            except Exception as ce:
                print(f"[Plex Collections Warnung] {ce}")

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
        queries.append(f"ytsearch6:{title} Theme Song")
        queries.append(f"ytsearch5:{title} Titelmelodie")
        if orig and orig.lower() != title.lower():
            queries.append(f"ytsearch6:{orig} Theme Song")
            queries.append(f"ytsearch5:{orig} Opening Theme")
    elif itype == "Collection":
        queries.append(f"ytsearch6:{title} Main Theme")
        clean_col = re.sub(r"\b(collection|reihe|sammlung|filmreihe)\b", "", title, flags=re.IGNORECASE).strip()
        if clean_col and clean_col.lower() != title.lower():
            queries.append(f"ytsearch6:{clean_col} Main Theme")
        if orig and orig.lower() != title.lower():
            queries.append(f"ytsearch6:{orig} Main Theme")
    else:
        if orig and orig.lower() != title.lower():
            queries.append(f"ytsearch6:{orig} {year or ''} Main Theme".strip())
        queries.append(f"ytsearch6:{title} {year or ''} Main Theme".strip())
        queries.append(f"ytsearch5:{title} Titelmelodie")

    base_name = orig if orig else title
    queries.append(f"scsearch5:{base_name} Main Theme")
    return queries


def find_candidate(search_expr: str, blacklist: List[str], max_dur: int) -> tuple[Optional[dict], str]:
    cmd = [
        "yt-dlp",
        search_expr,
        "--dump-json",
        "--flat-playlist",
        "--no-warnings"
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        lines = res.stdout.strip().splitlines()
        if not lines:
            return None, "Keine Suchergebnisse gefunden"

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
            if duration and duration > max_dur:
                continue
            if any(bad in lower_title for bad in EXCLUDE_KEYWORDS):
                continue

            return {"id": vid_id or url, "url": url, "title": vid_title}, "OK"
        return None, "Gefundene Treffer überschreiten max. Dauer oder enthalten Ausschluss-Wörter"
    except subprocess.TimeoutExpired:
        return None, "Such-Timeout (YouTube antwortet verzögert)"
    except Exception as e:
        return None, f"Suchfehler: {str(e)}"


def search_and_download(item: dict, custom_query_or_url: Optional[str] = None, blacklist: List[str] = None, max_dur: int = 270, jf_meta_base: str = "/jellyfin-metadata") -> dict:
    blacklist = blacklist or []
    target_dir = Path(item["dir"])

    # Test write permissions in target directory
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        test_write = target_dir / ".theme_test_write"
        test_write.write_text("ok")
        test_write.unlink()
    except Exception as e:
        # Fallback to local data cache if folder path is not writable
        safe_fallback = DATA_DIR / "local_themes" / item["id"]
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

    chosen_video = None
    search_reason = ""

    if is_direct_url:
        chosen_video = {"url": custom_query_or_url, "title": "Manueller Link", "id": custom_query_or_url}
    else:
        search_list = (
            [f"ytsearch8:{custom_query_or_url}", f"scsearch5:{custom_query_or_url}"]
            if custom_query_or_url
            else build_search_queries(item)
        )
        try:
            for expr in search_list:
                cand, reason = find_candidate(expr, blacklist, max_dur)
                if cand:
                    chosen_video = cand
                    break
                else:
                    search_reason = reason
        except Exception as e:
            return {"ok": False, "msg": f"Suchfehler: {e}"}

    if not chosen_video:
        return {"ok": False, "msg": f"Kein Treffer: {search_reason or 'Suche erfolglos'}"}

    try:
        dl_cmd = [
            "yt-dlp",
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "192K",
            "--no-playlist",
            "-o", temp_template,
            chosen_video["url"]
        ]
        res = subprocess.run(dl_cmd, capture_output=True, text=True, timeout=90)
        if res.returncode != 0:
            err_msg = res.stderr.strip().splitlines()[-1] if res.stderr else "Fehlercode 1"
            return {"ok": False, "msg": f"Downloadfehler ({err_msg[:80]})"}

        downloaded = target_dir / "theme_temp.mp3"
        if downloaded.exists():
            if output_mp3.exists():
                output_mp3.unlink()
            downloaded.rename(output_mp3)
            sync_collection_theme(item, output_mp3, jf_meta_base)
            return {
                "ok": True,
                "video_id": chosen_video["id"],
                "video_title": chosen_video["title"]
            }
        return {"ok": False, "msg": "MP3-Konvertierung fehlgeschlagen"}
    except Exception as e:
        return {"ok": False, "msg": f"Downloadfehler: {str(e)[:60]}"}


def run_full_scan():
    global scan_progress
    if scan_progress["is_running"]:
        return

    scan_progress["is_running"] = True
    scan_progress["current_step"] = "Lese Mediatheken aus..."
    scan_progress["processed"] = 0
    scan_progress["total"] = 0
    scan_progress["percent"] = 0
    scan_progress["new_downloaded"] = 0
    scan_progress["failed"] = 0
    scan_progress["folders_scanned"] = []

    try:
        cfg = load_config()
        state = load_state()

        raw_items = []
        all_scanned_folders = []

        if cfg.get("local_scan_enabled", True):
            loc_items, loc_folders = scan_local_folders(cfg.get("local_media_paths", "/media"))
            raw_items.extend(loc_items)
            all_scanned_folders.extend(loc_folders)

        if cfg.get("jellyfin_url") and cfg.get("jellyfin_api_key"):
            scan_progress["current_step"] = "Lese Jellyfin Mediathek ein..."
            jf_items, jf_folders = fetch_jellyfin_items(cfg.get("jellyfin_url", ""), cfg.get("jellyfin_api_key", ""))
            raw_items.extend(jf_items)
            all_scanned_folders.extend(jf_folders)

        if cfg.get("plex_url") and cfg.get("plex_token"):
            scan_progress["current_step"] = "Lese Plex Mediathek ein..."
            plex_items, plex_folders = fetch_plex_items(cfg.get("plex_url", ""), cfg.get("plex_token", ""))
            raw_items.extend(plex_items)
            all_scanned_folders.extend(plex_folders)

        by_dir = {}
        for it in raw_items:
            by_dir[it["dir"]] = it

        scan_progress["folders_scanned"] = all_scanned_folders
        state["folders"] = all_scanned_folders

        total_items = len(by_dir)
        scan_progress["total"] = total_items

        max_dur = cfg.get("max_duration_sec", 270)
        auto_dl = cfg.get("auto_download", True)
        jf_meta = cfg.get("jf_metadata_dir", "/jellyfin-metadata")

        items_list = list(by_dir.values())

        for idx, it in enumerate(items_list, 1):
            item_id = it["id"]
            title = it["title"]
            scan_progress["processed"] = idx
            scan_progress["percent"] = int((idx / max(1, total_items)) * 100)
            scan_progress["current_item"] = f"{title} ({it.get('type')})"

            existing = state["items"].get(item_id, {})
            mp3_path = Path(it["dir"]) / "theme.mp3"
            theme_exists = mp3_path.exists()

            if theme_exists and it.get("type") == "Collection":
                sync_collection_theme(it, mp3_path, jf_meta)

            record = {
                **it,
                "has_theme": theme_exists,
                "video_id": existing.get("video_id", ""),
                "video_title": existing.get("video_title", ""),
                "status": "Vorhanden" if theme_exists else existing.get("status", "Fehlt")
            }

            if auto_dl and not theme_exists:
                scan_progress["current_step"] = f"Suche & lade: {title}..."
                bl = state["blacklist"].get(item_id, [])
                dl_res = search_and_download(it, blacklist=bl, max_dur=max_dur, jf_meta_base=jf_meta)
                if dl_res["ok"]:
                    record["has_theme"] = True
                    record["video_id"] = dl_res["video_id"]
                    record["video_title"] = dl_res["video_title"]
                    record["status"] = "Heruntergeladen"
                    scan_progress["new_downloaded"] += 1
                else:
                    record["status"] = dl_res["msg"]
                    scan_progress["failed"] += 1

            state["items"][item_id] = record
            save_state(state)

        scan_progress["current_step"] = f"Fertig! ({scan_progress['new_downloaded']} geladen, {scan_progress['failed']} übersprungen/fehlend)"
    except Exception as e:
        scan_progress["current_step"] = f"Fehler beim Scan: {e}"
        print(f"[Scan Gesamtfehler] {e}")
    finally:
        scan_progress["is_running"] = False


def background_scheduler():
    time.sleep(5)
    run_full_scan()
    while True:
        cfg = load_config()
        interval = max(1, int(cfg.get("scan_interval_hours", 12)))
        time.sleep(interval * 3600)
        run_full_scan()


threading.Thread(target=background_scheduler, daemon=True).start()


@app.get("/audio/{item_id}")
def stream_audio(item_id: str):
    state = load_state()
    item = state["items"].get(item_id)
    if not item:
        raise HTTPException(status_code=404)
    mp3 = Path(item["dir"]) / "theme.mp3"
    if not mp3.exists():
        raise HTTPException(status_code=404)
    return FileResponse(mp3, media_type="audio/mpeg")


@app.get("/api/progress")
def get_progress():
    return scan_progress


@app.post("/scan")
def trigger_scan():
    threading.Thread(target=run_full_scan, daemon=True).start()
    return RedirectResponse("/", status_code=303)


# TEST API ENDPOINTS
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


@app.post("/settings")
def update_settings(
    local_media_paths: str = Form("/media"),
    local_scan_enabled: Optional[str] = Form(None),
    jellyfin_url: str = Form(""),
    jellyfin_api_key: str = Form(""),
    plex_url: str = Form(""),
    plex_token: str = Form(""),
    max_duration_sec: int = Form(270),
    scan_interval_hours: int = Form(12),
    auto_download: Optional[str] = Form(None)
):
    cfg = load_config()
    cfg["local_media_paths"] = local_media_paths.strip()
    cfg["local_scan_enabled"] = bool(local_scan_enabled)
    cfg["jellyfin_url"] = jellyfin_url.strip().rstrip("/")
    cfg["jellyfin_api_key"] = jellyfin_api_key.strip()
    cfg["plex_url"] = plex_url.strip().rstrip("/")
    cfg["plex_token"] = plex_token.strip()
    cfg["max_duration_sec"] = max_duration_sec
    cfg["scan_interval_hours"] = scan_interval_hours
    cfg["auto_download"] = bool(auto_download)

    save_config(cfg)
    threading.Thread(target=run_full_scan, daemon=True).start()
    return RedirectResponse("/?settings=saved", status_code=303)


@app.post("/replace/{item_id}")
def replace_theme(item_id: str, custom_input: str = Form("")):
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
        max_dur=cfg.get("max_duration_sec", 270),
        jf_meta_base=cfg.get("jf_metadata_dir", "/jellyfin-metadata")
    )
    if dl_res["ok"]:
        item["has_theme"] = True
        item["video_id"] = dl_res["video_id"]
        item["video_title"] = dl_res["video_title"]
        item["status"] = "Ersetzt"
    else:
        item["status"] = dl_res["msg"]

    state["items"][item_id] = item
    save_state(state)
    return RedirectResponse("/", status_code=303)


@app.post("/delete/{item_id}")
def delete_theme(item_id: str):
    state = load_state()
    item = state["items"].get(item_id)
    if item:
        mp3 = Path(item["dir"]) / "theme.mp3"
        mp3.unlink(missing_ok=True)
        old_vid = item.get("video_id")
        if old_vid:
            state["blacklist"].setdefault(item_id, []).append(old_vid)
        item["has_theme"] = False
        item["status"] = "Gelöscht (geblockt)"
        save_state(state)
    return RedirectResponse("/", status_code=303)


@app.get("/", response_class=HTMLResponse)
def index(q: str = "", filter_status: str = "all", tab: str = "filme", settings: str = ""):
    cfg = load_config()
    state = load_state()
    all_items = list(state["items"].values())

    # Count stats
    total_filme = sum(1 for i in all_items if i.get("type") == "Film")
    filme_ok = sum(1 for i in all_items if i.get("type") == "Film" and i.get("has_theme"))
    
    total_serien = sum(1 for i in all_items if i.get("type") == "Serie")
    serien_ok = sum(1 for i in all_items if i.get("type") == "Serie" and i.get("has_theme"))

    total_cols = sum(1 for i in all_items if i.get("type") == "Collection")
    cols_ok = sum(1 for i in all_items if i.get("type") == "Collection" and i.get("has_theme"))

    scanned_folders = state.get("folders", [])

    # Filter items according to active tab
    type_map = {"filme": "Film", "serien": "Serie", "collections": "Collection"}
    target_type = type_map.get(tab.lower())

    if target_type:
        items = [i for i in all_items if i.get("type") == target_type]
    else:
        items = all_items

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
        orig_hint = (
            f" <small style='color:#7d8590;'>[{it['original_title']}]</small>"
            if it.get("original_title") and it["original_title"].lower() != it["title"].lower()
            else ""
        )
        badge = (
            '<span class="badge ok">Vorhanden</span>'
            if it.get("has_theme")
            else '<span class="badge miss">Fehlt</span>'
        )
        player = (
            f'<audio controls preload="none" src="/audio/{iid}?t={int(time.time())}"></audio>'
            if it.get("has_theme")
            else "-"
        )
        
        status_color = "#5ce69a" if it.get("has_theme") else "#ff7b7b"
        status_display = it.get("video_title") or it.get("status")

        rows += f"""
        <tr>
            <td>
                <strong>{it['title']}</strong>{orig_hint} <small>({it.get('year') or '-'})</small><br>
                <span class="meta">Quelle: {it['source']} • Pfad: <code>{it.get('dir')}</code></span><br>
                <span style="font-size:0.8rem; color:{status_color};">Status: {status_display}</span>
            </td>
            <td>{badge}</td>
            <td>{player}</td>
            <td>
                <form method="post" action="/replace/{iid}" class="inline-form">
                    <input type="text" name="custom_input" placeholder="YouTube URL oder Suchbegriff...">
                    <button type="submit">Laden / Ersetzen</button>
                </form>
                <form method="post" action="/delete/{iid}" style="display:inline;">
                    <button type="submit" class="danger" title="Löschen & auf Blacklist setzen">✕</button>
                </form>
            </td>
        </tr>
        """

    saved_banner = '<div class="banner">✓ Einstellungen gespeichert & Scan gestartet!</div>' if settings == "saved" else ""

    folder_list_html = "".join(f"<li style='margin-bottom:6px;'><code>{f}</code></li>" for f in scanned_folders) or "<li>Bisher keine Ordner erfasst. Starte einen Scan!</li>"

    return f"""
    <!doctype html>
    <html lang="de">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Theme Fetcher</title>
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
            button.secondary {{ background: #2a2e36; color: var(--text); }}
            button.secondary:hover {{ background: #353b45; }}
            button.test {{ background: #2f3e54; color: #7db2ff; border: 1px solid #415575; font-size: 0.8rem; padding: 6px 12px; }}
            button.test:hover {{ background: #3a4d69; }}
            button.danger {{ background: var(--danger); padding: 8px 10px; }}
            input[type="text"], input[type="number"], select {{ background: #1e2126; border: 1px solid #343840; color: #fff; padding: 8px 12px; border-radius: 6px; font-size: 0.9rem; }}
            input[type="text"]:focus, select:focus {{ outline: none; border-color: var(--primary); }}
            table {{ width: 100%; border-collapse: collapse; background: var(--card-bg); border-radius: 8px; overflow: hidden; margin-top: 10px; }}
            th, td {{ padding: 12px 14px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: middle; }}
            th {{ background: #1f2329; font-size: 0.8rem; text-transform: uppercase; color: var(--text-muted); letter-spacing: 0.5px; }}
            .meta {{ font-size: 0.8rem; color: #8b929a; }}
            .badge {{ padding: 4px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }}
            .badge.ok {{ background: var(--success-bg); color: var(--success-text); }}
            .badge.miss {{ background: var(--miss-bg); color: var(--miss-text); }}
            .inline-form {{ display: inline-flex; gap: 6px; }}
            audio {{ height: 32px; width: 220px; }}
            .banner {{ background: #1d4d32; color: #5ce69a; padding: 10px 16px; border-radius: 6px; margin-bottom: 16px; font-weight: 500; }}
            
            /* PROGRESS BAR */
            .progress-card {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-bottom: 18px; }}
            .progress-bar-bg {{ background: #262a32; height: 12px; border-radius: 6px; overflow: hidden; margin: 10px 0; }}
            .progress-bar-fill {{ background: #28a745; height: 100%; width: 0%; transition: width 0.3s ease; }}
            
            /* TABS */
            .tab-nav {{ display: flex; gap: 8px; margin-bottom: 14px; border-bottom: 1px solid var(--border); padding-bottom: 8px; flex-wrap: wrap; }}
            .tab-btn {{ background: transparent; color: var(--text-muted); border: 1px solid transparent; padding: 8px 16px; border-radius: 6px; font-size: 0.95rem; cursor: pointer; text-decoration: none; font-weight: 600; }}
            .tab-btn:hover {{ background: #20242b; color: #fff; }}
            .tab-btn.active {{ background: #2b7de9; color: #fff; }}
            
            /* SETTINGS MODAL */
            .settings-panel {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-bottom: 24px; }}
            .settings-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-top: 14px; }}
            .form-group {{ display: flex; flex-direction: column; gap: 6px; }}
            .form-group label {{ font-size: 0.85rem; font-weight: 600; color: var(--text-muted); }}
            .checkbox-group {{ display: flex; align-items: center; gap: 8px; font-size: 0.9rem; }}
            .checkbox-group input {{ width: 18px; height: 18px; cursor: pointer; }}
            .section-title {{ font-size: 1rem; font-weight: 700; margin-top: 10px; border-bottom: 1px solid var(--border); padding-bottom: 6px; display:flex; justify-content:space-between; align-items:center; }}
            .test-result {{ margin-top: 6px; font-size: 0.85rem; padding: 6px 10px; border-radius: 4px; display: none; }}
            .test-result.ok {{ display: block; background: #133824; color: #5ce69a; border: 1px solid #1f5938; }}
            .test-result.err {{ display: block; background: #421c1c; color: #ff8585; border: 1px solid #6b2d2d; }}
        </style>
    </head>
    <body>
        <div class="header">
            <div>
                <h1>🎵 Theme Fetcher</h1>
                <div class="stats">Filme: {filme_ok}/{total_filme} • Serien: {serien_ok}/{total_serien} • Collections: {cols_ok}/{total_cols}</div>
            </div>
            <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
                <button type="button" class="secondary" onclick="toggleSettings()">⚙️ Einstellungen</button>
                <form method="post" action="/scan">
                    <button type="submit">🔄 Jetzt scannen & laden</button>
                </form>
            </div>
        </div>

        {saved_banner}

        <!-- LIVE FORTSCHRITTSANZEIGE -->
        <div id="progressCard" class="progress-card" style="display:none;">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <strong id="progStep">Scan läuft...</strong>
                <span id="progCount" style="color:var(--text-muted); font-size:0.9rem;">0 / 0</span>
            </div>
            <div class="progress-bar-bg">
                <div id="progFill" class="progress-bar-fill"></div>
            </div>
            <div style="display:flex; justify-content:space-between; font-size:0.85rem; color:var(--text-muted);">
                <span id="progItem">Verarbeite...</span>
                <span>Neu heruntergeladen: <strong id="progDown" style="color:#5ce69a;">0</strong></span>
            </div>
        </div>

        <!-- EINSTELLUNGEN PANEL -->
        <div id="settingsBox" class="settings-panel" style="display: {'block' if settings == 'open' else 'none'};">
            <h2 style="margin: 0 0 10px 0; font-size: 1.2rem;">⚙️ Einstellungen & Quellen</h2>
            <form method="post" action="/settings">
                
                <!-- 1. LOKALE MEDIEN -->
                <div class="section-title">
                    <span>📁 1. Lokale Medienquellen (Festplatte)</span>
                    <button type="button" class="test" onclick="runTestFolder()">📂 Ordnerpfad testen</button>
                </div>
                <div class="settings-grid">
                    <div class="form-group" style="grid-column: 1 / -1;">
                        <label>Medienpfad(e) im Container</label>
                        <input type="text" id="cfg_paths" name="local_media_paths" value="{cfg.get('local_media_paths', '/media')}" placeholder="/media">
                        <small style="color:var(--text-muted);">Standard: <code>/media</code>. Wird mit deinen Docker Compose Volumes verknüpft.</small>
                        <div id="res_folder" class="test-result"></div>
                    </div>
                    <div class="checkbox-group">
                        <input type="checkbox" id="local_scan_enabled" name="local_scan_enabled" {'checked' if cfg.get('local_scan_enabled', True) else ''}>
                        <label for="local_scan_enabled">Lokalen Festplatten-Scan aktivieren</label>
                    </div>
                </div>

                <!-- 2. JELLYFIN & PLEX -->
                <div class="section-title" style="margin-top:20px;">
                    <span>🎬 2. Medienserver (Jellyfin / Plex)</span>
                    <div style="display:flex; gap:6px;">
                        <button type="button" class="test" onclick="runTestJellyfin()">🔌 Jellyfin testen</button>
                        <button type="button" class="test" onclick="runTestPlex()">🍿 Plex testen</button>
                    </div>
                </div>
                <div class="settings-grid">
                    <div class="form-group">
                        <label>Jellyfin Server-URL</label>
                        <input type="text" id="cfg_jf_url" name="jellyfin_url" value="{cfg.get('jellyfin_url', '')}" placeholder="http://192.168.178.X:8096">
                    </div>
                    <div class="form-group">
                        <label>Jellyfin API-Key</label>
                        <input type="text" id="cfg_jf_key" name="jellyfin_api_key" value="{cfg.get('jellyfin_api_key', '')}" placeholder="API-Key">
                    </div>
                    <div id="res_jf" class="test-result" style="grid-column: 1 / -1;"></div>

                    <div class="form-group">
                        <label>Plex Server-URL (Optional)</label>
                        <input type="text" id="cfg_plex_url" name="plex_url" value="{cfg.get('plex_url', '')}" placeholder="http://192.168.178.X:32400">
                    </div>
                    <div class="form-group">
                        <label>Plex Token (Optional)</label>
                        <input type="text" id="cfg_plex_token" name="plex_token" value="{cfg.get('plex_token', '')}" placeholder="X-Plex-Token">
                    </div>
                    <div id="res_plex" class="test-result" style="grid-column: 1 / -1;"></div>
                </div>

                <!-- 3. DOWNLOAD & AUTOMATION -->
                <div class="section-title" style="margin-top:20px;">⚡ 3. Download & Automatisierung</div>
                <div class="settings-grid">
                    <div class="checkbox-group">
                        <input type="checkbox" id="auto_download" name="auto_download" {'checked' if cfg.get('auto_download', True) else ''}>
                        <label for="auto_download">Automatisch nach fehlenden Themes suchen & laden</label>
                    </div>
                    <div class="form-group">
                        <label>Max. Titellänge (Sekunden)</label>
                        <input type="number" name="max_duration_sec" value="{cfg.get('max_duration_sec', 270)}">
                    </div>
                    <div class="form-group">
                        <label>Scan-Intervall (Stunden)</label>
                        <input type="number" name="scan_interval_hours" value="{cfg.get('scan_interval_hours', 12)}">
                    </div>
                </div>

                <div style="margin-top:20px; display:flex; gap:10px;">
                    <button type="submit" style="background:#28a745; font-size:1rem; padding:10px 20px;">💾 Einstellungen speichern & Neu scannen</button>
                    <button type="button" class="secondary" onclick="toggleSettings()">Schließen</button>
                </div>
            </form>
        </div>

        <!-- REITER / TABS (STRICKT GETRENNT) -->
        <div class="tab-nav">
            <a href="/?tab=filme" class="tab-btn {'active' if tab=='filme' else ''}">🎬 Filme ({filme_ok}/{total_filme})</a>
            <a href="/?tab=serien" class="tab-btn {'active' if tab=='serien' else ''}">📺 Serien ({serien_ok}/{total_serien})</a>
            <a href="/?tab=collections" class="tab-btn {'active' if tab=='collections' else ''}">📁 Collections ({cols_ok}/{total_cols})</a>
            <a href="/?tab=ordner" class="tab-btn {'active' if tab=='ordner' else ''}">📂 Eingelesene Ordner ({len(scanned_folders)})</a>
        </div>

        {'<!-- ORDNER TAB -->' if tab == 'ordner' else ''}
        <div style="display: {'block' if tab == 'ordner' else 'none'}; background:var(--card-bg); padding:20px; border-radius:8px;">
            <h3 style="margin-top:0;">Gefundene & Eingelesene Verzeichnisse:</h3>
            <p style="color:var(--text-muted); font-size:0.9rem;">Hier siehst du schwarz auf weiß, welche Ordner der Theme Fetcher über Festplatte oder Jellyfin/Plex erfasst hat:</p>
            <ul style="line-height:1.8; font-size:0.95rem;">
                {folder_list_html}
            </ul>
        </div>

        <!-- FILTER & TABELLE (NUR FÜR FILME/SERIEN/COLLECTIONS) -->
        <div style="display: {'none' if tab == 'ordner' else 'block'};">
            <div style="background:var(--card-bg); padding:12px; border-radius:8px; margin-bottom:14px;">
                <form method="get" action="/" style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
                    <input type="hidden" name="tab" value="{tab}">
                    <input type="text" name="q" value="{q}" placeholder="{tab.capitalize()} suchen..." style="flex:1; min-width:200px;">
                    <select name="filter_status" onchange="this.form.submit()">
                        <option value="all" {'selected' if filter_status=='all' else ''}>Alle</option>
                        <option value="missing" {'selected' if filter_status=='missing' else ''}>Nur Fehlende</option>
                        <option value="ok" {'selected' if filter_status=='ok' else ''}>Nur Vorhandene</option>
                    </select>
                    <button type="submit">Suchen</button>
                    {f'<a href="/?tab={tab}" class="btn secondary">Filter zurücksetzen</a>' if (q or filter_status!='all') else ''}
                </form>
            </div>

            <table>
                <thead>
                    <tr>
                        <th>Titel & Pfad & Status</th>
                        <th>Status</th>
                        <th>Vorschau</th>
                        <th>Aktion (Nächster Treffer / Link)</th>
                    </tr>
                </thead>
                <tbody>{rows or f'<tr><td colspan="4" style="text-align:center; padding:30px; color:var(--text-muted);">Keine Einträge unter {tab.capitalize()} gefunden. Klicke oben auf 🔄 Jetzt scannen.</td></tr>'}</tbody>
            </table>
        </div>

        <script>
            function toggleSettings() {{
                const el = document.getElementById('settingsBox');
                el.style.display = (el.style.display === 'none' || el.style.display === '') ? 'block' : 'none';
            }}

            // LIVE PROGRESS POLLING
            async function pollProgress() {{
                try {{
                    const resp = await fetch('/api/progress');
                    const data = await resp.json();
                    const card = document.getElementById('progressCard');
                    
                    if (data.is_running) {{
                        card.style.display = 'block';
                        document.getElementById('progStep').innerText = data.current_step;
                        document.getElementById('progCount').innerText = data.processed + ' / ' + data.total + ' (' + data.percent + '%)';
                        document.getElementById('progFill').style.width = data.percent + '%';
                        document.getElementById('progItem').innerText = data.current_item || '';
                        document.getElementById('progDown').innerText = data.new_downloaded;
                    }} else if (data.processed > 0 && data.processed === data.total) {{
                        // Just finished
                        card.style.display = 'block';
                        document.getElementById('progStep').innerText = data.current_step;
                        document.getElementById('progFill').style.width = '100%';
                        document.getElementById('progDown').innerText = data.new_downloaded;
                    }}
                }} catch (e) {{}}
            }}

            setInterval(pollProgress, 1500);
            pollProgress();

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
                    resEl.innerText = '✕ Server-Anfrage fehlgeschlagen: ' + e;
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
                    resEl.innerText = '✕ Server-Anfrage fehlgeschlagen: ' + e;
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
                    resEl.innerText = '✕ Server-Anfrage fehlgeschlagen: ' + e;
                }}
            }}
        </script>
    </body>
    </html>
    """
