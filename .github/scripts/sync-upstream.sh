#!/usr/bin/env bash
# Build fork releases on top of the latest upstream release, without pushing.
#
#   sync-upstream.sh plan      pick the upstream tag and next <tag>-mtls.<n>
#   sync-upstream.sh build     cherry-pick master's fork commits onto that tag
#   sync-upstream.sh publish   zip the integration and create the release
#
# master is never rewritten: fork commits are the ones on master that are not
# upstream's. Releases are tagged at the master commit they were built from and
# carry the integration as a zip asset for HACS (hacs.json zip_release).
#
# Values are appended to $GITHUB_OUTPUT (stdout when unset). Needs GH_TOKEN.
set -euo pipefail

UPSTREAM_REPO="${UPSTREAM_REPO:-blakeblackshear/frigate-hass-integration}"
OUTPUT="${GITHUB_OUTPUT:-/dev/stdout}"
ZIP_NAME=frigate.zip

output() { echo "$1=$2" >>"$OUTPUT"; }

fetch_upstream() {
  git remote get-url upstream >/dev/null 2>&1 ||
    git remote add upstream "https://github.com/${UPSTREAM_REPO}.git"
  git fetch --quiet upstream master --tags
}

plan() {
  fetch_upstream
  local upstream_tag source last n
  upstream_tag=$(gh api "repos/${UPSTREAM_REPO}/releases/latest" --jq .tag_name)
  source=$(git rev-parse HEAD)
  last=$(git tag --list "${upstream_tag}-mtls.*" --sort=-v:refname | head -n1)
  echo "Latest upstream release: ${upstream_tag}, master: ${source}"

  if [ -n "$last" ] && [ "$(git rev-parse "${last}^{commit}")" = "$source" ]; then
    echo "Already released as ${last}"
    output needed false
    return
  fi

  n=${last##*-mtls.}
  output needed true
  output source "$source"
  output upstream_tag "$upstream_tag"
  output tag "${upstream_tag}-mtls.$((${n:-0} + 1))"
}

build() {
  local upstream_tag="${UPSTREAM_TAG:?}" source="${SOURCE:?}" title
  local -a upstream_tags patches
  mapfile -t upstream_tags < <(git tag --list 'v*' | grep -v -- '-mtls\.')
  mapfile -t patches < <(git rev-list --reverse --no-merges "$source" \
    --not upstream/master "${upstream_tags[@]}")
  echo "Fork commits:"
  git log --no-walk=unsorted --format='  %h %s' "${patches[@]}"

  git checkout --quiet --detach "$upstream_tag"
  if ! git cherry-pick --empty=drop "${patches[@]}"; then
    git cherry-pick --abort
    title="Fork commits conflict with upstream ${upstream_tag}"
    if [ -z "$(gh issue list --state open --search "\"${title}\" in:title" \
      --json number --jq '.[].number')" ]; then
      gh issue create --title "$title" --body-file - <<BODY
Cherry-picking master's fork commits onto \`${upstream_tag}\` conflicts, so no
release was built. Rebase master and push; the next run releases it:

\`\`\`sh
git fetch upstream --tags
git rebase --onto ${upstream_tag} \$(git merge-base master ${upstream_tag}) master
git push --force-with-lease origin master
\`\`\`

Run: ${GITHUB_SERVER_URL:-}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}
BODY
    fi
    echo "::error::fork commits conflict with ${upstream_tag}"
    exit 1
  fi
}

publish() {
  local tag="${TAG:?}" upstream_tag="${UPSTREAM_TAG:?}" source="${SOURCE:?}"
  local manifest=custom_components/frigate/manifest.json workdir
  workdir=$(mktemp -d)

  cp -r custom_components/frigate "$workdir/frigate"
  find "$workdir/frigate" -name __pycache__ -type d -prune -exec rm -rf {} +
  jq --indent 4 --arg v "${tag#v}" '.version = $v' "$manifest" \
    >"$workdir/frigate/manifest.json"
  (cd "$workdir/frigate" && zip -qr "$workdir/$ZIP_NAME" .)

  {
    echo "Upstream [${upstream_tag}](https://github.com/${UPSTREAM_REPO}/releases/tag/${upstream_tag})"
    echo "with client certificate / forward-auth (Authentik) support."
    echo
    echo "Fork commits:"
    git log --reverse --format='- %s' "${upstream_tag}..HEAD"
  } | gh release create "$tag" "$workdir/$ZIP_NAME" --target "$source" \
    --latest --title "$tag" --notes-file -
}

"$1"
