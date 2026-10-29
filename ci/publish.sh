#!/usr/bin/env bash
# Publish out/ as the GitHub release of the tag this run is for, or,
# with --dry-run, run the same gh command up to its first request,
# where a proxy that refuses every connection stops it. Runs after
# ci/job.sh, from the workspace root: the release workflow's last
# step, and the rehearsal's (always --dry-run there).
#
# Template file, listed with its checksum in ci/TEMPLATE: a change is
# made in the templates' source and vendored again, never here.
set -euo pipefail

dry_run=false
case "${1:-}" in
--dry-run) dry_run=true ;;
"") ;;
*) echo "usage: ci/publish.sh [--dry-run]" >&2; exit 2 ;;
esac

cd "${GITHUB_WORKSPACE:?}"
tag="${GITHUB_REF_NAME:?}"
export GH_TELEMETRY=false   # the reason is in ci/job.sh
# An rc never becomes "latest", so install blocks and consumers that
# follow latest keep the last final release.
prerelease=()
if [[ "$tag" == *-rc* ]]; then prerelease=(--prerelease); fi
# The release notes are the tagged commit's message, subject and body,
# as the release page used to show for a lightweight tag with no
# notes. The workspace has no .git, so the message comes from the
# API; the dry run, which reaches no API, writes the tag instead.
notes="$(mktemp)"
trap 'rm -f "$notes"' EXIT
if "$dry_run"; then
    printf '%s\n' "$tag" > "$notes"
else
    gh api "repos/${GITHUB_REPOSITORY:?}/commits/${GITHUB_SHA:?}" \
        --jq .commit.message > "$notes"
    [ -s "$notes" ] || { echo "ci/publish.sh: no commit message" >&2; exit 1; }
fi
# --repo: the workspace has no .git, so gh cannot resolve the
# repository from git remotes. --verify-tag: refuse rather than create
# a missing tag at the default branch's head.
cmd=(gh release create "$tag" out/*.tar.gz out/checksums.txt
     out/buildinfo.txt --repo "${GITHUB_REPOSITORY:?}" --verify-tag
     --title "$tag" --notes-file "$notes" "${prerelease[@]}")
if ! "$dry_run"; then
    "${cmd[@]}"
    exit
fi

# Tag runs used to die inside gh before its first request, in a git
# call that no run without a tag reached (2026-10-13). gh parses its
# flags, reads the assets and the notes and resolves the repository
# before that request, so a dead proxy and a dummy token stop it
# exactly there, and any other ending fails the dry run.
printf 'dry run:'
printf ' %q' "${cmd[@]}"
printf '\n'
dead=http://127.0.0.1:9
status=0
out="$(GH_TOKEN=dry-run HTTPS_PROXY="$dead" https_proxy="$dead" \
    "${cmd[@]}" 2>&1)" || status=$?
printf '%s\n' "$out"
if [ "$status" -eq 0 ] ||
    [[ "$out" != *"proxyconnect tcp: dial tcp 127.0.0.1:9"* ]]; then
    echo "ci/publish.sh: gh did not stop at its first request" >&2
    exit 1
fi
echo "dry run: gh got as far as its first request"
