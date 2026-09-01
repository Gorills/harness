# Human-operated user-global Harness refresh from this checkout.
#
# Isolated development remains `scripts/dev`. These targets leave overlay env
# first, then run `uv tool install` plus the tool-installed `harness install`.
# Checkout agents must not invoke them.

SHELL := /bin/bash
.DEFAULT_GOAL := help

INSTALL_GLOBAL := ./scripts/install-global
ACCEPT_CODEX := ./scripts/dev python scripts/accept_codex.py

.PHONY: help accept-global-codex benchmark-hot-paths install-global doctor-global

help:
	@printf '%s\n' \
	  'Harness developer targets' \
	  '' \
	  '  make install-global                       refresh the user-global uv-tool Harness' \
	  '  make install-global HOST=cursor           install one profile only' \
	  '  make install-global HOST=codex' \
	  '  make install-global HOST=cursor,codex   install an explicit profile set' \
	  '  make accept-global-codex                 install package, test Codex with temporary state' \
	  '  make benchmark-hot-paths                 measure project_status, watcher, and scan costs' \
	  '  make doctor-global                        run the user-global harness doctor only' \
	  '' \
	  'install-global reinstalls this tree with uv 0.12.5, then runs that' \
	  'tool-installed harness install for cursor and codex by default so a stale' \
	  'daemon is replaced. It strips HARNESS_DEV_ROOT, restores pre-overlay XDG,' \
	  'and drops checkout .venv/bin. This checkout still uses isolated MCP;' \
	  'test the global server elsewhere. Agents may run accept-global-codex after' \
	  'explicit user approval; live install-global requires separate explicit approval.'

accept-global-codex:
	$(ACCEPT_CODEX) --global-install --preflight-only \
	  --evidence /tmp/harness-codex-global-preflight.json

benchmark-hot-paths:
	./scripts/dev python scripts/benchmark_hot_paths.py --assert-counters

install-global:
ifdef HOST
	$(INSTALL_GLOBAL) --host "$(HOST)"
else
	$(INSTALL_GLOBAL)
endif

doctor-global:
	$(INSTALL_GLOBAL) --doctor-only
