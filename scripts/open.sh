#!/bin/sh
set -eu

herdr_bin="${HERDR_BIN_PATH:-herdr}"

exec "$herdr_bin" plugin pane open \
  --plugin io.github.tyz-works.task-graph \
  --entrypoint task-graph \
  --placement tab \
  --focus
