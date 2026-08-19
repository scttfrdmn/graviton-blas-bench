# graviton-blas-bench — build the harness once per BLAS implementation.
#
# The harness calls the Fortran BLAS ABI (dgemm_ etc.), which OpenBLAS, ArmPL
# and BLIS all export, so one source builds against all three. Each variant
# gets its own binary rather than LD_PRELOAD: preloading works, but it makes
# the build manifest ambiguous about what was actually measured, and this
# campaign lives or dies on provenance.
#
# -O2 not -O3, and no -march=native: the harness itself must not be the thing
# that differs between arms. Only the BLAS under test varies.

CC      ?= gcc
CFLAGS  ?= -O2 -g -Wall -Wextra -std=c11
LDLIBS  ?= -lm -lpthread

BIN     := bin
SRC     := src

# ---- OpenMP detection ----------------------------------------------------
# On the Graviton hosts this is gcc and -fopenmp always works. It is detected
# rather than assumed only so the harness still compiles for a local syntax
# check on macOS, where Apple clang needs libomp supplied separately.
#
# roofline.c guards every OpenMP use with #ifdef _OPENMP, so a build without it
# is valid -- it just cannot report peak_fma_allcore. That is a real loss of a
# measurement, so `make roofline` says so loudly rather than quietly producing a
# binary that emits fewer records than the analysis expects.
OMP_TEST := $(shell printf 'int main(void){return 0;}' > /tmp/gbb-omp-probe.c 2>/dev/null; \
              if $(CC) -fopenmp /tmp/gbb-omp-probe.c -o /tmp/gbb-omp-probe >/dev/null 2>&1; \
              then echo native; \
              elif [ -d /opt/homebrew/opt/libomp ] && \
                   $(CC) -Xpreprocessor -fopenmp -I/opt/homebrew/opt/libomp/include \
                     /tmp/gbb-omp-probe.c -o /tmp/gbb-omp-probe \
                     -L/opt/homebrew/opt/libomp/lib -lomp >/dev/null 2>&1; \
              then echo libomp; \
              else echo none; fi)

ifeq ($(OMP_TEST),native)
  OMPFLAGS := -fopenmp
else ifeq ($(OMP_TEST),libomp)
  OMPFLAGS := -Xpreprocessor -fopenmp -I/opt/homebrew/opt/libomp/include
  OMPLIBS  := -L/opt/homebrew/opt/libomp/lib -lomp
else
  OMPFLAGS :=
endif
OMPLIBS ?=

.PHONY: all clean openblas openblas-omp coreprobe armpl blis reference roofline dirs

all: dirs roofline

dirs:
	@mkdir -p $(BIN)

# ---- roofline probe: no BLAS dependency ---------------------------------
roofline: dirs
ifeq ($(OMP_TEST),none)
	@echo "WARNING: no OpenMP ($(CC) lacks -fopenmp and libomp was not found)."
	@echo "         Building serial roofline: peak_fma_allcore will NOT be emitted."
	@echo "         This is acceptable for a local syntax check only. Campaign"
	@echo "         hosts must build with OpenMP -- see standing order 5."
endif
	$(CC) $(CFLAGS) $(OMPFLAGS) $(SRC)/roofline.c -o $(BIN)/gbb-roofline $(LDLIBS) $(OMPLIBS)

# ---- one bench binary per library ---------------------------------------
# OPENBLAS_DIR must point at a prefix containing lib/libopenblas.so.
# Invoked once per TARGET= build by scripts/build-libs.sh, which passes
# VARIANT to keep the binaries distinct.
openblas: dirs
	@test -n "$(OPENBLAS_DIR)" || { echo "set OPENBLAS_DIR"; exit 1; }
	@test -n "$(VARIANT)"      || { echo "set VARIANT";      exit 1; }
	$(CC) $(CFLAGS) $(SRC)/bench.c -o $(BIN)/gbb-openblas-$(VARIANT) \
	  -L$(OPENBLAS_DIR)/lib -lopenblas \
	  -Wl,-rpath,$(OPENBLAS_DIR)/lib $(LDLIBS)

# The USE_OPENMP=1 OpenBLAS needs libgomp at link time. Note what is NOT here:
# -fopenmp. Adding a compiler flag for one arm would make the harness itself
# differ between arms, which standing order 6 forbids and gates/p0.sh checks.
# bench.c contains no OpenMP directives, so -lgomp alone is sufficient and
# leaves the compilation of bench.c byte-identical to every other arm.
#
# This arm exists to answer "how much of the ArmPL lead is the threading
# backend?" -- ArmPL is OpenMP and honours OMP_PROC_BIND; OpenBLAS as the
# wheels ship it is pthreads and does not. Measuring that is not the same as
# equalising it by rebuilding, which would change what is under test.
openblas-omp: dirs
	@test -n "$(OPENBLAS_DIR)" || { echo "set OPENBLAS_DIR"; exit 1; }
	@test -n "$(VARIANT)"      || { echo "set VARIANT";      exit 1; }
	$(CC) $(CFLAGS) $(SRC)/bench.c -o $(BIN)/gbb-openblas-$(VARIANT) \
	  -L$(OPENBLAS_DIR)/lib -lopenblas \
	  -Wl,-rpath,$(OPENBLAS_DIR)/lib $(LDLIBS) -lgomp

# Reports what OpenBLAS selected, per OPENBLAS_CORETYPE. See src/coreprobe.c:
# the coretype axis is a request until this confirms it was honoured.
coreprobe: dirs
	@test -n "$(OPENBLAS_DIR)" || { echo "set OPENBLAS_DIR"; exit 1; }
	@test -n "$(VARIANT)"      || { echo "set VARIANT";      exit 1; }
	$(CC) $(CFLAGS) $(SRC)/coreprobe.c -o $(BIN)/gbb-coreprobe-$(VARIANT) \
	  -L$(OPENBLAS_DIR)/lib -lopenblas \
	  -Wl,-rpath,$(OPENBLAS_DIR)/lib $(LDLIBS)

# ArmPL: link the OpenMP build (libarmpl_mp). Linking the serial libarmpl by
# mistake produces flat scaling that looks like a threading bug in ArmPL and
# is not; this is a documented foot-gun.
armpl: dirs
	@test -n "$(ARMPL_DIR)" || { echo "set ARMPL_DIR"; exit 1; }
	$(CC) $(CFLAGS) $(OMPFLAGS) $(SRC)/bench.c -o $(BIN)/gbb-armpl \
	  -L$(ARMPL_DIR)/lib -larmpl_mp \
	  -Wl,-rpath,$(ARMPL_DIR)/lib $(LDLIBS) $(OMPLIBS)

blis: dirs
	@test -n "$(BLIS_DIR)" || { echo "set BLIS_DIR"; exit 1; }
	$(CC) $(CFLAGS) $(SRC)/bench.c -o $(BIN)/gbb-blis \
	  -L$(BLIS_DIR)/lib -lblis \
	  -Wl,-rpath,$(BLIS_DIR)/lib $(LDLIBS)

# Reference netlib BLAS: the slow-but-correct control. Not for performance
# comparison -- it is there so a numerically wrong fast result is detectable.
reference: dirs
	$(CC) $(CFLAGS) $(SRC)/bench.c -o $(BIN)/gbb-reference -lblas $(LDLIBS)

clean:
	rm -rf $(BIN)
