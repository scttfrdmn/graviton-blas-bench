#!/usr/bin/env bash
# graviton-blas-bench — install Arm Performance Libraries as the reference arm.
#
# WHY THIS EXISTS AS A SCRIPT rather than a line in the README: ArmPL was absent from
# the first P2 pass because getting it was a manual, registration-gated step, and a
# manual step that is discovered at launch time is discovered on five hosts across
# three passes. The census recorded the absence honestly -- "ARMPL_DIR unset or not a
# directory" -- but an explained gap where the reference arm should be is still a gap
# in the campaign's headline framing, which is OpenBLAS against what the silicon can
# do. So the acquisition is now reproducible, verified, and pinned.
#
# THE DOWNLOAD IS PINNED BY CONTENT, NOT BY URL. Arm serves these from a CDN
# permalink that is stable by name and says nothing about what it returns. Five hosts
# built on different days off a name that silently changes contents is standing order
# 5's failure -- provenance that describes a library nobody has -- so the tarball is
# checked against a SHA-256 recorded here and the install aborts on a mismatch. The
# pinned digest below was verified by downloading the tarball independently and it
# agrees with the digest Spack publishes for the same version; two independent
# sources, which is the most that can be had for a vendor binary.
#
# THE EULA IS ACCEPTED BY A HUMAN, NOT BY THIS SCRIPT. ArmPL ships under a
# click-through end-user licence. `--accept` on the vendor installer is the documented
# non-interactive path and this script will pass it, but only after the operator has
# said so by setting GBB_ARMPL_ACCEPT_EULA=1. Automation must not agree to a licence
# on someone's behalf; the licence text is left on disk under license_terms/ and its
# path is printed.
#
# Usage:
#   GBB_ARMPL_ACCEPT_EULA=1 bash scripts/install-armpl.sh
#   export ARMPL_DIR="$(GBB_ARMPL_ACCEPT_EULA=1 bash scripts/install-armpl.sh --print-dir)"
#
# Then build-libs.sh picks it up from ARMPL_DIR and records the arm as measured
# rather than as an explained absence. scripts/workload.sh does both, and makes a
# missing ArmPL fatal on a P3 pass before any instance-hours are spent.
#
# GBB_ARMPL_MIRROR=s3://.../vendor (or a local directory) is checked before the CDN
# and populated from the first successful CDN fetch, so P3's fifteen passes read one
# set of bytes and the vendor CDN is off the critical path. See the MIRROR block.

set -euo pipefail

# Pinned. Bumping this is a campaign-visible change: it moves the reference arm, so
# every host and every pass must be rebuilt against the same version or the
# cross-host comparison is comparing two references. Both fields move together.
ARMPL_VERSION="${GBB_ARMPL_VERSION:-26.07}"
# One digest per package family, because the two tarballs are different files. Both
# were verified by independent download and both agree with Spack's published digests
# for 26.07. The campaign hosts are AL2023, so `rpm` is the path that matters; `deb`
# is here because the instrument-check boxes are Ubuntu and an installer that only
# works on the hosts you cannot test on is an installer you find out about at launch.
ARMPL_SHA256_RPM="896863e1c7be03f997c9cdfe3e8f236355111a80e4826dc53c749cb7a6fae614"
ARMPL_SHA256_DEB="6024f534554260939b369030bc4b6b47196f64bde840700b72c602e311aa7610"

# rpm for AL2023/RHEL, deb for Ubuntu/Debian. Chosen from the host rather than
# assumed: the wrong package family installs nothing and reports success.
PKG="${GBB_ARMPL_PKG:-}"
CACHE="${GBB_ARMPL_CACHE:-${TMPDIR:-/tmp}/gbb-armpl}"
INSTALL_TO="${GBB_ARMPL_PREFIX:-/opt/arm}"
PRINT_DIR=0
[ "${1:-}" = "--print-dir" ] && PRINT_DIR=1

# Everything conversational goes to stderr, unconditionally, so that --print-dir's
# stdout is exactly one path and safe to substitute into ARMPL_DIR=.
log() { printf '[gbb-armpl] %s\n' "$*" >&2; }
die() { printf '[gbb-armpl] FATAL: %s\n' "$*" >&2; exit 1; }

# ---- refuse to run where the result would be a lie -------------------------
case "$(uname -s)/$(uname -m)" in
  Linux/aarch64) ;;
  *) die "ArmPL is aarch64 Linux only; this is $(uname -s)/$(uname -m). Nothing installed." ;;
esac

if [ -z "$PKG" ]; then
  if command -v rpm >/dev/null 2>&1; then PKG=rpm
  elif command -v dpkg >/dev/null 2>&1; then PKG=deb
  else die "neither rpm nor dpkg found, so the package family cannot be chosen. Set GBB_ARMPL_PKG."; fi
fi
[ "$PKG" = rpm ] || [ "$PKG" = deb ] || die "GBB_ARMPL_PKG must be rpm or deb, got '$PKG'"

# GBB_ARMPL_SHA256 overrides, and is required if GBB_ARMPL_VERSION was overridden:
# the two digests above describe 26.07 and nothing else, so a version bump without a
# digest would abort on a mismatch that reads like a corrupt download rather than
# like a missing pin.
if [ -n "${GBB_ARMPL_SHA256:-}" ]; then
  ARMPL_SHA256="$GBB_ARMPL_SHA256"
elif [ "$ARMPL_VERSION" != "26.07" ]; then
  die "GBB_ARMPL_VERSION=$ARMPL_VERSION but the digests in this script are 26.07's.
     Verify the tarball's SHA-256 against a second source and pass it as
     GBB_ARMPL_SHA256, or pin it here. An unverified vendor download is not
     admissible provenance (standing order 5)."
elif [ "$PKG" = rpm ]; then
  ARMPL_SHA256="$ARMPL_SHA256_RPM"
else
  ARMPL_SHA256="$ARMPL_SHA256_DEB"
fi

if [ "${GBB_ARMPL_ACCEPT_EULA:-0}" != 1 ]; then
  die "ArmPL is under a click-through EULA and this script will not accept it for you.
     Read it -- the tarball carries it under license_terms/license_agreement.txt, and
     it is also at https://developer.arm.com/downloads/-/arm-performance-libraries --
     then re-run with GBB_ARMPL_ACCEPT_EULA=1."
fi

BASE="arm-performance-libraries_${ARMPL_VERSION}_${PKG}"
TARBALL="$CACHE/${BASE}_gcc.tar"
URL="https://developer.arm.com/-/cdn-downloads/permalink/Arm-Performance-Libraries/Version_${ARMPL_VERSION}/${BASE}_gcc.tar"

mkdir -p "$CACHE"

sha_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
  else die "no sha256sum and no shasum, so the download cannot be verified"; fi
}

# GBB_ARMPL_MIRROR: an s3:// prefix or a local directory holding the same tarball
# under the same basename. Tried before the CDN, and given no more trust than it --
# the digest check below is identical either way, so a poisoned mirror aborts the
# install exactly as a moved permalink would.
#
# Why it exists: P3 is fifteen passes (five hosts x three), and fifteen 1.0 GB pulls
# from a vendor CDN sit on the critical path of a spend-authorised sweep. The pin
# protects the campaign from the permalink changing mid-campaign, but it protects it
# by aborting a pass, and a pass that aborts on the download is instance-hours spent
# for nothing. Mirroring once means all fifteen passes read identical bytes and the
# CDN is off the critical path. Optional, because a single-host P2 does not need it.
MIRROR="${GBB_ARMPL_MIRROR:-}"
mirror_uri=""
[ -n "$MIRROR" ] && mirror_uri="${MIRROR%/}/${BASE}_gcc.tar"

fetch_from_mirror() {
  [ -n "$mirror_uri" ] || return 1
  case "$mirror_uri" in
    s3://*)
      command -v aws >/dev/null 2>&1 || { log "mirror is s3:// but no aws cli"; return 1; }
      log "trying mirror $mirror_uri"
      aws s3 cp ${GBB_AWS_REGION:+--region "$GBB_AWS_REGION"} --only-show-errors \
        "$mirror_uri" "$TARBALL.part" >/dev/null 2>&1 || { log "mirror miss"; return 1; }
      ;;
    *)
      [ -f "$mirror_uri" ] || { log "mirror miss ($mirror_uri)"; return 1; }
      log "trying mirror $mirror_uri"
      cp "$mirror_uri" "$TARBALL.part" || return 1
      ;;
  esac
  mv "$TARBALL.part" "$TARBALL"
  log "fetched from mirror"
}

# Re-download only if what is cached is not what is pinned. A cached file that fails
# the digest is deleted rather than reused: the common cause is a truncated earlier
# run, and the second most common is the permalink having moved under us, which is
# the case this pin exists to catch.
FETCHED_FROM_CDN=0
if [ -f "$TARBALL" ] && [ "$(sha_of "$TARBALL")" = "$ARMPL_SHA256" ]; then
  log "using cached $TARBALL (digest matches)"
else
  [ -f "$TARBALL" ] && { log "cached tarball fails the pinned digest; discarding it"; rm -f "$TARBALL"; }
  if ! fetch_from_mirror; then
    command -v curl >/dev/null 2>&1 || die "curl is required to fetch ArmPL"
    log "fetching $URL (~1.0 GB)"
    curl -fSL --retry 3 --retry-delay 5 -o "$TARBALL.part" "$URL" || die "download failed"
    mv "$TARBALL.part" "$TARBALL"
    FETCHED_FROM_CDN=1
  fi
fi

GOT="$(sha_of "$TARBALL")"
if [ "$GOT" != "$ARMPL_SHA256" ]; then
  die "SHA-256 mismatch on $TARBALL
     expected $ARMPL_SHA256
     got      $GOT
     The permalink is stable by name and not by content. Do NOT install this and do
     NOT relax the check: a reference arm nobody can reproduce is worse than no
     reference arm. If Arm has genuinely republished the version, verify the new
     digest against a second source and bump both fields at the top of this script."
fi
log "digest verified: $GOT"

# Populate the mirror only from a CDN fetch that passed the digest, and only
# upward: never overwrite a mirror entry with itself, and never write a file this
# run did not verify. Best effort -- a pass must not fail because the mirror is
# read-only, since the tarball it needs is already on disk.
if [ "$FETCHED_FROM_CDN" -eq 1 ] && [ -n "$mirror_uri" ]; then
  case "$mirror_uri" in
    s3://*)
      if aws s3 cp ${GBB_AWS_REGION:+--region "$GBB_AWS_REGION"} --only-show-errors \
           "$TARBALL" "$mirror_uri" >/dev/null 2>&1; then
        log "mirrored to $mirror_uri (subsequent passes will not touch the CDN)"
      else
        log "could not write the mirror at $mirror_uri -- continuing"
      fi
      ;;
    *)
      cp "$TARBALL" "$mirror_uri" 2>/dev/null \
        && log "mirrored to $mirror_uri" || log "could not write $mirror_uri -- continuing"
      ;;
  esac
fi

WORK="$CACHE/extract"
rm -rf "$WORK"; mkdir -p "$WORK"
tar xf "$TARBALL" -C "$WORK"
INSTALLER="$(find "$WORK" -maxdepth 2 -name "${BASE}.sh" -type f | head -1)"
[ -n "$INSTALLER" ] || die "no ${BASE}.sh inside the tarball; its layout has changed"
LICENCE="$(find "$WORK" -maxdepth 3 -name 'license_agreement.txt' -type f | head -1)"
[ -n "$LICENCE" ] && log "licence text: $LICENCE"

SUDO=""
if [ ! -w "$(dirname "$INSTALL_TO")" ] && [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || die "$INSTALL_TO is not writable and sudo is unavailable"
  SUDO="sudo"
fi

# --force because the rpm packages declare RHEL and AL2023 is not RHEL by name. The
# ISA is what matters and it is checked above; without --force the installer refuses
# on an OS string.
log "installing to $INSTALL_TO (--accept, per GBB_ARMPL_ACCEPT_EULA=1)"
chmod +x "$INSTALLER"
$SUDO "$INSTALLER" --accept --force --install-to "$INSTALL_TO" >"$CACHE/install.log" 2>&1 \
  || die "installer failed; see $CACHE/install.log"

# ---- ARMPL_DIR is discovered, not guessed ---------------------------------
# The installed directory carries the GCC version in its name (armpl_26.07_gcc-14.2),
# which is part of the provenance build-libs.sh records via basename. Guessing it
# would produce an ARMPL_DIR that does not exist, which the Makefile turns into a
# link error, or worse an ARMPL_DIR that exists and holds a different build.
ARMPL_DIR=""
for d in "$INSTALL_TO"/armpl_"${ARMPL_VERSION}"_gcc* "$INSTALL_TO"/armpl_"${ARMPL_VERSION}"*; do
  [ -f "$d/lib/libarmpl_mp.so" ] && { ARMPL_DIR="$d"; break; }
done
[ -n "$ARMPL_DIR" ] || die "installed, but no armpl_${ARMPL_VERSION}* under $INSTALL_TO contains
     lib/libarmpl_mp.so -- which is the library the harness links (see Makefile's
     armpl target). Not exporting an ARMPL_DIR that would fail at link time.
     Contents: $(ls -1 "$INSTALL_TO" 2>/dev/null | tr '\n' ' ')"

log "ARMPL_DIR=$ARMPL_DIR"
log "libarmpl_mp: $(ls -la "$ARMPL_DIR/lib/libarmpl_mp.so" | awk '{print $NF, $5}')"
if [ "$PRINT_DIR" -eq 1 ]; then
  printf '%s\n' "$ARMPL_DIR"
else
  log "next: export ARMPL_DIR=$ARMPL_DIR  then  bash scripts/build-libs.sh"
fi
