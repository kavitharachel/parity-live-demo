"""PARITY dashboard: Streamlit GUI for the static PARITY verifier.

Run with:  streamlit run app.py

All non-UI logic lives in parity_gui_logic.py (testable without streamlit).
The verifier (parity_verify.py) is used unchanged.

STATIC MODEL: a PASS does NOT prove Jenkins runtime equivalence.
"""

import pathlib

import streamlit as st

import parity_gui_logic as logic

st.set_page_config(page_title="PARITY \u2014 Migration Confidence Engine",
                   page_icon="\u2696\ufe0f", layout="wide")

REPO_ROOT_NOTE = "tests/fixtures/real/PROVENANCE.md"

# Embedded CSS only: no remote fonts, CDN, images, JavaScript or animation, and no
# reliance on Streamlit's generated class names or data-testid attributes (only the
# p-* classes defined here). The page background and widget colours come from the
# supported theme file .streamlit/config.toml. No percent signs are used on purpose
# (the product shows computed counts only, never scores).
CSS = """
<style>
:root {--p-bg:#F7F8FA; --p-card:#FFFFFF; --p-border:#E3E6EB; --p-text:#14171F;
       --p-muted:#515A6B; --p-accent:#3B4CCA; --p-loss:#B42318; --p-pass:#067647;
       --p-oom:#B54708;}
.p-header {margin: 0 0 .75rem 0; padding-bottom: .75rem; border-bottom: 1px solid var(--p-border);}
.p-title {margin: 0; padding: 0; color: var(--p-text); font-weight: 700; line-height: 1.2;
          font-size: clamp(1.45rem, 3.6vw, 2rem); letter-spacing: -.01em;}
.p-subtitle {margin: .35rem 0 0 0; color: var(--p-muted); font-size: 1.02rem;}
.p-notice {margin: .25rem 0 1rem 0; padding: .55rem .85rem; border: 1px solid var(--p-border);
           border-left: 4px solid var(--p-accent); border-radius: .4rem; background: var(--p-card);
           color: var(--p-text); font-size: .92rem;}
.p-status {margin: .5rem 0; color: var(--p-muted); font-size: .9rem; line-height: 2;}
.p-badge {display: inline-block; padding: .1rem .55rem; border-radius: .25rem; font-size: .78rem;
          font-weight: 600; border: 1px solid; margin-right: .35rem; background: var(--p-card);}
.p-b-loss {color: var(--p-loss); border-color: var(--p-loss);}
.p-b-pass {color: var(--p-pass); border-color: var(--p-pass);}
.p-b-oom {color: var(--p-oom); border-color: var(--p-oom);}
.p-b-unknown, .p-b-user {color: var(--p-muted); border-color: var(--p-muted);}
.p-b-accent {color: var(--p-accent); border-color: var(--p-accent);}
.p-b-real {color: var(--p-pass); border-color: var(--p-pass);}
.p-b-synth {color: var(--p-oom); border-color: var(--p-oom);}
.p-verdict {margin: .5rem 0 1rem 0; padding: 1.25rem 1.5rem; border: 1px solid var(--p-border);
            border-left: 8px solid var(--p-muted); border-radius: .5rem; background: var(--p-card);}
.p-verdict.p-loss {border-left-color: var(--p-loss);}
.p-verdict.p-pass {border-left-color: var(--p-pass);}
.p-verdict.p-oom {border-left-color: var(--p-oom);}
.p-vkicker {font-size: .75rem; font-weight: 600; letter-spacing: .08em; text-transform: uppercase;
            color: var(--p-muted);}
.p-vlabel {margin: .2rem 0 .5rem 0; padding: 0; font-weight: 800; line-height: 1.1;
           font-size: clamp(1.6rem, 4.5vw, 2.3rem); overflow-wrap: anywhere; color: var(--p-muted);}
.p-loss .p-vlabel {color: var(--p-loss);}
.p-pass .p-vlabel {color: var(--p-pass);}
.p-oom .p-vlabel {color: var(--p-oom);}
.p-vexp {margin: 0 0 .5rem 0; color: var(--p-text); font-size: 1.02rem; line-height: 1.5;}
.p-vnote {margin: 0; color: var(--p-muted); font-size: .9rem; font-weight: 600;}
.p-cards {display: grid; grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr)); gap: .75rem;
          margin: .25rem 0 1rem 0;}
.p-scard, .p-check, .p-finding {padding: .85rem 1rem; border: 1px solid var(--p-border);
          border-radius: .5rem; background: var(--p-card); min-width: 0;}
.p-finding {margin: .5rem 0;}
.p-stitle, .p-check-title, .p-card-title {font-size: .85rem; font-weight: 600; color: var(--p-muted);
          margin-bottom: .3rem;}
.p-card-title {color: var(--p-text); font-size: 1rem;}
.p-svalue {font-size: 1.5rem; font-weight: 700; color: var(--p-text); overflow-wrap: anywhere;}
.p-scard.p-loss .p-svalue {color: var(--p-loss);}
.p-scard.p-pass .p-svalue {color: var(--p-pass);}
.p-scard.p-oom .p-svalue {color: var(--p-oom);}
.p-scaption, .p-muted {color: var(--p-muted); font-size: .88rem; margin-top: .25rem;}
.p-explain {color: var(--p-text); font-size: .95rem; margin-top: .35rem;}
.p-compare {display: flex; flex-wrap: wrap; gap: .75rem; margin-top: .6rem;}
.p-pane {flex: 1 1 22rem; min-width: 0; border: 1px solid var(--p-border); border-radius: .5rem;
         overflow: hidden; background: var(--p-card);}
.p-pane-head {padding: .4rem .75rem; font-size: .78rem; font-weight: 600; letter-spacing: .04em;
              text-transform: uppercase; color: var(--p-muted); background: var(--p-bg);
              border-bottom: 1px solid var(--p-border);}
.p-line {display: block; padding: .1rem .75rem; white-space: pre-wrap; overflow-wrap: anywhere;
         font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .82rem;
         color: var(--p-text); border-left: 4px solid transparent;}
.p-no {display: inline-block; min-width: 2rem; margin-right: .6rem; color: var(--p-muted);
       user-select: none;}
.p-hl-loss {background: #FEF3F2; border-left-color: var(--p-loss); font-weight: 600;}
.p-hl-info {background: #EEF0FD; border-left-color: var(--p-accent); font-weight: 600;}
.p-miss {background: #FEF3F2; border-left-color: var(--p-loss); color: var(--p-loss);
         font-weight: 600; border-top: 1px dashed var(--p-loss);}
.p-note {padding: .5rem .75rem; color: var(--p-muted); font-style: italic;}
@media (max-width: 640px) {
  .p-verdict {padding: 1rem;}
}
</style>
"""


def pill(text, tone):
    """Badge; text is escaped, tone is restricted to the fixed set of tones."""
    return logic.pill_html(text, tone)


def label_pill(label):
    return logic.label_pill_html(label)


# ---------------------------------------------------------------- state ----

def init_state():
    defaults = {
        "j_mode": "Paste text", "g_mode": "Paste text",
        "j_text": "", "g_text": "",
        "example": None,          # logic.Example once loaded
        "example_error": "",
        "outcome": None,
        "verified_texts": ("", ""),   # exact text of the pair behind "outcome"
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


def verify_and_store(jraw, graw, verifier):
    """Run the verifier and remember the (decoded) texts for the comparison panel."""
    st.session_state["outcome"] = logic.run_verification(jraw, graw, verifier)
    st.session_state["verified_texts"] = (
        jraw.decode("utf-8", errors="replace") if isinstance(jraw, (bytes, bytearray)) else "",
        graw.decode("utf-8", errors="replace") if isinstance(graw, (bytes, bytearray)) else "")


def on_run_real_example():
    """One click: load the REAL pair and verify it (only on an explicit click)."""
    on_load_example()
    example = st.session_state["example"]
    if example is None:
        return
    verifier, error = logic.load_verifier()
    if verifier is None:
        st.session_state["outcome"] = logic.Outcome(False, error)
        return
    verify_and_store(example.jenkins, example.gitlab, verifier)


def on_clear():
    for key in ("j_text", "g_text"):
        st.session_state[key] = ""
    st.session_state["outcome"] = None
    st.session_state["verified_texts"] = ("", "")
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
    st.markdown(logic.header_html(), unsafe_allow_html=True)
    st.markdown('<div class="p-notice"><b>%s</b></div>' % logic.esc(logic.STATIC_MODEL_NOTICE),
                unsafe_allow_html=True)


def render_sidebar(verifier):
    with st.sidebar:
        st.header("About PARITY")
        st.write(
            "PARITY parses a **declarative Jenkinsfile** subset and a **GitLab CI YAML** "
            "subset, then compares four behaviours over a fixed scenario set: "
            "`branch_gate`, `always_cleanup`, `artifact_paths`, `build_commands`."
        )
        st.markdown(
            "- **PASS (modelled)** \u2013 no divergence in the modelled scenarios\n"
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
    st.markdown("**%s**" % title)
    st.radio("Source", ["Paste text", "Upload file"], key="%s_mode" % prefix,
             horizontal=True, label_visibility="collapsed")
    if st.session_state["%s_mode" % prefix] == "Upload file":
        st.file_uploader("Upload %s" % kind_hint, type=accept,
                         key="%s_upload" % prefix, help="Read as raw bytes; max %d KiB."
                         % (logic.MAX_INPUT_BYTES // 1024))
    else:
        st.text_area("Paste %s" % kind_hint, key="%s_text" % prefix, height=240,
                     label_visibility="collapsed", placeholder="Paste the %s here" % kind_hint)


def render_example_notes():
    if st.session_state["example_error"]:
        st.error(st.session_state["example_error"])
    if st.session_state["example"] is not None:
        st.markdown(label_pill(logic.LABEL_REAL) + " real conversion pair loaded from the "
                    "repository (details under Technical evidence).", unsafe_allow_html=True)


def render_verdict(outcome):
    st.markdown(logic.status_row_html(outcome), unsafe_allow_html=True)
    st.markdown(logic.verdict_card_html(logic.verdict_card(outcome.result)),
                unsafe_allow_html=True)
    if outcome.label == logic.LABEL_SYNTHETIC:
        st.info("This run includes a SYNTHETIC calibration fixture. It is not real "
                "conversion evidence.")
    elif outcome.label == logic.LABEL_USER:
        st.info("USER-SUPPLIED input: this is not part of the repository's real or "
                "synthetic evidence.")


def render_summary_cards(result):
    st.markdown(logic.summary_cards_html(logic.summary_cards(result)), unsafe_allow_html=True)


def render_comparison(outcome):
    st.subheader("Jenkins vs GitLab CI")
    jtext, gtext = st.session_state.get("verified_texts", ("", ""))
    view = logic.source_view(outcome.result, jtext, gtext)
    if view["mode"] == "neutral":
        st.info(view["neutral_message"])
    for finding in view["findings"]:
        st.markdown(logic.finding_html(finding), unsafe_allow_html=True)
    st.markdown(logic.compare_html(view), unsafe_allow_html=True)
    if view["jenkins"]["highlighted"]:
        st.caption("Highlighted: the Jenkins line(s) with no mapping in the supported GitLab "
                   "CI model, and the converted artifact block. Long inputs are shortened "
                   "here; the full text is in the input boxes.")
    for failed in view["failed_checks"]:
        st.markdown(pill("FAIL", "loss") + " <b>%s</b>: %s"
                    % (logic.esc(failed["id"]), logic.esc(failed["summary"])),
                    unsafe_allow_html=True)
        render_witness(outcome.result["checks"][failed["id"]].get("witness"))


def render_modelled_checks(result):
    st.subheader("Modelled checks")
    rows = logic.modelled_checks_view(result)
    if rows:
        st.markdown(logic.modelled_checks_html(rows), unsafe_allow_html=True)
    st.caption("PASS (modelled) means no divergence in the modelled scenarios. It is not "
               "proof of runtime equivalence.")
    st.markdown("##### Confirmed unsupported mappings")
    mappings = result.get("confirmed_unsupported_mappings") or []
    if not mappings:
        st.write("None found.")
    for m in mappings:
        if isinstance(m, dict):
            st.markdown(logic.mapping_card_html(m), unsafe_allow_html=True)


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
    st.caption("Raw witness JSON (collapsed tree):")
    st.json(witness, expanded=False)


def render_check_cards(result):
    cols = st.columns(2)
    for i, cid in enumerate(logic.CHECK_ORDER):
        check = result["checks"].get(cid)
        if check is None:
            continue
        label, _colour = logic.check_verdict_view(check["verdict"])
        with cols[i % 2]:
            with st.container(border=True):
                st.markdown("#### `%s`" % cid)
                st.markdown(pill(label, logic.CHECK_TONES.get(check["verdict"], "unknown")),
                            unsafe_allow_html=True)
                st.text(check.get("summary") or "")
                st.write("Scenarios evaluated: **%s** \u2022 Divergences: **%s**"
                         % (check.get("scenarios_evaluated"), check.get("divergences")))
                if check["verdict"] == "PASS":
                    st.caption("PASS (modelled) = no divergence in the modelled scenarios. "
                               "Not proof of runtime equivalence.")
                if check.get("reasons"):
                    st.markdown("Reasons")
                    for reason in check["reasons"]:
                        st.text("\u2022 " + str(reason))
                if check.get("witness"):
                    render_witness(check["witness"])


def render_mappings(result):
    st.markdown("##### Confirmed unsupported mappings (raw)")
    mappings = result.get("confirmed_unsupported_mappings") or []
    if not mappings:
        st.write("None found.")
        return
    for m in mappings:
        with st.container(border=True):
            st.markdown(pill("FAIL", "loss") + " **%s** \u2014 `%s`"
                        % (m.get("label", ""), m.get("id", "")), unsafe_allow_html=True)
            st.text(m.get("reason", ""))
            w = m.get("witness") or {}
            jw = w.get("jenkins") or {}
            if isinstance(jw, dict) and jw:
                st.write("Jenkins: stage `%s`, line %s, artifacts `%s`, fingerprint=%s"
                         % (jw.get("stage"), jw.get("line"), jw.get("artifacts"),
                            jw.get("fingerprint")))
            st.write("GitLab: %s" % w.get("gitlab", "n/a"))
            st.json(m, expanded=False)


def render_unparsed(result):
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
    st.caption("Never graded by the verifier. Each of these requires: runtime experiment.")
    for item in logic.unverified_items(result):
        st.markdown("- **`%s`**: %s _(requires: %s)_" % (
            item["id"], item["description"], item["requires"]))


def render_metadata(outcome):
    result = outcome.result
    st.write("Verifier version: `%s`" % result.get("verifier_version"))
    st.write("Verifier overall value: `%s`" % result.get("overall"))
    st.write("Jenkinsfile SHA-256:")
    st.code(result["inputs"]["jenkinsfile_sha256"], language=None)
    st.write("GitLab CI SHA-256:")
    st.code(result["inputs"]["gitlab_ci_sha256"], language=None)
    scen = result.get("scenario_set", {})
    st.caption("Scenario set: branches %s, pipeline_source %s, build_result %s (%d scenarios)"
               % (scen.get("branch"), scen.get("pipeline_source"), scen.get("build_result"),
                  len(scen.get("scenarios", []))))


def render_counts(result):
    """Verifier counts as plain metrics (computed from the result; no scores)."""
    cols = st.columns(3)
    for col, (label, value, help_text) in zip(cols, logic.metric_cards(result)):
        with col:
            st.metric(label, value, help=help_text)


def render_provenance(outcome):
    labels = (outcome.label_jenkins, outcome.label_gitlab)
    st.markdown("Jenkinsfile: " + label_pill(labels[0]) + " &nbsp; GitLab CI: "
                + label_pill(labels[1]), unsafe_allow_html=True)
    example = st.session_state.get("example")
    if example is not None and logic.LABEL_REAL in labels:
        bullets = logic.provenance_summary(example.provenance)
        if bullets:
            for b in bullets:
                st.markdown("- " + b)
        else:
            st.info("PROVENANCE.md was not found or is empty; the files were still "
                    "loaded from tests/fixtures/real.")
        if example.provenance:
            st.markdown("**Full PROVENANCE.md** (`%s`):" % REPO_ROOT_NOTE)
            st.text(example.provenance)
    elif logic.LABEL_SYNTHETIC in labels:
        st.caption("SYNTHETIC calibration fixtures are hand-made (tests/fixtures/synthetic/"
                   "README.md); they are not real conversion evidence.")
    else:
        st.caption("USER-SUPPLIED input: no provenance is recorded.")


def render_results(outcome):
    result = outcome.result
    render_verdict(outcome)
    render_summary_cards(result)
    st.download_button(
        "Download evidence JSON", data=outcome.evidence,
        file_name=outcome.filename, mime="application/json",
        help="Deterministic: exactly parity_verify.render_json(result); same inputs give "
             "byte-identical files.")
    render_comparison(outcome)
    render_modelled_checks(result)
    st.subheader("Technical evidence")
    with st.expander("Input provenance, SHA-256 and verifier"):
        render_provenance(outcome)
        render_metadata(outcome)
        render_counts(result)
    with st.expander("Per-check results"):
        render_check_cards(result)
        render_mappings(result)
    with st.expander("Unparsed / out-of-model items"):
        render_unparsed(result)
    with st.expander("Unverified runtime semantics (%d)"
                     % len(result.get("unverified_runtime_semantics", []))):
        render_unverified(result)
    st.warning(logic.DISCLAIMER)
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


def render_gate():
    st.subheader("CI verification gate")
    info = logic.gate_overview()
    st.write(info["intro"])
    for fact in info["facts"]:
        st.markdown("- " + fact)
    st.markdown("##### Exit codes")
    st.markdown(logic.gate_exit_code_markdown())
    st.caption("Strict mode is enabled by `--strict` or `PARITY_STRICT=1`; usage/IO errors "
               "exit 3 in both modes. Details: PARITY_GATE.md.")
    st.markdown("##### Merge request review workflow")
    for i, step in enumerate(info["workflow"], 1):
        st.markdown("%d. %s" % (i, step))
    st.info(info["not_implemented"])


# ------------------------------------------------------------------ main ----

def main():
    init_state()
    verifier, load_error = logic.load_verifier()
    render_header()
    render_sidebar(verifier)
    if verifier is None:
        st.error("Status: Error. " + load_error)
        st.stop()

    tab_verify, tab_calibration, tab_gate = st.tabs(["Verify", "Calibration", "CI gate"])
    with tab_verify:
        # Primary workflow: every action is visible without scrolling or expanding.
        c_run, c_load, c_verify, c_clear = st.columns(4)
        with c_run:
            st.button("Run the real conversion example", type="primary",
                      on_click=on_run_real_example, use_container_width=True,
                      help="Loads the REAL Jenkins\u2192GitLab conversion from "
                           "tests/fixtures/real/ and verifies it. Runs only when clicked.")
        with c_load:
            st.button("Load real conversion example", on_click=on_load_example,
                      use_container_width=True,
                      help="Only fills the inputs from tests/fixtures/real/Jenkinsfile and "
                           "tests/fixtures/real/gitlab-ci.converted.yml; click Verify "
                           "Migration to run the checks.")
        with c_verify:
            clicked = st.button("Verify Migration", use_container_width=True,
                                help="Runs the static verifier on the two inputs below.")
        with c_clear:
            st.button("Clear inputs", on_click=on_clear, use_container_width=True)
        render_example_notes()

        with st.expander("Paste or upload your own files"):
            left, right = st.columns(2)
            with left:
                render_input_column("j", "Jenkinsfile", "Jenkinsfile", None)
            with right:
                render_input_column("g", "GitLab CI YAML", "GitLab CI YAML",
                                    ["yml", "yaml", "txt"])

        if clicked:
            # Verification is synchronous: the spinner is shown only while it really runs.
            with st.spinner("Running verification\u2026"):
                try:
                    jraw = current_bytes("j", "Jenkinsfile")
                    graw = current_bytes("g", "GitLab CI YAML")
                    verify_and_store(jraw, graw, verifier)
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
                st.error("Status: Error. " + outcome.error)

    with tab_calibration:
        render_calibration()
    with tab_gate:
        render_gate()


main()
