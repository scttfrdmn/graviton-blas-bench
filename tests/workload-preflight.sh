#!/usr/bin/env bash
# graviton-blas-bench — scripts/workload.sh must refuse a P3 pass BEFORE it spends
# anything.
#
# The whole value of moving the ArmPL acquisition and the commit pin into the
# payload is that a pass which cannot produce an admissible dataset aborts in
# seconds rather than after ~40 minutes of build-libs.sh and hours of sweep. That
# property is a couple of `if` statements deep in a script nobody runs locally, so
# it is tested here: each case asserts both that the script exits non-zero AND that
# build-libs.sh was never reached.
#
# The second assertion is the load-bearing one. A version of workload.sh that
# aborted *after* the build would pass an exit-code-only test while costing exactly
# what this design exists to avoid, so the stubs record whether they ran.
#
# Needs no AWS, no instance and no ArmPL: build-libs.sh, run-matrix.sh and
# install-armpl.sh are all replaced by stubs that touch a marker file, and `git` is
# stubbed to report a fixed HEAD. What is under test is the ordering and the
# conditions, both of which are pure shell.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

pass=0; fail=0
chk() { if [ "$2" = "$3" ]; then pass=$((pass+1)); printf '  ok   %s\n' "$1";
        else fail=$((fail+1)); printf '  FAIL %s\n       want=%s\n       got =%s\n' "$1" "$3" "$2"; fi; }

HEAD_FAKE=1111111111111111111111111111111111111111

# One scratch world per case. WORK and ROOT both live in it, so nothing here can
# touch the real tree or the real /opt/gbb-work.
setup() {
  W="$(mktemp -d)"
  mkdir -p "$W/repo/scripts" "$W/work" "$W/bin"
  cp "$ROOT/scripts/workload.sh" "$W/repo/scripts/workload.sh"

  # Stubs that record the fact of having run. `armpl-ran` doubles as the install
  # outcome: --print-dir must put a path on stdout and nothing else, which is the
  # contract workload.sh consumes.
  cat > "$W/repo/scripts/build-libs.sh" <<EOF
#!/usr/bin/env bash
touch "$W/work/BUILD-RAN"
EOF
  cat > "$W/repo/scripts/run-matrix.sh" <<EOF
#!/usr/bin/env bash
touch "$W/work/SWEEP-RAN"
EOF
  cat > "$W/repo/scripts/diag-numa.sh" <<EOF
#!/usr/bin/env bash
touch "$W/work/DIAG-RAN"
EOF
  cat > "$W/repo/scripts/install-armpl.sh" <<EOF
#!/usr/bin/env bash
touch "$W/work/ARMPL-RAN"
[ "\${STUB_ARMPL_FAIL:-0}" = 1 ] && { echo "stub: refusing" >&2; exit 1; }
mkdir -p "$W/armpl_fake"
printf '%s\n' "$W/armpl_fake"
EOF
  chmod +x "$W/repo/scripts/"*.sh

  # git, hostname and aws stubbed. aws must exist and do nothing: ship_logs is
  # called on the abort path and a real aws would try to reach S3 from a test.
  cat > "$W/bin/git" <<EOF
#!/usr/bin/env bash
case " \$* " in
  *" rev-parse "*) echo $HEAD_FAKE ;;
  *" log "*)      echo "${HEAD_FAKE:0:7} stub commit" ;;
  *)              exit 0 ;;
esac
EOF
  printf '#!/usr/bin/env bash\necho testhost\n' > "$W/bin/hostname"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$W/bin/aws"
  chmod +x "$W/bin/"*
}
teardown() { rm -rf "$W"; }

run_case() {
  # run_case <extra env assignments...>
  # GBB_COMPLETE_MARKER is redirected into the scratch dir. Not cosmetic: touching
  # the real /tmp/SPAWN_COMPLETE is what tells spawn's on-complete hook the pass is
  # over, so a test that used the default path would terminate any instance it was
  # run on -- including one mid-sweep.
  ( cd "$W/repo" && env PATH="$W/bin:$PATH" GBB_WORK="$W/work" GBB_ROOT="$W/repo" \
      GBB_S3_URI="s3://example/gbb" GBB_AWS_REGION=us-east-1 JOBS=1 \
      GBB_COMPLETE_MARKER="$W/work/COMPLETE" \
      "$@" bash scripts/workload.sh ) >"$W/out" 2>&1
  echo $?
}

printf '=== workload.sh preflight ===\n'

# ---- 1. p3 with no commit pin: refuse, and spend nothing ------------------
# "It was main at the time" is not a pin, and three passes off a moving main are
# three different harnesses with nothing in the dataset saying so.
setup
rc="$(run_case GBB_PHASE=p3 GBB_ARMPL_ACCEPT_EULA=1)"
chk "p3 without GBB_EXPECT_HEAD exits non-zero" "$([ "$rc" != 0 ] && echo yes || echo no)" yes
chk "p3 without GBB_EXPECT_HEAD did not build"  "$([ -f "$W/work/BUILD-RAN" ] && echo ran || echo no)" no
chk "p3 without GBB_EXPECT_HEAD did not sweep"  "$([ -f "$W/work/SWEEP-RAN" ] && echo ran || echo no)" no
chk "abort message names the pin" \
  "$(grep -q "GBB_EXPECT_HEAD" "$W/out" && echo yes || echo no)" yes
teardown

# ---- 2. commit pin mismatch: refuse ---------------------------------------
setup
rc="$(run_case GBB_PHASE=p2 GBB_EXPECT_HEAD=deadbeefdeadbeef)"
chk "HEAD mismatch exits non-zero" "$([ "$rc" != 0 ] && echo yes || echo no)" yes
chk "HEAD mismatch did not build"  "$([ -f "$W/work/BUILD-RAN" ] && echo ran || echo no)" no
teardown

# ---- 3. p3 with the EULA not accepted: refuse before the build ------------
# The failure mode this prevents is a six-hour P3 pass whose reference arm is an
# explained absence -- admissible for P2, not for P3.
setup
rc="$(run_case GBB_PHASE=p3 GBB_EXPECT_HEAD="$HEAD_FAKE")"
chk "p3 without the EULA exits non-zero" "$([ "$rc" != 0 ] && echo yes || echo no)" yes
chk "p3 without the EULA did not install ArmPL" \
  "$([ -f "$W/work/ARMPL-RAN" ] && echo ran || echo no)" no
chk "p3 without the EULA did not build" "$([ -f "$W/work/BUILD-RAN" ] && echo ran || echo no)" no
chk "p3 without the EULA did not sweep" "$([ -f "$W/work/SWEEP-RAN" ] && echo ran || echo no)" no
teardown

# ---- 4. p3 where the ArmPL install fails: refuse before the build ---------
setup
rc="$(run_case GBB_PHASE=p3 GBB_EXPECT_HEAD="$HEAD_FAKE" GBB_ARMPL_ACCEPT_EULA=1 STUB_ARMPL_FAIL=1)"
chk "p3 with a failed ArmPL install exits non-zero" "$([ "$rc" != 0 ] && echo yes || echo no)" yes
chk "p3 with a failed ArmPL install tried"  "$([ -f "$W/work/ARMPL-RAN" ] && echo ran || echo no)" ran
chk "p3 with a failed ArmPL install did not build" "$([ -f "$W/work/BUILD-RAN" ] && echo ran || echo no)" no
teardown

# ---- 5. p2 with no ArmPL: PROCEED, recording an explained absence ---------
# The mirror image of case 3, and the reason PHASE exists. P2 pass 1 ran without
# ArmPL on purpose; a workload that refused here would have refused the pass whose
# whole job was to price the campaign.
setup
rc="$(run_case GBB_PHASE=p2)"
chk "p2 without ArmPL exits zero"        "$rc" 0
chk "p2 without ArmPL built"             "$([ -f "$W/work/BUILD-RAN" ] && echo ran || echo no)" ran
chk "p2 without ArmPL swept"             "$([ -f "$W/work/SWEEP-RAN" ] && echo ran || echo no)" ran
chk "p2 without ArmPL records the status" "$(cat "$W/work/armpl-status" 2>/dev/null)" eula_not_accepted
teardown

# ---- 6. p3 fully satisfied: PROCEED, and ARMPL_DIR reaches build-libs -----
# Guards the other direction: a preflight that rejected a good pass would be worse
# than no preflight, because it would look like a capacity problem.
setup
rc="$(run_case GBB_PHASE=p3 GBB_EXPECT_HEAD="$HEAD_FAKE" GBB_ARMPL_ACCEPT_EULA=1)"
chk "p3 satisfied exits zero"     "$rc" 0
chk "p3 satisfied installed ArmPL" "$([ -f "$W/work/ARMPL-RAN" ] && echo ran || echo no)" ran
chk "p3 satisfied built"           "$([ -f "$W/work/BUILD-RAN" ] && echo ran || echo no)" ran
chk "p3 satisfied swept"           "$([ -f "$W/work/SWEEP-RAN" ] && echo ran || echo no)" ran
chk "p3 satisfied records the status" "$(cat "$W/work/armpl-status" 2>/dev/null)" installed
chk "completion signalled" "$([ -f "$W/work/COMPLETE" ] && echo yes || echo no)" yes
teardown

# ---- 7. GBB_WORKLOAD=diag-numa: the diagnostic runs and the sweep does NOT --
# The failure this guards is the expensive one in the other direction: a diagnostic
# launch that also ran the sweep would charge ~6 hours for an hour's question, and
# would do it silently, since both produce records and both ship.
setup
rc="$(run_case GBB_PHASE=p2 GBB_WORKLOAD=diag-numa)"
chk "diag-numa exits zero"          "$rc" 0
chk "diag-numa built"               "$([ -f "$W/work/BUILD-RAN" ] && echo ran || echo no)" ran
chk "diag-numa ran the diagnostic"  "$([ -f "$W/work/DIAG-RAN" ] && echo ran || echo no)" ran
chk "diag-numa did NOT sweep"       "$([ -f "$W/work/SWEEP-RAN" ] && echo ran || echo no)" no
teardown

# ---- 8. an unknown GBB_WORKLOAD: refuse, and spend nothing ------------------
# Two properties, and the second is the one worth the test: it must not default to
# the sweep, and it must be caught BEFORE the build. A name validated at the
# dispatch point instead would exit non-zero having already paid for build-libs.sh,
# which passes an exit-code-only test while costing what this file exists to avoid.
setup
rc="$(run_case GBB_PHASE=p2 GBB_WORKLOAD=diag-nuna)"
chk "unknown workload exits non-zero" "$([ "$rc" != 0 ] && echo yes || echo no)" yes
chk "unknown workload did not build"  "$([ -f "$W/work/BUILD-RAN" ] && echo ran || echo no)" no
chk "unknown workload did not sweep"  "$([ -f "$W/work/SWEEP-RAN" ] && echo ran || echo no)" no
chk "unknown workload did not run the diagnostic" \
  "$([ -f "$W/work/DIAG-RAN" ] && echo ran || echo no)" no
chk "abort message names the accepted values" \
  "$(grep -q "sweep, diag-numa" "$W/out" && echo yes || echo no)" yes
teardown

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
