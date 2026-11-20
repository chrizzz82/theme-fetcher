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
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
import xml.etree.ElementTree as ET

app = FastAPI(title="Theme Fetcher Alpha")

# --- Konfiguration & Umgebungsvariablen ---
JELLYFIN_URL = os.getenv("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_API_KEY = os.getenv("JELLYFIN_API_KEY", "")
JF_METADATA_DIR = os.getenv("JF_METADATA_DIR", "/jellyfin-metadata")

PLEX_URL = os.getenv("PLEX_URL", "").rstrip("/")
PLEX_TOKEN = os.getenv("PLEX_TOKEN", "")

EMBY_URL = os.getenv("EMBY_URL", "").rstrip("/")
EMBY_API_KEY = os.getenv("EMBY_API_KEY", "")

LOCAL_SCAN_ENABLED = os.getenv("LOCAL_SCAN_ENABLED", "false").lower() in ("true", "1", "yes")
LOCAL_MEDIA_DIR = Path(os.getenv("LOCAL_MEDIA_DIR", "/media"))

MAX_DURATION = int(os.getenv("MAX_DURATION_SEC", "270"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL_HOURS", "12"))

DATA_DIR = Path("/data")
COLLECTIONS_DIR = DATA_DIR / "collections"
DATA_DIR.mkdir(parents=True, exist_ok=True)
COLLECTIONS_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"

EXCLUDE_KEYWORDS = [
    "cover", "remix", "reaction", "reaktion", "1 hour", "10 hours", "loop",
    "tutorial", "piano", "klavier", "guitar", "gitarre", "karaoke",
    "full soundtrack", "full ost", "trailer", "ganzer film", "hörspiel",
    "review", "kritik", "analys"
]

state_lock = threading.Lock()
is_scanning = False


def slugify(text: str) -> str:
    clean = re.sub(r"[^\w\s-]", "", str(text), flags=re.UNICODE).strip().lower()
    return re.sub(r"[-\s]+", "_", clean) or "item"


def parse_folder_name(name: str):
    m = re.match(r"^(?P<title>.+?)\s*\((?P<year>\d{4})\)$", name.strip())
    if m:
        return m.group("title").strip(), m.group("year")
    return name.strip(), None


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"items": {}, "blacklist": {}}


def save_state(state: dict):
    with state_lock:
        STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def find_jellyfin_boxset_dir(title: str) -> Optional[Path]:
    base = Path(JF_METADATA_DIR)
    if not base.exists():
        return None
    target_slug = slugify(title)
    try:
        for p in base.rglob("*"):
            if p.is_dir() and slugify(p.name) == target_slug:
                if any(k in str(p).lower() for k in ("boxset", "collection", "metadata")):
                    return p
    except Exception:
        pass
    return None


def sync_collection_theme(item: dict, mp3_path: Path):
    if item.get("type") != "Collection" or not mp3_path.exists():
        return
    jf_dir = find_jellyfin_boxset_dir(item["title"])
    if jf_dir:
        try:
            shutil.copy2(mp3_path, jf_dir / "theme.mp3")
        except Exception as e:
            print(f"[Collection Sync Warnung] {e}")


# --- 1. Jellyfin API ---
def fetch_jellyfin_items() -> List[dict]:
    if not JELLYFIN_URL or not JELLYFIN_API_KEY:
        return []
    headers = {"X-Emby-Token": JELLYFIN_API_KEY}
    results = []
    try:
        users_resp = requests.get(f"{JELLYFIN_URL}/Users", headers=headers, timeout=10)
        users_resp.raise_for_status()
        users = users_resp.json()
        if not users:
            return []
        user_id = users[0]["Id"]

        params = {
            "IncludeItemTypes": "Movie,Series,BoxSet",
            "Recursive": "true",
            "Fields": "Path,ProductionYear,OriginalTitle"
        }
        resp = requests.get(f"{JELLYFIN_URL}/Users/{user_id}/Items", headers=headers, params=params, timeout=20)
        resp.raise_for_status()

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
            p = Path(raw_path)
            target_dir = p.parent if item_type == "Movie" else p
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
        print(f"[Jellyfin Fehler] {e}")
    return results


# --- 2. Plex API ---
def fetch_plex_items() -> List[dict]:
    if not PLEX_URL or not PLEX_TOKEN:
        return []
    headers = {"X-Plex-Token": PLEX_TOKEN, "Accept": "application/xml"}
    results = []
    try:
        sec_resp = requests.get(f"{PLEX_URL}/library/sections", headers=headers, timeout=10)
        sec_resp.raise_for_status()
        root = ET.fromstring(sec_resp.text)

        for directory in root.findall("Directory"):
            sec_id = directory.attrib.get("key")
            sec_type = directory.attrib.get("type")
            if sec_type not in ("movie", "show"):
                continue

            items_resp = requests.get(f"{PLEX_URL}/library/sections/{sec_id}/all", headers=headers, timeout=20)
            items_root = ET.fromstring(items_resp.text)

            if sec_type == "movie":
                for video in items_root.findall("Video"):
                    part = video.find(".//Part")
                    if part is not None and "file" in part.attrib:
                        p = Path(part.attrib["file"]).parent
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
                        results.append({
                            "id": f"plex_{show.attrib.get('ratingKey')}",
                            "source": "Plex",
                            "type": "Serie",
                            "title": show.attrib.get("title", "Unbekannt"),
                            "original_title": show.attrib.get("originalTitle", ""),
                            "year": show.attrib.get("year"),
                            "dir": loc.attrib["path"]
                        })

            try:
                col_resp = requests.get(f"{PLEX_URL}/library/sections/{sec_id}/collections", headers=headers, timeout=15)
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
                print(f"[Plex Collections Warnung Sektion {sec_id}] {ce}")

    except Exception as e:
        print(f"[Plex Fehler] {e}")
    return results


# --- 3. Emby API ---
def fetch_emby_items() -> List[dict]:
    if not EMBY_URL or not EMBY_API_KEY:
        return []
    headers = {"X-Emby-Token": EMBY_API_KEY}
    results = []
    try:
        users_resp = requests.get(f"{EMBY_URL}/Users", headers=headers, timeout=10)
        users_resp.raise_for_status()
        users = users_resp.json()
        if not users:
            return []
        user_id = users[0]["Id"]

        params = {
            "IncludeItemTypes": "Movie,Series,BoxSet",
            "Recursive": "true",
            "Fields": "Path,ProductionYear,OriginalTitle"
        }
        resp = requests.get(f"{EMBY_URL}/Users/{user_id}/Items", headers=headers, params=params, timeout=20)
        resp.raise_for_status()

        for item in resp.json().get("Items", []):
            item_type = item.get("Type")
            title = item.get("Name", "Unbekannt")
            item_id = f"emby_{item['Id']}"

            if item_type == "BoxSet":
                col_dir = COLLECTIONS_DIR / f"{item_id}_{slugify(title)}"
                col_dir.mkdir(parents=True, exist_ok=True)
                results.append({
                    "id": item_id,
                    "source": "Emby",
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
            p = Path(raw_path)
            target_dir = p.parent if item_type == "Movie" else p
            results.append({
                "id": item_id,
                "source": "Emby",
                "type": "Serie" if item_type == "Series" else "Film",
                "title": title,
                "original_title": item.get("OriginalTitle") or "",
                "year": item.get("ProductionYear"),
                "dir": str(target_dir)
            })
    except Exception as e:
        print(f"[Emby Fehler] {e}")
    return results


# --- 4. Lokaler / Universeller Ordner-Scan (Kodi, Infuse, etc.) ---
def fetch_local_items() -> List[dict]:
    if not LOCAL_SCAN_ENABLED or not LOCAL_MEDIA_DIR.exists():
        return []
    results = []
    video_exts = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".iso"}
    try:
        for root, dirs, files in os.walk(LOCAL_MEDIA_DIR):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in ("@eadir", "extrafanart", "sample", ".actors")]
            p_root = Path(root)
            if p_root == LOCAL_MEDIA_DIR:
                continue

            has_seasons = any(d.lower().startswith(("season", "staffel")) for d in dirs)
            has_videos = any(Path(f).suffix.lower() in video_exts for f in files)

            # Skip Sub-Season-Folders like "Season 1"
            if p_root.name.lower().startswith(("season ", "staffel ", "season", "staffel")):
                continue

            is_serie = has_seasons or any(s in str(p_root).lower() for s in ("serie", "show"))
            is_movie = has_videos and not has_seasons and any(m in str(p_root).lower() for m in ("film", "movie"))

            if has_seasons or has_videos:
                title, year = parse_folder_name(p_root.name)
                mtype = "Serie" if is_serie else "Film"
                rel_id = slugify(str(p_root.relative_to(LOCAL_MEDIA_DIR)))
                results.append({
                    "id": f"local_{rel_id}",
                    "source": "Lokal",
                    "type": mtype,
                    "title": title,
                    "original_title": "",
                    "year": year,
                    "dir": str(p_root)
                })
                if not has_seasons:
                    dirs.clear()
    except Exception as e:
        print(f"[Lokaler Scan Fehler] {e}")
    return results


# --- Such- & Download-Logik ---
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


def find_candidate(search_expr: str, blacklist: List[str]) -> Optional[dict]:
    cmd = [
        "yt-dlp",
        search_expr,
        "--dump-json",
        "--flat-playlist",
        "--no-warnings"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    for line in res.stdout.strip().splitlines():
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
        if duration and duration > MAX_DURATION:
            continue
        if any(bad in lower_title for bad in EXCLUDE_KEYWORDS):
            continue

        return {"id": vid_id or url, "url": url, "title": vid_title}
    return None


def search_and_download(item: dict, custom_query_or_url: Optional[str] = None, blacklist: List[str] = None) -> dict:
    blacklist = blacklist or []
    target_dir = Path(item["dir"])
    target_dir.mkdir(parents=True, exist_ok=True)

    output_mp3 = target_dir / "theme.mp3"
    temp_template = str(target_dir / "theme_temp.%(ext)s")

    for old_tmp in target_dir.glob("theme_temp.*"):
        old_tmp.unlink(missing_ok=True)

    is_direct_url = custom_query_or_url and (
        custom_query_or_url.startswith("http://") or custom_query_or_url.startswith("https://")
    )

    chosen_video = None

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
                chosen_video = find_candidate(expr, blacklist)
                if chosen_video:
                    break
        except Exception as e:
            return {"ok": False, "msg": f"Suchfehler: {e}"}

    if not chosen_video:
        return {"ok": False, "msg": "Kein passender Treffer (DE/Orig/SC)"}

    try:
        dl_cmd = [
            "yt-dlp",
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "192K",
            "-o", temp_template,
            chosen_video["url"]
        ]
        subprocess.run(dl_cmd, check=True, capture_output=True, text=True, timeout=90)
        downloaded = target_dir / "theme_temp.mp3"
        if downloaded.exists():
            if output_mp3.exists():
                output_mp3.unlink()
            downloaded.rename(output_mp3)
            sync_collection_theme(item, output_mp3)
            return {
                "ok": True,
                "video_id": chosen_video["id"],
                "video_title": chosen_video["title"]
            }
        return {"ok": False, "msg": "MP3-Konvertierung fehlgeschlagen"}
    except Exception as e:
        return {"ok": False, "msg": f"Downloadfehler: {e}"}


def run_full_scan(auto_download: bool = True):
    global is_scanning
    if is_scanning:
        return
    is_scanning = True
    try:
        state = load_state()
        raw_items = (
            fetch_jellyfin_items() +
            fetch_plex_items() +
            fetch_emby_items() +
            fetch_local_items()
        )

        by_dir = {}
        for it in raw_items:
            norm_dir = str(Path(it["dir"]).resolve())
            if norm_dir in by_dir:
                # Prioritize dedicated server items over generic local crawler
                if by_dir[norm_dir].get("source") == "Lokal" and it.get("source") != "Lokal":
                    by_dir[norm_dir] = it
            else:
                by_dir[norm_dir] = it

        for dir_path, it in by_dir.items():
            item_id = it["id"]
            existing = state["items"].get(item_id, {})
            mp3_path = Path(dir_path) / "theme.mp3"
            theme_exists = mp3_path.exists()

            if theme_exists and it.get("type") == "Collection":
                sync_collection_theme(it, mp3_path)

            record = {
                **it,
                "has_theme": theme_exists,
                "video_id": existing.get("video_id", ""),
                "video_title": existing.get("video_title", ""),
                "status": "Vorhanden" if theme_exists else existing.get("status", "Fehlt")
            }

            if auto_download and not theme_exists:
                bl = state["blacklist"].get(item_id, [])
                dl_res = search_and_download(it, blacklist=bl)
                if dl_res["ok"]:
                    record["has_theme"] = True
                    record["video_id"] = dl_res["video_id"]
                    record["video_title"] = dl_res["video_title"]
                    record["status"] = "Heruntergeladen"
                else:
                    record["status"] = dl_res["msg"]

            state["items"][item_id] = record
            save_state(state)
    finally:
        is_scanning = False


def background_scheduler():
    while True:
        time.sleep(10)
        run_full_scan(auto_download=True)
        time.sleep(max(1, SCAN_INTERVAL) * 3600)


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


@app.post("/scan")
def trigger_scan():
    threading.Thread(target=run_full_scan, kwargs={"auto_download": True}, daemon=True).start()
    return RedirectResponse("/", status_code=303)


@app.post("/replace/{item_id}")
def replace_theme(item_id: str, custom_input: str = Form("")):
    state = load_state()
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
        blacklist=state["blacklist"].get(item_id, [])
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
def index(q: str = "", filter_status: str = "all", filter_type: str = "all", filter_source: str = "all"):
    state = load_state()
    items = list(state["items"].values())
    items.sort(key=lambda x: (x.get("has_theme", False), x.get("title", "").lower()))

    if q:
        items = [
            i for i in items
            if q.lower() in i.get("title", "").lower() or q.lower() in (i.get("original_title") or "").lower()
        ]
    if filter_status == "missing":
        items = [i for i in items if not i.get("has_theme")]
    elif filter_status == "ok":
        items = [i for i in items if i.get("has_theme")]
    if filter_type != "all":
        items = [i for i in items if i.get("type") == filter_type]
    if filter_source != "all":
        items = [i for i in items if i.get("source") == filter_source]

    total = len(state["items"])
    present = sum(1 for i in state["items"].values() if i.get("has_theme"))
    col_count = sum(1 for i in state["items"].values() if i.get("type") == "Collection")

    sources = sorted(list({i.get("source", "Unbekannt") for i in state["items"].values()}))

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
        rows += f"""
        <tr>
            <td><strong>{it['title']}</strong>{orig_hint} <small>({it.get('year') or '-'})</small><br>
                <span class="meta">{it['type']} • <span class="src-tag">{it['source']}</span> • {it.get('video_title') or it.get('status')}</span>
            </td>
            <td>{badge}</td>
            <td>{player}</td>
            <td>
                <form method="post" action="/replace/{iid}" class="inline-form">
                    <input type="text" name="custom_input" placeholder="Leer = nächster Treffer / Suchbegriff / URL">
                    <button type="submit">Laden / Neu</button>
                </form>
                <form method="post" action="/delete/{iid}" style="display:inline;">
                    <button type="submit" class="danger" title="Löschen & Track blockieren">✕</button>
                </form>
            </td>
        </tr>
        """

    source_options = '<option value="all">Alle Quellen</option>'
    for s in ("Jellyfin", "Plex", "Emby", "Lokal"):
        sel = 'selected' if filter_source == s else ''
        source_options += f'<option value="{s}" {sel}>{s}</option>'

    return f"""
    <!doctype html>
    <html lang="de">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Theme Fetcher Alpha</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #121417; color: #e4e6eb; margin: 0; padding: 24px; }}
            .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; flex-wrap: wrap; gap: 12px; }}
            h1 {{ margin: 0; font-size: 1.5rem; letter-spacing: -0.5px; }}
            .stats {{ color: #9aa0a6; font-size: 0.95rem; margin-top: 4px; }}
            button {{ background: #2b7de9; color: white; border: none; padding: 8px 14px; border-radius: 6px; cursor: pointer; font-weight: 500; transition: background 0.2s; }}
            button:hover {{ background: #1b66c9; }}
            button.danger {{ background: #c93b3b; padding: 8px 10px; }}
            button.danger:hover {{ background: #a32a2a; }}
            input[type="text"], select {{ background: #1e2126; border: 1px solid #343840; color: #fff; padding: 7px 10px; border-radius: 6px; outline: none; }}
            input[type="text"]:focus, select:focus {{ border-color: #2b7de9; }}
            table {{ width: 100%; border-collapse: collapse; background: #181b20; border-radius: 8px; overflow: hidden; }}
            th, td {{ padding: 12px 14px; border-bottom: 1px solid #262a32; text-align: left; vertical-align: middle; }}
            th {{ background: #1f2329; font-size: 0.85rem; text-transform: uppercase; color: #9aa0a6; }}
            .meta {{ font-size: 0.8rem; color: #8b929a; }}
            .src-tag {{ background: #2b303a; color: #79a8f5; padding: 2px 6px; border-radius: 4px; font-weight: 600; font-size: 0.75rem; }}
            .badge {{ padding: 3px 8px; border-radius: 4px; font-size: 0.8rem; font-weight: 600; }}
            .badge.ok {{ background: #1d4d32; color: #5ce69a; }}
            .badge.miss {{ background: #4d2323; color: #ff7b7b; }}
            .inline-form {{ display: inline-flex; gap: 6px; }}
            audio {{ height: 32px; width: 220px; }}
        </style>
    </head>
    <body>
        <div class="header">
            <div>
                <h1>🎵 Theme Fetcher <span style="font-size:0.75rem; vertical-align:middle; background:#343840; padding:2px 8px; border-radius:10px;">Alpha</span></h1>
                <div class="stats">{present} von {total} Themes vorhanden ({col_count} Collections) {'• ⏳ Scan läuft...' if is_scanning else ''}</div>
            </div>
            <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
                <form method="get" action="/" style="display:flex; gap:8px; flex-wrap:wrap;">
                    <input type="text" name="q" value="{q}" placeholder="Titel / Originaltitel...">
                    <select name="filter_source" onchange="this.form.submit()">
                        {source_options}
                    </select>
                    <select name="filter_type" onchange="this.form.submit()">
                        <option value="all" {'selected' if filter_type=='all' else ''}>Alle Typen</option>
                        <option value="Film" {'selected' if filter_type=='Film' else ''}>Filme</option>
                        <option value="Serie" {'selected' if filter_type=='Serie' else ''}>Serien</option>
                        <option value="Collection" {'selected' if filter_type=='Collection' else ''}>Collections</option>
                    </select>
                    <select name="filter_status" onchange="this.form.submit()">
                        <option value="all" {'selected' if filter_status=='all' else ''}>Alle Status</option>
                        <option value="missing" {'selected' if filter_status=='missing' else ''}>Nur Fehlende</option>
                        <option value="ok" {'selected' if filter_status=='ok' else ''}>Nur Vorhandene</option>
                    </select>
                    <button type="submit">Filtern</button>
                </form>
                <form method="post" action="/scan">
                    <button type="submit">🔄 Jetzt scannen</button>
                </form>
            </div>
        </div>
        <table>
            <thead>
                <tr>
                    <th>Titel [Originaltitel]</th>
                    <th>Status</th>
                    <th>Vorschau</th>
                    <th>Aktion (Nächster Treffer / Suchbegriff / URL)</th>
                </tr>
            </thead>
            <tbody>{rows or '<tr><td colspan="4">Keine Medienelemente gefunden. Bitte Verbindungsdaten oder Dateipfade prüfen.</td></tr>'}</tbody>
        </table>
    </body>
    </html>
    """
