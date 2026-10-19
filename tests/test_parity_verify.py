"""Tests for parity_verify.py (unittest; needs PyYAML only).

Fixtures under tests/fixtures/real/ are REAL files (see PROVENANCE.md).
Fixtures under tests/fixtures/synthetic/ are SYNTHETIC CALIBRATION FIXTURES.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import parity_verify as pv  # noqa: E402

REAL = os.path.join(HERE, "fixtures", "real")
SYN = os.path.join(HERE, "fixtures", "synthetic")
REAL_J = os.path.join(REAL, "Jenkinsfile")
REAL_G = os.path.join(REAL, "gitlab-ci.converted.yml")
SYN_J_NOFP = os.path.join(SYN, "Jenkinsfile.no-fingerprint")

FOUR = ("branch_gate", "always_cleanup", "artifact_paths", "build_commands")


def read(path):
    with open(path, "rb") as fh:
        return fh.read()


def run(jpath, gpath):
    return pv.verify_bytes(read(jpath), read(gpath))


def run_text(jtext, gtext):
    return pv.verify_bytes(jtext.encode("utf-8"), gtext.encode("utf-8"))


def verdicts(res):
    return {k: v["verdict"] for k, v in res["checks"].items()}


class RealFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.res = run(REAL_J, REAL_G)

    def test_fixture_sizes_match_provenance(self):
        self.assertEqual(len(read(REAL_J)), 477)
        self.assertEqual(len(read(REAL_G)), 381)

    def test_no_dot_gitlab_ci_file_in_repo(self):
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            # The root-level .gitlab-ci.yml is the single intentionally added
            # validation CI config; any other location (notably fixtures)
            # must still be rejected.
            if ".gitlab-ci.yml" in filenames and os.path.normpath(
                dirpath
            ) != os.path.normpath(ROOT):
                self.fail(
                    "unexpected .gitlab-ci.yml at: "
                    + os.path.join(dirpath, ".gitlab-ci.yml")
                )

    def test_four_checks_pass(self):
        for check in FOUR:
            self.assertEqual(self.res["checks"][check]["verdict"], "PASS", check)
            self.assertIsNone(self.res["checks"][check]["witness"], check)

    def test_fingerprint_is_confirmed_unsupported_mapping(self):
        m = self.res["confirmed_unsupported_mappings"]
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0]["id"], "fingerprint")
        self.assertEqual(m[0]["verdict"], "FAIL")
        self.assertEqual(m[0]["label"], "CONFIRMED UNSUPPORTED MAPPING")
        self.assertEqual(m[0]["witness"]["jenkins"]["line"], 7)
        self.assertEqual(m[0]["witness"]["jenkins"]["stage"], "Build")
        self.assertTrue(m[0]["witness"]["jenkins"]["fingerprint"])

    def test_overall_is_confirmed_loss(self):
        self.assertEqual(self.res["overall"], "CONFIRMED_LOSS")
        self.assertNotEqual(self.res["overall"], "CHECKED_EQUIVALENT_WITH_UNVERIFIED_RUNTIME")

    def test_unparsed_empty_for_real_inputs(self):
        self.assertEqual(self.res["unparsed"], [])

    def test_unverified_runtime_semantics_non_empty(self):
        ids = {u["id"] for u in self.res["unverified_runtime_semantics"]}
        self.assertTrue(ids)
        for needed in ("cancellation_behavior", "workspace_sharing",
                       "artifact_retention_expiry", "branch_gate_tag_and_mr_pipelines",
                       "jenkins_and_gitlab_artifact_glob_nuances",
                       "runner_image_environment", "fingerprint_cross_build_tracking"):
            self.assertIn(needed, ids)
        for u in self.res["unverified_runtime_semantics"]:
            self.assertNotIn("verdict", u)

    def test_input_hashes(self):
        self.assertEqual(self.res["inputs"]["jenkinsfile_sha256"],
                         hashlib.sha256(read(REAL_J)).hexdigest())
        self.assertEqual(self.res["inputs"]["gitlab_ci_sha256"],
                         hashlib.sha256(read(REAL_G)).hexdigest())

    def test_parsed_models(self):
        j = self.res["models"]["jenkins"]
        self.assertEqual([s["name"] for s in j["stages"]], ["Build", "Deploy"])
        self.assertEqual(j["stages"][1]["gate"]["branch"], "main")
        self.assertEqual(j["post_always"][0]["cmd"], 'echo "cleanup"')
        g = self.res["models"]["gitlab"]
        self.assertEqual([x["name"] for x in g["jobs"]], ["build", "deploy", "cleanup"])

    def test_scenario_set(self):
        s = self.res["scenario_set"]
        self.assertEqual(s["branch"], ["main", "parity/fixture-1", "other-branch"])
        self.assertEqual(s["pipeline_source"], ["push"])
        self.assertEqual(s["build_result"], ["ok", "fail"])
        self.assertEqual(len(s["scenarios"]), 6)


class CalibrationDefectTests(unittest.TestCase):
    """SYNTHETIC CALIBRATION FIXTURES CAL-D1..D3 against the REAL Jenkinsfile."""

    def _run(self, name):
        return run(REAL_J, os.path.join(SYN, name))

    def _only_fails(self, res, failing):
        for check in FOUR:
            expect = "FAIL" if check == failing else "PASS"
            self.assertEqual(res["checks"][check]["verdict"], expect, check)

    def test_cal_d1_wrong_deploy_branch(self):
        res = self._run("cal-d1-wrong-branch.yml")
        self._only_fails(res, "branch_gate")
        w = res["checks"]["branch_gate"]["witness"]
        self.assertEqual(w["scenario"], {"branch": "main", "build_result": "ok",
                                         "pipeline_source": "push"})
        self.assertEqual(w["jenkins_executes"], ["Build", "Deploy"])
        self.assertEqual(w["gitlab_executes"], ["build"])
        self.assertEqual(w["diff"]["only_jenkins"], ["Deploy"])
        self.assertEqual(w["diff"]["only_gitlab"], [])
        self.assertEqual(res["checks"]["branch_gate"]["divergences"], 1)
        self.assertEqual(res["overall"], "CONFIRMED_LOSS")

    def test_cal_d2_missing_always_cleanup(self):
        res = self._run("cal-d2-missing-always.yml")
        self._only_fails(res, "always_cleanup")
        w = res["checks"]["always_cleanup"]["witness"]
        self.assertEqual(w["scenario"], {"branch": "main", "build_result": "fail",
                                         "pipeline_source": "push"})
        self.assertEqual(w["jenkins_executes"], ['echo "cleanup"'])
        self.assertEqual(w["gitlab_executes"], [])
        self.assertTrue(w["diff"]["jenkins_runs_post_always"])
        self.assertFalse(w["diff"]["gitlab_runs_post_job"])
        # runs fine when the build succeeds, so only build_result=fail scenarios diverge
        self.assertEqual(res["checks"]["always_cleanup"]["divergences"], 3)
        self.assertEqual(res["overall"], "CONFIRMED_LOSS")

    def test_cal_d3_wrong_artifact_path(self):
        res = self._run("cal-d3-wrong-artifact-path.yml")
        self._only_fails(res, "artifact_paths")
        w = res["checks"]["artifact_paths"]["witness"]
        self.assertEqual(w["diff"]["uncovered_by_gitlab"], ["out/app.txt", "out/nested/deep.txt"])
        self.assertIn("out/app.txt", w["diff"]["uncovered_by_gitlab"])
        self.assertEqual(w["diff"]["extra_in_gitlab"], ["dist/app.txt"])
        self.assertEqual(w["gitlab_executes"]["paths"], ["dist/"])
        self.assertEqual(w["jenkins_executes"]["patterns"], ["out/**"])
        self.assertEqual(res["overall"], "CONFIRMED_LOSS")


class CalibrationControlTests(unittest.TestCase):
    """SYNTHETIC CALIBRATION control CAL-C1."""

    C1 = os.path.join(SYN, "cal-c1-equivalent.yml")

    def test_cal_c1_with_synthetic_no_fingerprint_jenkinsfile(self):
        res = run(SYN_J_NOFP, self.C1)
        self.assertEqual(verdicts(res), {c: "PASS" for c in FOUR})
        self.assertEqual(res["unparsed"], [])
        self.assertEqual(res["confirmed_unsupported_mappings"], [])
        self.assertEqual(res["overall"], "CHECKED_EQUIVALENT_WITH_UNVERIFIED_RUNTIME")
        self.assertTrue(res["unverified_runtime_semantics"])

    def test_cal_c1_with_real_jenkinsfile_is_loss(self):
        res = run(REAL_J, self.C1)
        self.assertEqual(verdicts(res), {c: "PASS" for c in FOUR})
        self.assertEqual([m["id"] for m in res["confirmed_unsupported_mappings"]],
                         ["fingerprint"])
        self.assertEqual(res["overall"], "CONFIRMED_LOSS")

    def test_cal_c1_surface_forms_parsed_correctly(self):
        g = run(SYN_J_NOFP, self.C1)["models"]["gitlab"]
        jobs = {j["name"]: j for j in g["jobs"]}
        self.assertEqual(jobs["deploy"]["rules"][0]["if"],
                         {"op": "==", "value": "main", "var": "CI_COMMIT_BRANCH"})
        self.assertEqual(jobs["cleanup"]["rules"], [{"if": None, "when": "always"}])
        self.assertEqual(jobs["build"]["artifacts_paths"], ["out/"])
        self.assertEqual(g["stages"], ["build", "deploy"])

    def test_missing_fingerprint_never_full_equivalence_when_checks_pass(self):
        res = run(REAL_J, REAL_G)
        self.assertEqual(verdicts(res), {c: "PASS" for c in FOUR})
        self.assertNotEqual(res["overall"], "CHECKED_EQUIVALENT_WITH_UNVERIFIED_RUNTIME")
        self.assertEqual(res["overall"], "CONFIRMED_LOSS")


class DeterminismTests(unittest.TestCase):
    def test_twice_in_process_identical(self):
        a = pv.render_json(run(REAL_J, REAL_G))
        b = pv.render_json(run(REAL_J, REAL_G))
        self.assertEqual(a, b)
        self.assertTrue(a.endswith("\n"))
        self.assertFalse(a.endswith("\n\n"))

    def _cli(self, *args):
        return subprocess.run([sys.executable, os.path.join(ROOT, "parity_verify.py")] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT)

    def test_cli_byte_identical_and_matches_in_process(self):
        args = ["--jenkinsfile", os.path.join("tests", "fixtures", "real", "Jenkinsfile"),
                "--gitlab-ci", os.path.join("tests", "fixtures", "real",
                                            "gitlab-ci.converted.yml")]
        p1, p2 = self._cli(*args), self._cli(*args)
        self.assertEqual(p1.returncode, 1, p1.stderr)
        self.assertEqual(p1.stdout, p2.stdout)
        self.assertEqual(p1.stdout.decode("utf-8"), pv.render_json(run(REAL_J, REAL_G)))

    def test_output_sorted_keys_no_timestamps_no_abs_paths(self):
        text = pv.render_json(run(REAL_J, REAL_G))
        obj = json.loads(text)

        def check_sorted(o):
            if isinstance(o, dict):
                self.assertEqual(list(o.keys()), sorted(o.keys()))
                for v in o.values():
                    check_sorted(v)
            elif isinstance(o, list):
                for v in o:
                    check_sorted(v)

        check_sorted(json.loads(text, object_pairs_hook=lambda pairs: dict(pairs)))
        # object_pairs_hook keeps file order: verify the raw text order too
        self.assertEqual(text, json.dumps(obj, sort_keys=True, indent=2) + "\n")
        for forbidden in ("timestamp", "datetime", "generated_at", ROOT):
            self.assertNotIn(forbidden, text)
        self.assertIn("verifier_version", obj)

    def test_cli_output_file_and_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "o.json")
            p = self._cli("--jenkinsfile", REAL_J, "--gitlab-ci", REAL_G, "--output", out)
            self.assertEqual(p.returncode, 1)
            self.assertEqual(read(out), p.stdout)
            p = self._cli("--jenkinsfile", SYN_J_NOFP, "--gitlab-ci",
                          os.path.join(SYN, "cal-c1-equivalent.yml"))
            self.assertEqual(p.returncode, 0, p.stderr)
            bad = os.path.join(tmp, "bad.yml")
            with open(bad, "w") as fh:
                fh.write("include: x.yml\n")
            p = self._cli("--jenkinsfile", REAL_J, "--gitlab-ci", bad)
            self.assertEqual(p.returncode, 1)  # real Jenkinsfile still has fingerprint loss
            p = self._cli("--jenkinsfile", SYN_J_NOFP, "--gitlab-ci", bad)
            self.assertEqual(p.returncode, 2)

    def test_cli_usage_and_io_errors_exit_3(self):
        self.assertEqual(self._cli().returncode, 3)
        p = self._cli("--jenkinsfile", os.path.join(ROOT, "nope"), "--gitlab-ci", REAL_G)
        self.assertEqual(p.returncode, 3)
        self.assertEqual(p.stdout, b"")


JENKINS_TMPL = """pipeline {
    agent any
    stages {
        stage('Build') {
            steps {
                sh 'mkdir -p out && echo "build-output" > out/app.txt'
                archiveArtifacts artifacts: 'out/**', fingerprint: false
            }
        }
        stage('Deploy') {
            when { branch 'main' }
            steps {
                sh 'echo "deploying"'
            }
        }
    }
    post {
        always {
            sh 'echo "cleanup"'
        }
    }
}
"""


class UnsupportedJenkinsSyntaxTests(unittest.TestCase):
    GOOD_G = read(os.path.join(SYN, "cal-c1-equivalent.yml")).decode("utf-8")

    def _assert_out_of_model(self, jtext, expect_checks, reason_fragment):
        res = run_text(jtext, self.GOOD_G)
        self.assertTrue(res["unparsed"])
        for check in expect_checks:
            self.assertEqual(res["checks"][check]["verdict"], "OUT_OF_MODEL", check)
            self.assertNotEqual(res["checks"][check]["verdict"], "PASS")
        self.assertTrue(any(reason_fragment in u["reason"] for u in res["unparsed"]),
                        res["unparsed"])
        for u in res["unparsed"]:
            self.assertIsInstance(u["line"], int)
            self.assertGreaterEqual(u["line"], 1)
        self.assertEqual(res["overall"], "INSUFFICIENT_EVIDENCE")
        return res

    def test_template_itself_is_clean(self):
        res = run_text(JENKINS_TMPL, self.GOOD_G)
        self.assertEqual(res["unparsed"], [])
        self.assertEqual(res["overall"], "CHECKED_EQUIVALENT_WITH_UNVERIFIED_RUNTIME")

    def test_parallel(self):
        j = JENKINS_TMPL.replace(
            "        stage('Deploy') {",
            "        stage('Par') {\n            parallel {\n                stage('A') {\n"
            "                    steps { sh 'true' }\n                }\n            }\n        }\n"
            "        stage('Deploy') {")
        res = self._assert_out_of_model(j, FOUR, "unsupported")
        self.assertTrue(any(u["line"] == 11 and "parallel" in u["reason"] for u in res["unparsed"]),
                        res["unparsed"])

    def test_script_block(self):
        j = JENKINS_TMPL.replace("sh 'echo \"deploying\"'",
                                 "script {\n                    echo 'x'\n                }")
        res = self._assert_out_of_model(j, ("build_commands", "artifact_paths"),
                                        "unsupported step 'script'")
        self.assertEqual(res["checks"]["branch_gate"]["verdict"], "PASS")

    def test_post_failure(self):
        j = JENKINS_TMPL.replace("always {", "failure {")
        self._assert_out_of_model(j, ("always_cleanup",), "unsupported post condition 'failure'")
        for cond in ("success", "cleanup", "unstable"):
            self._assert_out_of_model(JENKINS_TMPL.replace("always {", cond + " {"),
                                      ("always_cleanup",), "post condition")

    def test_post_failure_added_next_to_always(self):
        j = JENKINS_TMPL.replace("    post {\n", "    post {\n        failure {\n"
                                 "            sh 'echo bad'\n        }\n")
        res = self._assert_out_of_model(j, ("always_cleanup",), "post condition 'failure'")
        self.assertEqual(res["checks"]["branch_gate"]["verdict"], "PASS")

    def test_when_expression_and_friends(self):
        for cond in ("expression { env.X == '1' }", "not { branch 'main' }",
                     "anyOf { branch 'main' }", "allOf { branch 'main' }",
                     "tag 'v*'", "changeset 'src/**'"):
            j = JENKINS_TMPL.replace("when { branch 'main' }", "when { %s }" % cond)
            res = self._assert_out_of_model(j, ("branch_gate",), "unsupported `when` condition")
            self.assertEqual(res["checks"]["build_commands"]["verdict"], "PASS", cond)

    def test_unsupported_directives(self):
        for directive in ("environment { A = '1' }", "parameters { string(name: 'P') }",
                          "options { timeout(time: 1) }", "triggers { cron('H * * * *') }",
                          "tools { maven 'm' }"):
            j = JENKINS_TMPL.replace("    agent any\n", "    agent any\n    %s\n" % directive)
            self._assert_out_of_model(j, FOUR, "unsupported pipeline directive")

    def test_stage_level_input_matrix(self):
        for directive in ("input { message 'ok?' }", "matrix { axes { } }"):
            j = JENKINS_TMPL.replace("            when { branch 'main' }\n",
                                     "            when { branch 'main' }\n            %s\n"
                                     % directive)
            self._assert_out_of_model(j, FOUR, "unsupported stage directive")

    def test_agent_variants(self):
        for agent in ("agent { docker 'x' }", "agent { label 'l' }", "agent none"):
            j = JENKINS_TMPL.replace("agent any", agent)
            self._assert_out_of_model(j, FOUR, "unsupported agent")

    def test_other_steps(self):
        for step in ("junit 'r.xml'", "stash name: 'a'", "withCredentials([]) { }"):
            j = JENKINS_TMPL.replace("sh 'echo \"deploying\"'", step)
            self._assert_out_of_model(j, ("build_commands",), "unsupported step")

    def test_unbalanced_braces(self):
        j = JENKINS_TMPL.rstrip("\n")[:-1]  # drop final }
        res = self._assert_out_of_model(j, FOUR, "unbalanced")
        self.assertFalse(res["models"]["jenkins"]["valid"])
        j = JENKINS_TMPL + "}\n"
        self._assert_out_of_model(j, FOUR, "unbalanced")

    def test_gstring_interpolation_and_triple_quotes(self):
        j = JENKINS_TMPL.replace("sh 'echo \"deploying\"'", 'sh "echo ${env.X}"')
        self._assert_out_of_model(j, ("build_commands",), "GString")
        j = JENKINS_TMPL.replace("sh 'echo \"deploying\"'", "sh '''echo hi'''")
        self._assert_out_of_model(j, ("build_commands",), "triple-quoted")

    def test_unparsed_with_real_fail_still_confirmed_loss(self):
        # FAIL / loss wins over OUT_OF_MODEL
        j = JENKINS_TMPL.replace("always {", "failure {")
        d1 = read(os.path.join(SYN, "cal-d1-wrong-branch.yml")).decode("utf-8")
        res = run_text(j, d1)
        self.assertEqual(res["checks"]["always_cleanup"]["verdict"], "OUT_OF_MODEL")
        self.assertEqual(res["checks"]["branch_gate"]["verdict"], "FAIL")
        self.assertEqual(res["overall"], "CONFIRMED_LOSS")

    def test_empty_jenkinsfile(self):
        res = run_text("", self.GOOD_G)
        self.assertEqual(res["overall"], "INSUFFICIENT_EVIDENCE")
        self.assertTrue(res["unparsed"])
        for check in FOUR:
            self.assertEqual(res["checks"][check]["verdict"], "OUT_OF_MODEL")


class UnsupportedGitLabTests(unittest.TestCase):
    J = read(SYN_J_NOFP).decode("utf-8")
    BASE = read(os.path.join(SYN, "cal-c1-equivalent.yml")).decode("utf-8")

    def _res(self, gtext):
        return run_text(self.J, gtext)

    def test_include(self):
        res = self._res("include:\n  - local: other.yml\n" + self.BASE)
        self.assertEqual(res["overall"], "INSUFFICIENT_EVIDENCE")
        for check in FOUR:
            self.assertEqual(res["checks"][check]["verdict"], "OUT_OF_MODEL", check)
        self.assertTrue(any("include" in u["reason"] and u["side"] == "gitlab"
                            and u["line"] == 1 for u in res["unparsed"]), res["unparsed"])

    def test_global_constructs(self):
        for kw in ("workflow:\n  rules:\n    - if: '$CI_COMMIT_BRANCH'\n",
                   "default:\n  image: x\n", "variables:\n  A: b\n", "image: alpine\n"):
            res = self._res(kw + self.BASE)
            self.assertEqual(res["overall"], "INSUFFICIENT_EVIDENCE", kw)
            self.assertEqual(res["checks"]["branch_gate"]["verdict"], "OUT_OF_MODEL", kw)

    def test_job_level_constructs(self):
        cases = {
            "extends": "  extends: .tpl\n",
            "trigger": "  trigger: other/project\n",
            "image": "  image: alpine\n",
            "before_script": "  before_script:\n    - echo hi\n",
            "needs": "  needs: []\n",
        }
        for name, extra in cases.items():
            gtext = self.BASE.replace("deploy:\n  stage: deploy\n", "deploy:\n  stage: deploy\n" + extra)
            res = self._res(gtext)
            self.assertEqual(res["checks"]["branch_gate"]["verdict"], "OUT_OF_MODEL", name)
            self.assertEqual(res["overall"], "INSUFFICIENT_EVIDENCE", name)

    def test_rules_changes_exists_and_complex_if(self):
        for rule in ("- changes: ['a']", "- exists: ['a']",
                     "- if: '$CI_COMMIT_BRANCH =~ /^ma/'",
                     "- if: '$CI_COMMIT_BRANCH == \"main\" && $X'",
                     "- if: '$CI_COMMIT_TAG'", "- if: '$CI_COMMIT_BRANCH == \"main\"'\n      when: manual"):
            gtext = self.BASE.replace("    - if: '$CI_COMMIT_BRANCH == \"main\"'", "    " + rule)
            res = self._res(gtext)
            self.assertEqual(res["checks"]["branch_gate"]["verdict"], "OUT_OF_MODEL", rule)
            self.assertEqual(res["overall"], "INSUFFICIENT_EVIDENCE", rule)

    def test_invalid_yaml(self):
        res = self._res("a: [unclosed\n")
        self.assertEqual(res["overall"], "INSUFFICIENT_EVIDENCE")
        self.assertTrue(res["unparsed"])

    def test_not_equivalent_never_pass_on_unmodelled_artifact_keys(self):
        gtext = self.BASE.replace("    paths: [out/]", "    paths: [out/]\n    expire_in: 1 day")
        res = self._res(gtext)
        self.assertEqual(res["checks"]["artifact_paths"]["verdict"], "OUT_OF_MODEL")


class SemanticsTests(unittest.TestCase):
    def test_glob_semantics(self):
        m = pv._glob_match
        self.assertTrue(m("out/**", "out/app.txt"))
        self.assertTrue(m("out/**", "out/a/b.txt"))
        # documented Ant semantics: a trailing `**` matches zero segments, so it also
        # matches the bare path "out"; but never a sibling with the same prefix
        self.assertTrue(m("out/**", "out"))
        self.assertFalse(m("out/**", "outer/x.txt"))
        self.assertTrue(m("out/", "out/a/b.txt"))
        self.assertTrue(m("**/*.txt", "a.txt"))
        self.assertTrue(m("**/*.txt", "a/b/c.txt"))
        self.assertFalse(m("out/*", "out/a/b.txt"))
        self.assertTrue(m("out/*.txt", "out/a.txt"))
        self.assertFalse(m("app.txt", "out/app.txt"))
        self.assertTrue(pv.gitlab_collects(["out"], "out/app.txt"))
        self.assertTrue(pv.gitlab_collects(["out/"], "out/app.txt"))
        self.assertFalse(pv.gitlab_collects(["out/"], "outer/app.txt"))
        self.assertFalse(pv.gitlab_collects(["dist/"], "out/app.txt"))

    def test_normalize_command_whitespace_only(self):
        n = pv.normalize_command
        self.assertEqual(n("  echo   hi\t there "), "echo hi there")
        self.assertEqual(n('echo "a   b"'), 'echo "a   b"')
        self.assertNotEqual(n("echo a"), n("echo b"))
        self.assertNotEqual(n("a\nb"), n("a b"))

    def test_command_difference_is_fail(self):
        g = read(os.path.join(SYN, "cal-c1-equivalent.yml")).decode("utf-8")
        g = g.replace('echo "deploying"', 'echo "deploying!"')
        res = run_text(read(SYN_J_NOFP).decode("utf-8"), g)
        self.assertEqual(res["checks"]["build_commands"]["verdict"], "FAIL")
        w = res["checks"]["build_commands"]["witness"]
        self.assertEqual(w["jenkins_executes"]["sh"], ['echo "deploying"'])
        self.assertEqual(w["gitlab_executes"]["script"], ['echo "deploying!"'])
        self.assertEqual(res["overall"], "CONFIRMED_LOSS")

    def test_whitespace_only_command_difference_passes(self):
        g = read(os.path.join(SYN, "cal-c1-equivalent.yml")).decode("utf-8")
        g = g.replace("echo \"deploying\"", "echo   \"deploying\" ")
        res = run_text(read(SYN_J_NOFP).decode("utf-8"), g)
        self.assertEqual(res["checks"]["build_commands"]["verdict"], "PASS")

    def test_derive_created_files(self):
        self.assertEqual(pv.derive_created_files(['mkdir -p out && echo "x" > out/app.txt']),
                         ["out/app.txt"])
        self.assertEqual(pv.derive_created_files(["touch a.txt b/c.txt"]), ["a.txt", "b/c.txt"])


if __name__ == "__main__":
    unittest.main()
