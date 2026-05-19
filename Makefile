.PHONY: help phase0 setup setup-apply init-db migrate test doctor publish visuals \
        morning scout trending process digest optimize experiment rollback tiktok-confirm \
        pilot-start pilot-status pilot-verdict pilot-record-revenue pilot-record-time \
        pilot-record-claim pilot-finalize

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
	@echo ""
	@echo "Day 14 validation pilot gate (operator-driven):"
	@echo "  pilot-start [PLATFORM=instagram_reels] [CLIPS=30]    open a pilot run"
	@echo "  pilot-status                                          show progress numbers"
	@echo "  pilot-record-revenue AMOUNT=1.23 SOURCE=ad_rev        log observed revenue"
	@echo "  pilot-record-time MINUTES=32                          log end-of-day op minutes"
	@echo "  pilot-record-claim [CLIP_ID=<id>]                     log a Content ID claim"
	@echo "  pilot-verdict                                         evaluate the 3 gate criteria"
	@echo "  pilot-finalize VERDICT=pass|fail|abandon              close the pilot"

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

# === Day 14 validation pilot gate ==========================================
# Operator-driven: pilot orchestration tooling exists, but the actual posting
# + observing remains a manual exercise on the operator's end. See
# docs/runbook.md "Pilot gate" for the daily loop.

PLATFORM ?= instagram_reels
CLIPS    ?= 30

pilot-start:
	python3 scripts/pilot.py start --platform $(PLATFORM) --clips $(CLIPS)

pilot-status:
	python3 scripts/pilot.py status

pilot-record-revenue:
ifndef AMOUNT
	$(error "AMOUNT is required; usage: make pilot-record-revenue AMOUNT=1.23 SOURCE=ad_rev")
endif
ifndef SOURCE
	$(error "SOURCE is required; usage: make pilot-record-revenue AMOUNT=1.23 SOURCE=ad_rev|affiliate|creator_fund|other")
endif
	python3 scripts/pilot.py record-revenue --amount $(AMOUNT) --source $(SOURCE) $(if $(DETAIL),--detail "$(DETAIL)",)

pilot-record-time:
ifndef MINUTES
	$(error "MINUTES is required; usage: make pilot-record-time MINUTES=32")
endif
	python3 scripts/pilot.py record-time --minutes $(MINUTES) $(if $(NOTE),--note "$(NOTE)",)

pilot-record-claim:
	python3 scripts/pilot.py record-claim $(if $(CLIP_ID),--clip-id $(CLIP_ID),) $(if $(DETAIL),--detail "$(DETAIL)",)

pilot-verdict:
	python3 scripts/pilot.py verdict

pilot-finalize:
ifndef VERDICT
	$(error "VERDICT is required; usage: make pilot-finalize VERDICT=pass|fail|abandon")
endif
	python3 scripts/pilot.py finalize --$(VERDICT) $(if $(NOTES),--notes "$(NOTES)",)

tiktok-confirm:
ifndef CLIP_ID
	$(error "CLIP_ID is required; usage: make tiktok-confirm CLIP_ID=<id> POST_ID=<id>")
endif
ifndef POST_ID
	$(error "POST_ID is required; usage: make tiktok-confirm CLIP_ID=<id> POST_ID=<id>")
endif
	$(_PHASE2_NOT_WIRED)
