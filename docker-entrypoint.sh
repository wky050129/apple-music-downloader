#!/bin/sh
# Container entrypoint for amdl.
#
# The default `lite-server` (127.0.0.1) points at the container itself, so the
# wrapper-lite address is usually supplied from outside. Setting
# AMDL_LITE_SERVER does that without editing config.yaml; an explicit
# --lite-server argument still wins.
set -e

# `mcp`: run the MCP server for Claude Code over stdio instead of amdl, e.g.
#   claude mcp add amdl -- docker run -i --rm -v <data>:/data -e AMDL_LITE_SERVER=... <image> mcp
# It needs config.yaml in /data, so create it from the template on first use.
if [ "${1:-}" = "mcp" ]; then
  shift
  [ -f config.yaml ] || cp /opt/mcp/config.yaml.example config.yaml
  exec /opt/mcp/.venv/bin/python /opt/mcp/server.py "$@"
fi

if [ -n "${AMDL_LITE_SERVER:-}" ]; then
  case " $* " in
    *" --lite-server"*) ;;
    *) set -- --lite-server "$AMDL_LITE_SERVER" "$@" ;;
  esac
fi

exec amdl "$@"
