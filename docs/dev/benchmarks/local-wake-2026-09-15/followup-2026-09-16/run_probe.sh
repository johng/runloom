#!/bin/bash
set +e
S=/private/tmp/claude-501/-Users-johng-repos-runloom/005e6dab-cfdb-4cee-850c-efac05d80922/scratchpad
WT=/Users/johng/repos/runloom/.claude/worktrees/piped-coalescing-harbor
PY=/Users/johng/.pyenv/versions/3.14.4t-mig/bin/python3.14
H=$S/harness/benchmark/workflows
ADDR=127.0.0.1:19890
export PYTHON_GIL=0 RUNLOOM_MIGRATION=1
out="$S/probe_${1:-mixed}.log"; : > "$out"
"$H/wf_go/wf_go" -echoserver -addr $ADDR > "$S/echo.log" 2>&1 &
EP=$!
sleep 1
WL=${1:-mixed}
for hubs in ${HUBS:-2 8}; do
  for rep in 1 2 3; do
    for b in ${BUILDS:-base2 new exp}; do
      PYTHONPATH="$S/$b:$H:$WT/src" "$PY" "$S/mixed_probe.py" --workload $WL --workers $hubs --addr $ADDR >> "$out" 2>&1
    done
  done
done
kill $EP
wait $EP 2>/dev/null
echo DONE >> "$out"
