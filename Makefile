.PHONY: help phase0 scout trending process publish digest optimize experiment rollback doctor test init-db visuals tiktok-confirm

help:
	@echo "agentic-clipper — make targets"
	@echo "  init-db          create data/main.db from data/schema.sql"
	@echo "  test             run pytest"
	@echo "  doctor           health-check APIs, budget, strikes, warming, providers"
	@echo "  scout            run Scout once"
	@echo "  trending         refresh /data/trending.md from all sources"
	@echo "  process N=5      run full pipeline on next N candidates"
	@echo "  visuals CLIP_ID=<id>  re-run only the Visuals stage for a clip"
	@echo "  publish          flush ready queue, respecting schedule"
	@echo "  digest           produce today's human digest"
	@echo "  optimize         run Optimizer manually"
	@echo "  experiment NAME=<name>  open a new bandit experiment"
	@echo "  rollback CHANGE_ID=<id>  manually roll back an auto-applied change"
	@echo "  tiktok-confirm CLIP_ID=<id> POST_ID=<id>  mark a manually-uploaded TikTok as posted"
	@echo "  phase0           (no-op; Phase 0 already complete — see docs/phase0_digest.md)"

phase0:
	@echo "Phase 0 is complete. See docs/phase0_digest.md for the human approval digest."

init-db:
	python3 -c "from agents.db import init_schema; init_schema()"

test:
	python3 -m pytest tests/ -v

doctor:
	python3 scripts/doctor.py

scout:
	python3 -m agents.scout

trending:
	python3 scripts/refresh_trending.py

N ?= 5
process:
	python3 scripts/process_pipeline.py --count $(N)

visuals:
ifndef CLIP_ID
	$(error "CLIP_ID is required; usage: make visuals CLIP_ID=2026-05-14-1200-abc")
endif
	python3 -m agents.visuals --clip-id $(CLIP_ID)

publish:
	python3 -m agents.publisher

digest:
	python3 scripts/daily_digest.py

optimize:
	python3 -m agents.optimizer

experiment:
ifndef NAME
	$(error "NAME is required; usage: make experiment NAME=hook_template_AB")
endif
	python3 scripts/open_experiment.py --name $(NAME)

rollback:
ifndef CHANGE_ID
	$(error "CHANGE_ID is required; usage: make rollback CHANGE_ID=<id>")
endif
	python3 scripts/rollback_change.py --change-id $(CHANGE_ID)

tiktok-confirm:
ifndef CLIP_ID
	$(error "CLIP_ID is required; usage: make tiktok-confirm CLIP_ID=<id> POST_ID=<id>")
endif
ifndef POST_ID
	$(error "POST_ID is required; usage: make tiktok-confirm CLIP_ID=<id> POST_ID=<id>")
endif
	python3 scripts/tiktok_confirm.py --clip-id $(CLIP_ID) --post-id $(POST_ID)
