# Contributing to nexdash

Thank you for helping.

## How to contribute

1. Fork the repository and clone your fork.
2. Create a branch from `develop` (`feat/short-name`, `fix/short-name`, `docs/short-name`).
3. Commit small changes with conventional commit messages in English.
4. Open a pull request against `develop`, never against `main`, and fill in the template.
5. Continuous integration must pass and the maintainer must approve
   (`.github/CODEOWNERS`). Reviews may be assisted by an AI reviewer; the final
   approval is always human.
6. Pull requests are squash-merged into `develop`. The maintainer releases
   `develop` into `main` through a release pull request.

Branches `main` and `develop` are protected: no direct pushes, no force pushes and
no deletion. Pull requests from forks run CI only after the maintainer approves
the workflow run.

Security issues: see `SECURITY.md`. Conduct: see `CODE_OF_CONDUCT.md`.
