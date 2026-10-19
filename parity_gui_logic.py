"""Non-UI logic for the PARITY Streamlit GUI (app.py).

This module deliberately does NOT import streamlit, so it can be unit tested in
the normal CI job that only installs PyYAML (tests/test_parity_gui.py).

It never changes verifier behaviour: it validates/decodes inputs, calls
``parity_verify.verify_bytes`` / ``parity_verify.render_json`` unchanged,
labels inputs (REAL / SYNTHETIC / USER-SUPPLIED), computes the calibration
table by really running the verifier, and builds the evidence download.

THE VERIFIER IS A STATIC MODEL: a PASS does NOT prove Jenkins runtime
equivalence.
"""

import dataclasses
import hashlib
import importlib
import json
import pathlib
import re

BASE_DIR = pathlib.Path(__file__).resolve().parent
FIXTURES_DIR = BASE_DIR / "tests" / "fixtures"
REAL_DIR = FIXTURES_DIR / "real"
SYNTHETIC_DIR = FIXTURES_DIR / "synthetic"

REAL_JENKINSFILE = "Jenkinsfile"
REAL_GITLAB_CI = "gitlab-ci.converted.yml"
REAL_PROVENANCE = "PROVENANCE.md"
SYNTHETIC_JENKINSFILE_NO_FP = "Jenkinsfile.no-fingerprint"

# Per-input size cap (bytes). Real inputs are a few hundred bytes.
MAX_INPUT_BYTES = 256 * 1024
# PyYAML expands aliases in safe_load; many aliases can be a memory bomb
# ("billion laughs"), so reject suspicious inputs up front.
MAX_YAML_ALIASES = 25

LABEL_REAL = "REAL conversion evidence"
LABEL_SYNTHETIC = "SYNTHETIC calibration fixture"
LABEL_USER = "USER-SUPPLIED"

CHECK_ORDER = ("branch_gate", "always_cleanup", "artifact_paths", "build_commands")

DISCLAIMER = (
    "Static model only. A PASS does NOT prove Jenkins runtime equivalence. "
    "Even the best overall verdict only means the modelled checks agreed; "
    "everything under 'Unverified runtime semantics' still needs runtime experiments."
)

OVERALL_EQUIVALENT = "CHECKED_EQUIVALENT_WITH_UNVERIFIED_RUNTIME"
OVERALL_LOSS = "CONFIRMED_LOSS"
OVERALL_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"


# --------------------------------------------------------------------------
# verifier loading
# --------------------------------------------------------------------------

def load_verifier(import_func=None):
    """Return ``(module, None)`` or ``(None, friendly_error_message)``."""
    import_func = import_func or importlib.import_module
    try:
        module = import_func("parity_verify")
    except ImportError as exc:
        return None, (
            "The PARITY verifier (parity_verify.py) could not be imported: %s. "
            "Run the app from the repository root and install the dependencies "
            "with `pip install -r requirements-gui.txt`." % (exc,)
        )
    except Exception as exc:  # pragma: no cover - defensive
        return None, "The PARITY verifier failed to load (%s)." % type(exc).__name__
    if getattr(module, "yaml", None) is None:
        return None, (
            "PyYAML is not installed, so the verifier cannot parse GitLab CI YAML. "
            "Install it with `pip install -r requirements-gui.txt`."
        )
    return module, None


# --------------------------------------------------------------------------
# input validation
# --------------------------------------------------------------------------

@dataclasses.dataclass
class InputCheck:
    ok: bool
    error: str = ""
    data: bytes = b""


_ALIAS_RE = re.compile(r"(?:^|[\s\[\{,:\-])\*[A-Za-z0-9_\-]+", re.MULTILINE)


def validate_input(raw, side_name, kind="text", max_bytes=MAX_INPUT_BYTES):
    """Validate raw bytes for one side. Never raises.

    ``side_name`` is e.g. "Jenkinsfile"; ``kind`` is "text" or "yaml".
    Returns InputCheck(ok, error, data) with a friendly error when not ok.
    """
    if raw is None:
        return InputCheck(False, "%s: no input provided." % side_name)
    if isinstance(raw, str):
        try:
            raw = raw.encode("utf-8")
        except UnicodeEncodeError:
            return InputCheck(False, "%s: the text contains characters that cannot be "
                                     "encoded as UTF-8." % side_name)
    if not isinstance(raw, (bytes, bytearray)):
        return InputCheck(False, "%s: unsupported input type." % side_name)
    raw = bytes(raw)
    if len(raw) > max_bytes:
        return InputCheck(
            False,
            "%s: input is too large (%d bytes; the limit is %d bytes / %d KiB). "
            "Please use a smaller file." % (side_name, len(raw), max_bytes, max_bytes // 1024),
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return InputCheck(
            False,
            "%s: the input is not valid UTF-8 (invalid byte at offset %d). "
            "Please save the file as UTF-8 text." % (side_name, exc.start),
        )
    if not text.strip():
        return InputCheck(False, "%s: the input is empty." % side_name)
    if "\x00" in text:
        return InputCheck(False, "%s: the input contains NUL bytes and does not look like "
                                 "a text file." % side_name)
    if kind == "yaml" and len(_ALIAS_RE.findall(text)) > MAX_YAML_ALIASES:
        return InputCheck(
            False,
            "%s: too many YAML aliases (more than %d). Alias-heavy YAML can exhaust memory "
            "and is rejected by the GUI." % (side_name, MAX_YAML_ALIASES),
        )
    return InputCheck(True, "", raw)


# --------------------------------------------------------------------------
# labelling
# --------------------------------------------------------------------------

def _read_bytes(path):
    try:
        return pathlib.Path(path).read_bytes()
    except OSError:
        return None


def _known_fixtures():
    """{bytes: label} for every fixture file present in the repo."""
    known = {}
    for path in (REAL_DIR / REAL_JENKINSFILE, REAL_DIR / REAL_GITLAB_CI):
        data = _read_bytes(path)
        if data is not None:
            known[data] = LABEL_REAL
    if SYNTHETIC_DIR.is_dir():
        for path in sorted(SYNTHETIC_DIR.iterdir()):
            if path.name == "README.md" or not path.is_file():
                continue
            data = _read_bytes(path)
            if data is not None and data not in known:
                known[data] = LABEL_SYNTHETIC
    return known


def classify_input(data):
    """Label one input by content: byte-identical to a repo fixture => REAL or
    SYNTHETIC; anything else => USER-SUPPLIED."""
    if data is None:
        return LABEL_USER
    return _known_fixtures().get(bytes(data), LABEL_USER)


def combine_labels(label_j, label_g):
    """Label for a pair. Anything user-supplied makes the pair USER-SUPPLIED; a
    synthetic member makes it SYNTHETIC; only two REAL files are REAL."""
    labels = (label_j, label_g)
    if LABEL_USER in labels:
        return LABEL_USER
    if LABEL_SYNTHETIC in labels:
        return LABEL_SYNTHETIC
    return LABEL_REAL


# --------------------------------------------------------------------------
# verification + evidence
# --------------------------------------------------------------------------

def evidence_filename(result):
    inputs = result["inputs"]
    return "parity-evidence-%s-%s.json" % (
        inputs["jenkinsfile_sha256"][:12], inputs["gitlab_ci_sha256"][:12])


def evidence_bytes(result, verifier=None):
    """Exactly ``parity_verify.render_json(result)`` encoded as UTF-8."""
    if verifier is None:
        verifier, err = load_verifier()
        if verifier is None:
            raise RuntimeError(err)
    return verifier.render_json(result).encode("utf-8")


@dataclasses.dataclass
class Outcome:
    ok: bool
    error: str = ""
    result: dict = None
    evidence: bytes = b""
    filename: str = ""
    label_jenkins: str = LABEL_USER
    label_gitlab: str = LABEL_USER
    label: str = LABEL_USER


def run_verification(jenkins_raw, gitlab_raw, verifier=None):
    """Validate both inputs, run the verifier, build the evidence.

    Never raises. The inputs are passed to the verifier as the original bytes
    (so the reported SHA-256 values are those of the raw input).
    """
    if verifier is None:
        verifier, err = load_verifier()
        if verifier is None:
            return Outcome(False, err)
    jcheck = validate_input(jenkins_raw, "Jenkinsfile", "text")
    gcheck = validate_input(gitlab_raw, "GitLab CI YAML", "yaml")
    errors = [c.error for c in (jcheck, gcheck) if not c.ok]
    if errors:
        return Outcome(False, "\n".join(errors))
    try:
        result = verifier.verify_bytes(jcheck.data, gcheck.data)
        evidence = verifier.render_json(result).encode("utf-8")
        filename = evidence_filename(result)
    except RecursionError:
        return Outcome(False, "The input is nested too deeply to be analysed.")
    except MemoryError:
        return Outcome(False, "The input needs too much memory to be analysed.")
    except Exception as exc:  # the GUI must never crash on any input
        return Outcome(False, "The verifier could not process this input (%s). "
                              "No verdict was produced." % type(exc).__name__)
    lj, lg = classify_input(jcheck.data), classify_input(gcheck.data)
    return Outcome(True, "", result, evidence, filename, lj, lg, combine_labels(lj, lg))


# --------------------------------------------------------------------------
# presentation helpers
# --------------------------------------------------------------------------

# overall -> (headline, colour, explanation).  Deliberately never says plain
# "equivalent" or "passed".
_OVERALL_VIEW = {
    OVERALL_LOSS: (
        "CONFIRMED LOSS", "#b42318",
        "The static model found a divergence or a confirmed unsupported mapping. "
        "The conversion does not preserve at least one modelled behaviour.",
    ),
    OVERALL_INSUFFICIENT: (
        "INSUFFICIENT EVIDENCE", "#b54708",
        "Some syntax is out of the static model (see 'Unparsed / out-of-model items'). "
        "No verdict is given for the affected checks; this is NOT a pass.",
    ),
    OVERALL_EQUIVALENT: (
        "NO DIVERGENCE IN STATIC MODEL \u2014 RUNTIME UNVERIFIED", "#175cd3",
        "All modelled checks agreed. This is NOT proof of Jenkins runtime equivalence; "
        "see the unverified runtime semantics below.",
    ),
}


def overall_view(overall):
    """(headline, colour, explanation) for the overall verdict."""
    return _OVERALL_VIEW.get(overall, (
        "UNKNOWN RESULT", "#475467", "The verifier returned an unrecognised verdict."))


_CHECK_VIEW = {
    "PASS": ("PASS (static model only)", "#067647"),
    "FAIL": ("FAIL", "#b42318"),
    "OUT_OF_MODEL": ("OUT OF MODEL (no verdict)", "#b54708"),
}


def check_verdict_view(verdict):
    return _CHECK_VIEW.get(verdict, (str(verdict), "#475467"))


def scenario_text(scenario):
    if not isinstance(scenario, dict):
        return "n/a"
    return ", ".join("%s=%s" % (k, scenario[k]) for k in sorted(scenario))


def _pretty(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, sort_keys=True)


def witness_view(witness):
    """Readable strings for a mismatch witness (None when there is none)."""
    if not witness:
        return None
    diff = witness.get("diff")
    unified = None
    if isinstance(diff, dict) and isinstance(diff.get("commands"), list):
        unified = "\n".join(str(x) for x in diff["commands"])
    return {
        "scenario": scenario_text(witness.get("scenario")),
        "scenario_independent": bool(witness.get("scenario_independent")),
        "jenkins_executes": _pretty(witness.get("jenkins_executes")),
        "gitlab_executes": _pretty(witness.get("gitlab_executes")),
        "diff": _pretty(diff),
        "diff_unified": unified,
    }


def unparsed_rows(result):
    return [
        {"side": u.get("side"), "line": u.get("line"), "reason": u.get("reason"),
         "text": u.get("text"), "affects": ", ".join(u.get("affects") or [])}
        for u in result.get("unparsed", [])
    ]


# --------------------------------------------------------------------------
# examples
# --------------------------------------------------------------------------

@dataclasses.dataclass
class Example:
    ok: bool
    error: str = ""
    jenkins: bytes = b""
    gitlab: bytes = b""
    provenance: str = ""
    name: str = ""


def load_real_example(real_dir=None):
    """Load the REAL conversion fixtures (paths relative to this module)."""
    real_dir = pathlib.Path(real_dir) if real_dir is not None else REAL_DIR
    missing = [n for n in (REAL_JENKINSFILE, REAL_GITLAB_CI) if not (real_dir / n).is_file()]
    if missing:
        return Example(False, "The real conversion example could not be loaded; missing "
                              "file(s): %s (expected under tests/fixtures/real/)."
                       % ", ".join(missing))
    try:
        jenkins = (real_dir / REAL_JENKINSFILE).read_bytes()
        gitlab = (real_dir / REAL_GITLAB_CI).read_bytes()
    except OSError as exc:
        return Example(False, "The real conversion example could not be read (%s)."
                       % (exc.strerror or type(exc).__name__))
    prov_path = real_dir / REAL_PROVENANCE
    try:
        provenance = prov_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        provenance = ""
    return Example(True, "", jenkins, gitlab, provenance, "real conversion")


def parse_provenance_table(text):
    """Rows of the markdown table in PROVENANCE.md as a list of dicts."""
    rows, header = [], None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            if header is not None and rows:
                break
            continue
        cells = [c.strip().strip("`").strip() for c in line.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
            continue
        if header is None:
            header = cells
            continue
        if len(cells) == len(header):
            rows.append(dict(zip(header, cells)))
    return rows


def provenance_summary(text):
    """Short bullet list summarising PROVENANCE.md (empty list if no text)."""
    if not text:
        return []
    rows = parse_provenance_table(text)
    if not rows:
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()
                      and not p.strip().startswith("#")]
        return [paragraphs[0].replace("\n", " ")] if paragraphs else []
    bullets = ["Real files copied byte-for-byte from git history (not synthetic, not edited)."]
    for row in rows:
        values = list(row.values())
        bullets.append("%s \u2190 `%s` @ commit `%s` (%s); %s"
                       % (values[0], values[1], values[2][:12], values[3], values[-1]))
    return bullets


# --------------------------------------------------------------------------
# calibration (synthetic fixtures; expectations come from
# tests/fixtures/synthetic/README.md and tests/test_parity_verify.py)
# --------------------------------------------------------------------------

def _all_pass_except(failing=None):
    return {c: ("FAIL" if c == failing else "PASS") for c in CHECK_ORDER}


@dataclasses.dataclass(frozen=True)
class CalibrationCase:
    case_id: str
    description: str
    jenkins_dir: str  # "real" or "synthetic"
    jenkins_file: str
    gitlab_file: str  # always under tests/fixtures/synthetic/
    expected_checks: dict
    expected_overall: str

    @property
    def jenkins_label(self):
        return LABEL_REAL if self.jenkins_dir == "real" else LABEL_SYNTHETIC


CALIBRATION_CASES = (
    CalibrationCase("CAL-D1", "deploy rule uses 'master' instead of 'main'",
                    "real", REAL_JENKINSFILE, "cal-d1-wrong-branch.yml",
                    _all_pass_except("branch_gate"), OVERALL_LOSS),
    CalibrationCase("CAL-D2", "`when: always` removed from cleanup",
                    "real", REAL_JENKINSFILE, "cal-d2-missing-always.yml",
                    _all_pass_except("always_cleanup"), OVERALL_LOSS),
    CalibrationCase("CAL-D3", "artifact path 'dist/' instead of 'out/'",
                    "real", REAL_JENKINSFILE, "cal-d3-wrong-artifact-path.yml",
                    _all_pass_except("artifact_paths"), OVERALL_LOSS),
    CalibrationCase("CAL-C1 (no fingerprint)",
                    "equivalent surface form vs synthetic Jenkinsfile without fingerprint",
                    "synthetic", SYNTHETIC_JENKINSFILE_NO_FP, "cal-c1-equivalent.yml",
                    _all_pass_except(None), OVERALL_EQUIVALENT),
    CalibrationCase("CAL-C1 (real Jenkinsfile)",
                    "equivalent surface form vs REAL Jenkinsfile: fingerprint is lost",
                    "real", REAL_JENKINSFILE, "cal-c1-equivalent.yml",
                    _all_pass_except(None), OVERALL_LOSS),
)


def calibration_case_by_id(case_id):
    for case in CALIBRATION_CASES:
        if case.case_id == case_id:
            return case
    return None


def load_calibration_pair(case):
    """(jenkins_bytes, gitlab_bytes, error). error is "" when both exist."""
    jdir = REAL_DIR if case.jenkins_dir == "real" else SYNTHETIC_DIR
    jenkins = _read_bytes(jdir / case.jenkins_file)
    gitlab = _read_bytes(SYNTHETIC_DIR / case.gitlab_file)
    missing = [n for n, d in ((case.jenkins_file, jenkins), (case.gitlab_file, gitlab))
               if d is None]
    if missing:
        return None, None, "missing fixture file(s): " + ", ".join(missing)
    return jenkins, gitlab, ""


def describe_expectation(checks, overall):
    failing = sorted(c for c, v in checks.items() if v == "FAIL")
    if failing:
        head = "%s FAIL, other checks PASS" % ", ".join(failing)
    elif checks and all(v == "PASS" for v in checks.values()):
        head = "all four checks PASS"
    else:
        head = ", ".join("%s=%s" % (c, checks[c]) for c in sorted(checks))
    return "%s; overall %s" % (head, overall)


def run_calibration(verifier=None):
    """Really run the verifier over the synthetic fixtures.

    Returns ``{"rows": [...], "matched": k, "total": n}``. Each row has
    fixture, jenkinsfile, expected, actual, match (bool) and error.
    """
    if verifier is None:
        verifier, err = load_verifier()
        if verifier is None:
            raise RuntimeError(err)
    rows, matched = [], 0
    for case in CALIBRATION_CASES:
        expected = describe_expectation(case.expected_checks, case.expected_overall)
        jenkins, gitlab, error = load_calibration_pair(case)
        row = {
            "fixture": "%s: %s" % (case.case_id, case.gitlab_file),
            "jenkinsfile": "%s (%s)" % (case.jenkins_file, case.jenkins_label),
            "expected": expected,
            "actual": "", "match": False, "error": error,
        }
        if not error:
            try:
                res = verifier.verify_bytes(jenkins, gitlab)
                checks = {c: res["checks"][c]["verdict"] for c in CHECK_ORDER}
                row["actual"] = describe_expectation(checks, res["overall"])
                row["match"] = (checks == case.expected_checks
                                and res["overall"] == case.expected_overall)
            except Exception as exc:
                row["error"] = "verifier error (%s)" % type(exc).__name__
        else:
            row["actual"] = "not run (%s)" % error
        if row["error"] and not row["actual"]:
            row["actual"] = row["error"]
        matched += 1 if row["match"] else 0
        rows.append(row)
    return {"rows": rows, "matched": matched, "total": len(rows)}


def calibration_summary(calibration):
    if calibration is None:
        return "Not run"
    return "%d of %d fixtures matched the repo-defined expectations" % (
        calibration["matched"], calibration["total"])


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()
