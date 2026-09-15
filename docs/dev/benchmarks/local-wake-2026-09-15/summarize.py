"""Print median throughput and median absolute deviation from results.json."""
import json
import statistics
import sys
from pathlib import Path

path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name('results.json')
doc = json.loads(path.read_text())
runs = doc['runs']
print('| Hubs | Workload | Baseline ops/s | Local ops/s | Change | MAD % (base/local) |')
print('| ---: | --- | ---: | ---: | ---: | ---: |')
for workers, workload in dict.fromkeys((r['workers'], r['workload']) for r in runs):
    groups = [[r for r in runs if r['workers'] == workers
               and r['workload'] == workload and r['variant'] == variant
               and r['sample'] >= 0] for variant in ('base', 'local')]
    if any(len(group) != doc['samples'] or any('error' in r for r in group) for group in groups):
        print(f'| {workers} | {workload} | incomplete/error | | | |')
        continue
    rates = [[r['ops_per_s'] for r in group] for group in groups]
    medians = [statistics.median(rate) for rate in rates]
    deviations = [100 * statistics.median(abs(x - med) for x in rate) / med
                  for rate, med in zip(rates, medians)]
    base, local = medians
    print(f'| {workers} | {workload} | {base:,.0f} | {local:,.0f} | '
          f'{100 * (local / base - 1):+.1f}% | {deviations[0]:.1f}/{deviations[1]:.1f} |')
