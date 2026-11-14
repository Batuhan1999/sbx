#!/usr/bin/env bash
# The orchestrator container's command while Amp's checkpoints are on
# (compose.yaml here): Embed's own launcher, running SBX_ORCHESTRATOR_BINARY
# on the host instead of the image's (build.sh made it).
set -euo pipefail
printf -v binary '%q' "${SBX_ORCHESTRATOR_BINARY:?the amp settings name no orchestrator_binary}"
export OL_COMMAND="nsenter -t 1 -m -u -i -n -C -w/ -- /usr/bin/env PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin $binary"
exec /bin/bash /opt/e2b/scripts/orchestrator-launch.sh
