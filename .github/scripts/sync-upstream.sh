#!/usr/bin/env bash
# Rebase master onto the latest upstream release and decide whether to release.
#
#   sync-upstream.sh rebase    rebase HEAD onto the newest upstream release tag
#   sync-upstream.sh plan      work out the next <upstream tag>-mtls.<n> tag
#   sync-upstream.sh publish   tag HEAD + manifest version bump, create release
#
# Values are appended to $GITHUB_OUTPUT (stdout when unset). Needs GH_TOKEN.
set -euo pipefail

UPSTREAM_REPO="${UPSTREAM_REPO:-blakeblackshear/frigate-hass-integration}"
OUTPUT="${GITHUB_OUTPUT:-/dev/stdout}"

output() { echo "$1=$2" >>"$OUTPUT"; }

rebase() {
  git remote get-url upstream >/dev/null 2>&1 ||
    git remote add upstream "https://github.com/${UPSTREAM_REPO}.git"
  git fetch --quiet upstream --tags

  local upstream_tag old_base title
  upstream_tag=$(gh api "repos/${UPSTREAM_REPO}/releases/latest" --jq .tag_name)
  echo "Latest upstream release: ${upstream_tag}"
  output before "$(git rev-parse HEAD)"

  if git merge-base --is-ancestor "${upstream_tag}" HEAD; then
    echo "HEAD already contains ${upstream_tag}"
    output rebased false
    return
  fi

  old_base=$(git merge-base HEAD "${upstream_tag}")
  if ! git rebase --onto "${upstream_tag}" "${old_base}"; then
    git rebase --abort
    title="Patches conflict with upstream ${upstream_tag}"
    if [ -z "$(gh issue list --state open --search "\"${title}\" in:title" \
      --json number --jq '.[].number')" ]; then
      gh issue create --title "${title}" --body-file - <<BODY
The rebase of master onto \`${upstream_tag}\` hit conflicts. Rebase manually:

\`\`\`sh
git fetch upstream --tags
git rebase --onto ${upstream_tag} ${old_base} master
git push --force-with-lease origin master
\`\`\`

Run: ${GITHUB_SERVER_URL:-}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}
BODY
    fi
    echo "::error::rebase onto ${upstream_tag} conflicts"
    exit 1
  fi
  output rebased true
}

plan() {
  local base_tag last n
  base_tag=$(git describe --tags --abbrev=0 --match 'v[0-9]*' \
    --exclude '*-mtls.*' HEAD)
  last=$(git tag --list "${base_tag}-mtls.*" --sort=-v:refname | head -n1)

  if [ -n "$last" ] && [ "$(git rev-parse "${last}^{commit}^")" = "$(git rev-parse HEAD)" ]; then
    echo "HEAD is already released as ${last}"
    output needed false
    return
  fi

  n=${last##*-mtls.}
  output needed true
  output base_tag "${base_tag}"
  output tag "${base_tag}-mtls.$((${n:-0} + 1))"
}

publish() {
  local tag="${TAG:?}" base_tag="${BASE_TAG:?}" manifest
  manifest=custom_components/frigate/manifest.json
  jq --indent 4 --arg v "${tag#v}" '.version = $v' "$manifest" >"$manifest.new"
  mv "$manifest.new" "$manifest"

  git commit --quiet -m "Release ${tag}" -- "$manifest"
  git tag -a "${tag}" -m "${tag}"
  git push origin "refs/tags/${tag}"

  {
    echo "Upstream [${base_tag}](https://github.com/${UPSTREAM_REPO}/releases/tag/${base_tag})"
    echo "with client certificate / forward-auth (Authentik) support."
    echo
    echo "Fork patches:"
    git log --reverse --format='- %s' "${base_tag}..HEAD~1"
  } | gh release create "${tag}" --verify-tag --latest --title "${tag}" --notes-file -
}

"$1"
