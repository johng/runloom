#!/bin/bash
# Cadence A/B on the cover build `c16` (this tree, default 16): every run sets
# RUNLOOM_SELF_PUMP_TURNS explicitly so 64 vs 16 come from the same binary.
# Migration mode at 2/8/18 hubs and default mode (stock scheduler on the same
# patched interpreter, RUNLOOM_MIGRATION=0) at 2/8/18, for the two socket
# workloads the cadence can touch.
set +e
S=/private/tmp/claude-501/-Users-johng-repos-runloom/005e6dab-cfdb-4cee-850c-efac05d80922/scratchpad
WT=/Users/johng/repos/runloom/.claude/worktrees/piped-coalescing-harbor
PY=/Users/johng/.pyenv/versions/3.14.4t-mig/bin/python3.14
H=$S/harness/benchmark/workflows
ADDR=127.0.0.1:19892
export PYTHON_GIL=0
out="$S/cadence_bench.log"; : > "$out"
"$H/wf_go/wf_go" -echoserver -addr $ADDR > "$S/echo3.log" 2>&1 &
EP=$!
sleep 1
for mode in 1 0; do
  for wl in mixed fanout_io; do
    for hubs in 2 8 18; do
      for rep in 1 2 3; do
        for turns in 64 16; do
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
