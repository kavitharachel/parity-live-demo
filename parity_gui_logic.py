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
import html
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
    "Static model only. A PASS (modelled) does NOT prove Jenkins runtime equivalence. "
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
    "PASS": ("PASS (modelled)", "#067647"),
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


# ==========================================================================
# Dashboard (milestone 7). Everything below is NEW and purely additive; all
# values are computed from the verifier result (counts only: no scores, no
# confidence figures). No streamlit import here.
# ==========================================================================

DASHBOARD_TITLE = "PARITY \u2014 Migration Confidence Engine"
DASHBOARD_SUBTITLE = "Static verification for Jenkins \u2192 GitLab CI migrations."
# Shown with every verdict and in the page header, in every state.
STATIC_MODEL_NOTICE = "Static model check. This is not proof of runtime equivalence."
DASHBOARD_DISCLAIMER = (STATIC_MODEL_NOTICE + " Unverified runtime areas are listed with "
                        "every result.")

VERDICT_LOSS_LABEL = "CONFIRMED LOSS"
VERDICT_PASS_LABEL = "STATIC MODEL PASS"
VERDICT_OOM_LABEL = "OUT OF MODEL / INSUFFICIENT EVIDENCE"
VERDICT_UNKNOWN_LABEL = "UNKNOWN RESULT"

# colour semantics: red = loss, amber = out of model, green = ONLY with the
# "static model" qualifier, grey = unverified / unknown
COLOURS = {"red": "#b42318", "amber": "#b54708", "green": "#067647", "grey": "#475467"}

FINGERPRINT_MAPPING_ID = "fingerprint"  # id used by parity_verify for this mapping
NO_COUNTERPART_MARKER = "NO GITLAB COUNTERPART"
NEUTRAL_NO_LOSS = "No confirmed loss found by the static checks."
CONTEXT_LINES = 1
MAX_DISPLAY_CHARS = 240


def esc(text):
    """Escape user-supplied text for embedding in HTML passed to st.markdown.

    ``html.escape`` (quotes included) plus ``$`` -> ``&#36;`` so that markdown
    math rendering cannot swallow CI variables such as ``$CI_COMMIT_BRANCH``.
    """
    return html.escape("" if text is None else str(text), quote=True).replace("$", "&#36;")


def _dict(value):
    return value if isinstance(value, dict) else {}


def _checks_of(result):
    return _dict(_dict(result).get("checks"))


def check_counts(result):
    """``(checks with verdict PASS, total checks)``."""
    checks = _checks_of(result)
    passed = sum(1 for c in checks.values() if _dict(c).get("verdict") == "PASS")
    return passed, len(checks)


def _failed_check_ids(result):
    checks = _checks_of(result)
    return [cid for cid in CHECK_ORDER + tuple(sorted(set(checks) - set(CHECK_ORDER)))
            if _dict(checks.get(cid)).get("verdict") == "FAIL"]


def _mappings_of(result):
    return [m for m in (_dict(result).get("confirmed_unsupported_mappings") or [])
            if isinstance(m, dict)]


def summary_metrics(result):
    """Three computed counts for the metric cards (no scores, no percentages)."""
    passed, total = check_counts(result)
    mappings = len(_mappings_of(result))
    failed = len(_failed_check_ids(result))
    unverified = len(_dict(result).get("unverified_runtime_semantics") or [])
    return {
        "checks_passed": passed,
        "checks_total": total,
        "checks_display": "%d / %d" % (passed, total),
        "confirmed_mappings": mappings,
        "failed_checks": failed,
        "confirmed_losses": mappings + failed,
        "unverified_areas": unverified,
    }


def metric_cards(result):
    """``[(label, value, help)]`` for the three metric cards."""
    m = summary_metrics(result)
    return [
        ("Checks passed", m["checks_display"],
         "Modelled checks that found no divergence, PASS (modelled), out of all modelled "
         "checks. A static model result only."),
        ("Confirmed losses", str(m["confirmed_losses"]),
         "%d confirmed unsupported mapping(s) + %d FAIL check(s)."
         % (m["confirmed_mappings"], m["failed_checks"])),
        ("Unverified runtime areas", str(m["unverified_areas"]),
         "Runtime semantics the verifier never grades; each needs a runtime experiment."),
    ]


def _mapping_sentence(m):
    jw = _dict(_dict(m.get("witness")).get("jenkins"))
    where = []
    if jw.get("stage") is not None:
        where.append("Jenkins stage '%s'" % jw.get("stage"))
    if jw.get("line") is not None:
        where.append("line %s" % jw.get("line"))
    if jw.get("artifacts"):
        where.append("artifacts %s" % jw.get("artifacts"))
    gitlab = _dict(m.get("witness")).get("gitlab")
    text = "'%s'" % m.get("id", "mapping")
    if where:
        text += " at " + ", ".join(where)
    if gitlab:
        text += " (GitLab side: %s)" % gitlab
    return text


def verdict_view(result):
    """Banner data derived from the result.

    Returns ``{key, label, colour_key, colour, explanation, overall}``. The
    explanation is plain text (escape it before putting it into HTML).
    """
    result = _dict(result)
    overall = result.get("overall")
    passed, total = check_counts(result)
    metrics = summary_metrics(result)
    base = {"overall": overall}
    if overall == OVERALL_LOSS:
        n_loss = metrics["confirmed_losses"]
        parts = ["%d of %d modelled checks passed, but %d confirmed %s found."
                 % (passed, total, n_loss, "loss was" if n_loss == 1 else "losses were")]
        mappings = _mappings_of(result)
        if mappings:
            parts.append("Lost in the conversion: " + "; ".join(
                _mapping_sentence(m) for m in mappings) + ".")
        failed = _failed_check_ids(result)
        if failed:
            parts.append("Failed check(s): " + "; ".join(
                "%s (%s)" % (cid, _dict(_checks_of(result)[cid]).get("summary", "FAIL"))
                for cid in failed) + ".")
        parts.append("The converted GitLab CI does not preserve this behavior.")
        base.update(key="loss", label=VERDICT_LOSS_LABEL, colour_key="red",
                    explanation=" ".join(parts))
    elif overall == OVERALL_INSUFFICIENT:
        unparsed = [u for u in (result.get("unparsed") or []) if isinstance(u, dict)]
        oom = [cid for cid, c in sorted(_checks_of(result).items())
               if _dict(c).get("verdict") == "OUT_OF_MODEL"]
        parts = ["%d of %d checks have a verdict; %d check(s) are outside the static model%s."
                 % (total - len(oom), total, len(oom),
                    " (" + ", ".join(oom) + ")" if oom else "")]
        if unparsed:
            first = unparsed[0]
            parts.append("%d unparsed item(s); first: %s line %s: %s."
                         % (len(unparsed), first.get("side"), first.get("line"),
                            first.get("reason")))
        parts.append("No verdict is given for the affected checks. This is NOT a pass.")
        base.update(key="out_of_model", label=VERDICT_OOM_LABEL, colour_key="amber",
                    explanation=" ".join(parts))
    elif overall == OVERALL_EQUIVALENT:
        scen = len(_dict(result.get("scenario_set")).get("scenarios") or [])
        text = ("No divergence found in %d of %d modelled checks%s. This is a static "
                "model result and does not prove runtime equivalence; %d runtime "
                "area(s) remain unverified."
                % (passed, total, " over %d scenarios" % scen if scen else "",
                   metrics["unverified_areas"]))
        base.update(key="static_pass", label=VERDICT_PASS_LABEL, colour_key="green",
                    explanation=text)
    else:
        base.update(key="unknown", label=VERDICT_UNKNOWN_LABEL, colour_key="grey",
                    explanation="The verifier returned an unrecognised verdict; "
                                "nothing is concluded.")
    base["colour"] = COLOURS[base["colour_key"]]
    return base


# ---- comparison panel -----------------------------------------------------

def _split_lines(text):
    if not isinstance(text, str) or text == "":
        return []
    return [ln[:-1] if ln.endswith("\r") else ln for ln in text.split("\n")]


def _clip(text):
    return text if len(text) <= MAX_DISPLAY_CHARS else text[:MAX_DISPLAY_CHARS] + "\u2026"


_CONTROL_RE = re.compile("[\\x00-\\x08\\x0b-\\x1f\\x7f\\u2028\\u2029]")


def _entry(no, text, highlight=False):
    shown = _clip(_CONTROL_RE.sub(" ", text))
    return {"no": no, "text": shown, "html": esc(shown) or "&nbsp;",
            "highlight": bool(highlight)}


def _valid_line(value, lines):
    return (isinstance(value, int) and not isinstance(value, bool)
            and 1 <= value <= len(lines))


def _gitlab_job_for(result, stage):
    jobs = [j for j in (_dict(_dict(_dict(result).get("models")).get("gitlab")).get("jobs")
                        or []) if isinstance(j, dict)]
    if isinstance(stage, str):
        for job in jobs:
            if str(job.get("name", "")).casefold() == stage.casefold():
                return job
    for job in jobs:
        if job.get("artifacts_paths"):
            return job
    return None


def _artifacts_block(lines, job):
    """[(line_no, text)] of the ``artifacts:`` block of ``job`` (may be empty)."""
    name, start = str(job.get("name", "")), job.get("line")
    if not (_valid_line(start, lines) and lines[start - 1].startswith(name)):
        start = next((i + 1 for i, ln in enumerate(lines) if ln.startswith(name + ":")), None)
        if start is None:
            return []
    for k in range(start, len(lines)):
        raw = lines[k]
        if raw.strip() == "":
            continue
        if not raw[:1].isspace():
            return []  # next top-level key: this job has no artifacts block
        if raw.strip().startswith("artifacts:"):
            indent = len(raw) - len(raw.lstrip())
            block = [(k + 1, raw)]
            for m in range(k + 1, len(lines)):
                nxt = lines[m]
                if nxt.strip() == "" or len(nxt) - len(nxt.lstrip()) <= indent:
                    break
                block.append((m + 1, nxt))
            return block
    return []


def _mapping_panel(result, mapping, jlines, glines):
    witness = _dict(mapping.get("witness"))
    jw = _dict(witness.get("jenkins"))
    line = jw.get("line")
    jenkins = {"line": line, "found": False, "lines": [], "note": ""}
    if not jlines:
        jenkins["note"] = "Jenkinsfile text is not available."
    elif not _valid_line(line, jlines):
        jenkins["note"] = ("Line %s is not available in the Jenkinsfile text (%d line(s))."
                           % (line, len(jlines)))
    else:
        jenkins["found"] = True
        lo, hi = max(1, line - CONTEXT_LINES), min(len(jlines), line + CONTEXT_LINES)
        jenkins["lines"] = [_entry(n, jlines[n - 1], n == line) for n in range(lo, hi + 1)]
        jenkins["highlight_text"] = jlines[line - 1]
    job = _gitlab_job_for(result, jw.get("stage"))
    gitlab = {"found": False, "job": job.get("name") if job else None, "lines": [],
              "paths": [str(p) for p in (job.get("artifacts_paths") or [])] if job else [],
              "note": "", "marker": "%s: %s" % (
                  NO_COUNTERPART_MARKER, witness.get("gitlab")
                  or "no corresponding mechanism in the supported model")}
    if not glines:
        gitlab["note"] = "GitLab CI text is not available."
    elif job is None:
        gitlab["note"] = "No matching GitLab job found."
    else:
        block = _artifacts_block(glines, job)
        if block:
            gitlab["found"] = True
            gitlab["lines"] = [_entry(n, t) for n, t in block]
        else:
            gitlab["note"] = "The matching GitLab job '%s' has no artifacts block." % job.get(
                "name")
    explanation = ""
    if mapping.get("id") == FINGERPRINT_MAPPING_ID:
        paths = ", ".join(gitlab["paths"]) if gitlab["paths"] else ""
        explanation = (
            ("The converted GitLab CI keeps the artifact paths (%s)" % paths if paths else
             "The converted GitLab CI declares no artifact paths for the matching job")
            + ", but the supported GitLab CI model has no mapping for Jenkins "
              "fingerprinting (fingerprint: true).")
    return {
        "explanation": explanation,
        "id": mapping.get("id"), "label": mapping.get("label"),
        "reason": mapping.get("reason"), "stage": jw.get("stage"), "line": line,
        "artifacts": jw.get("artifacts"), "jenkins": jenkins, "gitlab": gitlab,
        "title": "Lost in conversion: %s" % mapping.get("id", "mapping"),
    }


def comparison_view(result, jenkins_text, gitlab_text):
    """Data for the Jenkins-vs-GitLab panel. Never raises on odd input.

    Every ``html`` field is escaped; ``text`` fields are raw (for st.code).
    """
    result = _dict(result)
    jlines, glines = _split_lines(jenkins_text), _split_lines(gitlab_text)
    panels = [_mapping_panel(result, m, jlines, glines) for m in _mappings_of(result)]
    failed = []
    for cid in _failed_check_ids(result):
        check = _dict(_checks_of(result)[cid])
        failed.append({"id": cid, "summary": check.get("summary", ""),
                       "divergences": check.get("divergences"),
                       "scenarios_evaluated": check.get("scenarios_evaluated"),
                       "witness": witness_view(check.get("witness"))})
    neutral = ""
    if not panels and not failed:
        neutral = NEUTRAL_NO_LOSS
        if result.get("overall") == OVERALL_INSUFFICIENT:
            neutral += (" Part of the input is outside the static model, so the checks "
                        "are inconclusive (see the technical evidence).")
        elif result.get("overall") == OVERALL_EQUIVALENT:
            neutral += " This is a static model result only."
    return {"mode": "neutral" if neutral else "findings", "neutral_message": neutral,
            "mappings": panels, "failed_checks": failed}


def unverified_items(result):
    return [{"id": i.get("id"), "description": i.get("description", ""),
             "requires": i.get("requires") or "runtime experiment"}
            for i in (_dict(result).get("unverified_runtime_semantics") or [])
            if isinstance(i, dict)]


# ---- CI gate section (static text; test_parity_dashboard_logic.py compares the
# exit codes with parity_gate.exit_code_for so docs/UI cannot drift) ----------

GATE_EXIT_ROWS = (
    {"outcome": "STATIC_MODEL_PASS", "overall": OVERALL_EQUIVALENT,
     "non_strict": 0, "strict": 0,
     "meaning": "static model pass only; does NOT prove runtime equivalence"},
    {"outcome": "CONFIRMED_LOSS", "overall": OVERALL_LOSS, "non_strict": 0, "strict": 1,
     "meaning": "a check failed or a confirmed unsupported mapping exists"},
    {"outcome": "OUT_OF_MODEL", "overall": OVERALL_INSUFFICIENT, "non_strict": 0, "strict": 2,
     "meaning": "unsupported syntax; no verdict for the affected checks; not a pass"},
    {"outcome": "USAGE / IO ERROR", "overall": "n/a", "non_strict": 3, "strict": 3,
     "meaning": "missing or unreadable input, unwritable output, missing PyYAML, "
                "bad arguments"},
)


def gate_exit_code_table():
    """Rows ``{outcome, overall, non_strict, strict, meaning}`` (fresh copies)."""
    return [dict(row) for row in GATE_EXIT_ROWS]


def gate_exit_code_markdown():
    lines = ["| Outcome | Non-strict exit (default) | Strict exit | Meaning |",
             "|---|---|---|---|"]
    for r in gate_exit_code_table():
        lines.append("| `%s` | %d | %d | %s |" % (
            r["outcome"], r["non_strict"], r["strict"], r["meaning"]))
    return "\n".join(lines)


def gate_overview():
    """Accurate static text for the 'CI gate' tab (see PARITY_GATE.md)."""
    return {
        "intro": ("PARITY plugs into GitLab CI as a verification gate: "
                  "`parity_gate.py` runs the same deterministic verifier and turns the "
                  "result into downloadable evidence and an exit code."),
        "facts": [
            "CLI: `python parity_gate.py --jenkinsfile PATH --gitlab-ci PATH "
            "[--json-out PATH] [--md-out PATH] [--strict]`.",
            "CI job `parity-gate` runs the gate on the REAL fixtures "
            "(`tests/fixtures/real/`) and prints the Markdown summary in the job log.",
            "Evidence is uploaded as downloadable CI job artifacts: "
            "`parity-evidence/parity-evidence.json` (JSON) and "
            "`parity-evidence/parity-summary.md` (Markdown), kept 30 days.",
            "Non-strict by default (`PARITY_STRICT: \"0\"`): the job reports the result "
            "and does not fail on a loss.",
            "Strict mode: `--strict` or `PARITY_STRICT=1` (also `true`/`yes`). The manual "
            "job `parity-gate-strict` demonstrates it.",
        ],
        "workflow": [
            "**Convert** the Jenkinsfile with GitLab Duo (Convert to GitLab CI/CD); it "
            "opens a merge request.",
            "**Pipeline** of the merge request runs `parity-gate`. In this repository it "
            "checks the bundled real pair; for another project, point the two paths at "
            "its files (see PARITY_GATE.md).",
            "**Review**: the reviewer downloads `parity-summary.md` and "
            "`parity-evidence.json` from the job artifacts (the summary is also in the "
            "job log).",
            "**Decide**: fix the YAML, accept a documented loss, hand-review out-of-model "
            "parts, or run runtime experiments for the unverified areas.",
        ],
        "not_implemented": ("Not implemented: the gate does not post merge request "
                            "comments and makes no network calls; reviewers download "
                            "the artifacts."),
    }


# ==========================================================================
# PARITY 2.0 redesign helpers. Purely additive: nothing above is changed in
# behaviour. All values are computed from the verifier result (counts only: no
# scores, no confidence or accuracy figures) and every dynamic string is passed
# through esc() before it reaches HTML. No streamlit import here.
# ==========================================================================

STATE_LOSS = "CONFIRMED_LOSS"
STATE_PASS = "STATIC_MODEL_PASS"
STATE_OOM = "OUT_OF_MODEL"
STATE_UNKNOWN = "UNKNOWN"

# state -> (headline, css tone, colour). Colours follow the approved palette.
STATE_VIEW = {
    STATE_LOSS: ("CONFIRMED LOSS", "loss", "#B42318"),
    STATE_PASS: ("STATIC MODEL PASS", "pass", "#067647"),
    STATE_OOM: ("OUT OF MODEL", "oom", "#B54708"),
    STATE_UNKNOWN: ("UNKNOWN RESULT", "unknown", "#515A6B"),
}
_OVERALL_STATE = {OVERALL_LOSS: STATE_LOSS, OVERALL_EQUIVALENT: STATE_PASS,
                  OVERALL_INSUFFICIENT: STATE_OOM}

STATIC_MODEL_NOTICE_TEXT = STATIC_MODEL_NOTICE  # alias kept for readability in tests
FINGERPRINT_SENTENCE = ("Jenkins artifact fingerprinting has no corresponding mapping in "
                        "the supported GitLab CI model.")
CHECK_TITLES = {"branch_gate": "Branch gating", "artifact_paths": "Artifact paths",
                "always_cleanup": "Always cleanup", "build_commands": "Build commands"}
CHECK_DISPLAY_ORDER = ("branch_gate", "artifact_paths", "always_cleanup", "build_commands")
CHECK_TONES = {"PASS": "pass", "FAIL": "loss", "OUT_OF_MODEL": "oom"}
LABEL_TONES = {LABEL_REAL: "real", LABEL_SYNTHETIC: "synth", LABEL_USER: "user"}
TONES = ("loss", "pass", "oom", "unknown", "accent", "real", "synth", "user")
SOURCE_MAX_LINES = 80

ACCENT = "#3B4CCA"


def verdict_state(result):
    """CONFIRMED_LOSS / STATIC_MODEL_PASS / OUT_OF_MODEL / UNKNOWN from the verifier's
    overall value (the same classification parity_gate uses)."""
    return _OVERALL_STATE.get(_dict(result).get("overall"), STATE_UNKNOWN)


def _check_title(cid):
    return CHECK_TITLES.get(cid, str(cid))


def verdict_card(result):
    """Data for the prominent verdict card. Plain text (escape before HTML)."""
    result = _dict(result)
    state = verdict_state(result)
    headline, tone, colour = STATE_VIEW[state]
    passed, total = check_counts(result)
    if state == STATE_LOSS:
        sentences = []
        for mapping in _mappings_of(result):
            if mapping.get("id") == FINGERPRINT_MAPPING_ID:
                sentence = FINGERPRINT_SENTENCE
            else:
                sentence = "Confirmed unsupported mapping '%s': %s" % (
                    mapping.get("id", "mapping"),
                    str(mapping.get("reason") or "no corresponding mapping in the "
                        "supported GitLab CI model.").rstrip("."))
                sentence += "."
            if sentence not in sentences:
                sentences.append(sentence)
        for cid in _failed_check_ids(result):
            summary = str(_dict(_checks_of(result)[cid]).get("summary") or "FAIL").rstrip(".")
            sentences.append("Modelled check '%s' failed: %s." % (_check_title(cid), summary))
        explanation = " ".join(sentences) or (
            "The static model found a loss in the conversion.")
    elif state == STATE_PASS:
        explanation = (
            "No divergence was found by the %d modelled checks. Jenkins behaviour outside "
            "the model (%d runtime area(s)) is not verified."
            % (total, summary_metrics(result)["unverified_areas"]))
    elif state == STATE_OOM:
        oom = [cid for cid in CHECK_ORDER
               if _dict(_checks_of(result).get(cid)).get("verdict") == "OUT_OF_MODEL"]
        explanation = (
            "Part of the input is outside the supported static model, so %d of %d modelled "
            "check(s) have no verdict. This is not a pass."
            % (len(oom) if oom else total - passed, total))
    else:
        explanation = "The verifier returned an unrecognised result; nothing is concluded."
    return {"state": state, "headline": headline, "tone": tone, "colour": colour,
            "explanation": explanation, "notice": STATIC_MODEL_NOTICE,
            "overall": result.get("overall")}


def summary_cards(result):
    """The three summary cards, computed from the verifier output only."""
    result = _dict(result)
    card = verdict_card(result)
    metrics = summary_metrics(result)
    mappings = metrics["confirmed_mappings"]
    return [
        {"title": "Overall verdict", "value": card["headline"], "tone": card["tone"],
         "caption": "Static model check"},
        {"title": "Modelled checks passed", "value": metrics["checks_display"],
         "tone": "unknown",
         "caption": "Checks with PASS (modelled): no divergence in the modelled scenarios"},
        {"title": "Confirmed unsupported mappings", "value": str(mappings),
         "tone": "loss" if mappings else "unknown",
         "caption": "Jenkins features with no mapping in the supported GitLab CI model"},
    ]


def modelled_checks_view(result):
    """The four supported checks in display order (only those present in the result)."""
    checks = _checks_of(result)
    rows = []
    for cid in CHECK_DISPLAY_ORDER:
        check = _dict(checks.get(cid))
        if not check:
            continue
        label, _colour = check_verdict_view(check.get("verdict"))
        rows.append({"id": cid, "title": _check_title(cid), "verdict": check.get("verdict"),
                     "label": label, "tone": CHECK_TONES.get(check.get("verdict"), "unknown"),
                     "summary": str(check.get("summary") or ""),
                     "scenarios": check.get("scenarios_evaluated"),
                     "divergences": check.get("divergences")})
    return rows


# ---- HTML builders (every dynamic value escaped; one line, no blank lines) ----

def pill_html(text, tone):
    tone = tone if tone in TONES else "unknown"
    return '<span class="p-badge p-b-%s">%s</span>' % (tone, esc(text))


def label_pill_html(label):
    return pill_html(label, LABEL_TONES.get(label, "user"))


def header_html():
    return ('<div class="p-header"><h1 class="p-title">%s</h1>'
            '<p class="p-subtitle">%s</p></div>'
            % (esc(DASHBOARD_TITLE), esc(DASHBOARD_SUBTITLE)))


def verdict_card_html(card):
    return ('<div class="p-verdict p-%s"><div class="p-vkicker">Verification verdict</div>'
            '<h2 class="p-vlabel">%s</h2><p class="p-vexp">%s</p><p class="p-vnote">%s</p></div>'
            % (card["tone"] if card["tone"] in TONES else "unknown", esc(card["headline"]),
               esc(card["explanation"]), esc(card["notice"])))


def summary_cards_html(cards):
    body = "".join(
        '<div class="p-scard p-%s"><div class="p-stitle">%s</div><div class="p-svalue">%s</div>'
        '<div class="p-scaption">%s</div></div>'
        % (c["tone"] if c["tone"] in TONES else "unknown", esc(c["title"]), esc(c["value"]),
           esc(c["caption"])) for c in cards)
    return '<div class="p-cards">%s</div>' % body


def modelled_checks_html(rows):
    body = "".join(
        '<div class="p-check"><div class="p-check-title">%s</div>%s<div class="p-muted">%s</div>'
        '</div>' % (esc(r["title"]), pill_html(r["label"], r["tone"]), esc(r["summary"]))
        for r in rows)
    return '<div class="p-cards">%s</div>' % body


def status_row_html(outcome):
    """Truthful state of the finished (synchronous) run plus the input labels."""
    return ('<div class="p-status">%s %s %s</div>' % (
        pill_html("Status: Complete", "accent"),
        "Jenkinsfile: " + label_pill_html(outcome.label_jenkins),
        "GitLab CI: " + label_pill_html(outcome.label_gitlab)))


def _source_entries(lines, highlights, max_lines=SOURCE_MAX_LINES):
    """Entries for a source pane: the first ``max_lines`` lines plus a line of context
    around every highlighted line; omitted ranges become one placeholder entry."""
    total = len(lines)
    keep = set(range(1, min(total, max_lines) + 1))
    for h in highlights:
        keep.update(k for k in (h - 1, h, h + 1) if 1 <= k <= total)
    entries, prev = [], 0

    def gap(count):
        text = "\u2026 %d more line(s) not shown" % count
        return {"no": "\u2026", "text": text, "html": esc(text), "highlight": False, "gap": True}

    for k in sorted(keep):
        if k != prev + 1:
            entries.append(gap(k - prev - 1))
        entries.append(_entry(k, lines[k - 1], k in highlights))
        prev = k
    if prev < total:
        entries.append(gap(total - prev))
    return entries


def source_view(result, jenkins_text, gitlab_text):
    """Side-by-side data: the actual Jenkins source and converted GitLab CI with the
    lines of the REAL findings highlighted, plus one finding per confirmed mapping."""
    view = comparison_view(result, jenkins_text, gitlab_text)
    jlines, glines = _split_lines(jenkins_text), _split_lines(gitlab_text)
    jhl = {p["line"] for p in view["mappings"] if p["jenkins"]["found"]}
    ghl = {e["no"] for p in view["mappings"] for e in p["gitlab"]["lines"]}
    findings = []
    for p in view["mappings"]:
        findings.append({
            "id": p["id"], "title": p["title"], "reason": p["reason"] or "",
            "explanation": p["explanation"], "stage": p["stage"], "line": p["line"],
            "job": p["gitlab"]["job"], "paths": p["gitlab"]["paths"],
            "marker": p["gitlab"]["marker"], "jenkins_found": p["jenkins"]["found"],
            "note": " ".join(n for n in (p["jenkins"]["note"], p["gitlab"]["note"]) if n)})
    return {
        "jenkins": {"entries": _source_entries(jlines, jhl), "highlighted": sorted(jhl),
                    "available": bool(jlines)},
        "gitlab": {"entries": _source_entries(glines, ghl), "highlighted": sorted(ghl),
                   "available": bool(glines)},
        "findings": findings, "failed_checks": view["failed_checks"],
        "neutral_message": view["neutral_message"], "mode": view["mode"],
    }


def pane_html(head, entries, hl_tone, available=True, markers=()):
    if not available:
        body = '<div class="p-note">Source text is not available.</div>'
    else:
        body = "".join(
            '<span class="p-line%s"><span class="p-no">%s</span>%s</span>'
            % (" p-hl-" + hl_tone if e["highlight"] else "", esc(e["no"]), e["html"])
            for e in entries)
    body += "".join('<span class="p-line p-miss">\u26D4 %s</span>' % esc(m) for m in markers)
    return '<div class="p-pane"><div class="p-pane-head">%s</div>%s</div>' % (esc(head), body)


def compare_html(view):
    markers = [f["marker"] for f in view["findings"]]
    left = pane_html("Jenkins source (Jenkinsfile)", view["jenkins"]["entries"], "loss",
                     view["jenkins"]["available"])
    right = pane_html("Converted GitLab CI", view["gitlab"]["entries"], "info",
                      view["gitlab"]["available"], markers)
    return '<div class="p-compare">%s%s</div>' % (left, right)


def finding_html(f):
    where = []
    if f["stage"] is not None:
        where.append("Jenkins stage %s" % f["stage"])
    if f["line"] is not None:
        where.append("Jenkinsfile line %s" % f["line"])
    if f["job"]:
        where.append("GitLab job %s" % f["job"])
    if f["paths"]:
        where.append("artifact paths %s" % ", ".join(f["paths"]))
    parts = ['<div class="p-finding"><div class="p-card-title">\u26D4 %s</div>' % esc(f["title"])]
    if f["reason"]:
        parts.append('<div class="p-muted">%s</div>' % esc(f["reason"]))
    if f["explanation"]:
        parts.append('<div class="p-explain">%s</div>' % esc(f["explanation"]))
    if where:
        parts.append('<div class="p-muted">%s</div>' % esc(" \u00B7 ".join(where)))
    if f["note"]:
        parts.append('<div class="p-muted">%s</div>' % esc(f["note"]))
    parts.append("</div>")
    return "".join(parts)


def mapping_card_html(mapping):
    """Compact card for one confirmed unsupported mapping (separate from the checks)."""
    jw = _dict(_dict(mapping.get("witness")).get("jenkins"))
    facts = []
    if jw.get("stage") is not None:
        facts.append("stage %s" % jw.get("stage"))
    if jw.get("line") is not None:
        facts.append("line %s" % jw.get("line"))
    if jw.get("artifacts"):
        facts.append("artifacts %s" % jw.get("artifacts"))
    gitlab = _dict(mapping.get("witness")).get("gitlab")
    return ('<div class="p-finding"><div class="p-card-title">%s %s</div>'
            '<div class="p-muted">%s</div><div class="p-muted">Jenkins: %s</div>'
            '<div class="p-muted">GitLab: %s</div></div>'
            % (pill_html("UNSUPPORTED", "loss"), esc(mapping.get("id", "")),
               esc(mapping.get("reason", "")), esc(", ".join(facts) or "n/a"),
               esc(gitlab or "n/a")))
