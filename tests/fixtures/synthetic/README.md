# SYNTHETIC CALIBRATION FIXTURES

**Everything in this directory is SYNTHETIC.** These files are hand-made
(derived mechanically from the real fixtures in `../real/`) to calibrate the
PARITY verifier. They are not real conversion output.

| File | Role | Expected |
|------|------|----------|
| `Jenkinsfile.no-fingerprint` | Copy of the real Jenkinsfile with `fingerprint: true` removed | used with CAL-C1 |
| `cal-d1-wrong-branch.yml` | CAL-D1: deploy rule uses `"master"` instead of `"main"` | `branch_gate` FAIL |
| `cal-d2-missing-always.yml` | CAL-D2: `when: always` removed from `cleanup` | `always_cleanup` FAIL |
| `cal-d3-wrong-artifact-path.yml` | CAL-D3: artifact path `dist/` instead of `out/` | `artifact_paths` FAIL |
| `cal-c1-equivalent.yml` | CAL-C1: equivalent surface form (reordered jobs, single-quoted rule, `rules: [{when: always}]`, `paths: [out/]`) | all four checks PASS |

The `cal-d*` files are compared against the REAL Jenkinsfile; CAL-C1 is compared
against both `Jenkinsfile.no-fingerprint` (-> `CHECKED_EQUIVALENT_WITH_UNVERIFIED_RUNTIME`)
and the real Jenkinsfile (-> `CONFIRMED_LOSS`, because of fingerprint).
