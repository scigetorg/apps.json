#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "Usage: $0 <domain> <image_builddate>" >&2
    exit 2
fi

DOMAIN="$1"
IMAGE_BUILD="$2"
if [[ ! "$DOMAIN" =~ ^[a-z][a-z0-9]*(-[a-z0-9]+)*$ ]] ||
   [[ ! "$IMAGE_BUILD" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*_[0-9]{8}$ ]]; then
    echo "Invalid domain or image/build ID" >&2
    exit 2
fi
if [[ "$DOMAIN" != "neuroimaging" ]]; then
    echo "This Stratum 0 sync currently manages only the neuroimaging domain" >&2
    exit 2
fi

CATALOG_REMOTE="${CATALOG_REMOTE:-git@github.com:NeuroDesk/apps.json.git}"
NEUROCOMMAND_ROOT="$HOME/neurocommand"
CVMFS_CONTAINERS_ROOT="${CVMFS_CONTAINERS_ROOT:-/cvmfs/neurodesk.ardc.edu.au/containers}"
if ! command -v flock >/dev/null ||
   [[ ! -f "$NEUROCOMMAND_ROOT/cvmfs/download_log_artifact.py" ]]; then
    echo "The CVMFS sync needs flock and its published-log downloader" >&2
    exit 2
fi
if ! grep -Fq 'flock -w "$LOCK_WAIT" 9' "$NEUROCOMMAND_ROOT/cvmfs/sync_containers_to_cvmfs.sh"; then
    echo "Deploy the atomic-lock neurocommand CVMFS sync before retiring an image" >&2
    exit 2
fi
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/retire-cvmfs.XXXXXX")"
trap 'rm -rf -- "$WORK_DIR"' EXIT

# Reapply the change from a fresh checkout if another catalog commit wins the push.
for attempt in 1 2 3; do
    git clone --quiet --branch main --single-branch "$CATALOG_REMOTE" "$WORK_DIR/catalog"
    cd "$WORK_DIR/catalog"
    npm ci --silent
    SOURCE_REPOSITORY="$(node scripts/workflow.mjs source-repository "$DOMAIN")"
    SOURCE_COMMIT="$(node -e 'const fs=require("fs"); const d=process.argv[1]; process.stdout.write(JSON.parse(fs.readFileSync(`${d}/manifest.json`)).source_commit)' "$DOMAIN")"
    if [[ ! "$SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
        echo "Invalid source commit in $DOMAIN/manifest.json" >&2
        exit 2
    fi
    mkdir -p ".catalog-sources"
    git clone --quiet --depth=1 --filter=blob:none "https://github.com/$SOURCE_REPOSITORY.git" ".catalog-sources/$DOMAIN"
    if ! git -C ".catalog-sources/$DOMAIN" cat-file -e "$SOURCE_COMMIT^{commit}" 2>/dev/null; then
        git -C ".catalog-sources/$DOMAIN" fetch --quiet --depth=1 origin "$SOURCE_COMMIT"
    fi
    git -C ".catalog-sources/$DOMAIN" checkout --quiet --detach "$SOURCE_COMMIT"

    node scripts/retire.mjs "$DOMAIN" "$IMAGE_BUILD"
    git add -- "$DOMAIN/apps.json" "$DOMAIN/logs.txt" "$DOMAIN/manifest.json" "$DOMAIN/retired-images.json"
    if git diff --cached --quiet; then
        echo "Catalog already records retirement of $IMAGE_BUILD"
        break
    fi
    git -c user.name='cvmfs-retirement' -c user.email='cvmfs-retirement@users.noreply.github.com' \
        commit --quiet -m "Retire $IMAGE_BUILD from $DOMAIN CVMFS"
    if git push origin HEAD:main; then
        break
    fi
    if [[ "$attempt" -eq 3 ]]; then
        echo "Could not push retirement after three attempts; CVMFS was not changed" >&2
        exit 1
    fi
    echo "Catalog changed during retirement; retrying from current main" >&2
    cd "$WORK_DIR"
    rm -rf -- "$WORK_DIR/catalog"
done

# The CVMFS sync fetches GitHub's published log. Wait until that API serves the
# retirement before allowing any local deletion.
PUBLISHED_LOG="$WORK_DIR/published-logs.txt"
READY=0
for attempt in $(seq 1 30); do
    if python3 "$NEUROCOMMAND_ROOT/cvmfs/download_log_artifact.py" \
        --domain "$DOMAIN" --output "$PUBLISHED_LOG" &&
       ! awk -v image="$IMAGE_BUILD" '$1 == image { found=1 } END { exit !found }' "$PUBLISHED_LOG"; then
        READY=1
        break
    fi
    echo "Waiting for GitHub to serve the retired CVMFS log ($attempt/30)" >&2
    sleep 10
done
if [[ "$READY" -ne 1 ]]; then
    echo "GitHub did not serve the retired log; CVMFS was not changed" >&2
    exit 1
fi

CVMFS_SYNC_LOCK_WAIT_SECONDS=1800 bash "$NEUROCOMMAND_ROOT/cvmfs/sync_containers_to_cvmfs.sh"
if [[ -e "$CVMFS_CONTAINERS_ROOT/$IMAGE_BUILD/$IMAGE_BUILD.simg" ]]; then
    echo "CVMFS sync returned but $IMAGE_BUILD.simg still exists" >&2
    exit 1
fi
echo "Retired $IMAGE_BUILD from $DOMAIN catalog and CVMFS"
