"""amdl MCP server.

Exposes the local Apple Music download stack to Claude Code:
  * amdl.exe (the downloader in the root of this repository) for downloading/decrypting,
  * wrapper-lite in Docker (see mcp/wrapper-lite/compose.yaml) as amdl's decryption backend,
  * the Apple Music catalog API for search / album / artist lookups,
  * the local foobar2000 playlists and music folder, to see what is already owned.

Downloads run as background jobs (one at a time, queued) because an album can take
minutes; poll them with amdl_jobs.

stdout belongs to the MCP stdio transport: never print() in this file.
"""

from __future__ import annotations

import base64
import functools
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import mutagen
from mcp.server.mcpserver import MCPServer

HERE = Path(__file__).resolve().parent
AMDL_DIR = Path(os.environ.get("AMDL_DIR", str(HERE.parent)))  # repo root: amdl.exe, config.yaml, save folders
AMDL_EXE = Path(os.environ.get("AMDL_EXE", str(AMDL_DIR / "amdl.exe")))
WRAPPER_DIR = Path(os.environ.get("WRAPPER_DIR", str(HERE / "wrapper-lite")))
WRAPPER_CONTAINER = os.environ.get("WRAPPER_CONTAINER", "wrapper-lite")
DOCKER_DESKTOP_EXE = Path(os.environ.get("DOCKER_DESKTOP_EXE", r"C:\Program Files\Docker\Docker\Docker Desktop.exe"))
FOOBAR_PROFILE = Path(os.environ.get("FOOBAR_PROFILE", str(Path(os.environ.get("APPDATA", Path.home())) / "foobar2000-v2")))
MUSIC_ROOT = Path(os.environ.get("MUSIC_ROOT", str(Path.home() / "Music")))
STATE_DIR = Path(os.environ.get("AMDL_MCP_STATE", str(HERE / "state")))
JOBS_DIR = STATE_DIR / "jobs"
# Hosts amdl reaches without the HTTPS_PROXY inherited from Claude Code. Behind a proxy the
# audio CDN can crawl (~30 kB/s, with stream resets) while a direct connection reaches a
# nearby Apple node (~5 MB/s). Apple API calls still use the proxy. "" disables this.
DIRECT_HOSTS = os.environ.get("AMDL_DIRECT_HOSTS", "aod.itunes.apple.com,mzstatic.com")
# Post-download check. When the CDN stream ends early, amdl still reports "Decrypted / Audio integrity
# intact" and writes a short file (seen: 60 s of a 418 s track). Catalog durationInMillis matches amdl's
# files to the millisecond, so a file more than VERIFY_TOLERANCE_S shorter is treated as truncated.
VERIFY_TOLERANCE_S = 1.5
AUTO_RETRY = int(os.environ.get("AMDL_AUTO_RETRY", "2"))  # extra runs for failed / truncated / missing tracks
QUARANTINE_DIR = STATE_DIR / "quarantine"  # truncated files are moved here (amdl skips files that exist)
AUDIO_EXT = {".m4a", ".mp4", ".flac", ".alac", ".wav", ".aiff", ".mp3", ".opus", ".ogg"}


def _amdl_env() -> dict[str, str]:
    env = dict(os.environ)
    extra = [h.strip() for h in DIRECT_HOSTS.split(",") if h.strip()]
    if extra:
        cur = env.get("NO_PROXY") or env.get("no_proxy") or "localhost,127.0.0.1,::1"
        hosts = [h.strip() for h in cur.split(",") if h.strip()]
        merged = ",".join(hosts + [h for h in extra if h not in hosts])
        env.pop("no_proxy", None)  # Windows env names are case-insensitive; keep a single key
        env["NO_PROXY"] = merged
    return env

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
AMP = "https://amp-api.music.apple.com/v1/catalog"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
URL_RE = re.compile(
    r"https?://(?:beta\.)?music\.apple\.com/([a-z]{2})/(album|song|playlist|artist|music-video|station)/(?:[^/?#]+/)?([^/?#]+)"
)

server = MCPServer(
    "amdl",
    instructions=(
        "Apple Music search and lossless/Atmos downloads via the local amdl + wrapper-lite setup, "
        "plus the user's foobar2000 playlists and music folder.\n"
        "Typical flow: amdl_status (call wrapper_start if wrapper-lite is down) -> apple_music_search / "
        "apple_music_album / apple_music_artist_albums to get URLs -> amdl_download -> poll amdl_jobs.\n"
        "Finished jobs are checked against the catalog durations and retried automatically; when amdl_jobs shows "
        "latest_retry, poll that job for the final result. Report verify problems to the user as they are.\n"
        "Use foobar_library to check what the user already owns before downloading.\n"
        "The wrapper-lite account serves only the storefronts listed in amdl_status.regions; prefer "
        "URLs from those storefronts. Only download content the user is entitled to."
    ),
)


# --------------------------------------------------------------------------- helpers


def tool(fn):
    """Register fn as an MCP tool that reports failures as {"ok": False, "error": ...}:
    the SDK otherwise hides the exception behind a bare "Error executing tool"."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    return server.tool()(wrapper)


def _http_get(url: str, headers: dict[str, str] | None = None, timeout: float = 20, direct: bool = False) -> bytes:
    """GET with the environment proxy (HTTPS_PROXY), or without any proxy when direct=True."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if direct else urllib.request.build_opener()
    with opener.open(req, timeout=timeout) as resp:
        return resp.read()


def _run(cmd: list[str], cwd: Path | None = None, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL,
    )


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_url(url: str) -> tuple[str, str, str] | None:
    """-> (storefront, kind, id) for a music.apple.com URL; song id comes from ?i= on album URLs."""
    m = URL_RE.match(url.strip())
    if not m:
        return None
    sf, kind, ident = m.groups()
    return sf, kind, ident


def _amdl_config_text() -> str:
    p = AMDL_DIR / "config.yaml"
    if not p.exists():
        p = AMDL_DIR / "config.yaml.example"
    return p.read_text(encoding="utf-8")


def _config_str(key: str) -> str | None:
    """First `key: "value"` in amdl's config.yaml (keys used here are unique across its sections)."""
    m = re.search(rf'^\s*{re.escape(key)}:\s*"([^"]*)"', _amdl_config_text(), re.M)
    return m.group(1) if m else None


def _save_folders() -> dict[str, str | None]:
    paths = {k: _config_str(k) for k in ("alac", "atmos", "aac", "mv")}
    return {k: (str(AMDL_DIR / v) if v and not Path(v).is_absolute() else v) for k, v in paths.items()}


def _lite_server() -> str:
    m = re.search(r'^\s*lite-server:\s*"?([^"\s#]+)"?', _amdl_config_text(), re.M)
    return (m.group(1) if m else "http://127.0.0.1:12340").rstrip("/")


def _lite_status() -> dict[str, Any]:
    url = _lite_server() + "/status"
    try:
        data = json.loads(_http_get(url, timeout=5, direct=True))
        return {"reachable": True, "url": url, "regions": (data.get("data") or {}).get("regions", [])}
    except Exception as e:  # noqa: BLE001
        return {"reachable": False, "url": url, "error": str(e)}


def _docker_engine_up() -> bool:
    try:
        return _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=20).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _container_state() -> str:
    try:
        r = _run(["docker", "inspect", "-f", "{{.State.Status}}", WRAPPER_CONTAINER], timeout=20)
        return r.stdout.strip() if r.returncode == 0 else "not created"
    except Exception as e:  # noqa: BLE001
        return f"unknown ({e})"


def _container_needs_login() -> bool:
    try:
        r = _run(["docker", "logs", "--tail", "50", WRAPPER_CONTAINER], timeout=20)
        return "run --login first" in (r.stdout + r.stderr)
    except Exception:  # noqa: BLE001
        return False


LOGIN_HELP = (
    "wrapper-lite has no Apple Music login. Re-login manually (needs the Apple ID password and a 2FA code): "
    f"in {WRAPPER_DIR}: fill AM_PASSWORD in .env, `docker compose stop`, `docker compose run --rm login`, "
    'then `docker exec wrapper-lite-login sh -c "printf %s <code> > /app/rootfs/data/2fa.txt"`, '
    "clear AM_PASSWORD, `docker compose up -d`."
)


# --------------------------------------------------------------------------- Apple Music catalog API

_token_lock = threading.Lock()
_token: dict[str, Any] = {"value": None, "exp": 0.0}
_token_file = STATE_DIR / "web_token.json"


def _jwt_exp(tok: str) -> float:
    try:
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
    except Exception:  # noqa: BLE001
        return 0.0


def _apple_token(force: bool = False) -> str:
    """Public web-player bearer token, scraped the same way amdl does (internal/amp-api/token.go)."""
    with _token_lock:
        if not force and _token["value"] and _token["exp"] - 3600 > time.time():
            return _token["value"]
        if not force and _token_file.exists():
            try:
                cached = json.loads(_token_file.read_text(encoding="utf-8"))
                if cached.get("exp", 0) - 3600 > time.time():
                    _token.update(cached)
                    return cached["value"]
            except Exception:  # noqa: BLE001
                pass
        html = _http_get("https://music.apple.com").decode("utf-8", "replace")
        m = re.search(r'/assets/index~[^/"\']+\.js', html) or re.search(r'/assets/index[^/"\']*\.js', html)
        if not m:
            raise RuntimeError("could not find the web player bundle on music.apple.com")
        js = _http_get("https://music.apple.com" + m.group(0)).decode("utf-8", "replace")
        t = re.search(r"eyJ[A-Za-z0-9\-_=]+\.[A-Za-z0-9\-_=]+\.[A-Za-z0-9\-_=]+", js)
        if not t:
            raise RuntimeError("could not find a bearer token in the web player bundle")
        tok = t.group(0)
        _token.update(value=tok, exp=_jwt_exp(tok) or time.time() + 86400)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _token_file.write_text(json.dumps(_token), encoding="utf-8")
        return tok


def _amp(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    qs = ("?" + urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})) if params else ""
    url = AMP + path + qs
    refreshed = False
    last: Exception | None = None
    for attempt in range(4):
        headers = {"Authorization": f"Bearer {_apple_token()}", "Origin": "https://music.apple.com"}
        try:
            return json.loads(_http_get(url, headers=headers))
        except urllib.error.HTTPError as e:
            if e.code == 401 and not refreshed:
                refreshed = True  # token rotated: drop the cache so the next attempt scrapes a fresh one
                _token.update(value=None, exp=0.0)
                _token_file.unlink(missing_ok=True)
                continue
            if e.code in (429, 500, 502, 503, 504):
                last = e
                time.sleep(1.5 * (attempt + 1))
                continue
            body = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"Apple Music API {e.code} for {path}: {body}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Apple Music API request failed after retries for {path}: {last}")


def _year(date: str | None) -> str:
    return (date or "")[:4]


def _album_item(d: dict[str, Any]) -> dict[str, Any]:
    a = d.get("attributes", {})
    return {
        "id": d.get("id"),
        "name": a.get("name"),
        "artist": a.get("artistName"),
        "released": a.get("releaseDate"),
        "tracks": a.get("trackCount"),
        "audio": a.get("audioTraits", []),
        "apple_digital_master": a.get("isMasteredForItunes", False),
        "rating": a.get("contentRating"),
        "single": a.get("isSingle", False),
        "url": a.get("url"),
    }


def _song_item(d: dict[str, Any]) -> dict[str, Any]:
    a = d.get("attributes", {})
    return {
        "id": d.get("id"),
        "name": a.get("name"),
        "artist": a.get("artistName"),
        "album": a.get("albumName"),
        "released": a.get("releaseDate"),
        "disc": a.get("discNumber"),
        "track": a.get("trackNumber"),
        "duration_s": round((a.get("durationInMillis") or 0) / 1000),
        "audio": a.get("audioTraits", []),
        "url": a.get("url"),
    }


def _all_tracks(d: dict[str, Any]) -> list[dict[str, Any]]:
    """Every track of an album / playlist resource, following the tracks relationship past its first page."""
    rel = d.get("relationships", {}).get("tracks", {})
    items = list(rel.get("data", []))
    nxt = rel.get("next")
    while nxt and len(items) < 1000:
        page = _amp(nxt.replace("/v1/catalog", "", 1).split("?")[0], dict(urllib.parse.parse_qsl(urllib.parse.urlparse(nxt).query)))
        items += page.get("data", [])
        nxt = page.get("next")
    return items


def _resolve_id(url_or_id: str, want: str, storefront: str | None) -> tuple[str, str]:
    parsed = _parse_url(url_or_id)
    if parsed:
        sf, kind, ident = parsed
        if kind != want:
            raise ValueError(f"expected a {want} URL, got a {kind} URL")
        return storefront or sf, ident
    if not re.fullmatch(r"[0-9]+|pl\.[0-9a-zA-Z.\-]+", url_or_id.strip()):
        raise ValueError("pass a music.apple.com URL or a numeric catalog id")
    return storefront or "us", url_or_id.strip()


# --------------------------------------------------------------------------- tools: status


@tool
def amdl_status() -> dict[str, Any]:
    """Health of the download stack: amdl.exe, Docker engine, wrapper-lite container, its /status
    (regions = storefronts the logged-in account can download from), and the save folders from amdl's config."""
    paths = _save_folders()
    engine = _docker_engine_up()
    state = _container_state() if engine else "docker engine not running"
    lite = _lite_status()
    out: dict[str, Any] = {
        "amdl_exe": str(AMDL_EXE),
        "amdl_exe_exists": AMDL_EXE.exists(),
        "docker_engine": engine,
        "wrapper_container": state,
        "wrapper_lite": lite,
        "save_folders": paths,
        "ready": AMDL_EXE.exists() and lite.get("reachable", False) and bool(lite.get("regions")),
    }
    if engine and state == "running" and lite.get("reachable") and not lite.get("regions"):
        out["hint"] = LOGIN_HELP
    elif not out["ready"]:
        out["hint"] = "Call wrapper_start to start Docker Desktop and the wrapper-lite container."
    return out


@tool
def wrapper_start() -> dict[str, Any]:
    """Start Docker Desktop if needed, then `docker compose up -d` the wrapper-lite container and wait for
    its /status. Does not log in; if the account login is missing it returns re-login instructions."""
    steps: list[str] = []
    if not _docker_engine_up():
        if not DOCKER_DESKTOP_EXE.exists():
            return {"ok": False, "error": f"Docker engine is down and {DOCKER_DESKTOP_EXE} was not found"}
        subprocess.Popen([str(DOCKER_DESKTOP_EXE)], creationflags=NO_WINDOW)
        steps.append("started Docker Desktop")
        deadline = time.time() + 180
        while time.time() < deadline and not _docker_engine_up():
            time.sleep(3)
        if not _docker_engine_up():
            return {"ok": False, "steps": steps, "error": "Docker engine did not come up within 180 s"}
        steps.append("docker engine ready")
    r = _run(["docker", "compose", "up", "-d"], cwd=WRAPPER_DIR, timeout=180)
    steps.append(f"docker compose up -d -> exit {r.returncode}")
    if r.returncode != 0:
        return {"ok": False, "steps": steps, "error": (r.stderr or r.stdout)[-1500:]}
    deadline = time.time() + 90
    lite = _lite_status()
    while time.time() < deadline and not (lite.get("reachable") and lite.get("regions")):
        if _container_needs_login():
            return {"ok": False, "steps": steps, "wrapper_lite": lite, "error": LOGIN_HELP}
        time.sleep(3)
        lite = _lite_status()
    ok = bool(lite.get("reachable") and lite.get("regions"))
    return {"ok": ok, "steps": steps, "wrapper_lite": lite, **({} if ok else {"error": "wrapper-lite did not report regions in 90 s; check `docker compose logs` in " + str(WRAPPER_DIR)})}


# --------------------------------------------------------------------------- tools: catalog


@tool
def apple_music_search(
    query: str,
    types: str = "albums",
    storefront: str = "us",
    limit: int = 10,
    language: str | None = None,
) -> dict[str, Any]:
    """Search the Apple Music catalog. types: comma list of albums, songs, artists, playlists, music-videos.
    Album/song results include `audio` traits (lossless, hi-res-lossless, atmos, ...) and a `url` that
    amdl_download accepts. language: optional catalog language such as "ja", "zh-Hans-CN", "en-US"."""
    limit = max(1, min(int(limit), 25))
    data = _amp(f"/{storefront}/search", {"term": query, "types": types, "limit": limit, "l": language})
    res = data.get("results", {})
    out: dict[str, Any] = {"storefront": storefront}
    if "albums" in res:
        out["albums"] = [_album_item(d) for d in res["albums"].get("data", [])]
    if "songs" in res:
        out["songs"] = [_song_item(d) for d in res["songs"].get("data", [])]
    if "artists" in res:
        out["artists"] = [
            {"id": d.get("id"), "name": d["attributes"].get("name"), "genres": d["attributes"].get("genreNames"), "url": d["attributes"].get("url")}
            for d in res["artists"].get("data", [])
        ]
    if "playlists" in res:
        out["playlists"] = [
            {"id": d.get("id"), "name": d["attributes"].get("name"), "curator": d["attributes"].get("curatorName"), "url": d["attributes"].get("url")}
            for d in res["playlists"].get("data", [])
        ]
    if "music-videos" in res:
        out["music_videos"] = [_song_item(d) for d in res["music-videos"].get("data", [])]
    if len(out) == 1:
        out["note"] = "no results"
    return out


@tool
def apple_music_album(url_or_id: str, storefront: str | None = None, language: str | None = None) -> dict[str, Any]:
    """Album details and full tracklist (disc/track numbers, per-track audio traits and song URLs).
    To download only some tracks, pass those tracks' `url`s to amdl_download."""
    sf, ident = _resolve_id(url_or_id, "album", storefront)
    data = _amp(f"/{sf}/albums/{ident}", {"l": language})
    d = (data.get("data") or [{}])[0]
    album = _album_item(d)
    a = d.get("attributes", {})
    album.update(storefront=sf, label=a.get("recordLabel"), upc=a.get("upc"), genres=a.get("genreNames"), copyright=a.get("copyright"))
    album["tracklist"] = [_song_item(t) | {"type": t.get("type")} for t in _all_tracks(d)]
    return album


@tool
def apple_music_artist_albums(url_or_id: str, storefront: str | None = None, include_singles: bool = True) -> dict[str, Any]:
    """List an artist's albums (newest first) so specific ones can be picked for amdl_download.
    amdl_download refuses bare artist URLs unless all_artist_albums=True."""
    sf, ident = _resolve_id(url_or_id, "artist", storefront)
    artist = (_amp(f"/{sf}/artists/{ident}").get("data") or [{}])[0].get("attributes", {})
    albums: list[dict[str, Any]] = []
    offset = 0
    while offset < 1000:
        page = _amp(f"/{sf}/artists/{ident}/albums", {"limit": 100, "offset": offset})
        batch = page.get("data", [])
        albums += [_album_item(d) for d in batch]
        if not page.get("next") or not batch:
            break
        offset += len(batch)
    if not include_singles:
        albums = [x for x in albums if not x.get("single")]
    albums.sort(key=lambda x: x.get("released") or "", reverse=True)
    return {"artist": artist.get("name"), "genres": artist.get("genreNames"), "storefront": sf, "count": len(albums), "albums": albums}


# --------------------------------------------------------------------------- tools: downloads (background jobs)

_jobs: dict[str, dict[str, Any]] = {}
_procs: dict[str, subprocess.Popen] = {}
_job_queue: "queue.Queue[str]" = queue.Queue()
_jobs_lock = threading.Lock()
_worker_started = False


def _save_job(job: dict[str, Any]) -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    (JOBS_DIR / f"{job['id']}.json").write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")


def _mcp_config() -> Path:
    """amdl's own config.yaml with exit-on-error forced on: amdl otherwise waits for Enter after a failure."""
    text = _amdl_config_text()
    if re.search(r"^(\s*)exit-on-error:.*$", text, re.M):
        text = re.sub(r"^(\s*)exit-on-error:.*$", r"\1exit-on-error: true", text, count=1, flags=re.M)
    else:
        text = re.sub(r"^general:\s*$", "general:\n  exit-on-error: true", text, count=1, flags=re.M)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = STATE_DIR / "amdl-mcp.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def _expected_tracks(url: str) -> list[dict[str, Any]] | None:
    """The songs a job URL should produce, from the catalog; None for URL kinds that are not checked."""
    sf, kind, ident = _parse_url(url) or ("", "", "")
    song_id = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("i", [None])[0]
    params = {"l": _config_str("language")}  # same language as amdl, so names match its tags
    if kind == "song" or (kind == "album" and song_id):
        items = _amp(f"/{sf}/songs/{song_id or ident}", params).get("data", [])
    elif kind in ("album", "playlist"):
        items = _all_tracks((_amp(f"/{sf}/{kind}s/{ident}", params).get("data") or [{}])[0])
    else:
        return None  # artist / music-video / station
    out = []
    for t in items:
        a = t.get("attributes", {})
        if t.get("type") != "songs" or not a.get("playParams"):
            continue  # music videos go to the MV folder; tracks without playParams cannot be streamed
        out.append({
            "name": a.get("name"), "isrc": a.get("isrc"), "disc": a.get("discNumber"), "track": a.get("trackNumber"),
            "duration_s": (a.get("durationInMillis") or 0) / 1000, "url": a.get("url"),
        })
    return out


def _audio_info(path: Path) -> dict[str, Any]:
    f = mutagen.File(path)
    if f is None or getattr(f, "info", None) is None:
        raise ValueError("not a recognised audio file")
    tags = f.tags or {}

    def first(*keys: str) -> Any:
        for k in keys:
            v = tags.get(k)
            if v:
                return v[0] if isinstance(v, list) else v
        return None

    def num(v: Any) -> int | None:  # MP4 trkn/disk are (n, total) tuples; Vorbis comments are "3/12"
        if isinstance(v, tuple):
            return v[0]
        m = re.match(r"\d+", str(v or ""))
        return int(m.group()) if m else None

    isrc = first("----:com.apple.iTunes:ISRC", "isrc")
    title = first("\xa9nam", "title")
    return {
        "length_s": f.info.length,
        "isrc": bytes(isrc).decode("utf-8", "replace") if isinstance(isrc, bytes) else isrc,
        "title": str(title) if title else None,
        "disc": num(first("disk", "discnumber")),
        "track": num(first("trkn", "tracknumber")),
    }


def _match(info: dict[str, Any], expected: list[dict[str, Any]], used: set[int]) -> int | None:
    """Index of the catalog track a file is, by ISRC, then title, then disc/track; None if unclear."""
    best, best_score = None, 0
    for i, e in enumerate(expected):
        if i in used:
            continue
        score = (4 if info["isrc"] and info["isrc"] == e["isrc"] else 0) \
            + (2 if info["title"] and e["name"] and info["title"].casefold() == e["name"].casefold() else 0) \
            + (1 if (info["disc"], info["track"]) == (e["disc"], e["track"]) else 0)
        if score > best_score:
            best, best_score = i, score
    return best if best_score >= 2 else None


def _files_written_since(started: str, mode: str) -> list[str]:
    root = _save_folders().get(mode)
    if not root or not Path(root).is_dir():
        return []
    since = datetime.fromisoformat(started).timestamp() - 2
    out = []
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            p = Path(dirpath) / n
            if p.suffix.lower() in AUDIO_EXT and p.stat().st_mtime >= since:
                out.append(str(p))
    return out


def _quarantine(p: Path, jid: str) -> Path:
    dest_dir = QUARANTINE_DIR / jid
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest, n = dest_dir / p.name, 1
    while dest.exists():
        dest, n = dest_dir / f"{p.stem} ({n}){p.suffix}", n + 1
    shutil.move(str(p), str(dest))
    return dest


def _verify(job: dict[str, Any], files: list[str] | None) -> dict[str, Any]:
    """Compare a finished job's files with the catalog durations. Truncated files are moved to the
    quarantine folder, so that re-running the URL downloads them again instead of skipping them."""
    expected: list[dict[str, Any]] = []
    for u in job["urls"]:
        expected += _expected_tracks(u) or []
    if not expected:
        return {"status": "skipped", "reason": "no songs to check for these URL types (artist / music video / station)"}
    full_list = files is not None
    if files is None:  # amdl exits with errors before printing its file list: check what this job wrote
        files = _files_written_since(job["started"], job["mode"])
    ok, short, unchecked, used = 0, [], [], set()
    for path in dict.fromkeys(files):
        p = Path(path)
        if p.suffix.lower() not in AUDIO_EXT or not p.exists():
            continue
        try:
            info = _audio_info(p)
        except Exception as e:  # noqa: BLE001
            unchecked.append({"file": path, "reason": str(e)})
            continue
        i = _match(info, expected, used)
        if i is None:
            unchecked.append({"file": path, "reason": "no matching catalog track"})
            continue
        used.add(i)
        e = expected[i]
        if info["length_s"] < e["duration_s"] - VERIFY_TOLERANCE_S:
            short.append({
                "file": path, "expected_s": round(e["duration_s"], 1), "actual_s": round(info["length_s"], 1),
                "url": e["url"], "moved_to": str(_quarantine(p, job["id"])),
            })
        else:
            ok += 1
    rep: dict[str, Any] = {"status": "problems" if short else "ok", "expected": len(expected), "ok": ok}
    if short:
        rep["truncated"] = short
    if full_list:  # only a complete file list shows which tracks are missing
        missing = [{k: e[k] for k in ("name", "disc", "track", "url")} for i, e in enumerate(expected) if i not in used]
        if missing:
            rep.update(status="problems", missing=missing)
    else:
        rep["note"] = "amdl exited with errors, so only the files this job wrote were checked"
    if unchecked:
        rep["unchecked"] = unchecked
    return rep


def _worker() -> None:
    while True:
        jid = _job_queue.get()
        job = _jobs[jid]
        if job["status"] == "cancelled":
            continue
        log_path = Path(job["log"])
        try:
            cmd = [str(AMDL_EXE), "-c", str(_mcp_config()), *job["flags"], "--json", *job["urls"]]
            with open(log_path, "wb") as log:
                proc = subprocess.Popen(cmd, cwd=AMDL_DIR, env=_amdl_env(), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
                with _jobs_lock:
                    _procs[jid] = proc
                    job.update(status="running", pid=proc.pid, started=_now(), command=cmd)
                    _save_job(job)
                rc = proc.wait()
        except Exception as e:  # noqa: BLE001
            rc = -1
            job["error"] = str(e)
        cancelled = bool(job.get("cancel_requested"))
        verify = None
        if not cancelled and rc in (0, 1):  # 1 = amdl's "Error detected, exiting..."
            try:
                verify = _verify(job, _added_files(_log_lines(log_path)))
            except Exception as e:  # noqa: BLE001
                verify = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        status = "cancelled" if cancelled else ("done" if rc == 0 else "failed")
        if status == "done" and verify and verify["status"] == "problems":
            status = "incomplete"
        retry_urls: list[str] = []
        if job.get("attempt", 1) <= AUTO_RETRY:
            if rc == 1:  # amdl skips the tracks that already exist, so re-running the URLs redoes only the failures
                retry_urls = list(job["urls"])
            elif status == "incomplete":
                bad = (verify.get("truncated") or []) + (verify.get("missing") or [])
                retry_urls = list(dict.fromkeys(x["url"] for x in bad if x.get("url")))
        child = None
        if retry_urls:
            child, _ = _new_job(retry_urls, job["mode"], job["flags"], attempt=job.get("attempt", 1) + 1, retry_of=jid)
        with _jobs_lock:
            _procs.pop(jid, None)
            job.update(status=status, exit_code=rc, finished=_now(), verify=verify)
            if child:
                job["retry_job"] = child["id"]
            _save_job(job)


def _new_job(urls: list[str], mode: str, flags: list[str], attempt: int = 1, retry_of: str | None = None) -> tuple[dict[str, Any], int]:
    jid = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job: dict[str, Any] = {
        "id": jid, "status": "queued", "created": _now(), "urls": urls, "mode": mode, "flags": flags,
        "attempt": attempt, "log": str(JOBS_DIR / f"{jid}.log"),
    }
    if retry_of:
        job["retry_of"] = retry_of
    with _jobs_lock:
        _jobs[jid] = job
        _save_job(job)
        ahead = sum(1 for j in _jobs.values() if j["status"] in ("queued", "running")) - 1
    _ensure_worker()
    _job_queue.put(jid)
    return job, ahead


def _ensure_worker() -> None:
    global _worker_started
    with _jobs_lock:
        if not _worker_started:
            threading.Thread(target=_worker, name="amdl-jobs", daemon=True).start()
            _worker_started = True


def _clean_lines(raw: str) -> list[str]:
    raw = ANSI_RE.sub("", raw)
    lines = []
    for ln in raw.split("\n"):
        seg = next((s for s in reversed(ln.split("\r")) if s.strip()), "")  # last redraw of a progress bar
        if seg.strip():
            lines.append(seg.rstrip())
    return lines


def _log_lines(log_path: Path) -> list[str]:
    return _clean_lines(log_path.read_bytes().decode("utf-8", "replace")) if log_path.exists() else []


def _added_files(lines: list[str]) -> list[str] | None:
    """Absolute paths from amdl's --json line (printed only when it ends without errors), else None."""
    for ln in reversed(lines):
        if ln.startswith("[{") or ln == "null":
            try:
                added = json.loads(ln) or []
            except json.JSONDecodeError:
                return None
            return [str(AMDL_DIR / x["path"]) for x in added if x.get("path")]  # amdl reports cwd-relative paths
    return None


def _load_job(job_id: str) -> dict[str, Any] | None:
    job = _jobs.get(job_id)
    if job is None and (JOBS_DIR / f"{job_id}.json").exists():
        job = json.loads((JOBS_DIR / f"{job_id}.json").read_text(encoding="utf-8"))
    return job


def _job_report(job: dict[str, Any], tail: int) -> dict[str, Any]:
    log_path = Path(job["log"])
    lines = _log_lines(log_path)
    rep = {k: job.get(k) for k in ("id", "status", "created", "started", "finished", "exit_code", "urls", "mode", "error", "attempt", "retry_of", "retry_job")}
    rep = {k: v for k, v in rep.items() if v is not None or k in ("id", "status")}
    queue_pos = next((m.groups() for ln in reversed(lines) if (m := re.search(r"Queue (\d+) of (\d+)", ln))), None)
    track_pos = next((m.groups() for ln in reversed(lines) if (m := re.search(r"Track (\d+) of (\d+)", ln))), None)
    if queue_pos:
        rep["queue_item"] = f"{queue_pos[0]}/{queue_pos[1]}"
    if track_pos:
        rep["track"] = f"{track_pos[0]}/{track_pos[1]}"
    summary = [ln for ln in lines if "Completed:" in ln]
    if summary:
        rep["summary"] = summary[-1].strip("= ")
    problems = [ln for ln in lines if re.search(r"Failed|Error|Unavailable|\u26A0|\u2716|panic", ln) and "Errors: 0" not in ln]
    if problems:
        rep["problems"] = problems[-15:]
    files = _added_files(lines)
    if files is not None:
        rep["files"] = files
    if job.get("verify"):
        rep["verify"] = job["verify"]
    if job["status"] == "running" and job["id"] not in _procs:
        rep["status"] = "unknown (MCP server restarted while this job was running)"
    last, seen = job, {job["id"]}  # follow automatic retries to the newest one
    while last.get("retry_job") and last["retry_job"] not in seen and (nxt := _load_job(last["retry_job"])):
        seen.add(nxt["id"])
        last = nxt
    if last is not job:
        rep["latest_retry"] = {"id": last["id"], "status": last["status"], "attempt": last.get("attempt")}
    rep["log_tail"] = lines[-tail:]
    rep["log_file"] = str(log_path)
    return rep


@tool
def amdl_download(
    urls: list[str],
    mode: Literal["alac", "atmos", "aac"] = "alac",
    alac_max: int | None = None,
    atmos_max: int | None = None,
    aac_type: Literal["aac-lc", "aac", "aac-binaural", "aac-downmix"] | None = None,
    all_artist_albums: bool = False,
) -> dict[str, Any]:
    """Queue a background amdl download and return a job id (poll it with amdl_jobs).
    urls: music.apple.com album / song (or album?i=<trackId>) / playlist / music-video URLs; one job runs them in order.
    mode: alac (lossless, default; capped by alac_max sample rate e.g. 48000/96000/192000), atmos, or aac.
    Artist URLs are refused unless all_artist_albums=True (that downloads EVERY album and music video);
    prefer apple_music_artist_albums and pass album URLs. Station URLs need a media-user-token in amdl's config.
    Tracks that already exist on disk are skipped by amdl. Files go to the save folders shown by amdl_status.
    After each run the files are checked against the catalog durations: truncated files are moved to the
    MCP's quarantine folder and, like failed or missing tracks, retried automatically (up to AMDL_AUTO_RETRY
    extra runs). A job whose files fail the check ends as "incomplete"; amdl_jobs shows `verify` and `latest_retry`."""
    if not AMDL_EXE.exists():
        return {"ok": False, "error": f"{AMDL_EXE} not found"}
    if not urls:
        return {"ok": False, "error": "no URLs"}
    parsed = []
    for u in urls:
        p = _parse_url(u)
        if not p:
            return {"ok": False, "error": f"not a music.apple.com album/song/playlist/artist/music-video/station URL: {u}"}
        if p[1] == "artist" and not all_artist_albums:
            return {"ok": False, "error": "artist URL given; use apple_music_artist_albums and pass album URLs, or set all_artist_albums=True"}
        parsed.append(p)
    if sum(p[1] == "artist" for p in parsed) and len(parsed) > 1:
        return {"ok": False, "error": "amdl only expands an artist URL when it is the only URL; queue it as its own job"}
    lite = _lite_status()
    if not lite.get("reachable"):
        return {"ok": False, "error": f"wrapper-lite is not reachable at {lite['url']}; call wrapper_start first", "wrapper_lite": lite}
    if not lite.get("regions"):
        return {"ok": False, "error": LOGIN_HELP, "wrapper_lite": lite}
    args: list[str] = []
    if mode == "atmos":
        args.append("--atmos")
    elif mode == "aac":
        args.append("--aac")
    if alac_max:
        args += ["--alac-max", str(int(alac_max))]
    if atmos_max:
        args += ["--atmos-max", str(int(atmos_max))]
    if aac_type:
        args += ["--aac-type", aac_type]
    if all_artist_albums:
        args.append("--all-album")
    job, ahead = _new_job(list(urls), mode, args)
    regions = set(lite.get("regions") or [])
    other = sorted({p[0] for p in parsed} - regions)
    out = {"ok": True, "job_id": job["id"], "jobs_ahead": ahead, "hint": "poll with amdl_jobs(job_id); follow latest_retry if it appears"}
    if other:
        out["warning"] = f"storefront(s) {other} are not in wrapper-lite regions {sorted(regions)}; if tracks fail, use the same album from {sorted(regions)}"
    return out


@tool
def amdl_jobs(job_id: str | None = None, tail: int = 25) -> dict[str, Any]:
    """Without job_id: list recent download jobs. With job_id: status, current queue item/track,
    problems, downloaded file paths (once finished), the duration check (`verify`), the newest automatic
    retry of this job (`latest_retry`: poll that id until it finishes) and the last `tail` log lines.
    Status: queued / running / done (checked OK) / incomplete (truncated or missing tracks) / failed / cancelled."""
    if job_id:
        job = _load_job(job_id)
        if job is None:
            return {"error": f"unknown job {job_id}"}
        return _job_report(job, max(1, min(int(tail), 200)))
    seen = dict(_jobs)
    if JOBS_DIR.exists():
        for f in sorted(JOBS_DIR.glob("*.json"))[-30:]:
            j = json.loads(f.read_text(encoding="utf-8"))
            seen.setdefault(j["id"], j)
    rows = sorted(seen.values(), key=lambda j: j["created"], reverse=True)[:30]
    return {"jobs": [{k: j[k] for k in ("id", "status", "created", "finished", "mode", "urls", "retry_of", "retry_job") if j.get(k)} for j in rows]}


@tool
def amdl_cancel(job_id: str) -> dict[str, Any]:
    """Cancel a queued or running download job (the track being written may be left incomplete)."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return {"ok": False, "error": f"unknown job {job_id} (or it belongs to an earlier MCP session)"}
        if job["status"] not in ("queued", "running"):
            return {"ok": False, "error": f"job is already {job['status']}"}
        job["cancel_requested"] = True
        proc = _procs.get(job_id)
        if proc is None:
            job["status"] = "cancelled"
            _save_job(job)
            return {"ok": True, "status": "cancelled"}
    proc.terminate()
    return {"ok": True, "status": "cancelling"}


# --------------------------------------------------------------------------- tools: foobar2000 / local library


def _fb_entry_parts(entry: str) -> list[str]:
    path = re.sub(r"^file(-relative)?://", "", entry.strip())
    parts = [p for p in re.split(r"[\\/]+", path) if p and p not in (".", "..")]
    return parts


@tool
def foobar_library(playlist: str | None = None, include_folders: bool = True) -> dict[str, Any]:
    """The user's foobar2000 playlists and local music folder.
    Without `playlist`: every playlist with its track count and main artist/album folders, plus (include_folders)
    the artist -> album folders under the music root, to check what is already owned.
    With `playlist` (case-insensitive substring of a playlist name): that playlist's tracks."""
    pl_dir = FOOBAR_PROFILE / "playlists-v2.0"
    index = pl_dir / "index.txt"
    if not index.exists():
        return {"error": f"foobar2000 v2 playlist index not found at {index}"}
    names: list[tuple[str, str]] = []
    for line in index.read_text(encoding="utf-8-sig").splitlines():
        if ":" in line:
            gid, name = line.split(":", 1)
            names.append((gid.strip(), name.strip()))

    def entries(gid: str) -> list[str]:
        f = pl_dir / f"playlist-{gid}.fplite"
        if not f.exists():
            return []
        return [ln for ln in f.read_text(encoding="utf-8-sig", errors="replace").splitlines() if ln.strip()]

    if playlist:
        hits = [(g, n) for g, n in names if playlist.lower() in n.lower()]
        if not hits:
            return {"error": f"no playlist matching {playlist!r}", "playlists": [n for _, n in names]}
        out = []
        for gid, name in hits[:5]:
            tracks = []
            for e in entries(gid):
                p = _fb_entry_parts(e)
                tracks.append({"file": p[-1] if p else e, "album_folder": p[-2] if len(p) > 1 else None, "artist_folder": p[-3] if len(p) > 2 else None})
            out.append({"name": name, "count": len(tracks), "tracks": tracks})
        return {"playlists": out}

    rows = []
    for gid, name in names:
        es = entries(gid)
        arts: Counter[str] = Counter()
        albs: Counter[str] = Counter()
        for e in es:
            p = _fb_entry_parts(e)
            if len(p) > 2:
                arts[p[-3]] += 1
            if len(p) > 1:
                albs[p[-2]] += 1
        rows.append({"name": name, "tracks": len(es), "artists": [a for a, _ in arts.most_common(2)], "albums": [a for a, _ in albs.most_common(2)]})
    out: dict[str, Any] = {"foobar_profile": str(FOOBAR_PROFILE), "playlists": rows}
    if include_folders and MUSIC_ROOT.exists():
        lib = {}
        for artist in sorted(MUSIC_ROOT.iterdir(), key=lambda p: p.name.lower()):
            if artist.is_dir():
                lib[artist.name] = sorted(p.name for p in artist.iterdir() if p.is_dir())
        out["music_root"] = str(MUSIC_ROOT)
        out["library_folders"] = lib
    return out


if __name__ == "__main__":
    server.run()
