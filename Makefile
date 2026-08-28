# Human-operated user-global Harness refresh from this checkout.
#
# Isolated development remains `scripts/dev`. These targets leave overlay env
# first, then run `uv tool install` plus the tool-installed `harness install`.
# Checkout agents must not invoke them.

SHELL := /bin/bash
.DEFAULT_GOAL := help

HOST ?= cursor
INSTALL_GLOBAL := ./scripts/install-global

.PHONY: help install-global doctor-global

help:
	@printf '%s\n' \
	  'Harness developer targets' \
	  '' \
	  '  make install-global                 refresh the user-global uv-tool Harness' \
	  '  make install-global HOST=cursor     same (default host; primary Linux close-out)' \
	  '  make install-global HOST=claude-code' \
	  '  make install-global HOST=codex' \
	  '  make doctor-global                  run the user-global harness doctor only' \
	  '' \
	  'install-global reinstalls this tree with uv 0.12.5, then runs that' \
	  'tool-installed harness install so a stale daemon is replaced. It strips' \
	  'HARNESS_DEV_ROOT, restores pre-overlay XDG, and drops checkout .venv/bin.' \
	  'This checkout still uses isolated MCP; test the global server elsewhere.' \
	  'Checkout agents must not run these targets.'

install-global:
	$(INSTALL_GLOBAL) --host $(HOST)

doctor-global:
	$(INSTALL_GLOBAL) --doctor-only
