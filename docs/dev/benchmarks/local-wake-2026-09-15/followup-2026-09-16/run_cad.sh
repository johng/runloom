#!/bin/bash
set +e
S=/private/tmp/claude-501/-Users-johng-repos-runloom/005e6dab-cfdb-4cee-850c-efac05d80922/scratchpad
WT=/Users/johng/repos/runloom/.claude/worktrees/piped-coalescing-harbor
PY=/Users/johng/.pyenv/versions/3.14.4t-mig/bin/python3.14
H=$S/harness/benchmark/workflows
ADDR=127.0.0.1:19891
export PYTHON_GIL=0 RUNLOOM_MIGRATION=1
WL=${1:-mixed}
out="$S/cad_${WL}.log"; : > "$out"
"$H/wf_go/wf_go" -echoserver -addr $ADDR > "$S/echo2.log" 2>&1 &
EP=$!
sleep 1
for hubs in ${HUBS:-2}; do
  for rep in 1 2 3; do
    echo "-- base2" >> "$out"
    PYTHONPATH="$S/base2:$H:$WT/src" "$PY" "$S/mixed_probe.py" --workload $WL --workers $hubs --addr $ADDR >> "$out" 2>&1
    echo "-- half (mask 63)" >> "$out"
    PYTHONPATH="$S/half:$H:$WT/src" "$PY" "$S/mixed_probe.py" --workload $WL --workers $hubs --addr $ADDR >> "$out" 2>&1
    for m in 15 3 0; do
      echo "-- cad mask $m" >> "$out"
      RUNLOOM_SELF_PUMP_MASK=$m PYTHONPATH="$S/cad:$H:$WT/src" "$PY" "$S/mixed_probe.py" --workload $WL --workers $hubs --addr $ADDR >> "$out" 2>&1
    done
  done
done
kill $EP
wait $EP 2>/dev/null
echo DONE >> "$out"
