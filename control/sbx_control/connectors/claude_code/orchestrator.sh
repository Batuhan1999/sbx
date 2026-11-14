#!/bin/sh
# claude-orchestrator.service: polls Anthropic for sessions queued on the
# self-hosted environment and runs hooks/spawn-runner for each. Without the
# environment secret it exits; systemd tries again after RestartSec (10
# minutes). Runs in the active release (the unit's WorkingDirectory); the
# unit sets ORCHESTRATOR_HEALTH_PORT and SBX_CONTROL_PORT (its hooks' target).
set -eu
secret="$RUNTIME_DIRECTORY/environment-secret"
umask 077
if ! venv/bin/python -m sbx_control.settings secret claude:ENVIRONMENT_SECRET > "$secret"; then
  echo "no CLAUDE_ENVIRONMENT_SECRET yet; trying again in 10 minutes"
  exit 0
fi
eval "$(venv/bin/python -m sbx_control.settings shell)"
exec /root/.local/bin/claude self-hosted-runner orchestrator --environment-secret-file "$secret" \
  --hooks-dir "$PWD/sbx_control/connectors/claude_code/hooks" --hook-concurrency "$claude_hook_concurrency" \
  --expected-spawn-seconds "$claude_expected_spawn_seconds" --health-port "$ORCHESTRATOR_HEALTH_PORT"
