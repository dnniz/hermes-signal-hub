#!/usr/bin/env bash
# Publish hermes-signal-hub to GitHub.
#
# Refuses to publish a dirty tree. The whole point of a publish script is that
# what is on GitHub is exactly what passed the test suite, and a stray edit that
# never made it into a commit would quietly break that guarantee.
#
# Credentials come from the git credential helper configured in
# ~/.gitconfig, which reads the PAT from the profile .env at call time. Nothing
# is stored in ~/.git-credentials and the token never appears in argv.
#
#   git config --global credential.helper '/home/hermes/.hermes/bin/git-credential-env'
#
# Usage:
#   ./scripts/publish.sh            # publish current main
#   ./scripts/publish.sh --check    # report what would be pushed, push nothing
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE="${SIGNALHUB_REMOTE:-origin}"
BRANCH="${SIGNALHUB_BRANCH:-main}"
CHECK_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

cd "$REPO_DIR"

# Never publish an uncommitted change: the tree on GitHub must equal the tree
# that was tested.
if ! git diff --quiet HEAD -- 2>/dev/null || ! git diff --cached --quiet -- 2>/dev/null; then
  echo "refusing to publish: working tree has uncommitted changes" >&2
  git status --short >&2
  echo >&2
  echo "commit them first, or stash them:" >&2
  echo "  git add -A && git commit" >&2
  exit 1
fi

if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
  echo "refusing to publish: no commits yet" >&2
  exit 1
fi

# Untracked files are not part of HEAD and are not caught by the diff above.
if [ -n "$(git ls-files --others --exclude-standard)" ]; then
  echo "refusing to publish: untracked files present" >&2
  git status --short --untracked-files=all >&2
  echo >&2
  echo "add them to git or ignore them in .gitignore, then re-run" >&2
  exit 1
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
  echo "--check: nothing pushed. pending changes:"
  git log --oneline "${REMOTE}/${BRANCH}..HEAD" 2>/dev/null || echo "  (no upstream ref yet)"
  exit 0
fi

# GIT_TERMINAL_PROMPT=0 turns a credential problem into an immediate error
# instead of an interactive prompt that would hang an unattended cron run.
echo "pushing $(git rev-parse --short HEAD) to ${REMOTE}/${BRANCH}..."
GIT_TERMINAL_PROMPT=0 git push --set-upstream "$REMOTE" "$BRANCH"

echo
echo "published. verify with:"
echo "  gh api repos/dnniz/hermes-signal-hub/commits/${BRANCH} --jq '.sha'"
