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

Every setup needs wrapper-lite logged in once. Use `mcp/wrapper-lite/compose.yaml`; the steps are at the top of
that file, and you need the Apple ID password and a 2FA code. Copy `mcp/wrapper-lite/.env.example` to `.env` for
the login and clear the password afterwards. The login is cached in a Docker volume.

This fork's [Releases](https://github.com/wky050129/apple-music-downloader/releases/latest) and Docker image follow
upstream: a workflow merges upstream once a day, and every upstream version is released again here with the MCP
server added.

### From a release (Windows)

1. Download `amdl_windows_amd64_mcp.zip` from the latest release and unpack it. You get `amdl\amdl.exe`,
   `amdl\config.yaml.example` and `amdl\mcp\`. Copy `config.yaml.example` to `config.yaml`; it already has
   `lite-server: "http://127.0.0.1:12340"`. Other platforms: unpack `amdl_mcp.zip` next to your amdl binary and set
   `AMDL_EXE`.
2. Install and register the server:

```powershell
cd amdl\mcp
uv sync                                   # creates .venv with the MCP SDK (mcp 2.x) and mutagen
claude mcp add amdl -s user -- $PWD\.venv\Scripts\python.exe $PWD\server.py
claude mcp list                           # amdl: ... ✔ Connected
```

### From source

Clone this repository and put `amdl.exe` in its root: build it with Go (`go build -o amdl.exe .`, see the main
[README](../README.md#for-developers)) or take it from a release. Create `config.yaml` from `config.yaml.example`,
then run the commands above in `apple-music-downloader\mcp`.

### Docker image

`ghcr.io/wky050129/apple-music-downloader` contains amdl and the MCP server. The `mcp` command runs the server over
stdio. amdl's `config.yaml` (created on first use), the downloads and the MCP's job state are in `/data`:

```powershell
claude mcp add amdl -s user -- docker run -i --rm -v C:\amdl-data:/data `
  -e AMDL_LITE_SERVER=http://host.docker.internal:12340 --add-host host.docker.internal:host-gateway `
  ghcr.io/wky050129/apple-music-downloader:latest mcp
```

- With this repository's `compose.yaml`, which sets the same variables:
  `claude mcp add amdl -s user -- docker compose -f <repo>\compose.yaml run --rm -T amdl mcp`.
- If wrapper-lite runs from `mcp/wrapper-lite/compose.yaml`, the container can join its network instead:
  `--network wrapper-lite_default -e AMDL_LITE_SERVER=http://wrapper-lite:12340`.
- `wrapper_start` does not work in the container: start wrapper-lite on the host.
- `foobar_library` reads a foobar2000 profile mounted at `/foobar` and a music folder mounted at `/music`.

## Configuration

Environment variables (`claude mcp add ... -e NAME=value`). The defaults fit this repository's layout and the
release zip. The Docker image sets its own defaults: `/data`, `/foobar` and `/music`.

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
| `AMDL_LITE_SERVER` | unset | wrapper-lite address; overrides `lite-server` in `config.yaml` (used by the Docker image) |

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
