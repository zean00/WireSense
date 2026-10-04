# wiresense service control
#
#   make start     launch Phonon (:8010), llama-server (:8998), web (:8996),
#                  omdet detector sidecar (:8011)
#   make stop      stop the wiresense services (only ours; nothing else)
#   make restart   stop + start
#   make status    health of each service + GPU snapshot
#   make canary    GPU fp4 health check (docs/INCIDENT-2026-10-04-GPU.md)
#   make logs      tail the service logs
#   make help      this list (default)

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.ONESHELL:

WS_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

PORT_PHONON := 8010
PORT_LLAMA  := 8998
PORT_WEB    := 8996
PORT_OMDET  := 8011

LOG_LLAMA  := /tmp/llama_server_8998.log
LOG_PHONON := /tmp/phonon_8010.log
LOG_WEB    := /tmp/wiresense_web.log
LOG_OMDET  := /tmp/omdet_8011.log

PID_LLAMA  := /tmp/wiresense-llama.pid
PID_PHONON := /tmp/wiresense-phonon.pid
PID_WEB    := /tmp/wiresense-web.pid
PID_GUARD  := /tmp/wiresense-guard.pid
PID_OMDET  := /tmp/wiresense-omdet.pid

# VRAM placement guard — shifts llama-server allocations ~0.3 GB up to avoid
# the fp4 NaN fault (docs/INCIDENT-2026-10-04-GPU.md)
GUARD_GB    := 0.3
READY_GUARD := /tmp/wiresense_guard.ready
LOG_GUARD   := /tmp/wiresense_guard.log

LLAMA_BIN   := $(HOME)/.local/bin/llama-server
# Interim engine: Qwen3.5-4B UD-Q4_K_XL (unsloth). The NVFP4 quant corrupts on
# this GPU (docs/INCIDENT-2026-10-04-GPU.md, Revision 2); this quant verifies
# clean end-to-end and its embeddings sit at cos ~0.996 to the entry-U refs.
# Original 32K footprint fits again (2.6 GiB weights): 4096 ctx per slot x 8.
# NOTE: models/sig_embeddings.npy is re-recorded from the serving model
# (backups: *.q8_0.npy.bak, *.instruct-nvfp4.npy.bak) — decide needs
# self-consistent refs, originals don't survive the head. Any change to
# serving numerics (model, context, KV type) requires re-recording refs.
# Voice-loop budget (measured, docs in git history): prefill ~0.55 ms/tok
# linear, warm turn 66 ms, decode ~50 tok/s; thinking ON costs ~5.7 s to
# first content token — the voice path pre-closes thinking (EMPTY_THINK).
LLAMA_MODEL := $(HOME)/models/unsloth-qwen35-4b-q4xl/Qwen3.5-4B-UD-Q4_K_XL.gguf
LLAMA_MODEL_DIR := $(HOME)/models/unsloth-qwen35-4b-q4xl

# shared shell helpers
define HELPERS
port_up() { curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$$1/" ; }
wait_up() { # name port [tries]
  local name=$$1 port=$$2 tries=$${3:-60} i
  for ((i=0; i<tries; i++)); do
    if port_up "$$port"; then echo "  $$name: up (127.0.0.1:$$port)"; return 0; fi
    sleep 1
  done
  echo "  $$name: not answering on :$$port — check its log" >&2
  return 1
}
wait_ready() { # name url [tries] — wait until endpoint answers HTTP 200
  local name=$$1 url=$$2 tries=$${3:-120} i code
  for ((i=0; i<tries; i++)); do
    code=$$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "$$url" || true)
    [ "$$code" = 200 ] && { echo "  $$name: ready"; return 0; }
    sleep 1
  done
  echo "  $$name: no 200 at $$url — check its log" >&2
  return 1
}
find_pids() { # frag... → pids owned by us whose cmdline contains every frag
  local p pid cmd owner ok frag
  for p in /proc/[0-9]*; do
    pid=$${p#/proc/}
    [ "$$pid" = "$$$$" ] && continue
    cmd=$$(cat "$$p/cmdline" 2>/dev/null | tr '\0' ' ') || continue
    [ "$$(stat -c %u "$$p" 2>/dev/null)" = "$$EUID" ] || continue
    ok=1
    for frag in "$$@"; do
      [[ "$$cmd" == *$$frag* ]] || { ok=0; break; }
    done
    [ "$$ok" = 1 ] && echo "$$pid"
  done
  return 0
}
find_web() { # 'python server.py' processes whose cwd is the web/ dir
  local p pid cmd
  for p in /proc/[0-9]*; do
    pid=$${p#/proc/}
    [ "$$pid" = "$$$$" ] && continue
    cmd=$$(cat "$$p/cmdline" 2>/dev/null | tr '\0' ' ') || continue
    [[ "$$cmd" == *"python server.py"* ]] || continue
    [ "$$(readlink "$$p/cwd" 2>/dev/null)" = "$(WS_ROOT)/web" ] || continue
    [ "$$(stat -c %u "$$p" 2>/dev/null)" = "$$EUID" ] || continue
    echo "$$pid"
  done
  return 0
}
kill_pids() { # name pids...
  local name=$${1:-}
  [ "$$name" = "" ] && return 0
  shift
  [ "$$#" -eq 0 ] && { echo "  $$name: not running"; return 0; }
  echo "  $$name: stopping pid $$*"
  kill "$$@" 2>/dev/null || true
  local left i
  for i in $$(seq 1 10); do
    left=""
    for p in "$$@"; do kill -0 "$$p" 2>/dev/null && left="$$left $$p"; done
    [ -z "$$left" ] && break
    sleep 0.5
  done
  [ -n "$$left" ] && kill -9 $$left 2>/dev/null || true
  return 0
}
endef

.PHONY: start stop restart status canary logs detector test-omdet test-deep record-router eval-router eval-followup tree-probe test-voice help

start:
	@$(HELPERS)
	echo "starting wiresense services..."
	if port_up $(PORT_PHONON); then
	  echo "  phonon: already up"
	else
	  echo "  phonon: starting..."
	  ( cd $(WS_ROOT) ; nohup .venv-asr/bin/fermion serve phonon-2 --port $(PORT_PHONON) > $(LOG_PHONON) 2>&1 & echo $$! > $(PID_PHONON) )
	  wait_up phonon $(PORT_PHONON) 90
	fi
	if port_up $(PORT_LLAMA); then
	  echo "  llama-server: already up"
	else
	  echo "  llama-server: starting (model load can take ~30 s)..."
	  nohup $(LLAMA_BIN) -m $(LLAMA_MODEL) --alias wiresense-llm \
	    --host 127.0.0.1 --port $(PORT_LLAMA) \
	    -c 65536 -np 8 --flash-attn on -b 8192 -ub 2048 \
	    -ctk q8_0 -ctv q8_0 \
	    --embedding --pooling last -ngl 99 \
	    -mm $(LLAMA_MODEL_DIR)/mmproj-F16.gguf \
	    --image-max-tokens 1024 \
	    > $(LOG_LLAMA) 2>&1 &
	  echo $$! > $(PID_LLAMA)
	  wait_ready llama-server "http://127.0.0.1:$(PORT_LLAMA)/health" 180
	fi
	if port_up $(PORT_WEB); then
	  echo "  web: already up"
	else
	  echo "  web: starting..."
	  mkdir -p /tmp/wiresense_mic
	  ( cd $(WS_ROOT)/web ; WIRESENSE_MIC_CAPTURE=/tmp/wiresense_mic nohup $(WS_ROOT)/.venv/bin/python server.py > $(LOG_WEB) 2>&1 & echo $$! > $(PID_WEB) )
	  wait_up web $(PORT_WEB) 30
	fi
	$(MAKE) detector
	echo "all services up — web UI: http://127.0.0.1:$(PORT_WEB)"

stop:
	@$(HELPERS)
	echo "stopping wiresense services..."
	kill_pids web      $$(find_web)
	kill_pids phonon   $$(find_pids "fermion serve phonon-2")
	kill_pids llama    $$(find_pids "llama-server" "$(PORT_LLAMA)")
	kill_pids guard    $$(find_pids "tools/vram_guard.py")
	kill_pids detector $$(find_pids "omdet_sidecar.py")
	rm -f $(PID_LLAMA) $(PID_PHONON) $(PID_WEB) $(PID_GUARD) $(PID_OMDET) $(READY_GUARD)

restart: stop start

detector:
	@$(HELPERS)
	if port_up $(PORT_OMDET); then
	  echo "  detector: already up"
	else
	  if [ ! -f "$(WS_ROOT)/models/omdet/detector_enc_640.onnx" ]; then
	    echo "  detector: models/omdet/*.onnx missing — export first (omdet workspace export_onnx_cpu.py)" >&2
	    exit 1
	  fi
	  echo "  detector: starting..."
	  ( cd $(WS_ROOT)/web ; nohup $(WS_ROOT)/.venv/bin/python omdet_sidecar.py > $(LOG_OMDET) 2>&1 & echo $$! > $(PID_OMDET) )
	  wait_up detector $(PORT_OMDET) 120
	fi

status:
	@$(HELPERS)
	for spec in "phonon:$(PORT_PHONON)" "llama-server:$(PORT_LLAMA)" "web:$(PORT_WEB)" "detector:$(PORT_OMDET)"; do
	  name=$${spec%%:*}; port=$${spec##*:}
	  if port_up $$port; then echo "  $$name: UP   (127.0.0.1:$$port)"; else echo "  $$name: DOWN (127.0.0.1:$$port)"; fi
	done
	if command -v nvidia-smi >/dev/null; then
	  nvidia-smi --query-gpu=memory.used,memory.total,temperature.gpu,power.draw --format=csv,noheader | sed 's/^/  gpu: /'
	fi

canary:
	@$(WS_ROOT)/.venv/bin/python $(WS_ROOT)/tools/canary.py $(PORT_LLAMA)

record-router:
	@$(WS_ROOT)/.venv/bin/python $(WS_ROOT)/tools/record_router.py

eval-router:
	@$(WS_ROOT)/.venv/bin/python $(WS_ROOT)/tools/eval_router.py

eval-followup:
	@$(WS_ROOT)/.venv/bin/python $(WS_ROOT)/tools/eval_followup.py

tree-probe:
	@$(WS_ROOT)/.venv/bin/python $(WS_ROOT)/tools/tree_probe.py

test-voice:
	@$(WS_ROOT)/.venv/bin/python $(WS_ROOT)/tools/test_voice.py

test-omdet:
	@$(WS_ROOT)/.venv/bin/python $(WS_ROOT)/tools/test_omdet.py

test-deep:
	@$(WS_ROOT)/.venv/bin/python $(WS_ROOT)/tools/test_deep_think.py

logs:
	@tail -n 25 -F $(LOG_LLAMA) $(LOG_PHONON) $(LOG_WEB) $(LOG_OMDET)

help:
	@echo "wiresense services:"
	@echo "  make start     launch Phonon (:$(PORT_PHONON)), llama-server (:$(PORT_LLAMA)), web (:$(PORT_WEB)), detector (:$(PORT_OMDET))"
	@echo "  make detector  start/verify the omdet detector sidecar only"
	@echo "  make stop      stop the wiresense services (only ours; nothing else)"
	@echo "  make restart   stop + start"
	@echo "  make status    health of each service + GPU snapshot"
	@echo "  make canary    GPU fp4 health check (docs/INCIDENT-2026-10-04-GPU.md)"
	@echo "  make logs      tail the service logs"
	@echo "voice loop evals:"
	@echo "  make record-router  re-record router refs (any serving-numerics change)"
	@echo "  make eval-router    56 held-out root-route evals"
	@echo "  make eval-followup  fragment rewrite vs raw routing (frozen 8-case bar)"
	@echo "  make tree-probe     subtree feasibility probe (think/vision nodes)"
	@echo "  make test-voice     e2e: synthesized speech -> route/reply assertions"
	@echo "  make test-omdet     detector sidecar: health + latency budget"
	@echo "  make test-deep      deep-think adapters: offline parser/config tests"
