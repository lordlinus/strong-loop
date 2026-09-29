#!/usr/bin/env bash
# set-model.sh — change the model the deployed agent reasons with, safely, in one step.
#
#   ./scripts/set-model.sh gpt-6-astra                 # the loop's model
#   ./scripts/set-model.sh claude-sonnet-5 --typesafe <model>   # and TypeSafe's
#   ./scripts/set-model.sh --default                   # back to DEFAULT_MODEL in loop/models.py
#   ./scripts/set-model.sh gpt-6-astra --no-deploy     # set it; the next push deploys it
#
# The model is a deploy setting, not code: the GitHub `production` environment variable
# LOOP_MODEL (it overrides any repo-level variable of the same name; empty = the code
# default). `claude-*` routes to the Anthropic API through APIM, anything else to
# /openai/v1. This script proves the model answers through the gateway, sets the variable,
# runs the deploy workflow and waits for it. The workflow probes again before deploying and
# checks the deployed agent version carries the model, so a name the gateway does not
# serve can never reach the live agent.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"; [[ -x "$PY" ]] || PY="$ROOT/.venv/Scripts/python.exe"
ENVIRONMENT=production
model="" typesafe="" deploy=1 use_default=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --typesafe)  typesafe="${2:?--typesafe needs a model}"; shift 2 ;;
    --no-deploy) deploy=0; shift ;;
    --default)   use_default=1; shift ;;
    -h|--help)   sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*)          echo "unknown option $1" >&2; exit 2 ;;
    *)           model="$1"; shift ;;
  esac
done
[[ -n "$model" || $use_default -eq 1 || -n "$typesafe" ]] || { sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
command -v gh >/dev/null || { echo "gh (GitHub CLI) is required" >&2; exit 1; }

if [[ -n "$model" || $use_default -eq 1 ]]; then
  if [[ -x "$PY" ]]; then
    probe="$model"
    [[ -n "$probe" ]] || probe="$(cd "$ROOT/src/strong-loop" && "$PY" -c 'from loop.models import DEFAULT_MODEL; print(DEFAULT_MODEL)')"
    echo "==> probing $probe through the gateway"
    (cd "$ROOT/src/strong-loop" && "$PY" -m loop models --model "$probe") \
      || { echo "!! the gateway did not answer for $probe; nothing changed" >&2; exit 1; }
  else
    echo "==> no local .venv; skipping the local probe (the deploy workflow probes before deploying)"
  fi
  if [[ $use_default -eq 1 ]]; then
    gh variable delete LOOP_MODEL --env "$ENVIRONMENT" 2>/dev/null || true
    echo "==> LOOP_MODEL cleared in '$ENVIRONMENT'; the code default applies"
  else
    gh variable set LOOP_MODEL --env "$ENVIRONMENT" --body "$model"
    echo "==> LOOP_MODEL=$model in '$ENVIRONMENT'"
  fi
fi
if [[ -n "$typesafe" ]]; then
  gh variable set TYPESAFE_DEFAULT_MODEL --env "$ENVIRONMENT" --body "$typesafe"
  echo "==> TYPESAFE_DEFAULT_MODEL=$typesafe in '$ENVIRONMENT' (TypeSafe falls back to code-only screens if it cannot answer)"
fi

if [[ $deploy -eq 0 ]]; then
  echo "==> not deploying; the next push to master (or: gh workflow run deploy.yml --ref master) applies it"
  exit 0
fi

echo "==> running the deploy workflow"
before="$(gh run list --workflow deploy.yml --limit 1 --json databaseId -q '.[0].databaseId' 2>/dev/null || true)"
gh workflow run deploy.yml --ref master
for _ in $(seq 1 30); do
  sleep 4
  id="$(gh run list --workflow deploy.yml --limit 1 --json databaseId -q '.[0].databaseId')"
  [[ -n "$id" && "$id" != "$before" ]] && break
done
[[ -n "${id:-}" && "$id" != "$before" ]] || { echo "!! could not find the new workflow run" >&2; exit 1; }
echo "    $(gh run view "$id" --json url -q .url)"
if gh run watch "$id" --exit-status --interval 20 >/dev/null; then
  echo "==> deployed: the workflow probed the model and confirmed the live agent version carries it"
else
  echo "!! the deploy failed; see: gh run view $id --log-failed" >&2
  exit 1
fi
