"""Tests for parity_gui_logic.py (unittest; needs PyYAML only).

The optional AppTest smoke tests at the bottom need streamlit and are skipped
cleanly when it is not installed.

Fixtures under tests/fixtures/real/ are REAL files (see PROVENANCE.md).
Fixtures under tests/fixtures/synthetic/ are SYNTHETIC CALIBRATION FIXTURES.
"""

import importlib.util
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import parity_gui_logic as gl  # noqa: E402
import parity_verify as pv  # noqa: E402

REAL = os.path.join(HERE, "fixtures", "real")
SYN = os.path.join(HERE, "fixtures", "synthetic")
REAL_J = os.path.join(REAL, "Jenkinsfile")
REAL_G = os.path.join(REAL, "gitlab-ci.converted.yml")
SYN_J_NOFP = os.path.join(SYN, "Jenkinsfile.no-fingerprint")
FOUR = ("branch_gate", "always_cleanup", "artifact_paths", "build_commands")

GOOD_J = b"pipeline {\n  agent any\n  stages {\n    stage('A') {\n      steps { sh 'echo a' }\n    }\n  }\n}\n"
GOOD_G = b"stages: [a]\na:\n  stage: a\n  script:\n    - echo a\n"


def read(path):
    with open(path, "rb") as fh:
        return fh.read()


class InputValidationTests(unittest.TestCase):
    def test_size_cap_rejects_with_friendly_message(self):
        res = gl.validate_input(b"a" * (gl.MAX_INPUT_BYTES + 1), "Jenkinsfile")
        self.assertFalse(res.ok)
        self.assertIn("too large", res.error)
        self.assertNotIn("Traceback", res.error)

    def test_size_cap_boundary_accepted(self):
        res = gl.validate_input(b"a" * gl.MAX_INPUT_BYTES, "Jenkinsfile")
        self.assertTrue(res.ok)

    def test_huge_input_never_reaches_verifier(self):
        class Boom:
            def verify_bytes(self, *a, **k):
                raise AssertionError("verifier must not be called")

            render_json = verify_bytes
        out = gl.run_verification(b"x" * (gl.MAX_INPUT_BYTES + 10), GOOD_G, Boom())
        self.assertFalse(out.ok)
        self.assertIn("too large", out.error)

    def test_invalid_utf8(self):
        res = gl.validate_input(b"pipeline \xff\xfe {", "Jenkinsfile")
        self.assertFalse(res.ok)
        self.assertIn("UTF-8", res.error)

    def test_empty_and_whitespace_only(self):
        for raw in (b"", b"   \n\t\n", "", None):
            res = gl.validate_input(raw, "GitLab CI YAML", "yaml")
            self.assertFalse(res.ok, repr(raw))
            self.assertTrue(res.error)

    def test_nul_bytes_rejected(self):
        self.assertFalse(gl.validate_input(b"a\x00b", "Jenkinsfile").ok)

    def test_yaml_alias_bomb_rejected(self):
        bomb = b"a: &a [x]\n" + b"".join(b"b%d: [*a, *a, *a]\n" % i for i in range(30))
        res = gl.validate_input(bomb, "GitLab CI YAML", "yaml")
        self.assertFalse(res.ok)
        self.assertIn("alias", res.error)

    def test_str_input_is_encoded(self):
        res = gl.validate_input("pipeline {}", "Jenkinsfile")
        self.assertTrue(res.ok)
        self.assertEqual(res.data, b"pipeline {}")

    def test_run_verification_empty_input_is_error_not_crash(self):
        out = gl.run_verification(b"", b"")
        self.assertFalse(out.ok)
        self.assertIn("empty", out.error)
        self.assertIsNone(out.result)

    def test_run_verification_invalid_utf8_is_error_not_crash(self):
        out = gl.run_verification(b"\xff\xfe\x00", GOOD_G)
        self.assertFalse(out.ok)
        self.assertIn("UTF-8", out.error)

    def test_verifier_exception_is_caught(self):
        class Boom:
            def verify_bytes(self, *a, **k):
                raise ValueError("secret internals")

            render_json = verify_bytes
        out = gl.run_verification(GOOD_J, GOOD_G, Boom())
        self.assertFalse(out.ok)
        self.assertIn("ValueError", out.error)
        self.assertNotIn("secret internals", out.error)

    def test_verifier_import_failure_is_friendly(self):
        def bad_import(name):
            raise ImportError("No module named 'parity_verify'")
        module, err = gl.load_verifier(bad_import)
        self.assertIsNone(module)
        self.assertIn("could not be imported", err)
        self.assertNotIn("Traceback", err)

    def test_missing_pyyaml_is_friendly(self):
        class NoYaml:
            yaml = None
        module, err = gl.load_verifier(lambda name: NoYaml)
        self.assertIsNone(module)
        self.assertIn("PyYAML", err)

    def test_load_verifier_ok(self):
        module, err = gl.load_verifier()
        self.assertIs(module, pv)
        self.assertIsNone(err)


class VerifierPassThroughTests(unittest.TestCase):
    """Malformed / unsupported inputs surface the verifier's own output."""

    def test_malformed_yaml_is_insufficient_evidence(self):
        out = gl.run_verification(GOOD_J, b"stages: [a\na: {")
        self.assertTrue(out.ok)
        res = out.result
        self.assertEqual(res["overall"], "INSUFFICIENT_EVIDENCE")
        self.assertTrue(any(u["side"] == "gitlab" and "YAML" in u["reason"]
                            for u in res["unparsed"]))
        for c in FOUR:
            self.assertEqual(res["checks"][c]["verdict"], "OUT_OF_MODEL")

    def test_unbalanced_jenkins_braces_is_insufficient_evidence(self):
        out = gl.run_verification(b"pipeline {\n  agent any\n  stages {\n", GOOD_G)
        self.assertTrue(out.ok)
        res = out.result
        self.assertEqual(res["overall"], "INSUFFICIENT_EVIDENCE")
        self.assertTrue(any(u["side"] == "jenkins" and "unbalanced" in u["reason"]
                            for u in res["unparsed"]))
        for c in FOUR:
            self.assertEqual(res["checks"][c]["verdict"], "OUT_OF_MODEL")

    def test_unsupported_syntax_is_insufficient_evidence_with_line_numbers(self):
        jenkins = (b"pipeline {\n  agent any\n  stages {\n    stage('A') {\n"
                   b"      parallel {\n        stage('B') { steps { sh 'x' } }\n      }\n"
                   b"    }\n  }\n}\n")
        out = gl.run_verification(jenkins, GOOD_G)
        self.assertTrue(out.ok)
        res = out.result
        self.assertEqual(res["overall"], "INSUFFICIENT_EVIDENCE")
        self.assertNotEqual(res["overall"], "CHECKED_EQUIVALENT_WITH_UNVERIFIED_RUNTIME")
        self.assertTrue(res["unparsed"])
        rows = gl.unparsed_rows(res)
        self.assertEqual(len(rows), len(res["unparsed"]))
        for row in rows:
            self.assertIsInstance(row["line"], int)
            self.assertEqual(row["side"], "jenkins")
        self.assertTrue(any("OUT_OF_MODEL" == c["verdict"] for c in res["checks"].values()))

    def test_unsupported_gitlab_syntax_is_out_of_model(self):
        gitlab = GOOD_G + b"include: other.yml\n"
        out = gl.run_verification(GOOD_J, gitlab)
        self.assertTrue(out.ok)
        self.assertEqual(out.result["overall"], "INSUFFICIENT_EVIDENCE")
        self.assertTrue(any(u["side"] == "gitlab" for u in out.result["unparsed"]))

    def test_non_mapping_yaml(self):
        out = gl.run_verification(GOOD_J, b"- a\n- b\n")
        self.assertTrue(out.ok)
        self.assertEqual(out.result["overall"], "INSUFFICIENT_EVIDENCE")

    def test_binary_garbage_never_crashes(self):
        for blob in (b"\x01\x02\x03 {{{{", b"\xef\xbb\xbfpipeline {", b"{" * 5000,
                     b"[" * 5000, b"a: " + b"[" * 2000):
            for pair in ((blob, GOOD_G), (GOOD_J, blob)):
                out = gl.run_verification(*pair)
                self.assertIsInstance(out, gl.Outcome)
                if out.ok:
                    self.assertNotEqual(out.result["overall"],
                                        "CHECKED_EQUIVALENT_WITH_UNVERIFIED_RUNTIME")
                else:
                    self.assertTrue(out.error)

    def test_equivalent_overall_is_never_presented_as_plain_equivalent(self):
        headline, _, expl = gl.overall_view("CHECKED_EQUIVALENT_WITH_UNVERIFIED_RUNTIME")
        self.assertIn("RUNTIME UNVERIFIED", headline)
        self.assertNotIn("PASSED", headline.upper())
        self.assertIn("NOT proof", expl)
        for overall in ("CONFIRMED_LOSS", "INSUFFICIENT_EVIDENCE"):
            self.assertTrue(gl.overall_view(overall)[0])


class RealExampleTests(unittest.TestCase):
    def test_real_example_loads_and_yields_verifier_verdict(self):
        ex = gl.load_real_example()
        self.assertTrue(ex.ok, ex.error)
        self.assertEqual(ex.jenkins, read(REAL_J))
        self.assertEqual(ex.gitlab, read(REAL_G))
        self.assertIn("parity/fixture-1", ex.provenance)
        out = gl.run_verification(ex.jenkins, ex.gitlab)
        self.assertTrue(out.ok, out.error)
        expected = pv.verify_bytes(read(REAL_J), read(REAL_G))
        self.assertEqual(out.result, expected)
        self.assertIn(out.result["overall"],
                      ("CONFIRMED_LOSS", "INSUFFICIENT_EVIDENCE",
                       "CHECKED_EQUIVALENT_WITH_UNVERIFIED_RUNTIME"))
        # the verifier itself reports the fingerprint loss for the real conversion
        self.assertEqual([m["id"] for m in out.result["confirmed_unsupported_mappings"]],
                         ["fingerprint"])

    def test_real_example_missing_files_handled(self):
        with tempfile.TemporaryDirectory() as tmp:
            ex = gl.load_real_example(tmp)
        self.assertFalse(ex.ok)
        self.assertIn("missing", ex.error)

    def test_real_example_missing_provenance_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in (gl.REAL_JENKINSFILE, gl.REAL_GITLAB_CI):
                with open(os.path.join(tmp, name), "wb") as fh:
                    fh.write(read(os.path.join(REAL, name)))
            ex = gl.load_real_example(tmp)
        self.assertTrue(ex.ok)
        self.assertEqual(ex.provenance, "")
        self.assertEqual(gl.provenance_summary(ex.provenance), [])

    def test_provenance_summary_reflects_provenance_md(self):
        ex = gl.load_real_example()
        bullets = gl.provenance_summary(ex.provenance)
        joined = "\n".join(bullets)
        self.assertIn("b4f460986be4", joined)
        self.assertIn("942c801a60f1", joined)
        self.assertIn("parity/fixture-1", joined)
        rows = gl.parse_provenance_table(ex.provenance)
        self.assertEqual(len(rows), 2)

    def test_paths_are_relative_to_module(self):
        self.assertEqual(str(gl.REAL_DIR), os.path.join(ROOT, "tests", "fixtures", "real"))


class LabelTests(unittest.TestCase):
    def test_real_fixtures_labelled_real(self):
        self.assertEqual(gl.classify_input(read(REAL_J)), gl.LABEL_REAL)
        self.assertEqual(gl.classify_input(read(REAL_G)), gl.LABEL_REAL)

    def test_synthetic_fixtures_labelled_synthetic_never_real(self):
        for name in sorted(os.listdir(SYN)):
            if name == "README.md":
                continue
            label = gl.classify_input(read(os.path.join(SYN, name)))
            self.assertEqual(label, gl.LABEL_SYNTHETIC, name)
            self.assertNotEqual(label, gl.LABEL_REAL, name)

    def test_user_supplied(self):
        self.assertEqual(gl.classify_input(GOOD_J), gl.LABEL_USER)
        self.assertEqual(gl.classify_input(read(REAL_J) + b"\n# edited\n"), gl.LABEL_USER)
        self.assertEqual(gl.classify_input(None), gl.LABEL_USER)

    def test_combine(self):
        R, S, U = gl.LABEL_REAL, gl.LABEL_SYNTHETIC, gl.LABEL_USER
        self.assertEqual(gl.combine_labels(R, R), R)
        self.assertEqual(gl.combine_labels(R, S), S)
        self.assertEqual(gl.combine_labels(S, S), S)
        self.assertEqual(gl.combine_labels(R, U), U)
        self.assertEqual(gl.combine_labels(S, U), U)

    def test_outcome_labels(self):
        real = gl.run_verification(read(REAL_J), read(REAL_G))
        self.assertEqual(real.label, gl.LABEL_REAL)
        syn = gl.run_verification(read(REAL_J), read(os.path.join(SYN, "cal-d1-wrong-branch.yml")))
        self.assertEqual(syn.label_jenkins, gl.LABEL_REAL)
        self.assertEqual(syn.label_gitlab, gl.LABEL_SYNTHETIC)
        self.assertEqual(syn.label, gl.LABEL_SYNTHETIC)
        user = gl.run_verification(GOOD_J, GOOD_G)
        self.assertEqual(user.label, gl.LABEL_USER)

    def test_label_texts(self):
        self.assertEqual(gl.LABEL_REAL, "REAL conversion evidence")
        self.assertEqual(gl.LABEL_SYNTHETIC, "SYNTHETIC calibration fixture")
        self.assertEqual(gl.LABEL_USER, "USER-SUPPLIED")


class CalibrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cal = gl.run_calibration()

    def test_calibration_summary_not_run(self):
        self.assertEqual(gl.calibration_summary(None), "Not run")

    def test_rows_cover_all_cases(self):
        self.assertEqual(self.cal["total"], len(gl.CALIBRATION_CASES))
        self.assertEqual(len(self.cal["rows"]), self.cal["total"])
        self.assertEqual(self.cal["matched"], sum(1 for r in self.cal["rows"] if r["match"]))

    def test_every_synthetic_gitlab_fixture_is_covered(self):
        used = {c.gitlab_file for c in gl.CALIBRATION_CASES}
        on_disk = {n for n in os.listdir(SYN) if n.endswith(".yml")}
        self.assertEqual(used, on_disk)

    def test_actual_comes_from_real_verifier_runs(self):
        # Recompute independently from the verifier and compare to the table.
        for case, row in zip(gl.CALIBRATION_CASES, self.cal["rows"]):
            jdir = REAL if case.jenkins_dir == "real" else SYN
            res = pv.verify_bytes(read(os.path.join(jdir, case.jenkins_file)),
                                  read(os.path.join(SYN, case.gitlab_file)))
            actual = {c: res["checks"][c]["verdict"] for c in FOUR}
            self.assertEqual(row["actual"], gl.describe_expectation(actual, res["overall"]))
            self.assertEqual(row["match"],
                             actual == case.expected_checks
                             and res["overall"] == case.expected_overall)

    def test_matches_repo_defined_expectations(self):
        # Expectations from tests/fixtures/synthetic/README.md + test_parity_verify.py:
        # D1/D2/D3 -> exactly one failing check; C1 -> all PASS.
        by_id = {c.case_id: row for c, row in zip(gl.CALIBRATION_CASES, self.cal["rows"])}
        self.assertEqual(self.cal["matched"], self.cal["total"],
                         [r for r in self.cal["rows"] if not r["match"]])
        self.assertIn("branch_gate FAIL", by_id["CAL-D1"]["actual"])
        self.assertIn("always_cleanup FAIL", by_id["CAL-D2"]["actual"])
        self.assertIn("artifact_paths FAIL", by_id["CAL-D3"]["actual"])
        self.assertIn("all four checks PASS", by_id["CAL-C1 (no fingerprint)"]["actual"])
        self.assertIn("CHECKED_EQUIVALENT_WITH_UNVERIFIED_RUNTIME",
                      by_id["CAL-C1 (no fingerprint)"]["actual"])
        self.assertIn("CONFIRMED_LOSS", by_id["CAL-C1 (real Jenkinsfile)"]["actual"])

    def test_calibration_detects_a_wrong_verifier(self):
        """A verifier that always says PASS must produce mismatches (the table is
        computed, not hardcoded)."""
        class AlwaysPass:
            yaml = True

            def verify_bytes(self, j, g):
                return {"overall": "CHECKED_EQUIVALENT_WITH_UNVERIFIED_RUNTIME",
                        "checks": {c: {"verdict": "PASS"} for c in FOUR}}
        cal = gl.run_calibration(AlwaysPass())
        self.assertEqual(cal["total"], len(gl.CALIBRATION_CASES))
        self.assertEqual(cal["matched"], 1)  # only CAL-C1 (no fingerprint) coincides
        self.assertEqual(gl.calibration_summary(cal), "1 of %d fixtures matched the "
                         "repo-defined expectations" % cal["total"])

    def test_missing_fixture_reported_not_invented(self):
        orig = gl.SYNTHETIC_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                gl.SYNTHETIC_DIR = gl.pathlib.Path(tmp)
                cal = gl.run_calibration()
        finally:
            gl.SYNTHETIC_DIR = orig
        self.assertEqual(cal["matched"], 0)
        for row in cal["rows"]:
            self.assertFalse(row["match"])
            self.assertIn("not run", row["actual"])


class EvidenceTests(unittest.TestCase):
    def test_filename_format(self):
        out = gl.run_verification(read(REAL_J), read(REAL_G))
        inputs = out.result["inputs"]
        self.assertEqual(out.filename, "parity-evidence-%s-%s.json" % (
            inputs["jenkinsfile_sha256"][:12], inputs["gitlab_ci_sha256"][:12]))
        self.assertRegex(out.filename, r"^parity-evidence-[0-9a-f]{12}-[0-9a-f]{12}\.json$")

    def test_evidence_bytes_equal_render_json(self):
        out = gl.run_verification(read(REAL_J), read(REAL_G))
        expected = pv.render_json(pv.verify_bytes(read(REAL_J), read(REAL_G))).encode("utf-8")
        self.assertEqual(out.evidence, expected)
        self.assertEqual(gl.evidence_bytes(out.result), expected)

    def test_evidence_byte_identical_across_runs(self):
        a = gl.run_verification(read(REAL_J), read(REAL_G))
        b = gl.run_verification(read(REAL_J), read(REAL_G))
        self.assertEqual(a.evidence, b.evidence)
        self.assertEqual(a.filename, b.filename)
        for forbidden in (b"timestamp", b"generated_at"):
            self.assertNotIn(forbidden, a.evidence)

    def test_evidence_for_unparsed_input_also_matches_render_json(self):
        out = gl.run_verification(GOOD_J, b"a: [")
        self.assertTrue(out.ok)
        self.assertEqual(out.evidence, pv.render_json(out.result).encode("utf-8"))

    def test_hashes_are_of_raw_bytes(self):
        out = gl.run_verification(GOOD_J, GOOD_G)
        self.assertEqual(out.result["inputs"]["jenkinsfile_sha256"], gl.sha256_hex(GOOD_J))
        self.assertEqual(out.result["inputs"]["gitlab_ci_sha256"], gl.sha256_hex(GOOD_G))


class PresentationTests(unittest.TestCase):
    def test_witness_view_for_failing_check(self):
        res = pv.verify_bytes(read(REAL_J), read(os.path.join(SYN, "cal-d2-missing-always.yml")))
        w = res["checks"]["always_cleanup"]["witness"]
        view = gl.witness_view(w)
        self.assertEqual(view["scenario"], "branch=main, build_result=fail, pipeline_source=push")
        self.assertFalse(view["scenario_independent"])
        self.assertIn("cleanup", view["jenkins_executes"])
        self.assertEqual(view["gitlab_executes"], "[]")
        self.assertIn('-echo "cleanup"', view["diff_unified"])

    def test_witness_view_scenario_independent(self):
        res = pv.verify_bytes(read(REAL_J),
                              read(os.path.join(SYN, "cal-d3-wrong-artifact-path.yml")))
        view = gl.witness_view(res["checks"]["artifact_paths"]["witness"])
        self.assertTrue(view["scenario_independent"])
        self.assertIn("dist/app.txt", view["diff"])

    def test_witness_view_none(self):
        self.assertIsNone(gl.witness_view(None))

    def test_check_verdict_view(self):
        self.assertEqual(gl.check_verdict_view("PASS")[0], "PASS (modelled)")
        self.assertEqual(gl.check_verdict_view("FAIL")[0], "FAIL")
        self.assertIn("OUT OF MODEL", gl.check_verdict_view("OUT_OF_MODEL")[0])

    def test_disclaimer_mentions_runtime(self):
        self.assertIn("does NOT prove Jenkins runtime equivalence", gl.DISCLAIMER)


# ---------------------------------------------------------------------------
# Optional Streamlit AppTest smoke tests (skipped when streamlit is absent)
# ---------------------------------------------------------------------------

HAS_STREAMLIT = importlib.util.find_spec("streamlit") is not None


def _button(at, label_part):
    for b in at.button:
        if label_part in b.label:
            return b
    raise AssertionError("button not found: %s" % label_part)


@unittest.skipUnless(HAS_STREAMLIT, "streamlit not installed (pip install -r requirements-gui.txt)")
class StreamlitAppSmokeTests(unittest.TestCase):
    def _app(self):
        from streamlit.testing.v1 import AppTest
        return AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=30)

    def test_app_starts_without_exception(self):
        at = self._app().run()
        self.assertEqual(len(at.exception), 0, [e.value for e in at.exception])
        self.assertIn(gl.STATIC_MODEL_NOTICE, " ".join(m.value for m in at.markdown))

    def test_load_real_example_then_verify(self):
        at = self._app().run()
        _button(at, "Load real conversion example").click().run()
        self.assertEqual(len(at.exception), 0, [e.value for e in at.exception])
        _button(at, "Verify Migration").click().run()
        self.assertEqual(len(at.exception), 0, [e.value for e in at.exception])
        html = " ".join(m.value for m in at.markdown)
        self.assertIn("CONFIRMED LOSS", html)
        self.assertIn(gl.LABEL_REAL, html)

    def test_empty_input_shows_error_not_crash(self):
        at = self._app().run()
        _button(at, "Verify Migration").click().run()
        self.assertEqual(len(at.exception), 0, [e.value for e in at.exception])
        self.assertTrue(any("empty" in e.value for e in at.error))

    def test_calibration_not_run_until_clicked(self):
        at = self._app().run()
        self.assertTrue(any(i.value == "Not run" for i in at.info))
        _button(at, "Run calibration").click().run()
        self.assertEqual(len(at.exception), 0, [e.value for e in at.exception])
        cal = gl.run_calibration()
        self.assertTrue(any(gl.calibration_summary(cal) in s.value
                            for s in list(at.success) + list(at.error)))


if __name__ == "__main__":
    unittest.main()
