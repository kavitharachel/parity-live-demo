# Provenance of the REAL fixtures

These two files are **real** (not synthetic) files copied byte-for-byte from git
history of the project
`gitlab-ai-hackathon/transcend-october-2026/43361357/showcase`
(https://gitlab.com/gitlab-ai-hackathon/transcend-october-2026/43361357/showcase).

| Fixture file                       | Source path       | Commit                                     | Branch                                         | Size      |
|------------------------------------|-------------------|--------------------------------------------|------------------------------------------------|-----------|
| `Jenkinsfile`                      | `Jenkinsfile`     | `b4f460986be46ba57339a9e1a7b2c1fc9c9e2eba` | `parity/fixture-1`                             | 477 bytes |
| `gitlab-ci.converted.yml`          | `.gitlab-ci.yml`  | `942c801a60f1077677d5a083a8df81dafbb12a3b` | `duo/feature/convert-jenkins-to-gitlab-ci` (MR !1 source) | 381 bytes |

* `gitlab-ci.converted.yml` is the `.gitlab-ci.yml` produced by the
  Jenkins -> GitLab CI conversion in MR !1. It is stored under a different file
  name on purpose so that no file named `.gitlab-ci.yml` exists in this branch
  (which would trigger a pipeline).
* Neither file has been edited. They were extracted with
  `git show <commit>:<path>`.

Everything under `tests/fixtures/synthetic/` is, by contrast, **synthetic** and
is labeled `SYNTHETIC CALIBRATION FIXTURE`.
