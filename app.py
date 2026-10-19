"""PARITY dashboard: Streamlit GUI for the static PARITY verifier.

Run with:  streamlit run app.py

All non-UI logic lives in parity_gui_logic.py (testable without streamlit).
The verifier (parity_verify.py) is used unchanged.

STATIC MODEL: a PASS does NOT prove Jenkins runtime equivalence.
"""

import pathlib

import streamlit as st

import parity_gui_logic as logic

st.set_page_config(page_title="PARITY - Jenkins to GitLab CI verifier", page_icon="\u2696\ufe0f",
                   layout="wide")

REPO_ROOT_NOTE = "tests/fixtures/real/PROVENANCE.md"

CSS = """
<style>
.parity-badge {padding: 1rem 1.25rem; border-radius: 0.6rem; color: #fff;
               font-size: 1.5rem; font-weight: 700; letter-spacing: .02em;}
.parity-badge small {display:block; font-size: .9rem; font-weight: 400; margin-top:.35rem;}
.parity-pill {display:inline-block; padding: .15rem .6rem; border-radius: 1rem; color:#fff;
              font-size: .8rem; font-weight: 600; margin-right: .4rem;}
</style>
"""


def pill(text, colour):
    # text/colour come only from fixed constants in parity_gui_logic, never from user input
    return '<span class="parity-pill" style="background:%s">%s</span>' % (colour, text)


LABEL_COLOURS = {
    logic.LABEL_REAL: "#067647",
    logic.LABEL_SYNTHETIC: "#7a2e0e",
    logic.LABEL_USER: "#344054",
}


def label_pill(label):
    return pill(label, LABEL_COLOURS.get(label, "#344054"))


# ---------------------------------------------------------------- state ----

def init_state():
    defaults = {
        "j_mode": "Paste text", "g_mode": "Paste text",
        "j_text": "", "g_text": "",
        "example": None,          # logic.Example once loaded
        "example_error": "",
        "outcome": None,
        "calibration": None,
        "calibration_error": "",
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def on_load_example():
    example = logic.load_real_example()
    st.session_state["outcome"] = None
    if not example.ok:
        st.session_state["example"] = None
        st.session_state["example_error"] = example.error
        return
    st.session_state["example_error"] = ""
    st.session_state["example"] = example
    st.session_state["j_mode"] = "Paste text"
    st.session_state["g_mode"] = "Paste text"
    st.session_state["j_text"] = example.jenkins.decode("utf-8", errors="replace")
    st.session_state["g_text"] = example.gitlab.decode("utf-8", errors="replace")


def on_clear():
    for key in ("j_text", "g_text"):
        st.session_state[key] = ""
    st.session_state["outcome"] = None
    st.session_state["example"] = None
    st.session_state["example_error"] = ""


def current_bytes(prefix, upload_label):
    """Raw bytes for one side from the chosen source (None if nothing given)."""
    mode = st.session_state["%s_mode" % prefix]
    if mode == "Upload file":
        up = st.session_state.get("%s_upload" % prefix)
        return up.getvalue() if up is not None else None
    text = st.session_state["%s_text" % prefix]
    example = st.session_state.get("example")
    if example is not None:
        # keep the exact fixture bytes if the text box still holds the loaded example
        original = example.jenkins if prefix == "j" else example.gitlab
        if text == original.decode("utf-8", errors="replace"):
            return original
    return text.encode("utf-8", errors="strict") if text else b""


# ----------------------------------------------------------------- views ----

def render_header():
    st.markdown(CSS, unsafe_allow_html=True)
    st.title("\u2696\ufe0f PARITY")
    st.caption("Static comparison of a Jenkins declarative pipeline and its GitLab CI "
               "conversion. Deterministic, evidence-producing, honest about what it "
               "cannot prove.")
    st.warning(
        "**Static model only. A PASS does NOT prove Jenkins runtime equivalence.** "
        "Even the best overall verdict (*no divergence in static model \u2014 runtime "
        "unverified*) only says the modelled checks agreed. Everything in "
        "\u201cUnverified runtime semantics\u201d still needs runtime experiments.",
        icon="\u26a0\ufe0f",
    )


def render_sidebar(verifier):
    with st.sidebar:
        st.header("About PARITY")
        st.write(
            "PARITY parses a **declarative Jenkinsfile** subset and a **GitLab CI YAML** "
            "subset, then compares four behaviours over a fixed scenario set: "
            "`branch_gate`, `always_cleanup`, `artifact_paths`, `build_commands`."
        )
        st.markdown(
            "- **PASS** \u2013 no divergence in the modelled scenarios\n"
            "- **FAIL** \u2013 divergence, with a reproducible witness\n"
            "- **OUT_OF_MODEL** \u2013 unsupported syntax; *no verdict*, never a pass\n"
        )
        st.subheader("Evidence labels")
        st.markdown(
            label_pill(logic.LABEL_REAL) + "<br>files from the real Jenkins\u2192GitLab "
            "conversion, byte-identical to git history.<br><br>"
            + label_pill(logic.LABEL_SYNTHETIC) + "<br>hand-made fixtures used only to "
            "calibrate the verifier; <b>not</b> real conversion evidence.<br><br>"
            + label_pill(logic.LABEL_USER) + "<br>anything you upload or paste.",
            unsafe_allow_html=True,
        )
        st.subheader("Limits")
        st.write("Max input size: %d KiB per file. Only a small, strict subset of both "
                 "formats is modelled." % (logic.MAX_INPUT_BYTES // 1024))
        if verifier is not None:
            st.caption("Verifier version: %s" % verifier.VERIFIER_VERSION)


def render_input_column(prefix, title, kind_hint, accept):
    st.subheader(title)
    st.radio("Source", ["Paste text", "Upload file"], key="%s_mode" % prefix,
             horizontal=True, label_visibility="collapsed")
    if st.session_state["%s_mode" % prefix] == "Upload file":
        st.file_uploader("Upload %s" % kind_hint, type=accept,
                         key="%s_upload" % prefix, help="Read as raw bytes; max %d KiB."
                         % (logic.MAX_INPUT_BYTES // 1024))
    else:
        st.text_area("Paste %s" % kind_hint, key="%s_text" % prefix, height=300,
                     label_visibility="collapsed", placeholder="Paste the %s here" % kind_hint)


def render_example_panel():
    st.button("\U0001F4E5 Load real conversion example", on_click=on_load_example,
              help="Loads tests/fixtures/real/Jenkinsfile and "
                   "tests/fixtures/real/gitlab-ci.converted.yml")
    if st.session_state["example_error"]:
        st.error(st.session_state["example_error"])
    example = st.session_state["example"]
    if example is not None:
        st.markdown(label_pill(logic.LABEL_REAL) + " loaded the real conversion pair "
                    "from the repository.", unsafe_allow_html=True)
        bullets = logic.provenance_summary(example.provenance)
        with st.expander("Provenance of the real example (PROVENANCE.md)", expanded=True):
            if bullets:
                for b in bullets:
                    st.markdown("- " + b)
            else:
                st.info("PROVENANCE.md was not found or is empty; the files were still "
                        "loaded from %s." % pathlib.PurePosixPath("tests/fixtures/real"))
            if example.provenance:
                st.markdown("**Full PROVENANCE.md** (`%s`):" % REPO_ROOT_NOTE)
                st.text(example.provenance)


def render_overall(outcome):
    result = outcome.result
    headline, colour, explanation = logic.overall_view(result["overall"])
    st.markdown(
        '<div class="parity-badge" style="background:%s">%s<small>%s</small></div>'
        % (colour, headline, explanation), unsafe_allow_html=True)
    st.caption("Verifier overall value: `%s`" % result["overall"])
    st.markdown(
        "Jenkinsfile: " + label_pill(outcome.label_jenkins)
        + " &nbsp; GitLab CI: " + label_pill(outcome.label_gitlab),
        unsafe_allow_html=True)
    if outcome.label == logic.LABEL_SYNTHETIC:
        st.info("This run includes a SYNTHETIC calibration fixture. It is not real "
                "conversion evidence.")
    elif outcome.label == logic.LABEL_USER:
        st.info("USER-SUPPLIED input: this is not part of the repository's real or "
                "synthetic evidence.")
    st.warning(logic.DISCLAIMER)


def render_witness(witness):
    view = logic.witness_view(witness)
    if view is None:
        return
    st.markdown("**Reproducible mismatch witness**")
    st.write("Scenario: `%s`" % view["scenario"])
    st.write("Scenario independent: **%s**" % ("yes" if view["scenario_independent"] else "no"))
    left, right = st.columns(2)
    with left:
        st.markdown("Jenkins executes")
        st.code(view["jenkins_executes"], language="json")
    with right:
        st.markdown("GitLab executes")
        st.code(view["gitlab_executes"], language="json")
    st.markdown("Diff")
    if view["diff_unified"]:
        st.code(view["diff_unified"], language="diff")
    st.code(view["diff"], language="json")
    with st.expander("Raw witness JSON"):
        st.json(witness)


def render_check_cards(result):
    st.subheader("Per-check results")
    cols = st.columns(2)
    for i, cid in enumerate(logic.CHECK_ORDER):
        check = result["checks"].get(cid)
        if check is None:
            continue
        label, colour = logic.check_verdict_view(check["verdict"])
        with cols[i % 2]:
            with st.container(border=True):
                st.markdown("#### `%s`" % cid)
                st.markdown(pill(label, colour), unsafe_allow_html=True)
                st.text(check.get("summary") or "")
                st.write("Scenarios evaluated: **%s** \u2022 Divergences: **%s**"
                         % (check.get("scenarios_evaluated"), check.get("divergences")))
                if check["verdict"] == "PASS":
                    st.caption("PASS = no divergence in the modelled scenarios. Not proof "
                               "of runtime equivalence.")
                if check.get("reasons"):
                    st.markdown("Reasons")
                    for reason in check["reasons"]:
                        st.text("\u2022 " + str(reason))
                if check.get("witness"):
                    render_witness(check["witness"])


def render_mappings(result):
    st.subheader("Confirmed unsupported mappings")
    mappings = result.get("confirmed_unsupported_mappings") or []
    if not mappings:
        st.write("None found.")
        return
    for m in mappings:
        with st.container(border=True):
            st.markdown(pill("FAIL", "#b42318") + " **%s** \u2014 `%s`"
                        % (m.get("label", ""), m.get("id", "")), unsafe_allow_html=True)
            st.text(m.get("reason", ""))
            w = m.get("witness") or {}
            jw = w.get("jenkins") or {}
            if isinstance(jw, dict) and jw:
                st.write("Jenkins: stage `%s`, line %s, artifacts `%s`, fingerprint=%s"
                         % (jw.get("stage"), jw.get("line"), jw.get("artifacts"),
                            jw.get("fingerprint")))
            st.write("GitLab: %s" % w.get("gitlab", "n/a"))
            with st.expander("Raw JSON"):
                st.json(m)


def render_unparsed(result):
    st.subheader("Unparsed / out-of-model items")
    rows = logic.unparsed_rows(result)
    if not rows:
        st.write("None: all input syntax was inside the static model.")
        return
    st.error("%d item(s) are outside the static model. Affected checks have no verdict "
             "(OUT_OF_MODEL). They are never treated as equivalent." % len(rows))
    st.dataframe(
        [{"side": r["side"], "line": r["line"], "reason": r["reason"],
          "text": r["text"], "affects": r["affects"]} for r in rows],
        hide_index=True)


def render_unverified(result):
    st.subheader("Unverified runtime semantics")
    st.caption("Never graded by the verifier. These require runtime experiments.")
    for item in result.get("unverified_runtime_semantics", []):
        with st.expander(str(item.get("id"))):
            st.write(item.get("description", ""))
            st.caption("Requires: %s" % item.get("requires", "runtime experiment"))


def render_metadata(outcome):
    result = outcome.result
    st.subheader("Inputs and verifier")
    st.write("Verifier version: `%s`" % result.get("verifier_version"))
    st.write("Jenkinsfile SHA-256:")
    st.code(result["inputs"]["jenkinsfile_sha256"], language=None)
    st.write("GitLab CI SHA-256:")
    st.code(result["inputs"]["gitlab_ci_sha256"], language=None)
    scen = result.get("scenario_set", {})
    st.caption("Scenario set: branches %s, pipeline_source %s, build_result %s (%d scenarios)"
               % (scen.get("branch"), scen.get("pipeline_source"), scen.get("build_result"),
                  len(scen.get("scenarios", []))))


def render_results(outcome):
    result = outcome.result
    render_overall(outcome)
    st.download_button(
        "\u2B07\uFE0F Download evidence JSON", data=outcome.evidence,
        file_name=outcome.filename, mime="application/json",
        help="Deterministic: exactly parity_verify.render_json(result); same inputs give "
             "byte-identical files.")
    render_check_cards(result)
    render_mappings(result)
    render_unparsed(result)
    render_unverified(result)
    render_metadata(outcome)
    with st.expander("Full evidence JSON (as downloaded)"):
        st.code(outcome.evidence.decode("utf-8"), language="json")


def render_calibration():
    st.subheader("Calibration on SYNTHETIC fixtures")
    st.markdown(label_pill(logic.LABEL_SYNTHETIC) + " These fixtures only calibrate the "
                "verifier. They are <b>not</b> real conversion evidence, and the expected "
                "outcomes come from tests/fixtures/synthetic/README.md and the repo's "
                "existing tests.", unsafe_allow_html=True)
    if st.button("Run calibration"):
        try:
            st.session_state["calibration"] = logic.run_calibration()
            st.session_state["calibration_error"] = ""
        except Exception as exc:
            st.session_state["calibration"] = None
            st.session_state["calibration_error"] = (
                "Calibration could not be run: %s" % (exc,))
    if st.session_state["calibration_error"]:
        st.error(st.session_state["calibration_error"])
    calibration = st.session_state["calibration"]
    if calibration is None:
        st.info("Not run")
        return
    summary = logic.calibration_summary(calibration)
    (st.success if calibration["matched"] == calibration["total"] else st.error)(summary)
    st.dataframe(
        [{"fixture": r["fixture"], "Jenkinsfile": r["jenkinsfile"],
          "expected": r["expected"], "actual": r["actual"],
          "match": "\u2705 match" if r["match"] else "\u274c MISMATCH"}
         for r in calibration["rows"]],
        hide_index=True)
    st.caption("Computed just now by running parity_verify.verify_bytes on each fixture "
               "pair. Calibration on a few synthetic cases does not establish general "
               "verifier accuracy.")


# ------------------------------------------------------------------ main ----

def main():
    init_state()
    verifier, load_error = logic.load_verifier()
    render_header()
    render_sidebar(verifier)
    if verifier is None:
        st.error(load_error)
        st.stop()

    tab_verify, tab_calibration = st.tabs(["Verify a migration", "Calibration"])
    with tab_verify:
        render_example_panel()
        left, right = st.columns(2)
        with left:
            render_input_column("j", "Jenkinsfile", "Jenkinsfile", None)
        with right:
            render_input_column("g", "GitLab CI YAML", "GitLab CI YAML", ["yml", "yaml", "txt"])

        clicked = st.button("\u2705 Verify Migration", type="primary")
        st.button("Clear inputs", on_click=on_clear)
        if clicked:
            try:
                jraw = current_bytes("j", "Jenkinsfile")
                graw = current_bytes("g", "GitLab CI YAML")
                st.session_state["outcome"] = logic.run_verification(jraw, graw, verifier)
            except Exception as exc:  # never let any input crash the app
                st.session_state["outcome"] = logic.Outcome(
                    False, "Unexpected error while preparing the inputs (%s)."
                           % type(exc).__name__)
        outcome = st.session_state["outcome"]
        if outcome is not None:
            st.divider()
            if outcome.ok:
                render_results(outcome)
            else:
                st.error(outcome.error)
    with tab_calibration:
        render_calibration()


main()
