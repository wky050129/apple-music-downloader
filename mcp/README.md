# amdl MCP server

English | [简体中文](README-CN.md)

An MCP server that lets Claude Code search Apple Music, look up albums and download them with amdl, the
downloader in the root of this repository, using **wrapper-lite** in Docker as amdl's decryption backend. It can also
read foobar2000 playlists and a local music folder, so Claude knows what is already in the library.

This repository is a fork of [zhaarey/apple-music-downloader](https://github.com/zhaarey/apple-music-downloader).
The downloader is the original author's code, unchanged; this fork only adds the `mcp/` folder, which drives amdl.
See [Credits](#credits).

Written for Windows (Docker Desktop, `amdl.exe`). It needs an Apple Music subscription; only download content you
are entitled to.

## Tools

| Tool | Purpose |
|---|---|
| `amdl_status` | amdl.exe / Docker / wrapper-lite health, account regions, save folders |
| `wrapper_start` | Start Docker Desktop if needed + `docker compose up -d` wrapper-lite (no login) |
| `apple_music_search` | Catalog search (albums / songs / artists / playlists / music-videos), with lossless / hi-res / atmos traits |
| `apple_music_album` | Album details + tracklist with per-track URLs (download single tracks by passing them) |
| `apple_music_artist_albums` | All albums of an artist, newest first |
| `amdl_download` | Queue a background download job (alac / atmos / aac); returns a job id |
| `amdl_jobs` | List jobs, or one job's progress, problems, downloaded files, duration check and log tail |
| `amdl_cancel` | Cancel a queued / running job |
| `foobar_library` | foobar2000 v2 playlists (or one playlist's tracks) + artist -> album folders under the music root |

Downloads run one at a time in a queue. Each job runs `amdl.exe -c mcp\state\amdl-mcp.yaml --json <urls>`, where
`amdl-mcp.yaml` is a copy of amdl's own `config.yaml` with `exit-on-error: true` (otherwise amdl waits for Enter after a
failed track). Edit amdl's `config.yaml` as usual; the copy is regenerated for every job. Logs: `mcp\state\jobs\`.

### Duration check and automatic retries

amdl can write a truncated track and still print "Decrypted / Audio integrity intact": when the CDN stream
ends early it treats that as the end of the file (seen: 60 s of a 418 s track). So after every run the MCP reads
each file's length (mutagen), matches it to the catalog track by ISRC / title / disc+track, and compares it with
the catalog `durationInMillis`, which matches amdl's files to the millisecond.

- More than 1.5 s short: the file counts as truncated and is moved to `mcp\state\quarantine\<job id>\`, because
  amdl skips files that already exist and would never redo it otherwise.
- Catalog tracks with no file (with a full file list) count as missing.
- Either problem marks the job `incomplete`; amdl exiting with errors marks it `failed`. Both queue a retry job
  automatically, up to `AMDL_AUTO_RETRY` extra runs (default 2). `amdl_jobs` shows the check under `verify` and
  the newest retry under `latest_retry`.
- If amdl exits with errors it prints no file list, so only the files written during that run are checked.
- Artist (`all_artist_albums`), music-video and station URLs are not checked.

Interactive amdl features are not exposed: `--select` (pass track URLs from `apple_music_album` instead), `--search`
(use `apple_music_search`), and artist URLs (use `apple_music_artist_albums`; `all_artist_albums=True` downloads all).

### Proxy

amdl inherits `HTTPS_PROXY` from Claude Code's environment. Downloading audio through a proxy can be very slow and
drop streams, so the MCP adds the audio and artwork hosts (`aod.itunes.apple.com,mzstatic.com`) to `NO_PROXY` for amdl;
Apple API calls still use the proxy. Change or disable this with `AMDL_DIRECT_HOSTS` (`""` turns it off).

## Setup

1. Clone this repository and put `amdl.exe` in its root: build it with Go (`go build -o amdl.exe .`, see the main
   [README](../README.md#for-developers)) or download the Windows binary from the upstream
   [releases](https://github.com/zhaarey/apple-music-downloader/releases/latest). Create `config.yaml` from
   `config.yaml.example` and keep `lite-server: "http://127.0.0.1:12340"`.
2. Log wrapper-lite in once, with `mcp/wrapper-lite/compose.yaml` (the steps are at the top of that file; you need
   the Apple ID password and a 2FA code). Copy `mcp/wrapper-lite/.env.example` to `.env` for the login and clear the
   password afterwards. The login is cached in a Docker volume.
3. Install and register the server:

```powershell
git clone https://github.com/wky050129/apple-music-downloader; cd apple-music-downloader\mcp
uv sync                                   # creates .venv with the MCP SDK (mcp 2.x) and mutagen
claude mcp add amdl -s user -- $PWD\.venv\Scripts\python.exe $PWD\server.py
claude mcp list                           # amdl: ... ✔ Connected
```

## Configuration

Environment variables (`claude mcp add ... -e NAME=value`); the defaults fit this repository's layout:

| Variable | Default | |
|---|---|---|
| `AMDL_DIR` | the repository root (parent of `mcp\`) | amdl folder (its `config.yaml`, relative save folders) |
| `AMDL_EXE` | `AMDL_DIR\amdl.exe` | |
| `WRAPPER_DIR` | `mcp\wrapper-lite\` | folder with the wrapper-lite `compose.yaml` |
| `WRAPPER_CONTAINER` | `wrapper-lite` | |
| `DOCKER_DESKTOP_EXE` | `C:\Program Files\Docker\Docker\Docker Desktop.exe` | |
| `FOOBAR_PROFILE` | `%APPDATA%\foobar2000-v2` | foobar2000 v2 profile (portable installs: `<install>\profile`) |
| `MUSIC_ROOT` | `%USERPROFILE%\Music` | `<artist>\<album>` folders listed by `foobar_library` |
| `AMDL_MCP_STATE` | `mcp\state\` | job logs, quarantine, cached web token |
| `AMDL_DIRECT_HOSTS` | `aod.itunes.apple.com,mzstatic.com` | hosts amdl reaches without the proxy |
| `AMDL_AUTO_RETRY` | `2` | extra runs for failed / truncated / missing tracks |

## wrapper-lite login

The MCP never handles the Apple ID password. When `amdl_status` says the login is missing, log in again by hand
in `WRAPPER_DIR` as described at the top of `mcp/wrapper-lite/compose.yaml`. Its API has no authentication, so the
compose file only publishes it on `127.0.0.1`.

## Credits

- [zhaarey/apple-music-downloader](https://github.com/zhaarey/apple-music-downloader) (amdl), the downloader this
  repository is forked from, itself forked from
  [alacleaker/apple-music-alac-downloader](https://github.com/alacleaker/apple-music-alac-downloader).
- [WorldObservationLog/wrapper](https://github.com/WorldObservationLog/wrapper), which provides the wrapper-lite
  image (`ghcr.io/worldobservationlog/wrapper-lite`).
- [mutagen](https://github.com/quodlibet/mutagen) for reading audio lengths and tags.
