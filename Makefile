.PHONY: help phase0 setup setup-apply init-db migrate test doctor publish visuals \
        morning scout trending process digest optimize experiment rollback tiktok-confirm

# === Active targets (work today) ============================================

help:
	@echo "agentic-clipper — make targets"
	@echo ""
	@echo "Active:"
	@echo "  morning          (recommended daily entry) digest + actions + queue"
	@echo "  setup            audit operator environment; print fixes for missing pieces"
	@echo "  setup-apply      run safe automations (pip deps + init-db + migrate)"
	@echo "  init-db          create data/main.db from data/schema.sql"
	@echo "  migrate          apply pending schema migrations from migrations/"
	@echo "  test             run pytest"
	@echo "  doctor           health-check APIs, budget, strikes, warming, providers"
	@echo "  publish          flush ready queue, respecting schedule (CLI wired)"
	@echo "  phase0           (no-op; Phase 0 already complete — see docs/phase0_digest.md)"
	@echo ""
	@echo "Phase 2 stubs (target prints 'not yet wired' and exits 1):"
	@echo "  scout            run Scout once"
	@echo "  trending         refresh data/trending.md"
	@echo "  process N=5      run full pipeline on next N candidates"
	@echo "  visuals CLIP_ID=<id>  re-run only Visuals stage"
	@echo "  optimize         run Optimizer manually"
	@echo "  experiment NAME=<name>     open a bandit experiment"
	@echo "  rollback CHANGE_ID=<id>    roll back an auto-applied change"
	@echo "  tiktok-confirm CLIP_ID=<id> POST_ID=<id>  flip manual_pending → posted"

phase0:
	@echo "Phase 0 is complete. See docs/phase0_digest.md for the human approval digest."

morning:
	python3 -m agents.digest morning

digest:
	python3 -m agents.digest

setup:
	python3 scripts/setup.py

setup-apply:
	python3 scripts/setup.py --apply

init-db:
	python3 -c "from agents.db import init_schema; init_schema()"

migrate:
	python3 scripts/migrate.py

test:
	python3 -m pytest tests/ -v

doctor:
	python3 scripts/doctor.py

# Publisher has an `if __name__ == "__main__":` block that runs run_publisher
# under asyncio. Manual-mode platforms (TikTok) write to their drop directory;
# api-mode platforms hit Phase 2 NotImplementedError stubs and the row is
# marked failed for later retry.
publish:
	python3 -m agents.publisher

# === Phase 2 stubs (intentionally fail loudly) ==============================
# Each target below references a script or module CLI that is not yet wired.
# Codex challenge 2026-05-17 flagged that the prior Makefile pointed at
# nonexistent scripts (`scripts/refresh_trending.py` etc.) and at module CLIs
# that have no `if __name__ == "__main__":`. Rather than silently failing
# with cryptic "No such file or directory" errors, each target now prints
# a clear "Phase 2 not yet wired" message and exits 1.

_PHASE2_NOT_WIRED = @echo "Phase 2 NOT YET WIRED: $@. See docs/runbook.md for Phase 2 plan."; exit 1

scout:
	$(_PHASE2_NOT_WIRED)

trending:
	$(_PHASE2_NOT_WIRED)

N ?= 5
process:
	$(_PHASE2_NOT_WIRED)

visuals:
ifndef CLIP_ID
	$(error "CLIP_ID is required; usage: make visuals CLIP_ID=2026-05-14-1200-abc")
endif
	$(_PHASE2_NOT_WIRED)

optimize:
	$(_PHASE2_NOT_WIRED)

experiment:
ifndef NAME
	$(error "NAME is required; usage: make experiment NAME=hook_template_AB")
endif
	$(_PHASE2_NOT_WIRED)

rollback:
ifndef CHANGE_ID
	$(error "CHANGE_ID is required; usage: make rollback CHANGE_ID=<id>")
endif
	$(_PHASE2_NOT_WIRED)

tiktok-confirm:
ifndef CLIP_ID
	$(error "CLIP_ID is required; usage: make tiktok-confirm CLIP_ID=<id> POST_ID=<id>")
endif
ifndef POST_ID
	$(error "POST_ID is required; usage: make tiktok-confirm CLIP_ID=<id> POST_ID=<id>")
endif
	$(_PHASE2_NOT_WIRED)
