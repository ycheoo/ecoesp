#!/usr/bin/env bash
# The release job's body: install the pinned toolchain, check the
# version, test, build, and pack out/ for ci/publish.sh. The release
# workflow and the rehearsal both run this file as root in the
# workflow's image, from the workspace root, with the same variables
# (GITHUB_WORKSPACE, GITHUB_REPOSITORY, GITHUB_REF_TYPE,
# GITHUB_REF_NAME, GITHUB_SHA, GITHUB_RUN_ID).
#
# Template file, listed with its checksum in ci/TEMPLATE: a change is
# made in the templates' source and vendored again, never here.
set -euo pipefail

cd "${GITHUB_WORKSPACE:?}"

# One fix landed by hand in three repos within twelve minutes while
# two tags died on the copies that lacked it (2026-10-13). A copy
# edited here instead of in the template stops the job.
sha256sum --quiet --check ci/TEMPLATE

# Fetched by checksum, so a rehearsal and CI run the same bytes. The
# stripped interpreter: the unstripped build makes binutils 2.38's
# readelf warn, and Nuitka 4.2.1 treats the warning as fatal.
readonly PYTHON_URL=https://github.com/astral-sh/python-build-standalone/releases/download/20261013/cpython-3.13.16%2B20261013-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz
readonly PYTHON_SHA256=d0080766b6b74aeb81e1949ef0d9a20e6c17f7b160fb295a69b936ccb17660be
readonly GH_URL=https://github.com/cli/cli/releases/download/v2.102.0/gh_2.102.0_linux_amd64.tar.gz
readonly GH_SHA256=bb766f710eef8ede859c18578c72c327597cd4c8a85b06001b1f3843c6019386
# A hung test must end as a stack trace in the log, not as the job
# timeout: a tag run once hung six hours in its tests with nothing in
# its log (2026-10).
readonly TEST_TIMEOUT=15m
# gh sends usage telemetry unless told not to, and even --version
# leaves a device id under HOME, which the runner cannot clear once
# root has written it (gh 2.102.0, measured 2026-10-21).
export GH_TELEMETRY=false

die() { printf 'ci/job.sh: %s\n' "$*" >&2; exit 1; }
phase() { printf '\n== %s\n' "$*"; }

phase "layout"
# Who the steps run as, and over what. A public repo's hosted runner
# is beyond the rehearsal's reach, and this part of its log is what
# gets compared with the rehearsal's; proxy variables by name only,
# since that log is public.
id
stat -c '%u:%g %a %n' "$PWD" "${HOME:?}"
proxies=
for v in HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy; do
    if [ -n "${!v:-}" ]; then proxies+=" $v"; fi
done
echo "proxy variables:${proxies:- none}"

phase "settings"
keys='BINARY|BUILD|APT_PACKAGES|PIP_REQUIREMENTS|VERSION_FILES|STAMP_FILE'
keys+='|VERSION_PROBE|SMOKE|UNIT_TESTS|PACKAGED_TESTS|PACKAGED_BINARY_ENV'
keys+='|TEST_PYTHONPATH|APT_MIRROR|PIP_INDEX_URL'
# An unset key turns its phase off, so a misspelt one would turn a
# test phase off without a word.
if grep -Evq "^(#.*|(${keys})=.*)?\$" ci/project.env; then
    die "ci/project.env: a line other than a comment or a known KEY="
fi
BINARY= BUILD= APT_PACKAGES= PIP_REQUIREMENTS= VERSION_FILES= STAMP_FILE=
VERSION_PROBE= SMOKE= UNIT_TESTS= PACKAGED_TESTS= PACKAGED_BINARY_ENV=
TEST_PYTHONPATH= APT_MIRROR= PIP_INDEX_URL=
. ci/project.env
[ -n "$BINARY" ] && [ -n "$BUILD" ] || die "BINARY and BUILD are required"

phase "version"
declared() {  # the version one file declares, on a single line
    local v
    case "$1" in
    *.toml) v="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$1")" ;;
    *.py) v="$(sed -n "s/^__version__ = [\"']\(.*\)[\"']\$/\1/p" "$1")" ;;
    *) die "$1: no known version syntax" ;;
    esac
    [ -n "$v" ] && [ "$(printf '%s\n' "$v" | wc -l)" -eq 1 ] ||
        die "$1 declares no single version"
    printf '%s\n' "$v"
}
name="${GITHUB_REF_NAME:?}"
base=
if [ "${GITHUB_REF_TYPE:?}" = tag ]; then
    [[ "$name" =~ ^v([0-9]+\.[0-9]+\.[0-9]+)(-rc[0-9]+)?$ ]] ||
        die "tag $name is not vX.Y.Z or vX.Y.Z-rcN"
    base="${BASH_REMATCH[1]}"
fi
version= first=
for f in $VERSION_FILES; do
    v="$(declared "$f")"
    [ -z "$version" ] || [ "$v" = "$version" ] ||
        die "$f says $v where $first says $version"
    version="$v" first="${first:-$f}"
done
# An rc and its final are built from one commit, so the source
# carries the final's number and only the tag adds -rcN.
if [ -n "$base" ] && [ -n "$VERSION_FILES" ] && [ "$version" != "$base" ]
then
    die "tag $name, but the source declares $version"
fi
if [ -n "$STAMP_FILE" ]; then
    [ -z "$VERSION_FILES" ] || die "set VERSION_FILES or STAMP_FILE"
    if [ -n "$base" ]; then
        sed -i "s/^__version__ = .*/__version__ = '$name'/" "$STAMP_FILE"
    fi
    version="$(declared "$STAMP_FILE")"
    # sed is silent when the line has changed shape, and the binary
    # would answer --version with its untagged value.
    [ -z "$base" ] || [ "$version" = "$name" ] ||
        die "$STAMP_FILE was not stamped with $name"
fi
[ -z "$VERSION_PROBE" ] || [ -n "$version" ] ||
    die "VERSION_PROBE needs VERSION_FILES or STAMP_FILE"
echo "ref $GITHUB_REF_TYPE $name, source version ${version:-none}"

phase "toolchain"
export DEBIAN_FRONTEND=noninteractive
# A mirror named in the settings replaces the image's default archives
# and is reached directly, outside any proxy the runner hands the job:
# a repo sets one where the default mirror is slow from its runner and
# a nearby mirror is fast without the proxy. Plain http suffices: apt
# checks the signed Release files, and https would need the
# ca-certificates this step installs.
apt=(apt-get)
if [ -n "$APT_MIRROR" ]; then
    [[ "$APT_MIRROR" =~ ^https?://[^#[:space:]]+/$ ]] ||
        die "APT_MIRROR must be an http(s) URL ending in /"
    sed -i -e "s#http://archive.ubuntu.com/ubuntu/#$APT_MIRROR#" \
        -e "s#http://security.ubuntu.com/ubuntu/#$APT_MIRROR#" \
        /etc/apt/sources.list
    # sed is silent when the file has changed shape, and apt would then
    # crawl to the default mirror, without a word.
    if grep -qE '^deb .*ubuntu\.com' /etc/apt/sources.list ||
        ! grep -q "^deb $APT_MIRROR" /etc/apt/sources.list; then
        die "/etc/apt/sources.list was not moved to $APT_MIRROR"
    fi
    apt=(env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY
         apt-get)
fi
if [ -n "$PIP_INDEX_URL" ]; then
    # Exported, so a build script's own pip, in its own venv, takes the
    # index as well; its host joins no_proxy, reached directly like the
    # apt mirror.
    export PIP_INDEX_URL
    index_host="${PIP_INDEX_URL#*://}"
    index_host="${index_host%%/*}"
    export no_proxy="${no_proxy:+$no_proxy,}$index_host"
    export NO_PROXY="$no_proxy"
fi
"${apt[@]}" update
"${apt[@]}" install -y --no-install-recommends ca-certificates curl \
    $APT_PACKAGES
downloads="$(mktemp -d)"
fetch() {  # URL SHA256 FILE
    # A stalled download ends within about a minute as curl's error
    # 28, not silently at the job timeout: a download can hang, and a
    # proxy that stops answering looks the same.
    curl --connect-timeout 30 --speed-limit 10240 --speed-time 60 \
        -fsSL -o "$3" "$1"
    printf '%s  %s\n' "$2" "$3" | sha256sum --quiet --check
}
fetch "$PYTHON_URL" "$PYTHON_SHA256" "$downloads/python.tar.gz"
tar -xzf "$downloads/python.tar.gz" -C /opt
fetch "$GH_URL" "$GH_SHA256" "$downloads/gh.tar.gz"
tar -xzf "$downloads/gh.tar.gz" -C /usr/local --strip-components=1 \
    --wildcards '*/bin/gh'
rm -rf "$downloads"
# Build scripts take the first python3 on PATH; the image has none
# of its own, so this is the one interpreter anything here sees.
export PATH="/opt/python/bin:$PATH"
# Caches go to the workspace's build/, which checkout empties before
# every job, not to HOME, where root's files outlive the job: a cache
# there would make CI warm where every rehearsal starts cold.
export XDG_CACHE_HOME="$PWD/build/ci-cache"
# Created here, so it is root's: pip run as root disables its cache
# when the nearest existing directory belongs to someone else, as the
# workspace does (the runner's user), and says so on every install.
mkdir -p "$XDG_CACHE_HOME"
for r in $PIP_REQUIREMENTS; do
    python -m pip install --disable-pip-version-check -r "$r"
done

tests() {
    env ${TEST_PYTHONPATH:+PYTHONPATH="$TEST_PYTHONPATH"} \
        timeout --signal=ABRT "$TEST_TIMEOUT" python -X faulthandler "$@"
}
if [ -n "$UNIT_TESTS" ]; then
    phase "unit tests"
    tests $UNIT_TESTS
fi

phase "build: $BUILD"
$BUILD
bin="dist/$BINARY"
[ -x "$bin" ] || die "$BUILD left no executable $bin"

phase "binary"
if [ -n "$SMOKE" ]; then "./$bin" $SMOKE; fi
if [ -n "$VERSION_PROBE" ]; then
    reported="$("./$bin" $VERSION_PROBE)"
    reported="${reported%%$'\n'*}"
    last="${reported##*[[:space:]]}"
    [ "${last#v}" = "${version#v}" ] ||
        die "$bin $VERSION_PROBE says '$reported', the source $version"
fi
if [ -e ci/hooks/smoke ]; then ci/hooks/smoke "$bin"; fi
if [ -n "$PACKAGED_TESTS" ]; then
    [ -n "$PACKAGED_BINARY_ENV" ] ||
        die "PACKAGED_TESTS needs PACKAGED_BINARY_ENV"
    phase "packaged tests"
    export "$PACKAGED_BINARY_ENV=$PWD/$bin"
    tests $PACKAGED_TESTS
fi

phase "out/"
rm -rf out
mkdir out
# A branch name may hold a slash; a tag cannot (the gate above).
asset="${BINARY}_${name//\//-}_linux_amd64.tar.gz"
# Fixed order, owner and mtime: the asset's checksum is a function
# of the binary alone.
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime=@0 \
    -C dist -cf - "$BINARY" | gzip -n > "out/$asset"
# Two small assets beside the tarball, never inside it: buildinfo
# carries the run id and the commit, and the tarball's checksum is a
# function of the binary alone.
(cd out && sha256sum -- *.tar.gz > checksums.txt)
{
    printf 'commit %s\nref %s\nrun %s\n' "${GITHUB_SHA:?}" "$name" \
        "${GITHUB_RUN_ID:?}"
    sed -n 's/^ *container: */image /p' .github/workflows/release.yml
    head -n 1 ci/TEMPLATE
    printf 'python %s sha256 %s\n' \
        "$(python -c 'import platform; print(platform.python_version())')" \
        "$PYTHON_SHA256"
    gh --version | sed -n 1p
    dpkg-query -W -f 'deb ${Package} ${Version}\n'
    python -m pip freeze --all | sed 's/^/pip /'
} > out/buildinfo.txt
cat out/checksums.txt
