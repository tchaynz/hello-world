# CLAUDE.md

This file provides guidance for AI assistants working in this repository.

## Repository Overview

**hello-world** is a personal general-purpose repository owned by `tchaynz`. It serves as a catch-all storage location for files, images, videos, and other assets. It is not a software project with a build system, test suite, or application framework.

## Repository Structure

```
hello-world/
└── README.md   # Brief description of the repository's purpose
```

The repository is intentionally minimal. There are no source code files, dependencies, build scripts, or configuration files at this time.

## Git Workflow

### Branches

- `master` — the primary branch; contains stable/accepted content
- Feature and AI-assisted branches follow the pattern `<prefix>/<description>-<id>` (e.g., `claude/add-claude-documentation-2sUmR`)

### Commit History

| Commit | Description |
|--------|-------------|
| `fcc21f7` | Merge pull request #1 from tchaynz/readme-edits |
| `e90b74a` | Update README.md |
| `906f37b` | Initial commit |

### Conventions

- Commit messages should be short and descriptive in the imperative mood (e.g., "Add image assets", "Update README").
- Changes should be made on a feature branch and merged into `master` via a pull request.
- Do not push directly to `master` without a pull request unless the change is trivial (e.g., a typo fix in README).

## Working in This Repository

Since this is a general-purpose file storage repository rather than a software project:

- **No build/test commands exist.** There is no `npm install`, `make`, `pytest`, or equivalent.
- **No linter or formatter is configured.** If code files are added in the future, tooling should be set up at that time and documented here.
- **File additions are the primary contribution type.** When adding new files, place them in logically named subdirectories (e.g., `images/`, `videos/`, `docs/`) and update the README or this file if the structure changes significantly.

## Updating This File

If the repository evolves (e.g., a programming project or structured assets collection is added), update CLAUDE.md to reflect:

1. The new directory structure
2. Any build, test, or lint commands
3. Language/framework conventions
4. Any contribution or review guidelines
