# Exit capsules - index

Generated 2026-10-15T07:17:50+00:00

## Baseline

- Python `3.12.3`; pins installed: **yes**; tests: {'passed': 3, 'failed': 0, 'error': 0, 'skipped': 0, 'total': 3}

## Dependencies

| dependency | replacement | outcome | evidence | regressions | open concerns | capsule |
|---|---|---|---|---|---|---|
| toml | tomllib | `regressions` | observed_failure | 1 | 2 | [md](toml.md) / [json](toml.json) |
| appdirs | platformdirs | `pass_weak_evidence` | weak | 0 | 2 | [md](appdirs.md) / [json](appdirs.json) |
| chardet | charset-normalizer | `patch_failed` | none | 0 | 2 | [md](chardet.md) / [json](chardet.json) |
| six | (removed) | `install_failed` | none | 0 | 2 | [md](six.md) / [json](six.json) |
| idna |  | `no_plan` | none | 0 | 1 | [md](idna.md) / [json](idna.json) |
| slowdep | x | `timeout` | none | 0 | 2 | [md](slowdep.md) / [json](slowdep.json) |

`pass_weak_evidence` = no regressions, but the swap touches code the baseline tests never executed (or no coverage data).
Sections marked AI-GENERATED inside each capsule are model output, not evidence.
