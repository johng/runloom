#!/bin/bash
# Repeat the cells that moved most in default mode, with 32 added and more
# repetitions, alternating cadences within each repetition.
set +e
S=/private/tmp/claude-501/-Users-johng-repos-runloom/005e6dab-cfdb-4cee-850c-efac05d80922/scratchpad
WT=/Users/johng/repos/runloom/.claude/worktrees/piped-coalescing-harbor
PY=/Users/johng/.pyenv/versions/3.14.4t-mig/bin/python3.14
H=$S/harness/benchmark/workflows
ADDR=127.0.0.1:19893
export PYTHON_GIL=0
out="$S/cadence_bench2.log"; : > "$out"
"$H/wf_go/wf_go" -echoserver -addr $ADDR > "$S/echo4.log" 2>&1 &
EP=$!
sleep 1
for mode in ${MODES:-0 1}; do
  for wl in ${WLS:-mixed}; do
    for hubs in ${HUBS:-2 8}; do
      for rep in 1 2 3 4 5; do
        for turns in 64 32 16; do
          echo "-- mig=$mode wl=$wl hubs=$hubs turns=$turns" >> "$out"
          RUNLOOM_MIGRATION=$mode RUNLOOM_SELF_PUMP_TURNS=$turns PYTHONPATH="$S/c16:$H:$WT/src" \
            "$PY" "$S/mixed_probe.py" --workload $wl --workers $hubs --addr $ADDR >> "$out" 2>&1
        done
      done
    done
  done
done
kill $EP
wait $EP 2>/dev/null
echo DONE >> "$out"
