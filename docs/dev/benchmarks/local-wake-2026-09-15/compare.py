"""Alternate baseline/local-wake subprocesses using the pinned workflow harness.

See README.md for preparation. Run with the migration-enabled interpreter.
"""
import argparse
import json
import os
import platform
import resource
import sys
import sysconfig
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--root', type=Path, default=Path(os.environ.get(
    'LOCAL_WAKE_AB_ROOT', '/private/tmp/runloom-local-wake-ab')))
parser.add_argument('--workers', default='2,8,18')
parser.add_argument('--workloads', default='fanout_io,worker_pool,pipeline,cpu_parallel,sleepers,mixed,spawn_churn')
parser.add_argument('--samples', type=int, default=5)
parser.add_argument('--out', type=Path)
args = parser.parse_args()
root = args.root.resolve()
harness = root / 'harness/benchmark/workflows'
sys.path.insert(0, str(harness))
import common as C
import run_workflows as B

soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE,
                   (hard if hard != resource.RLIM_INFINITY else 1048576, hard))
out = args.out or root / 'results.json'
env = dict(os.environ, PYTHON_GIL='0', RUNLOOM_MIGRATION='1',
           WF_RUNTIME_LABEL='runloom-mig')
doc = {
    'baseline': 'de94a1e5 + 55d25600', 'local': '3a71b576 + 55d25600',
    'harness': '75dd06e9', 'python': sys.version, 'executable': sys.executable,
    'configure': sysconfig.get_config_var('CONFIG_ARGS'),
    'platform': platform.platform(), 'cpus': os.cpu_count(),
    'samples': args.samples, 'warmup': 1, 'runs': [],
}
echo = B.start_echo('127.0.0.1:19889', env)
try:
    for workers in map(int, args.workers.split(',')):
        for workload in args.workloads.split(','):
            for sample in range(-1, args.samples):
                order = ['base', 'local'] if sample % 2 == 0 else ['local', 'base']
                for variant in order:
                    tree = root / variant
                    child_env = dict(env, PYTHONPATH=str(tree / 'build/lib-mig')
                                     + os.pathsep + str(tree / 'src'))
                    command = B.command('runloom-mig', workload,
                                        C.DEFAULT_N[workload], workers,
                                        '127.0.0.1:19889', sys.executable)
                    try:
                        result = B.run_once(command, child_env, 45)
                    except Exception as exc:
                        result = {'error': str(exc)}
                    result.update(variant=variant, workers=workers,
                                  workload=workload, sample=sample)
                    doc['runs'].append(result)
                    out.write_text(json.dumps(doc, indent=2))
                    print(workers, workload, sample, variant,
                          round(result.get('ops_per_s', 0)),
                          result.get('error', '')[:150], flush=True)
finally:
    echo.kill()
    echo.wait()
