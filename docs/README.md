# Result charts (Member 3)

`make_charts.py` turns the 45 experiment runs into the report's Figure 2, Figure 3 and
Table 2. Run it from the repo root after the experiments:

```bash
pip install matplotlib
python3 docs/charts/make_charts.py
```

It reads `experiments/results/` and writes to `docs/figures/`. Options: `--results PATH`,
`--out PATH`, `--repeats N` (default 3).

## What the runner must save (Member 2)

One folder per run in `experiments/results/`, named `<kind>-<target>_<pattern>_r<repeat>`:

```
experiments/results/
  hpa-70_spike_r1/
    meta.json         optional if the folder name follows the format
    k6-summary.json
    pods.csv
  custom-15_ramp_r2/
    ...
```

- `<kind>` is `hpa` or `custom`; `<target>` is the CPU % (HPA) or requests/s per pod (custom);
  `<pattern>` is `ramp`, `spike` or `periodic`; `<repeat>` is 1, 2 or 3.
- **`meta.json`** (optional): `{"kind": "hpa", "target": 70, "pattern": "spike", "repeat": 1}`.
- **`k6-summary.json`**: k6's JSON summary. Any of these works:
  - the file from `k6 run --summary-export=...`;
  - the JSON that `handleSummary(data)` receives;
  - k6's whole stdout, if the script prints the JSON on one line after `@@K6_SUMMARY_JSON@@`.
    k6 runs in a deleted pod, so stdout is the easy way to get the summary out. Add this to the
    end of `load/load-test.js` (Member 1's file) and save the `kubectl run ... -i` output:
    ```js
    export function handleSummary(data) {
      return { stdout: '\n@@K6_SUMMARY_JSON@@' + JSON.stringify(data) + '\n' };
    }
    ```
    Note: this replaces k6's usual text summary in the output.
- **`pods.csv`**: header `t,desired,ready`, one row about every 5 s, from a little before the
  test starts until after it ends.
  - `t` = seconds since the k6 test started (negative before it starts).
  - `desired` = the Deployment's `spec.replicas` (pods asked for).
  - `ready` = the Deployment's `status.readyReplicas` (pods ready; 0 if the field is missing).
  - Without `desired`, the charts still work but the reaction time can't be computed.

The traffic patterns are read from `load/load-test.js`, so the request-rate panel stays in
step with Member 1's file. The script prints a `CHECK:` line for every run it can't read and
for every setting and pattern that doesn't have 3 runs.

## Output (`docs/figures/`)

| File | What it is |
|---|---|
| `fig2_tradeoff.png` | Average pods vs p95 response time, one panel per pattern, error bars over the repeats |
| `fig3_spike_pods.png` | Spike pattern: traffic on top, then pods ready and pods asked for, one panel per setting |
| `table2_spike.md` | Table 2 for the report: mean ± standard deviation per setting, spike pattern |
| `summary.csv` | The same measures for every setting and pattern |
| `runs.csv` | One row per run, to check single runs |

How the measures are computed:

- **p95 response time, failed requests**: from each run's k6 summary.
- **Average pods**: mean of `ready` over the test (from t = 0 to the end of the pattern).
- **Reaction time** (spike only): seconds from the start of the jump (t = 60 s) until `ready`
  first reaches the highest `desired` value during the 3-minute peak. Empty with a note when
  the setting didn't scale up or never caught up during the peak.

## Tested

Tested on 45 fake runs in the format above, with all three k6 summary formats and one run
removed on purpose. The script read every format, flagged the missing run, and made both
charts. Fake input (a `FAKE_DATA` file in the results folder or `"fake": true` in
`meta.json`) puts a TEST DATA watermark on the charts, so test charts can't end up in the
report by mistake.
