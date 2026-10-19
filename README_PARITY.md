# PARITY verifier v0

`parity_verify.py` is a deterministic **static** comparison of a declarative
`Jenkinsfile` and a GitLab CI YAML file. Dependencies: PyYAML + the Python
standard library.

> **A static model does NOT prove Jenkins runtime equivalence.** Even the best
> overall verdict, `CHECKED_EQUIVALENT_WITH_UNVERIFIED_RUNTIME`, only says
> that the modelled checks agreed. Everything listed in
> `unverified_runtime_semantics` still requires runtime experiments.

## Reproduce

```sh
pip install -r requirements.txt
python parity_verify.py --jenkinsfile tests/fixtures/real/Jenkinsfile --gitlab-ci tests/fixtures/real/gitlab-ci.converted.yml
python -m unittest discover -s tests -v
```

CLI: `python parity_verify.py --jenkinsfile PATH --gitlab-ci PATH [--output PATH]`
prints JSON on stdout (and also writes it to `--output`).

| Exit code | Meaning |
|-----------|---------|
| 0 | `CHECKED_EQUIVALENT_WITH_UNVERIFIED_RUNTIME` |
| 1 | `CONFIRMED_LOSS` |
| 2 | `INSUFFICIENT_EVIDENCE` |
| 3 | usage / IO error |

The real conversion is expected to exit **1**: the Jenkins
`fingerprint: true` is dropped by the conversion and the verifier reports it.

## Supported Jenkins subset (declarative only)

```groovy
pipeline {
    agent any
    stages {
        stage('X') {                      // plain string name
            when { branch 'Y' }           // optional; one literal branch condition
            steps {
                sh '...'                  // or sh('...'); plain strings only
                archiveArtifacts artifacts: '...', fingerprint: true   // fingerprint optional (true|false)
            }
        }
    }
    post { always { sh '...' } }          // optional; only `always` with `sh`
}
```

Anything else is **out of model**: it is listed in `unparsed` with its line
number and the affected checks become `OUT_OF_MODEL`. Examples: `parallel`,
`matrix`, `script {}`, `environment`, `parameters`, `input`, `options`,
`triggers`, `tools`, `agent` other than `any`, `post` conditions other than
`always` (failure/success/cleanup/unstable/changed), `when` with
expression/not/anyOf/allOf/tag/changeset (or branch globs), other steps
(junit, stash, withCredentials, ...), GString interpolation, triple-quoted
strings, unbalanced braces. Unparsed syntax is never treated as equivalent.

## Supported GitLab CI subset

* `stages` (default `build, test, deploy` when absent), jobs with `stage`,
  `script`, `rules` (`if`/`when` only), `when` (`on_success|always|on_failure|never`),
  `allow_failure` (boolean), `artifacts.paths`.
* `rules:if` only `$CI_COMMIT_BRANCH` / `$CI_PIPELINE_SOURCE` with `==` / `!=`
  against one quoted literal.
* `needs` is recognised but scheduling is not modelled => `OUT_OF_MODEL`.
* Everything else (`include`, `extends`, `trigger`, `workflow`, `default`,
  `variables`, `image`, `cache`, `before_script`, `after_script`, rules
  `changes`/`exists`/`variables`, regex `if`, `manual`/`delayed`, other
  `artifacts` keys such as `expire_in`, ...) => `OUT_OF_MODEL`.
* Mapping conventions: Jenkins stage `X` <-> GitLab job named `X`
  (case-insensitive); Jenkins `post { always }` <-> job(s) in stage `.post`.
  A rename of every job makes `branch_gate`/`build_commands` `OUT_OF_MODEL`.

## Checks and verdicts

Each check has a verdict:

* `PASS` - no divergence found in the modelled scenarios.
* `FAIL` - a divergence was found; `witness` is filled in.
* `OUT_OF_MODEL` - unparsed/unsupported syntax affects the check; no verdict.

Checks:

| Check | What is compared |
|-------|------------------|
| `branch_gate` | which stages/jobs run per scenario (branch x `pipeline_source=push` x build_result). Default branches: `main`, `parity/fixture-1`, `other-branch`. A stage without `when` runs everywhere. |
| `always_cleanup` | Jenkins `post { always }` commands vs commands of the GitLab `.post` job(s) that run in each scenario; `build_result=fail` means the first running job/stage fails and later `on_success` work is skipped. |
| `artifact_paths` | files archived by Jenkins vs files collected by GitLab over a deterministic virtual file list. |
| `build_commands` | Jenkins `sh` list vs GitLab `script` list per stage, exact string equality after whitespace-only normalisation. |

Glob semantics (Ant-style): `*`/`?` do not cross `/`; a whole segment `**`
matches zero or more directories (so `out/**` also matches the bare path
`out`); trailing `/` means `/**`; a literal name matches only that file. GitLab
side: a path without glob characters covers that file or everything below it
as a directory (`out` and `out/` both cover `out/x`). Virtual files = files the
`sh` commands create (`> f`, `>> f`, `touch f`) plus the fixed probes
`README.md`, `dist/app.txt`, `other/file.txt`, `out/nested/deep.txt`.

Separate top-level lists (not per-check verdicts):

* `confirmed_unsupported_mappings` - currently `fingerprint` (Jenkins
  `fingerprint: true` has no counterpart in the supported GitLab model). It is
  FAIL-class, labelled `CONFIRMED UNSUPPORTED MAPPING`, with a witness pointing
  to the Jenkins line. It is never reported as PASS.
* `unverified_runtime_semantics` - never graded: cancellation behaviour,
  workspace sharing, artifact retention/expiry, branch gate for tag/MR
  pipelines, glob nuances, runner/image environment, fingerprint cross-build
  tracking. **These require runtime experiments.**

### Overall verdict (`overall`)

| Value | Condition |
|-------|-----------|
| `CONFIRMED_LOSS` | any check `FAIL` or any confirmed unsupported mapping (wins over `OUT_OF_MODEL`) |
| `INSUFFICIENT_EVIDENCE` | no FAIL/loss, but at least one `OUT_OF_MODEL` (or unparsed syntax) |
| `CHECKED_EQUIVALENT_WITH_UNVERIFIED_RUNTIME` | all checks PASS, no loss, nothing out of model |

No value claims proven runtime equivalence.

## Output / witness format

Sorted-key JSON, 2-space indent, trailing newline, no timestamps, no absolute
paths. Top-level keys: `checks`, `confirmed_unsupported_mappings`,
`disclaimer`, `extra_probe_files`, `inputs` (`jenkinsfile_sha256`,
`gitlab_ci_sha256` = SHA-256 of the raw bytes), `models` (parsed subset),
`overall`, `scenario_set`, `unparsed`, `unverified_runtime_semantics`,
`verifier_version`.

A FAIL witness contains `scenario` (the exact scenario dict; for
scenario-independent checks - `build_commands`, `artifact_paths` - the first
scenario plus `scenario_independent: true`), `jenkins_executes`,
`gitlab_executes` and `diff`. `unparsed` entries: `side`, `line`, `reason`,
`text`, `affects`.

## Fixtures

* `tests/fixtures/real/` - **REAL** files, byte-identical to git history; see
  `tests/fixtures/real/PROVENANCE.md`. No file named `.gitlab-ci.yml` exists in
  the repo (the converted file is `gitlab-ci.converted.yml`) so no pipeline is
  triggered.
* `tests/fixtures/synthetic/` - **SYNTHETIC CALIBRATION FIXTURES** (see its
  README): CAL-D1 wrong deploy branch, CAL-D2 missing `when: always`, CAL-D3
  wrong artifact path (all expected FAIL in exactly one check), CAL-C1
  equivalent surface form (expected all four checks PASS), plus a synthetic
  Jenkinsfile copy without `fingerprint`.

## Limitations

* Static, finite scenario set; only `pipeline_source=push`; failure modelled as
  "first running job fails".
* Small, strict parsers; most real-world Jenkins/GitLab features are
  `OUT_OF_MODEL` by design.
* Command equality is textual (whitespace only); semantically equal but
  differently written commands are `FAIL`.
* Calibration on 4 synthetic cases and 1 real pair does not establish general
  verifier accuracy.

## GUI

`app.py` is a Streamlit dashboard on top of the unchanged verifier. All non-UI
logic (input checks, labels, calibration, evidence file name, example loading)
lives in `parity_gui_logic.py`, which does not import streamlit.

### Run it

```sh
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-gui.txt
streamlit run app.py
```

`requirements-gui.txt` = `requirements.txt` + `streamlit==1.45.1`.
`requirements.txt` (used by the CI job `parity-tests`) is unchanged, so the
verifier and its tests still need PyYAML only.

### Screens

* **Header / sidebar** - what the tool is, the verdict meanings, the evidence
  labels and the size cap (256 KiB per input). A warning banner states that a
  PASS does NOT prove Jenkins runtime equivalence.
* **Verify a migration** - for the Jenkinsfile and the GitLab CI YAML, either
  paste text or upload a file (read as raw bytes). **Load real conversion
  example** fills both from `tests/fixtures/real/` and shows a summary and the
  full text of `PROVENANCE.md`. **Verify Migration** calls
  `parity_verify.verify_bytes` and shows:
  * the overall verdict as a coloured badge (`CONFIRMED_LOSS`,
    `INSUFFICIENT_EVIDENCE`, or "no divergence in static model - runtime
    unverified" for `CHECKED_EQUIVALENT_WITH_UNVERIFIED_RUNTIME`, which is never
    shown as plain "equivalent"/"passed");
  * one card per check (verdict, summary, reasons, scenarios evaluated,
    divergences) with the mismatch witness: scenario, what Jenkins executes vs
    what GitLab executes, diff, `scenario_independent`, plus raw JSON;
  * confirmed unsupported mappings (e.g. `fingerprint`);
  * unparsed / out-of-model items with side and line number;
  * unverified runtime semantics;
  * input SHA-256 values and verifier version;
  * **Download evidence JSON**: exactly `parity_verify.render_json(result)`,
    named `parity-evidence-<12 hex of Jenkinsfile sha256>-<12 hex of GitLab sha256>.json`;
    the same inputs give byte-identical files (no timestamps).
* **Calibration** - **Run calibration** really runs the verifier over the
  synthetic fixtures and compares the output with the expectations defined in
  `tests/fixtures/synthetic/README.md` / `tests/test_parity_verify.py`. Before
  the click it shows "Not run"; afterwards the computed table
  (fixture, expected, actual, match) and "k of n" are shown.

Bad input (empty, not UTF-8, too large, NUL bytes, alias-heavy YAML) gives a
friendly error. Malformed YAML, unbalanced Jenkins braces and unsupported syntax
are passed to the verifier, which reports them in `unparsed`; the result is
`OUT_OF_MODEL` / `INSUFFICIENT_EVIDENCE`, never a fake PASS.

### Real vs synthetic

Every input and result is labelled:

* **REAL conversion evidence** - byte-identical to the files in
  `tests/fixtures/real/` (see its `PROVENANCE.md`).
* **SYNTHETIC calibration fixture** - files from `tests/fixtures/synthetic/`.
  They calibrate the verifier and are never real conversion evidence.
* **USER-SUPPLIED** - anything else, including an edited copy of a real file.

### Limitation

The verifier is a static model of a small Jenkins/GitLab subset. A PASS only
means no divergence in the modelled scenarios; it does NOT prove Jenkins
runtime equivalence. See `unverified_runtime_semantics` in the output.

### GUI tests

`python -m unittest discover -s tests -v` runs `tests/test_parity_gui.py`. The
logic tests need PyYAML only; the Streamlit `AppTest` smoke tests are skipped
unless streamlit is installed (`pip install -r requirements-gui.txt`).
