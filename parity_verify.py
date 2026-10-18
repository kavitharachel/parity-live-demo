#!/usr/bin/env python3
"""PARITY verifier v0: deterministic static comparison of a Jenkinsfile and a
GitLab CI configuration.

THIS IS A STATIC MODEL.  A PASS here does NOT prove Jenkins runtime
equivalence; see ``unverified_runtime_semantics`` in the output and
README_PARITY.md.

Dependencies: PyYAML + the Python standard library only.

Supported Jenkins subset (declarative pipeline, nothing else)
-------------------------------------------------------------
::

    pipeline {
        agent any
        stages {
            stage('X') {                       // name: plain string, no ${} interpolation
                when { branch 'Y' }            // optional; exactly one `branch` condition,
                                               // literal name (no glob characters)
                steps {                        // required
                    sh '...'                   // or sh('...'); plain string only
                    archiveArtifacts artifacts: '...', fingerprint: true|false
                                               // `artifacts` required (comma separated Ant
                                               // patterns allowed); `fingerprint` optional
                }
            }
        }
        post { always { sh '...' } }           // optional; only `always` with `sh` steps
    }

Everything else is OUT OF MODEL: it is listed under ``unparsed`` (with line
numbers) and makes every check it can influence OUT_OF_MODEL.  Examples:
parallel, matrix, script blocks, environment, parameters, input, options,
triggers, tools, agent other than ``any``, post failure/success/cleanup/
unstable/changed, ``when`` with expression/not/anyOf/allOf/tag/changeset,
other steps (junit, stash, withCredentials, ...), GString interpolation,
triple-quoted strings, unbalanced braces.

Supported GitLab CI subset
--------------------------
Top level: ``stages`` and job mappings (hidden ``.name`` jobs are ignored as
inert).  Job keywords: ``stage``, ``script``, ``rules`` (items with ``if`` and/
or ``when`` only), ``when`` (on_success/always/on_failure/never),
``allow_failure`` (boolean), ``artifacts: {paths: [...]}`` and ``needs``
(recognised, but scheduling semantics are not modelled => OUT_OF_MODEL).
``rules:if`` supports only ``$CI_COMMIT_BRANCH`` / ``$CI_PIPELINE_SOURCE``
compared with ``==`` / ``!=`` against one quoted literal.  Everything else
(include, extends, trigger, workflow, default, variables, image, cache,
before_script, after_script, rules changes/exists/variables, manual/delayed,
...) is OUT OF MODEL.

Model conventions
-----------------
* Jenkins stage ``X`` corresponds to the GitLab job whose name equals ``X``
  case-insensitively (non-``.post`` stage).  Jenkins ``post { always }``
  corresponds to the jobs in the GitLab ``.post`` stage.
* Scenarios: branch x pipeline_source x build_result.  ``build_result=fail``
  means the first stage/job that runs (excluding post/``.post``) fails; later
  ``on_success`` work is skipped on both sides.
* Command comparison is exact string equality after whitespace-only
  normalisation (leading/trailing whitespace stripped, runs of spaces/tabs
  collapsed outside of quotes).
* Artifact glob semantics (Jenkins side, Ant style): ``*`` and ``?`` do not
  cross ``/``; a whole path segment ``**`` matches zero or more directories; a
  trailing ``/`` means ``/**``; a literal name matches only that exact file.
  GitLab side: a path without glob characters matches that file or, as a
  directory, everything below it (``out`` and ``out/`` both cover ``out/x``);
  a glob matches files with the same rules as above.  Files tested are a
  deterministic virtual list: files the Jenkins ``sh`` commands create (parsed
  from ``> file`` / ``>> file`` / ``touch file``) plus a fixed probe set
  (``EXTRA_PROBE_FILES``).
"""

import argparse
import difflib
import hashlib
import json
import re
import shlex
import sys

try:  # pragma: no cover - exercised implicitly
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

VERIFIER_VERSION = "0.1.0"

PASS = "PASS"
FAIL = "FAIL"
OUT_OF_MODEL = "OUT_OF_MODEL"

OVERALL_EQUIVALENT = "CHECKED_EQUIVALENT_WITH_UNVERIFIED_RUNTIME"
OVERALL_LOSS = "CONFIRMED_LOSS"
OVERALL_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"

EXIT_CODES = {
    OVERALL_EQUIVALENT: 0,
    OVERALL_LOSS: 1,
    OVERALL_INSUFFICIENT: 2,
}
EXIT_USAGE = 3

CHECK_IDS = ("always_cleanup", "artifact_paths", "branch_gate", "build_commands")
STAGE_CHECKS = ("artifact_paths", "branch_gate", "build_commands")
ALL_CHECKS = CHECK_IDS

DEFAULT_BRANCHES = ("main", "parity/fixture-1", "other-branch")
DEFAULT_PIPELINE_SOURCES = ("push",)
DEFAULT_BUILD_RESULTS = ("ok", "fail")
EXTRA_PROBE_FILES = ("README.md", "dist/app.txt", "other/file.txt", "out/nested/deep.txt")

GITLAB_DEFAULT_STAGES = ("build", "test", "deploy")
GITLAB_RESERVED_GLOBALS = frozenset(
    [
        "include", "workflow", "default", "variables", "image", "services",
        "before_script", "after_script", "cache", "spec",
    ]
)
GITLAB_JOB_KEYS = frozenset(
    ["stage", "script", "rules", "when", "allow_failure", "artifacts", "needs"]
)
GITLAB_WHEN_VALUES = ("on_success", "always", "on_failure", "never")

UNVERIFIED_RUNTIME_SEMANTICS = [
    {
        "id": "artifact_retention_expiry",
        "description": "Artifact retention/expiry defaults differ between Jenkins "
        "(job/build retention settings) and GitLab (instance default expire_in). "
        "Not modelled.",
        "requires": "runtime experiment",
    },
    {
        "id": "branch_gate_tag_and_mr_pipelines",
        "description": "Branch gating for tag pipelines and merge-request pipelines "
        "(CI_COMMIT_BRANCH is unset in those; Jenkins `when { branch }` depends on "
        "the multibranch job type). Only pipeline_source=push is evaluated.",
        "requires": "runtime experiment",
    },
    {
        "id": "build_failure_injection_model",
        "description": "build_result=fail is modelled as 'the first job/stage that "
        "runs fails'. Real failure modes (unstable, aborted, timeouts, failures "
        "in later stages) are not enumerated.",
        "requires": "runtime experiment",
    },
    {
        "id": "cancellation_behavior",
        "description": "Behaviour of Jenkins post { always } versus GitLab `.post` "
        "`when: always` when a run is cancelled/aborted is not modelled.",
        "requires": "runtime experiment",
    },
    {
        "id": "fingerprint_cross_build_tracking",
        "description": "Jenkins artifact fingerprinting (cross-build tracking of "
        "artifact checksums) has no mapping in the supported GitLab model; any "
        "runtime consequences are not evaluated.",
        "requires": "runtime experiment",
    },
    {
        "id": "jenkins_and_gitlab_artifact_glob_nuances",
        "description": "Real glob matching nuances (Jenkins default excludes, "
        "dotfiles, symlinks, case sensitivity, empty-archive behaviour; GitLab "
        "runner treatment of directories matched by a glob) are approximated by the "
        "documented static semantics only.",
        "requires": "runtime experiment",
    },
    {
        "id": "runner_image_environment",
        "description": "Runner/agent environment (OS, image, installed tools, "
        "tags) is not specified in either input and is not compared.",
        "requires": "runtime experiment",
    },
    {
        "id": "workspace_sharing",
        "description": "Jenkins stages share one workspace; GitLab jobs each get a "
        "fresh clone and only receive earlier files via artifacts/cache. Not "
        "modelled.",
        "requires": "runtime experiment",
    },
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def normalize_command(cmd):
    """Whitespace-only normalisation.

    Strips the ends and collapses runs of spaces/tabs to one space, except
    inside single/double quotes.  Newlines inside a command are preserved
    (they separate commands), so ``a\\nb`` never equals ``a b``.
    """
    cmd = cmd.strip()
    out = []
    quote = None
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            out.append(ch)
            if quote == '"' and ch == "\\" and i + 1 < len(cmd):
                out.append(cmd[i + 1])
                i += 1
            elif ch == quote:
                quote = None
        elif ch in ("'", '"'):
            out.append(ch)
            quote = ch
        elif ch in (" ", "\t"):
            if not out or out[-1] != " ":
                out.append(" ")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def build_scenarios(branches=DEFAULT_BRANCHES, sources=DEFAULT_PIPELINE_SOURCES,
                    results=DEFAULT_BUILD_RESULTS):
    scenarios = []
    for branch in branches:
        for source in sources:
            for result in results:
                scenarios.append(
                    {"branch": branch, "build_result": result, "pipeline_source": source}
                )
    return scenarios


def _unparsed(side, line, reason, affects, source_lines):
    text = ""
    if source_lines and 1 <= line <= len(source_lines):
        text = source_lines[line - 1].strip()[:200]
    return {
        "affects": sorted(set(affects)),
        "line": line,
        "reason": reason,
        "side": side,
        "text": text,
    }


# ---------------------------------------------------------------------------
# Jenkins parser (declarative subset)
# ---------------------------------------------------------------------------

class _Tok(object):
    __slots__ = ("type", "value", "line", "flag")

    def __init__(self, type_, value, line, flag=None):
        self.type = type_
        self.value = value
        self.line = line
        self.flag = flag


_ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_NUM_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_PUNCT = {
    "{": "lbrace", "}": "rbrace", "(": "lparen", ")": "rparen",
    "[": "lbracket", "]": "rbracket", ",": "comma", ":": "colon", ";": "semi",
}
_OPEN_FOR = {"rbrace": "lbrace", "rparen": "lparen", "rbracket": "lbracket"}


def _tokenize(text):
    """Return (tokens, errors).  errors is a list of (line, message)."""
    toks = []
    errors = []
    stack = []
    i = 0
    line = 1
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\n":
            toks.append(_Tok("nl", "\n", line))
            line += 1
            i += 1
        elif c in " \t\r":
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                errors.append((line, "unterminated block comment"))
                break
            line += text.count("\n", i, j + 2)
            i = j + 2
        elif c in ("'", '"'):
            if text.startswith(c * 3, i):
                j = text.find(c * 3, i + 3)
                if j < 0:
                    errors.append((line, "unterminated triple-quoted string"))
                    break
                toks.append(_Tok("str", text[i + 3:j], line, "triple-quoted string"))
                line += text.count("\n", i, j + 3)
                i = j + 3
                continue
            j = i + 1
            buf = []
            flag = None
            closed = False
            while j < n:
                ch = text[j]
                if ch == "\n":
                    break
                if ch == "\\":
                    nxt = text[j + 1] if j + 1 < n else ""
                    if nxt in ("\\", "'", '"'):
                        buf.append(nxt)
                        j += 2
                        continue
                    flag = flag or "unsupported escape sequence"
                    buf.append(ch)
                    j += 1
                    continue
                if ch == c:
                    closed = True
                    break
                if ch == "$" and c == '"':
                    flag = flag or "GString interpolation"
                buf.append(ch)
                j += 1
            if not closed:
                errors.append((line, "unterminated string literal"))
                break
            toks.append(_Tok("str", "".join(buf), line, flag))
            i = j + 1
        elif c in _PUNCT:
            ttype = _PUNCT[c]
            if ttype in ("lbrace", "lparen", "lbracket"):
                stack.append((ttype, line))
            elif ttype in _OPEN_FOR:
                if not stack or stack[-1][0] != _OPEN_FOR[ttype]:
                    errors.append((line, "unbalanced braces/brackets: unexpected '%s'" % c))
                    i += 1
                    continue
                stack.pop()
            toks.append(_Tok(ttype, c, line))
            i += 1
        else:
            m = _ID_RE.match(text, i)
            if m:
                toks.append(_Tok("id", m.group(0), line))
                i = m.end()
                continue
            m = _NUM_RE.match(text, i)
            if m:
                toks.append(_Tok("num", m.group(0), line))
                i = m.end()
                continue
            toks.append(_Tok("op", c, line))
            i += 1
    for ttype, ln in stack:
        errors.append((ln, "unbalanced braces/brackets: unclosed '%s'" % {
            "lbrace": "{", "lparen": "(", "lbracket": "["}[ttype]))
    return toks, errors


class _Node(object):
    __slots__ = ("head", "block", "line")

    def __init__(self, head, block, line):
        self.head = head
        self.block = block
        self.line = line

    @property
    def name(self):
        if self.head and self.head[0].type == "id":
            return self.head[0].value
        return None

    @property
    def args(self):
        return self.head[1:]


def _parse_statements(toks, pos, in_block):
    stmts = []
    while pos < len(toks):
        t = toks[pos]
        if t.type in ("nl", "semi"):
            pos += 1
            continue
        if t.type == "rbrace":
            if in_block:
                return stmts, pos
            pos += 1  # stray; tokenizer already reported it
            continue
        head = []
        depth = 0
        start_line = t.line
        while pos < len(toks):
            t = toks[pos]
            if t.type in ("lparen", "lbracket"):
                depth += 1
            elif t.type in ("rparen", "rbracket"):
                depth -= 1
            if depth <= 0:
                if t.type in ("nl", "semi", "rbrace"):
                    if t.type == "nl" and head and head[-1].type == "comma":
                        pos += 1
                        continue
                    break
                if t.type == "lbrace":
                    break
            if depth > 0 and t.type == "nl":
                pos += 1
                continue
            head.append(t)
            pos += 1
        block = None
        if pos < len(toks) and toks[pos].type == "lbrace":
            block, pos = _parse_statements(toks, pos + 1, True)
            pos += 1  # consume rbrace
        if head or block is not None:
            stmts.append(_Node(head, block, start_line))
    return stmts, pos


def _strip_parens(args):
    if len(args) >= 2 and args[0].type == "lparen" and args[-1].type == "rparen":
        return args[1:-1]
    return args


def parse_jenkinsfile(text):
    """Parse the supported declarative subset.

    Returns (model, unparsed).  model["valid"] is False when nothing could be
    parsed reliably (e.g. unbalanced braces).
    """
    src = text.split("\n")
    unparsed = []
    model = {"valid": True, "stages": [], "post_always": None}

    def add(line, reason, affects):
        unparsed.append(_unparsed("jenkins", line, reason, affects, src))

    toks, errors = _tokenize(text)
    if errors:
        for ln, msg in errors:
            add(ln, msg, ALL_CHECKS)
        model["valid"] = False
        return model, unparsed

    nodes, _ = _parse_statements(toks, 0, False)
    pipelines = [n for n in nodes if n.name == "pipeline" and n.block is not None
                 and len(n.head) == 1]
    for n in nodes:
        if n not in pipelines:
            add(n.line, "unsupported top-level construct (only a declarative "
                "`pipeline { }` is modelled)", ALL_CHECKS)
    if len(pipelines) != 1:
        if len(pipelines) > 1:
            add(pipelines[1].line, "more than one pipeline block", ALL_CHECKS)
        elif not nodes:
            add(1, "no pipeline block found", ALL_CHECKS)
        model["valid"] = False
        return model, unparsed

    def plain_string(tok, line, affects, what):
        if tok.flag:
            add(line, "%s uses unsupported string form: %s" % (what, tok.flag), affects)
            return None
        return tok.value

    def string_arg(node, affects, what):
        args = _strip_parens(node.args)
        if len(args) != 1 or args[0].type != "str":
            add(node.line, "unsupported %s syntax (expected a single plain string)" % what,
                affects)
            return None
        return plain_string(args[0], node.line, affects, what)

    seen_stage_names = set()

    def parse_sh(node, affects, what):
        if node.block is not None:
            add(node.line, "unsupported %s: block form" % what, affects)
            return None
        cmd = string_arg(node, affects, what)
        if cmd is None:
            return None
        return {"cmd": cmd, "line": node.line}

    def parse_archive(node, stage_affects):
        affects = ("artifact_paths", "build_commands")
        if node.block is not None:
            add(node.line, "unsupported archiveArtifacts: block form", affects)
            return None
        args = _strip_parens(node.args)
        parts, cur = [], []
        for t in args:
            if t.type == "comma":
                parts.append(cur)
                cur = []
            else:
                cur.append(t)
        parts.append(cur)
        opts = {}
        for part in parts:
            if (len(part) != 3 or part[0].type != "id" or part[1].type != "colon"
                    or part[2].type not in ("str", "id")):
                add(node.line, "unsupported archiveArtifacts argument syntax", affects)
                return None
            key, val = part[0].value, part[2]
            if key == "artifacts" and val.type == "str":
                s = plain_string(val, node.line, affects, "archiveArtifacts artifacts")
                if s is None:
                    return None
                opts["artifacts"] = s
            elif key == "fingerprint" and val.type == "id" and val.value in ("true", "false"):
                opts["fingerprint"] = val.value == "true"
            else:
                add(node.line, "unsupported archiveArtifacts option '%s'" % key, affects)
                return None
        if "artifacts" not in opts:
            add(node.line, "archiveArtifacts without `artifacts:`", affects)
            return None
        patterns = [p.strip() for p in opts["artifacts"].split(",")]
        for p in patterns:
            if not _pattern_ok(p):
                add(node.line, "unsupported artifact pattern %r" % p, ("artifact_paths",))
                return None
        return {
            "fingerprint": opts.get("fingerprint", False),
            "line": node.line,
            "patterns": patterns,
            "raw": opts["artifacts"],
        }

    def parse_stage(node):
        if node.name != "stage" or node.block is None:
            add(node.line, "unsupported construct inside `stages` (only stage('X') { })",
                ALL_CHECKS)
            return
        args = _strip_parens(node.args)
        if len(args) != 1 or args[0].type != "str":
            add(node.line, "unsupported stage declaration", ALL_CHECKS)
            return
        name = plain_string(args[0], node.line, ALL_CHECKS, "stage name")
        if name is None:
            return
        if name.casefold() in seen_stage_names:
            add(node.line, "duplicate stage name %r" % name, ALL_CHECKS)
            return
        seen_stage_names.add(name.casefold())
        stage = {"archives": [], "commands": [], "gate": None, "line": node.line,
                 "name": name}
        have_steps = False
        have_when = False
        for child in node.block:
            cname = child.name
            if cname == "when" and child.block is not None and len(child.head) == 1:
                if have_when:
                    add(child.line, "duplicate `when` block", ("branch_gate",))
                    continue
                have_when = True
                conds = child.block
                if len(conds) != 1:
                    add(child.line, "`when` with zero or multiple conditions is not "
                        "modelled", ("branch_gate",))
                    continue
                cond = conds[0]
                if cond.name == "branch" and cond.block is None:
                    val = string_arg(cond, ("branch_gate",), "when branch")
                    if val is None:
                        continue
                    if any(ch in val for ch in "*?[]{}"):
                        add(cond.line, "`when { branch }` glob pattern is not modelled",
                            ("branch_gate",))
                        continue
                    stage["gate"] = {"branch": val, "line": cond.line}
                else:
                    add(cond.line, "unsupported `when` condition %r (only `branch 'name'`)"
                        % (cond.name,), ("branch_gate",))
            elif cname == "steps" and child.block is not None and len(child.head) == 1:
                if have_steps:
                    add(child.line, "duplicate `steps` block", STAGE_CHECKS)
                    continue
                have_steps = True
                for step in child.block:
                    if step.name == "sh":
                        s = parse_sh(step, ("build_commands", "artifact_paths"), "sh")
                        if s:
                            stage["commands"].append(s)
                    elif step.name == "archiveArtifacts":
                        a = parse_archive(step, STAGE_CHECKS)
                        if a:
                            stage["archives"].append(a)
                    else:
                        add(step.line, "unsupported step %r" % (step.name,),
                            ("artifact_paths", "build_commands"))
            else:
                add(child.line, "unsupported stage directive %r" % (cname,), ALL_CHECKS)
        if not have_steps:
            add(node.line, "stage %r has no `steps` block" % name, STAGE_CHECKS)
        model["stages"].append(stage)

    pipe = pipelines[0]
    have_stages = False
    for child in pipe.block:
        cname = child.name
        if cname == "agent":
            args = child.args
            if not (child.block is None and len(args) == 1 and args[0].type == "id"
                    and args[0].value == "any"):
                add(child.line, "unsupported agent (only `agent any`)", ALL_CHECKS)
        elif cname == "stages" and child.block is not None and len(child.head) == 1:
            if have_stages:
                add(child.line, "duplicate `stages` block", ALL_CHECKS)
                continue
            have_stages = True
            for st in child.block:
                parse_stage(st)
        elif cname == "post" and child.block is not None and len(child.head) == 1:
            for cond in child.block:
                if cond.name == "always" and cond.block is not None and len(cond.head) == 1:
                    if model["post_always"] is not None:
                        add(cond.line, "duplicate post `always`", ("always_cleanup",))
                        continue
                    model["post_always"] = []
                    for step in cond.block:
                        if step.name == "sh":
                            s = parse_sh(step, ("always_cleanup",), "post always sh")
                            if s:
                                model["post_always"].append(s)
                        else:
                            add(step.line, "unsupported step %r in post always"
                                % (step.name,), ("always_cleanup",))
                else:
                    add(cond.line, "unsupported post condition %r (only `always`)"
                        % (cond.name,), ("always_cleanup",))
        else:
            add(child.line, "unsupported pipeline directive %r" % (cname,), ALL_CHECKS)
    if not have_stages:
        add(pipe.line, "pipeline has no `stages` block", ALL_CHECKS)
    return model, unparsed


# ---------------------------------------------------------------------------
# Artifact pattern matching
# ---------------------------------------------------------------------------

_PATTERN_RE = re.compile(r"^[A-Za-z0-9_./*?+@~=-]+$")


def _pattern_ok(p):
    if not p or not _PATTERN_RE.match(p):
        return False
    if p.startswith("/") or p.startswith("./") or p.startswith("../"):
        return False
    return ".." not in p.split("/")


def _has_glob(p):
    return "*" in p or "?" in p


def _segment_regex(seg):
    out = []
    for ch in seg:
        if ch == "*":
            if not out or out[-1] != "[^/]*":
                out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
    return re.compile("^" + "".join(out) + "$")


def _glob_match(pattern, path):
    """Ant-style match of ``pattern`` against file ``path`` (see module doc)."""
    if pattern.endswith("/"):
        pattern += "**"
    psegs = pattern.split("/")
    fsegs = path.split("/")
    memo = {}

    def rec(pi, fi):
        key = (pi, fi)
        if key in memo:
            return memo[key]
        if pi == len(psegs):
            res = fi == len(fsegs)
        elif psegs[pi] == "**":
            res = rec(pi + 1, fi) or (fi < len(fsegs) and rec(pi, fi + 1))
        elif fi == len(fsegs):
            res = False
        else:
            res = bool(_segment_regex(psegs[pi]).match(fsegs[fi])) and rec(pi + 1, fi + 1)
        memo[key] = res
        return res

    return rec(0, 0)


def jenkins_archives(patterns, path):
    for p in patterns:
        if _glob_match(p, path):
            return True
    return False


def gitlab_collects(paths, path):
    for p in paths:
        if _has_glob(p):
            if _glob_match(p, path):
                return True
        else:
            base = p.rstrip("/")
            if path == base or path.startswith(base + "/"):
                return True
    return False


def derive_created_files(commands):
    """Files created by shell commands: ``> f``, ``>> f`` and ``touch f``."""
    files = set()
    for cmd in commands:
        try:
            lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
            lex.whitespace_split = True
            toks = list(lex)
        except ValueError:
            continue
        i = 0
        seg_start = True
        while i < len(toks):
            t = toks[i]
            if t in ("&&", "||", ";", "|", "&", "(", ")"):
                seg_start = True
                i += 1
                continue
            if t in (">", ">>") and i + 1 < len(toks):
                files.add(toks[i + 1])
                i += 2
                seg_start = False
                continue
            if seg_start and t == "touch":
                j = i + 1
                while j < len(toks) and toks[j] not in ("&&", "||", ";", "|", "&", ">", ">>"):
                    if not toks[j].startswith("-"):
                        files.add(toks[j])
                    j += 1
                i = j
                seg_start = False
                continue
            seg_start = False
            i += 1
    return sorted(f for f in files if _pattern_ok(f) and not _has_glob(f))


# ---------------------------------------------------------------------------
# GitLab CI parser (subset)
# ---------------------------------------------------------------------------

_IF_RE = re.compile(
    r"""^\s*\$(CI_COMMIT_BRANCH|CI_PIPELINE_SOURCE)\s*(==|!=)\s*"""
    r"""(?:"([^"\\]*)"|'([^'\\]*)')\s*$"""
)


def parse_gitlab_ci(text):
    """Parse the supported GitLab CI subset. Returns (model, unparsed)."""
    src = text.split("\n")
    unparsed = []
    model = {"valid": True, "stages": None, "jobs": []}

    def add(line, reason, affects):
        unparsed.append(_unparsed("gitlab", line, reason, affects, src))

    def invalid(line, reason):
        add(line, reason, ALL_CHECKS)
        model["valid"] = False
        return model, unparsed

    if yaml is None:
        raise RuntimeError("PyYAML is required (pip install -r requirements.txt)")
    try:
        root = yaml.compose(text)
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = mark.line + 1 if mark is not None else 1
        return invalid(line, "YAML syntax error: %s" % (getattr(exc, "problem", None) or "invalid"))
    if not isinstance(data, dict) or not isinstance(root, yaml.MappingNode):
        return invalid(1, "top level of the YAML document is not a mapping")
    if any(not isinstance(k, str) for k in data):
        return invalid(1, "non-string top-level key")

    top_lines = {}
    job_key_lines = {}
    seen = set()
    for knode, vnode in root.value:
        key = knode.value
        if key in seen:
            add(knode.start_mark.line + 1, "duplicate top-level key %r" % key, ALL_CHECKS)
        seen.add(key)
        top_lines[key] = knode.start_mark.line + 1
        if isinstance(vnode, yaml.MappingNode):
            job_key_lines[key] = {
                k.value: k.start_mark.line + 1 for k, _ in vnode.value
                if isinstance(k, yaml.ScalarNode)
            }

    # stages
    if "stages" in data:
        stages = data["stages"]
        if not isinstance(stages, list) or any(not isinstance(s, str) for s in stages):
            return invalid(top_lines["stages"], "`stages` must be a list of strings")
        if ".pre" in stages or ".post" in stages:
            add(top_lines["stages"], "`.pre`/`.post` listed explicitly in `stages` is "
                "not modelled", ALL_CHECKS)
        model["stages"] = [s for s in stages if s not in (".pre", ".post")]
    else:
        model["stages"] = list(GITLAB_DEFAULT_STAGES)
    known_stages = set(model["stages"]) | {".pre", ".post"}

    for name, val in data.items():
        line = top_lines.get(name, 1)
        if name == "stages":
            continue
        if name in GITLAB_RESERVED_GLOBALS:
            add(line, "unsupported global keyword %r" % name, ALL_CHECKS)
            continue
        if name.startswith("."):
            continue  # hidden job / template: inert unless referenced (extends is flagged)
        if not isinstance(val, dict):
            add(line, "top-level key %r is not a job mapping" % name, ALL_CHECKS)
            continue
        lines = job_key_lines.get(name, {})
        stage = val.get("stage", "test")
        if not isinstance(stage, str):
            add(lines.get("stage", line), "job %r: `stage` is not a string" % name, ALL_CHECKS)
            continue
        role = ("always_cleanup",) if stage == ".post" else STAGE_CHECKS
        job = {
            "allow_failure": False, "artifacts_paths": None, "line": line,
            "name": name, "rules": None, "script": [], "stage": stage, "when": None,
        }
        if stage not in known_stages:
            add(lines.get("stage", line), "job %r uses undefined stage %r" % (name, stage),
                ALL_CHECKS)
        for key in val:
            if key not in GITLAB_JOB_KEYS:
                add(lines.get(key, line), "job %r: unsupported keyword %r" % (name, key), role)
        if "needs" in val:
            add(lines.get("needs", line), "job %r: `needs` is recognised but its scheduling "
                "semantics are not modelled" % name, role)
        # script
        script = val.get("script")
        if isinstance(script, str):
            script = [script]
        if (not isinstance(script, list) or not script
                or any(not isinstance(s, str) for s in script)):
            add(lines.get("script", line), "job %r: `script` missing or not a list of plain "
                "strings" % name, role)
        else:
            job["script"] = list(script)
        # when
        if "when" in val:
            if val["when"] in GITLAB_WHEN_VALUES:
                job["when"] = val["when"]
            else:
                add(lines.get("when", line), "job %r: unsupported `when: %s`"
                    % (name, val["when"]), role)
        # allow_failure
        if "allow_failure" in val:
            if isinstance(val["allow_failure"], bool):
                job["allow_failure"] = val["allow_failure"]
            else:
                add(lines.get("allow_failure", line), "job %r: unsupported allow_failure form"
                    % name, role)
        # rules
        if "rules" in val:
            rules = val["rules"]
            parsed_rules = []
            if not isinstance(rules, list):
                add(lines.get("rules", line), "job %r: `rules` is not a list" % name, role)
            else:
                for idx, r in enumerate(rules):
                    rl = lines.get("rules", line)
                    if not isinstance(r, dict):
                        add(rl, "job %r: rules[%d] is not a mapping" % (name, idx), role)
                        continue
                    bad = [k for k in r if k not in ("if", "when")]
                    for k in bad:
                        add(rl, "job %r: unsupported rules[%d] keyword %r" % (name, idx, k),
                            role)
                    cond = None
                    if "if" in r:
                        m = _IF_RE.match(r["if"]) if isinstance(r["if"], str) else None
                        if not m:
                            add(rl, "job %r: unsupported rules:if expression %r"
                                % (name, r["if"]), role)
                        else:
                            cond = {
                                "op": m.group(2),
                                "value": m.group(3) if m.group(3) is not None else m.group(4),
                                "var": m.group(1),
                            }
                    rw = None
                    if "when" in r:
                        if r["when"] in GITLAB_WHEN_VALUES:
                            rw = r["when"]
                        else:
                            add(rl, "job %r: unsupported rules[%d] `when: %s`"
                                % (name, idx, r["when"]), role)
                    parsed_rules.append({"if": cond, "when": rw})
                job["rules"] = parsed_rules
        # artifacts
        if "artifacts" in val:
            art = val["artifacts"]
            al = lines.get("artifacts", line)
            if not isinstance(art, dict):
                add(al, "job %r: `artifacts` is not a mapping" % name, ("artifact_paths",))
            else:
                for k in art:
                    if k != "paths":
                        add(al, "job %r: unsupported artifacts keyword %r" % (name, k),
                            ("artifact_paths",))
                paths = art.get("paths")
                if (not isinstance(paths, list) or any(not isinstance(p, str) for p in paths)):
                    add(al, "job %r: artifacts:paths missing or not a list of strings" % name,
                        ("artifact_paths",))
                else:
                    ok = True
                    for p in paths:
                        if not _pattern_ok(p):
                            add(al, "job %r: unsupported artifact path %r" % (name, p),
                                ("artifact_paths",))
                            ok = False
                    if ok:
                        job["artifacts_paths"] = list(paths)
        model["jobs"].append(job)
    return model, unparsed


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def jenkins_simulate(model, scenario):
    """Return the list of stage names executed in ``scenario``."""
    executed = []
    failed = False
    injected = False
    for stage in model["stages"]:
        if failed:
            break
        gate = stage["gate"]
        if gate is not None and gate["branch"] != scenario["branch"]:
            continue
        executed.append(stage["name"])
        if scenario["build_result"] == "fail" and not injected:
            injected = True
            failed = True
    return executed


def _rule_matches(rule, scenario):
    cond = rule["if"]
    if cond is None:
        return True
    actual = scenario["branch"] if cond["var"] == "CI_COMMIT_BRANCH" else scenario["pipeline_source"]
    return (actual == cond["value"]) == (cond["op"] == "==")


def gitlab_job_when(job, scenario):
    default = job["when"] or "on_success"
    if job["rules"] is None:
        return default
    for rule in job["rules"]:
        if _rule_matches(rule, scenario):
            return rule["when"] or default
    return "never"


def gitlab_simulate(model, scenario):
    """Return the list of GitLab job names that run, in execution order."""
    executed = []
    failed_before = False
    injected = False
    for stage in [".pre"] + model["stages"] + [".post"]:
        stage_failed = False
        for job in model["jobs"]:
            if job["stage"] != stage:
                continue
            when = gitlab_job_when(job, scenario)
            runs = (when == "always" or (when == "on_success" and not failed_before)
                    or (when == "on_failure" and failed_before))
            if not runs:
                continue
            executed.append(job["name"])
            if scenario["build_result"] == "fail" and not injected and stage != ".post":
                injected = True
                if not job["allow_failure"]:
                    stage_failed = True
        failed_before = failed_before or stage_failed
    return executed


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _key(name):
    return name.casefold()


def _gl_jobs_by_key(gmodel, include_post):
    return {_key(j["name"]): j for j in gmodel["jobs"]
            if include_post or j["stage"] != ".post"}


def _check_result(verdict, summary, scenarios_evaluated=None, divergences=0, witness=None,
                  reasons=None):
    return {
        "divergences": divergences,
        "reasons": reasons or [],
        "scenarios_evaluated": scenarios_evaluated,
        "summary": summary,
        "verdict": verdict,
        "witness": witness,
    }


def _list_diff(a, b, label_a="jenkins", label_b="gitlab"):
    return list(difflib.unified_diff(a, b, label_a, label_b, lineterm="", n=0))


def check_branch_gate(jmodel, gmodel, scenarios):
    jstages = {_key(s["name"]) for s in jmodel["stages"]}
    gjobs = _gl_jobs_by_key(gmodel, False)
    unmatched_j = [s["name"] for s in jmodel["stages"] if _key(s["name"]) not in gjobs]
    unmatched_g = [k for k in gjobs if k not in jstages]
    if unmatched_j and unmatched_g:
        return _check_result(
            OUT_OF_MODEL,
            "cannot map stages to jobs by name (possible rename)",
            reasons=["Jenkins stages without same-named GitLab job: %s; GitLab jobs "
                     "without same-named Jenkins stage: %s"
                     % (sorted(unmatched_j), sorted(gjobs[k]["name"] for k in unmatched_g))],
        )
    witness = None
    diverged = 0
    for sc in scenarios:
        jexec = jenkins_simulate(jmodel, sc)
        gexec = [n for n in gitlab_simulate(gmodel, sc)
                 if next(j for j in gmodel["jobs"] if j["name"] == n)["stage"] != ".post"]
        jkeys = [_key(n) for n in jexec]
        gkeys = [_key(n) for n in gexec]
        if jkeys != gkeys:
            diverged += 1
            if witness is None:
                witness = {
                    "diff": {
                        "only_gitlab": [n for n in gexec if _key(n) not in jkeys],
                        "only_jenkins": [n for n in jexec if _key(n) not in gkeys],
                        "order_or_sequence_diff": _list_diff(jkeys, gkeys),
                    },
                    "gitlab_executes": gexec,
                    "jenkins_executes": jexec,
                    "scenario": dict(sc),
                    "scenario_independent": False,
                }
    if diverged:
        return _check_result(FAIL, "stage execution differs in %d of %d scenarios"
                             % (diverged, len(scenarios)), len(scenarios), diverged, witness)
    return _check_result(PASS, "same stages execute in all %d scenarios" % len(scenarios),
                         len(scenarios))


def check_always_cleanup(jmodel, gmodel, scenarios):
    post_jobs = [j for j in gmodel["jobs"] if j["stage"] == ".post"]
    if len(post_jobs) > 1:
        return _check_result(
            OUT_OF_MODEL, "more than one `.post` job: ordering of cleanup is not modelled",
            reasons=["`.post` jobs: %s" % sorted(j["name"] for j in post_jobs)])
    jcmds = [normalize_command(c["cmd"]) for c in (jmodel["post_always"] or [])]
    witness = None
    diverged = 0
    for sc in scenarios:
        names = gitlab_simulate(gmodel, sc)
        gjobs = [j for j in gmodel["jobs"] if j["stage"] == ".post" and j["name"] in names]
        gcmds = [normalize_command(s) for j in gjobs for s in j["script"]]
        if jcmds != gcmds:
            diverged += 1
            if witness is None:
                witness = {
                    "diff": {
                        "commands": _list_diff(jcmds, gcmds),
                        "jenkins_runs_post_always": bool(jcmds),
                        "gitlab_runs_post_job": bool(gjobs),
                    },
                    "gitlab_executes": [{"job": j["name"], "script": [normalize_command(s)
                                                                    for s in j["script"]]}
                                        for j in gjobs],
                    "jenkins_executes": jcmds,
                    "scenario": dict(sc),
                    "scenario_independent": False,
                }
    if diverged:
        return _check_result(FAIL, "cleanup differs in %d of %d scenarios"
                             % (diverged, len(scenarios)), len(scenarios), diverged, witness)
    return _check_result(PASS, "same cleanup commands run in all %d scenarios (ok and fail)"
                         % len(scenarios), len(scenarios))


def check_build_commands(jmodel, gmodel, scenarios):
    jstages = {_key(s["name"]) for s in jmodel["stages"]}
    gjobs = _gl_jobs_by_key(gmodel, False)
    unmatched_j = [s["name"] for s in jmodel["stages"] if _key(s["name"]) not in gjobs]
    unmatched_g = [k for k in gjobs if k not in jstages]
    if unmatched_j and unmatched_g:
        return _check_result(
            OUT_OF_MODEL, "cannot map stages to jobs by name (possible rename)",
            reasons=["Jenkins stages without same-named GitLab job: %s; GitLab jobs "
                     "without same-named Jenkins stage: %s"
                     % (sorted(unmatched_j), sorted(gjobs[k]["name"] for k in unmatched_g))])
    witness = None
    diverged = 0
    for stage in jmodel["stages"]:
        jcmds = [normalize_command(c["cmd"]) for c in stage["commands"]]
        job = gjobs.get(_key(stage["name"]))
        gcmds = [normalize_command(s) for s in job["script"]] if job else []
        if jcmds != gcmds:
            diverged += 1
            if witness is None:
                witness = {
                    "diff": {"commands": _list_diff(jcmds, gcmds)},
                    "gitlab_executes": {"job": job["name"] if job else None,
                                        "script": gcmds},
                    "jenkins_executes": {"stage": stage["name"], "sh": jcmds},
                    "scenario": dict(scenarios[0]),
                    "scenario_independent": True,
                }
    n = len(jmodel["stages"])
    if diverged:
        return _check_result(FAIL, "commands differ in %d of %d stages" % (diverged, n),
                             n, diverged, witness)
    return _check_result(PASS, "commands identical (whitespace-normalised) in all %d stages" % n, n)


def virtual_files(jmodel):
    created = derive_created_files([c["cmd"] for s in jmodel["stages"] for c in s["commands"]])
    return sorted(set(created) | set(EXTRA_PROBE_FILES))


def check_artifact_paths(jmodel, gmodel, scenarios):
    files = virtual_files(jmodel)
    jstage = {_key(s["name"]): s for s in jmodel["stages"]}
    gjobs = _gl_jobs_by_key(gmodel, True)
    keys = sorted(set(jstage) | set(gjobs))
    witness = None
    diverged = 0
    compared = 0
    for k in keys:
        st = jstage.get(k)
        job = gjobs.get(k)
        jpat = [p for a in (st["archives"] if st else []) for p in a["patterns"]]
        gpat = list(job["artifacts_paths"] or []) if job else []
        if not jpat and not gpat:
            continue
        compared += 1
        j_set = [f for f in files if jenkins_archives(jpat, f)]
        g_set = [f for f in files if gitlab_collects(gpat, f)]
        if j_set != g_set:
            diverged += 1
            if witness is None:
                witness = {
                    "diff": {
                        "extra_in_gitlab": [f for f in g_set if f not in j_set],
                        "uncovered_by_gitlab": [f for f in j_set if f not in g_set],
                    },
                    "gitlab_executes": {"archived_files": g_set, "job": job["name"] if job
                                        else None, "paths": gpat},
                    "jenkins_executes": {"archived_files": j_set, "patterns": jpat,
                                         "stage": st["name"] if st else None},
                    "scenario": dict(scenarios[0]),
                    "scenario_independent": True,
                    "virtual_files": files,
                }
    if diverged:
        return _check_result(FAIL, "artifact coverage differs for %d of %d artifact-bearing "
                             "stages/jobs" % (diverged, compared), compared, diverged, witness)
    return _check_result(PASS, "artifact coverage identical over %d virtual files for %d "
                         "artifact-bearing stages/jobs" % (len(files), compared), compared)


def confirmed_unsupported_mappings(jmodel):
    out = []
    for stage in jmodel["stages"]:
        for a in stage["archives"]:
            if a["fingerprint"]:
                out.append({
                    "id": "fingerprint",
                    "label": "CONFIRMED UNSUPPORTED MAPPING",
                    "reason": "Jenkins archiveArtifacts `fingerprint: true` has no "
                              "corresponding mechanism in the supported GitLab CI model; "
                              "it is NOT reported as equivalent.",
                    "verdict": "FAIL",
                    "witness": {
                        "gitlab": "no corresponding mechanism in the supported model",
                        "jenkins": {
                            "artifacts": a["raw"],
                            "fingerprint": True,
                            "line": a["line"],
                            "stage": stage["name"],
                        },
                    },
                })
    return out


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def _decode(data, side, unparsed):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        unparsed.append(_unparsed(side, 1, "input is not valid UTF-8", ALL_CHECKS, None))
        return None


def verify_bytes(jenkins_bytes, gitlab_bytes, scenarios=None):
    scenarios = scenarios if scenarios is not None else build_scenarios()
    unparsed = []
    empty_j = {"valid": False, "stages": [], "post_always": None}
    empty_g = {"valid": False, "stages": [], "jobs": []}

    jtext = _decode(jenkins_bytes, "jenkins", unparsed)
    if jtext is None:
        jmodel = empty_j
    else:
        jmodel, ju = parse_jenkinsfile(jtext)
        unparsed.extend(ju)
    gtext = _decode(gitlab_bytes, "gitlab", unparsed)
    if gtext is None:
        gmodel = empty_g
    else:
        gmodel, gu = parse_gitlab_ci(gtext)
        unparsed.extend(gu)

    unparsed.sort(key=lambda u: (u["side"], u["line"], u["reason"]))

    funcs = {
        "always_cleanup": check_always_cleanup,
        "artifact_paths": check_artifact_paths,
        "branch_gate": check_branch_gate,
        "build_commands": check_build_commands,
    }
    checks = {}
    for cid in CHECK_IDS:
        reasons = ["%s line %d: %s" % (u["side"], u["line"], u["reason"])
                   for u in unparsed if cid in u["affects"]]
        if reasons:
            checks[cid] = _check_result(
                OUT_OF_MODEL, "unparsed/unsupported syntax affects this check; no verdict "
                "is given", reasons=reasons)
        else:
            checks[cid] = funcs[cid](jmodel, gmodel, scenarios)

    mappings = confirmed_unsupported_mappings(jmodel) if jmodel["valid"] else []

    verdicts = [c["verdict"] for c in checks.values()]
    if FAIL in verdicts or mappings:
        overall = OVERALL_LOSS
    elif OUT_OF_MODEL in verdicts or unparsed:
        overall = OVERALL_INSUFFICIENT
    else:
        overall = OVERALL_EQUIVALENT

    return {
        "checks": checks,
        "confirmed_unsupported_mappings": mappings,
        "disclaimer": "Static model only. This result does NOT prove Jenkins runtime "
                      "equivalence; see unverified_runtime_semantics.",
        "extra_probe_files": list(EXTRA_PROBE_FILES),
        "inputs": {
            "gitlab_ci_sha256": sha256_hex(gitlab_bytes),
            "jenkinsfile_sha256": sha256_hex(jenkins_bytes),
        },
        "models": {"gitlab": gmodel, "jenkins": jmodel},
        "overall": overall,
        "scenario_set": {
            "branch": list(sorted(set(s["branch"] for s in scenarios),
                                  key=lambda b: [s["branch"] for s in scenarios].index(b))),
            "build_result": list(sorted(set(s["build_result"] for s in scenarios),
                                        key=lambda b: [s["build_result"] for s in scenarios].index(b))),
            "pipeline_source": list(sorted(set(s["pipeline_source"] for s in scenarios),
                                           key=lambda b: [s["pipeline_source"] for s in scenarios].index(b))),
            "scenarios": scenarios,
        },
        "unparsed": unparsed,
        "unverified_runtime_semantics": list(UNVERIFIED_RUNTIME_SEMANTICS),
        "verifier_version": VERIFIER_VERSION,
    }


def render_json(result):
    return json.dumps(result, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write("usage error: %s\n" % message)
        sys.exit(EXIT_USAGE)


def main(argv=None):
    parser = _Parser(
        prog="parity_verify.py",
        description="Static PARITY verifier: Jenkinsfile vs GitLab CI YAML "
                    "(does not prove runtime equivalence).",
    )
    parser.add_argument("--jenkinsfile", required=True, metavar="PATH")
    parser.add_argument("--gitlab-ci", required=True, metavar="PATH")
    parser.add_argument("--output", metavar="PATH", help="also write the JSON to this file")
    args = parser.parse_args(argv)
    if yaml is None:
        sys.stderr.write("error: PyYAML is not installed (pip install -r requirements.txt)\n")
        return EXIT_USAGE
    try:
        with open(args.jenkinsfile, "rb") as fh:
            jbytes = fh.read()
        with open(args.gitlab_ci, "rb") as fh:
            gbytes = fh.read()
    except OSError as exc:
        sys.stderr.write("error: cannot read input: %s\n" % exc.strerror)
        return EXIT_USAGE
    out = render_json(verify_bytes(jbytes, gbytes))
    try:
        if args.output:
            with open(args.output, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(out)
    except OSError as exc:
        sys.stderr.write("error: cannot write output: %s\n" % exc.strerror)
        return EXIT_USAGE
    sys.stdout.write(out)
    sys.stdout.flush()
    return EXIT_CODES[json.loads(out)["overall"]]


if __name__ == "__main__":
    sys.exit(main())
