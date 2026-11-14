# amdl MCP 服务器

[English](README.md) | 简体中文

一个给 Claude Code 用的 MCP 服务器。接上后可以在 Claude Code 里直接搜索 Apple Music、查看专辑曲目，并用本仓库根目录的下载器
amdl 下载。解密后端是跑在 Docker 里的 **wrapper-lite**。它还能读取 foobar2000 播放列表和本地音乐文件夹，下载前 Claude 能先查一下
哪些已经有了。

本仓库 fork 自原作者的 [zhaarey/apple-music-downloader](https://github.com/zhaarey/apple-music-downloader)。下载器是原作者的代码，
没有改动；这个 fork 只新增了 `mcp/` 目录，用来调用 amdl。详见[致谢](#致谢)。

这个项目是按 Windows 环境写的（Docker Desktop、`amdl.exe`）。使用需要 Apple Music 订阅，请只下载你有权获取的内容。

## 工具

| 工具 | 用途 |
|---|---|
| `amdl_status` | 检查 amdl.exe、Docker、wrapper-lite 的状态，以及账号可用地区和保存目录 |
| `wrapper_start` | 需要时启动 Docker Desktop，再 `docker compose up -d` 启动 wrapper-lite（不负责登录） |
| `apple_music_search` | 搜索曲库（专辑 / 歌曲 / 艺人 / 歌单 / MV），结果带无损 / Hi-Res / 杜比全景声标记 |
| `apple_music_album` | 专辑详情和曲目表，每首都带链接，可以只下其中几首 |
| `apple_music_artist_albums` | 某位艺人的全部专辑，按发行时间从新到旧 |
| `amdl_download` | 把下载加入后台队列（alac / atmos / aac），返回任务号 |
| `amdl_jobs` | 列出任务，或查看单个任务的进度、问题、已下载文件、时长核对结果和日志末尾 |
| `amdl_cancel` | 取消排队中或正在运行的任务 |
| `foobar_library` | foobar2000 v2 播放列表（或某个列表的曲目），以及音乐目录下的 艺人 → 专辑 文件夹 |

下载任务在队列里一个接一个执行。每个任务运行 `amdl.exe -c mcp\state\amdl-mcp.yaml --json <链接>`。其中 `amdl-mcp.yaml` 是 amdl
自己的 `config.yaml` 的副本，只是强制设了 `exit-on-error: true`，否则 amdl 出错后会一直等人按回车。平时照常修改 amdl 的
`config.yaml` 即可，副本每次任务都会重新生成。日志在 `mcp\state\jobs\`。

### 时长核对和自动重试

CDN 连接中途断开时，amdl 会把它当成文件结束，写出一个被截断的文件，却照样显示"Decrypted / Audio integrity intact"。实际遇到过：
一首 418 秒的歌只下了 60 秒。所以每个任务跑完后，MCP 会用 mutagen 读出每个文件的时长，按 ISRC / 歌名 / 碟号和曲号对应到曲库里的
那一首，再和曲库的 `durationInMillis` 比。正常情况下两者精确到毫秒一致。

- 比曲库短 1.5 秒以上就算截断，文件会被挪到 `mcp\state\quarantine\<任务号>\`。不挪开的话，amdl 看到文件已存在就会跳过，永远
  不会重下。
- 拿到完整文件清单时，曲库里有、本地却没有文件的曲目算缺失。
- 出现截断或缺失时任务标记为 `incomplete`，amdl 报错退出时标记为 `failed`。这两种情况都会自动排一个重试任务，最多再跑
  `AMDL_AUTO_RETRY` 次（默认 2 次）。`amdl_jobs` 里 `verify` 是核对结果，`latest_retry` 是最近一次重试。
- amdl 报错退出时不会输出文件清单，这时只核对这次运行新写入的文件。
- 艺人整包下载（`all_artist_albums`）、MV 和电台不做核对。

amdl 的交互功能没有开放：`--select` 改为把 `apple_music_album` 给出的单曲链接传进来，`--search` 改用 `apple_music_search`，
艺人链接改用 `apple_music_artist_albums`（设 `all_artist_albums=True` 才会下载全部）。

### 代理

amdl 会继承 Claude Code 环境里的 `HTTPS_PROXY`。音频走代理可能非常慢，还容易断流，所以 MCP 会把音频和封面的域名
（`aod.itunes.apple.com,mzstatic.com`）加进 amdl 的 `NO_PROXY`，让它们直连。Apple 的 API 请求仍然走代理。可以用
`AMDL_DIRECT_HOSTS` 修改，设为 `""` 就是关闭。

## 安装

1. 克隆本仓库，把 `amdl.exe` 放在仓库根目录。可以用 Go 编译（`go build -o amdl.exe .`，见主
   [README](../README-CN.md#开发者指南)），也可以从上游的 [Releases](https://github.com/zhaarey/apple-music-downloader/releases/latest)
   下载 Windows 版。用 `config.yaml.example` 生成 `config.yaml`，保持 `lite-server: "http://127.0.0.1:12340"`。
2. 用 `mcp/wrapper-lite/compose.yaml` 给 wrapper-lite 登录一次，步骤写在该文件开头，需要 Apple ID 密码和双重认证验证码。登录时把
   `mcp/wrapper-lite/.env.example` 复制成 `.env` 并填好，登录完把密码清掉。登录状态会保存在 Docker 卷里。
3. 安装并注册服务器：

```powershell
git clone https://github.com/wky050129/apple-music-downloader; cd apple-music-downloader\mcp
uv sync                                   # 创建 .venv，装好 MCP SDK（mcp 2.x）和 mutagen
claude mcp add amdl -s user -- $PWD\.venv\Scripts\python.exe $PWD\server.py
claude mcp list                           # 显示 amdl: ... ✔ Connected 即成功
```

## 配置

用环境变量配置（`claude mcp add ... -e 名称=值`），默认值已经按本仓库的目录结构设好：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AMDL_DIR` | 仓库根目录（`mcp\` 的上一级） | amdl 所在目录（读取其中的 `config.yaml`，相对路径的保存目录也以它为基准） |
| `AMDL_EXE` | `AMDL_DIR\amdl.exe` | |
| `WRAPPER_DIR` | `mcp\wrapper-lite\` | 放 wrapper-lite `compose.yaml` 的目录 |
| `WRAPPER_CONTAINER` | `wrapper-lite` | |
| `DOCKER_DESKTOP_EXE` | `C:\Program Files\Docker\Docker\Docker Desktop.exe` | |
| `FOOBAR_PROFILE` | `%APPDATA%\foobar2000-v2` | foobar2000 v2 配置目录（便携版为 `<安装目录>\profile`） |
| `MUSIC_ROOT` | `%USERPROFILE%\Music` | `foobar_library` 列出的 `<艺人>\<专辑>` 文件夹所在目录 |
| `AMDL_MCP_STATE` | `mcp\state\` | 任务日志、隔离区、缓存的网页 token |
| `AMDL_DIRECT_HOSTS` | `aod.itunes.apple.com,mzstatic.com` | amdl 不走代理直连的域名 |
| `AMDL_AUTO_RETRY` | `2` | 失败、截断或缺失曲目的额外重试次数 |

## wrapper-lite 登录

MCP 不会经手 Apple ID 密码。如果 `amdl_status` 提示没有登录，请按 `mcp/wrapper-lite/compose.yaml` 开头的步骤，在 `WRAPPER_DIR`
里手动重新登录。wrapper-lite 的接口没有任何鉴权，所以 compose 文件只把它暴露在 `127.0.0.1` 上。

## 致谢

- [zhaarey/apple-music-downloader](https://github.com/zhaarey/apple-music-downloader)（amdl）：本仓库 fork 自它，它本身又 fork 自
  [alacleaker/apple-music-alac-downloader](https://github.com/alacleaker/apple-music-alac-downloader)。
- [WorldObservationLog/wrapper](https://github.com/WorldObservationLog/wrapper)：提供 wrapper-lite 镜像
  （`ghcr.io/worldobservationlog/wrapper-lite`）。
- [mutagen](https://github.com/quodlibet/mutagen)：用来读取音频时长和标签。
