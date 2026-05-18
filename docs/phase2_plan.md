<!-- /autoplan restore point: /Users/abhishekmishra/.gstack/projects/amishra883-agentic-clipper/claude-autonomous-clipping-pipeline-fxxPt-autoplan-restore-20260517-235244.md -->
# Phase 2 Wiring Plan

**Status:** draft (2026-05-17). Operator review required before kickoff.

**Predecessor:** Phase 1 scaffold is complete. All 10 agents have structurally-valid stubs, the Compliance gate is enforced end-to-end, the queue is concurrency-safe and fail-closed, and the test suite covers 67 cases. The runbook step 6 (locked avatar reference image) shipped on 2026-05-17.

**Scope:** wire every agent's external integration so the pipeline runs end-to-end on real source clips. Out of scope: model swaps, alternative providers (unless a primary fails), UI dashboards, A/B-test infrastructure beyond what's specified in CLAUDE.md.

---

## Dependency order

Wire bottom-up: stages that consume must come after stages that produce. The skeleton in `agents/` is already organized this way; this section just makes the order explicit.

```
1. Scout         (no deps; pulls candidates)
2. Curator       (depends on Scout output in clips_candidate)
3. Editor        (depends on a curated candidate)
4. Writer        (depends on Editor's transcript + punch segment)
5. Voice         (depends on Writer's script)
6. Visuals       (depends on Writer's shot list)
7. Compositor    (depends on Editor / Voice / Visuals outputs)
8. (Compliance — already wired)
9. (Publisher   — already wired except for live API uploads)
10. Analyst      (depends on published clips; reads platform metrics)
11. Optimizer    (depends on Analyst's learnings)
```

Five of these have a meaningful dependency outside the codebase too (Scout, Editor, Visuals, Publisher's live APIs, Analyst). Each is enumerated below with the credential it needs.

---

## Per-agent plan

Each section: **acceptance criterion** (binary, testable), **external deps**, **risk**, **rough sizing**.

### Scout — pull candidate clips every 4h

- **Acceptance:** `agents/scout.py` has an async `run_scout()` that on each call inserts >=1 new row into `clips_candidate` for each tracked source (Twitch + YouTube + TikTok). `make scout` succeeds with exit code 0 and `events` table has an `ingest_summary` row.
- **External deps:**
  - Twitch Helix API (client credentials grant — `TWITCH_CLIENT_ID` + `TWITCH_CLIENT_SECRET`)
  - YouTube Data API v3 (`YOUTUBE_API_KEY` — read-only quota; trending uses `videos.list?chart=mostPopular`)
  - TikTok Creative Center (no public API — scrape via Playwright; documented as a fallback in `docs/posting_apis.md`)
- **Risk:** TikTok scraping breakage is the highest risk; the layout changes every few months. Mitigation: keep the Twitch + YouTube path as the load-bearing source; treat TikTok as best-effort.
- **Sizing:** ~400 lines + retry/backoff helper. ~1 day if APIs cooperate.

### Curator — score and rank Scout output

- **Acceptance:** `agents/curator.py.run_curator()` reads `clips_candidate WHERE status='discovered'` and writes `virality_score` (0..1) + `predicted_views` per row, then flips status to `curated` for the top N (config-driven, default 5/day). Uses heuristics first (view velocity, transcript sentiment, age) and only invokes LLM judgment for the borderline tier (e.g., 0.4..0.7 heuristic score). LLM call goes through a sub-agent prompt template that reads `data/playbook.md` + `data/anti-patterns.md`.
- **External deps:** Anthropic API (sub-agent — covered by Claude Max; falls back to direct API key for batch reruns).
- **Risk:** Curator/Writer is where the most spend leaks. Cap LLM calls per run; log cost to `costs` table.
- **Sizing:** ~300 lines. ~half a day for heuristics + LLM-borderline path.

### Editor — download + transcribe + segment

- **Acceptance:** `agents/editor.py.run_editor(clip_id)` downloads source via yt-dlp, transcribes with `faster-whisper` (CPU fallback works on M-series), picks a 15–30s punch segment using LLM scene analysis of the transcript, and writes: `source_local_path`, `punch_segment_start_s`, `punch_segment_end_s`, `transcript_json`, `has_music_in_source_segment` (the tri-state column added in commit `7df1011`). Last field is non-NULL after this stage runs — Compliance will fail closed otherwise.
- **External deps:** `yt-dlp` and `faster-whisper` Python packages. ffmpeg binary.
- **Risk:**
  - **YouTube anti-bot.** yt-dlp gets fingerprinted; need rotating cookies or proxy. Budget line `proxy_pool` covers this.
  - **Music detection accuracy.** Whisper's confidence isn't a music detector. Use a real classifier (e.g., `panns_inference` or simple spectral analysis); cite false-negative rate in the runbook.
- **Sizing:** ~500 lines. ~1.5 days because music detection is its own sub-problem.

### Writer — LLM-generated commentary script + shot list

- **Acceptance:** `agents/writer.py._llm_generate_script()` returns a real `Script` from Claude (via sub-agent or direct API). Existing `_validate_script` enforcement stays — both substance/trending hard violations AND new content the LLM emits get checked. Persists to `clip_artifacts.script_text` + `shot_list_json`.
- **External deps:** Anthropic API. Persona prompt template + retrieval over `data/trending.md` + `data/playbook.md`.
- **Risk:** LLM produces sycophantic / generic content; the persona's `do_not` list catches some but not all. The recent hard-violation enforcement (commit `7dfffe6`) is the safety net.
- **Sizing:** ~300 lines + ~150 lines of prompt template. ~1 day.

### Voice — TTS for the commentary

- **Acceptance:** `agents/voice.py.run_voice(clip_id)` reads `clip_artifacts.script_text`, synthesizes via Coqui XTTS-v2 (local default), writes `voice_audio_path` + `voice_runtime_s` + `loudness_lufs=-14` (normalized). Sets `AudioTrack.voice_id` to a value in `persona.yaml voice.approved_voice_ids` (the structural guard from commit `33a39e0` blocks an unapproved value at Compliance).
- **External deps:**
  - Coqui XTTS-v2 (local, free, GPU optional)
  - ElevenLabs Creator tier (optional escalation per curator score)
- **Risk:** Coqui voices take ~2–4 GB on disk. First call is slow (model download). Mitigation: prewarm in `make init-db`-equivalent setup step; document in runbook.
- **Sizing:** ~250 lines. ~half a day.

### Visuals — Seedance video generation

- **Acceptance:** `agents/visuals.py._atlas_cloud_generate()` and `_fal_ai_generate()` call the real APIs (auth + submit + poll + download). Already partially wired: prompt-hash cache, per-clip cap, Pro-tier promotion gate, monthly line-item budget cap (commit `7dfffe6`). What's missing is the actual HTTP call.
- **External deps:** Atlas Cloud (primary, key already in operator's `.env`); fal.ai (fallback).
- **Risk:** Seedance video API differs from the Seedream image API the avatar script uses — async task model, longer poll times, real-face filter behavior (HTTP 200 with empty body — already structurally handled in visuals.py). Also sets `clip_artifacts.has_real_face_reference` to 0 after the filter passes.
- **Sizing:** ~250 lines (large chunks of the pattern already exist in `scripts/generate_avatar.py`). ~half a day.

### Compositor — ffmpeg + MoviePy assembly

- **Acceptance:** `agents/compositor.py._ffmpeg_compose()` produces a final 9:16 vertical MP4 at the output path with: trimmed source segment, voice ducked-and-mixed, word-level captions burned in (Whisper word timestamps), avatar reactions inserted at `punch_beats`, transition stingers, intro/outro. Returns the real duration. Compliance evidence columns (`has_music_in_source_segment`, `has_real_face_reference`) are read from upstream — Compositor does NOT assert them (commit `33a39e0` enforced this).
- **External deps:** ffmpeg binary (system install); MoviePy (pip).
- **Risk:** Audio ducking quality directly affects retention. Burn-in timing for word-level captions is fiddly. Reserve time for output-quality QA.
- **Sizing:** ~600 lines + ~3 hours of QA. ~1.5 days.

### Publisher — live API uploads (YouTube, Instagram)

- **Acceptance:** `_upload_youtube_shorts()` and `_upload_instagram_reels()` actually upload via OAuth + resumable upload. Returns `platform_post_id`. TikTok stays manual per operator decision; that path is already wired (commit `7dfffe6`).
- **External deps:**
  - YouTube Data API v3 OAuth refresh token per account (`YOUTUBE_OAUTH_REFRESH_TOKEN`)
  - Instagram Graph API long-lived page token (`IG_ACCESS_TOKEN`)
- **Risk:**
  - YouTube quota: 1600 units/upload, 10,000/day default → 6 uploads/day cap. Wire quota tracking; surface in doctor.
  - Instagram's two-step (create container → publish) has rate limits and "draft only" pitfalls for unverified accounts.
- **Sizing:** ~400 lines. ~1 day.

### Analyst — 48h-delayed platform metrics

- **Acceptance:** `agents/analyst.py.run_analyst()` pulls per-clip metrics from each platform's read API and writes a `performance_metrics` row joined to the `feature_records` row (which Publisher must also start populating in Phase 2). Appends a structured record to `data/learnings.jsonl`.
- **External deps:** Same OAuth tokens as Publisher's read scopes.
- **Risk:** None novel; this is the read side of Publisher.
- **Sizing:** ~300 lines. ~half a day.

### Optimizer — auto-tuning + experiments

- **Acceptance:** `agents/optimizer.py.run_optimizer()` reads `data/learnings.jsonl`, applies bounded auto-changes per `config/optimizer_bounds.yaml` (posting time ±60min, hashtag rotation, caption style, b-roll density, hook template weighting), writes a row to `auto_changes` per change. Auto-rollback fires if a change underperforms baseline by >15% over 72h. Weekly run proposes higher-risk changes as Markdown diffs in `/proposals/`.
- **External deps:** None (self-contained).
- **Risk:** Sample size needed for statistical significance. Initial month will mostly be exploration with high variance.
- **Sizing:** ~500 lines. ~1 day.

### Trending intake (Scout extension)

- **Acceptance:** `make trending` invokes a 12h refresher that writes `data/trending.md` from TikTok Creative Center + YouTube trending + Reddit JSON + Know Your Meme + Twitter/X trending. Decay rules (Hot → Cooked) apply.
- **External deps:** Mostly scraping; Reddit JSON has a public read API; Twitter requires either browser-scraping or a basic-tier paid plan.
- **Risk:** Trending sources break often. Treat as best-effort with a `last_refreshed_at` timestamp the Writer checks (`stale_check()` already enforces 48h freshness).
- **Sizing:** ~400 lines. ~1 day.

---

## Cross-cutting work

### Sub-agent vs direct-API decision

The spec calls for sub-agents inside Claude Code (covered by Max) for Curator and Writer to keep external-API spend low. Implement these as Claude Agent SDK invocations from the agent code; only fall back to direct API for batch reruns or cron-triggered (out-of-session) calls.

### Cookies / proxy management

yt-dlp will get caught without browser cookies + IP rotation. Stand up a small `agents/proxy.py` helper that picks a residential proxy from the pool and attaches it to outbound requests; budget line `proxy_pool` covers $30/mo.

### Cost ledger discipline

Every external paid call (Anthropic, ElevenLabs, Seedance, proxy) must write a `costs` row. The doctor's monthly burn check (commit `7dfffe6`) depends on this; if anyone forgets, the kill switch silently doesn't fire.

### Test strategy

- Unit-test each agent's internal logic (validators, score functions, prompt templates).
- Integration-test the queue claim → compliance gate → publisher dispatch path with mocked external calls. (Publisher path is already covered in `tests/test_publisher_queue.py`.)
- Smoke-test the live external calls separately under a `make integration` target that's NOT run in CI but is run before each release.

### CI

Currently none. Add a GitHub Action that runs `pytest` on every push and refuses merges if it fails. Keep it simple — no live API calls in CI.

---

## Estimated total

~10 days of sustained work, distributed over ~4 weeks given testing + operator review per agent. Order of go-live:

1. **Day 1–2:** Scout + Curator + cost discipline. Lands first because trending discovery feeds everything.
2. **Day 3–4:** Editor + Writer. Pipeline starts producing real artifacts.
3. **Day 5:** Voice + Visuals. Outputs are now audio+video real.
4. **Day 6–7:** Compositor. Operator can manually verify final clip quality.
5. **Day 8:** Publisher YouTube + Instagram. First real posts (small N, monitored).
6. **Day 9:** Analyst. First learnings flow.
7. **Day 10:** Optimizer. Self-tuning loop closes.

After day 10 the system runs without manual intervention except for the 30-min/day attention slot the operator promised in CLAUDE.md.

---

## Risks that should land in `/proposals/` before any Phase 2 code

- **Anthropic API spend ceiling.** Sub-agent vs direct-API split needs an explicit budget number — currently $40/mo line item but no per-call limit. Easy to blow past it.
- **YouTube anti-bot tightening.** yt-dlp is in a years-long cat-and-mouse with Google; if it breaks for the top-5 creators we lose the load-bearing source.
- **TikTok manual-mode operator load.** 3 posts/day TikTok = 90 manual uploads/month. Operator's 30 min/day budget is tight. Re-evaluate at month 1.
- **Compliance gate false-negative.** The music-detection and real-face-reference checks now fail-closed (commit `33a39e0`), but the upstream stages that populate those values don't exist yet. Phase 2's Editor + Visuals must populate them deliberately, not "good enough."

These should be operator-approved before code lands, not assumed.

---

# /autoplan Review Report

Generated 2026-05-17 via `/autoplan`. Full depth on autoplan structure; depth on sub-skill templates (CEO/Eng/DX) is autoplan-structure-faithful, not 6,117-line sub-skill-literal.

## Phase 1 — CEO Review (Strategy & Scope)

### 0A: Premise challenge

Five load-bearing premises this plan inherits without testing:

| # | Premise | Stated in | Reality | Severity |
|---|---------|-----------|---------|----------|
| P1 | Open clipper programs are the wedge | `CLAUDE.md:5` | Operator dropped them 2026-05-14 (`fair_use_position.md:7`) | **CRITICAL** — mission and current posture disagree |
| P2 | $500/mo ad-rev by month 6 | `CLAUDE.md:386` | Shorts RPM $0.01–$0.07 (`exit_strategy.md:57`) → need 7M–50M views/mo | **CRITICAL** — math unsupportable at 7 clips/day cadence |
| P3 | Zero strikes tolerated AND fair-use-only at scale | `CLAUDE.md:26` vs `fair_use_position.md:16` (expects "occasional Content ID claims") | The two stances are incompatible at 210 posts/mo | **CRITICAL** — single Content ID cluster kills account |
| P4 | 30 min/day operator budget | `CLAUDE.md:25` | 90 manual TikTok uploads/mo ≈ 6–10 hrs/mo on TikTok alone, plus strikes, disputes, weekly Optimizer review | **HIGH** — fantasy budget |
| P5 | Account-sale exit at 6–12x | `CLAUDE.md:393` | `exit_strategy.md:7` says transfer violates ToS of all three platforms; realistic multiple 3–6x | **HIGH** — exit conditional at best |

### 0B: Existing code leverage map

| Sub-problem | Existing code | Reusable? |
|---|---|---|
| HTTP + poll pattern for async APIs | `scripts/generate_avatar.py` (Atlas Cloud Seedream) | YES — Visuals stage can copy the submit/poll/download skeleton |
| Compliance gate enforcement | `agents/compliance.py` (already wired, 14/40 test fixtures) | YES — Phase 2 stages just populate the tri-state evidence columns |
| Per-clip + month-to-date budget caps | `agents/visuals.py._month_to_date_*` | YES — applies to ElevenLabs, Anthropic, proxy spend too |
| SQLite concurrency + atomic claim | `agents/db.py` + `publisher._pick_next_clip` | YES — pattern reusable for Scout/Curator queues |
| Quarantine + structured event log | `agents/visuals.py._quarantine_clip`, `agents/events.py` | YES — every stage should use the same pattern |
| Doctor liveness pings | `scripts/doctor.py._ping_https` | YES — reusable for Twitch Helix, Anthropic API |

### 0C: Dream state delta

```
CURRENT (post-Phase-1)
├─ 10 agents stubbed, structurally valid placeholder outputs
├─ Compliance gate fully enforced (40+27 tests passing)
├─ Concurrency + budget + timezone safety in place
└─ No real source data flowing through
        ↓
THIS PLAN (Phase 2 end-of-day-10)
├─ All 10 agents wired to external services
├─ 7 clips/day published autonomously
├─ Optimizer running weekly proposals
└─ Single point of failure: Compliance gate's upstream evidence
        ↓
12-MONTH IDEAL
├─ ??? — undefined; CLAUDE.md month-9–12 promises account sale at $0.04 RPM math that doesn't work
├─ Cross-model agreement: Phase 2 as-written does NOT reach the dream state
└─ Both models propose validation-first reframe
```

### 0C-bis: Implementation alternatives

| Approach | Effort | Risk | Verdict |
|---|---|---|---|
| **A. Phase 2 as-written** (10 agents in 10 days, $200/mo) | 10d CC | Premises P1–P5 fail before agents matter | NOT RECOMMENDED |
| **B. Validation-first (14-day manual pilot)** | 14d operator + 0d CC | Tests claim rate, RPV, retention before any spend | **RECOMMENDED by both models** |
| **C. Reduced-scope Phase 2** (skip Scout, Optimizer, ElevenLabs → 6 agents, 5 days) | 5d CC | Still inherits P1–P3 contradictions | CONDITIONAL on validation results |
| **D. Pivot to B2B creator-permission clipping** | Multi-quarter reframe | Avoids ToS violation, clearer buyer, lower strike risk | Out of /autoplan scope but flagged by Codex |

### 0D: Mode-specific analysis (SELECTIVE EXPANSION)

Both voices flagged scope that was DISMISSED without writing the dismissal. Plan should add explicit "Alternatives considered" with re-evaluation dates:
- **Defer Scout** (let operator hand-pick clips first 30d) — save $0/mo but de-risk biggest scraping fragility
- **Drop ElevenLabs escalation** — save $22/mo + complexity; add back in month 3 if data justifies
- **Defer Optimizer to month 2** — 210 clips/mo is below statistical-significance threshold; building it now invites acting on noise

### 0E: Temporal interrogation

| Horizon | What happens | Risk |
|---|---|---|
| Hour 1 (Day 1 morning) | Scout pulls first candidates | YouTube anti-bot may break yt-dlp immediately; no fallback wired |
| Day 1 evening | First clip composed + compliance-gated | Compositor must populate `has_music_in_source_segment`; if music detector is the half-day afterthought the plan describes, false-negatives ship to publisher |
| Week 1 | ~50 clips posted | First Content ID claim cluster likely; no kill switch documented in plan |
| Week 2 | TikTok manual queue backlog | Operator's 30 min/day budget breached; backlog grows |
| Week 4 | Optimizer's first proposal | 210 clips, single persona, no controls — proposals are noise |
| Month 3 | First strike on a primary | Failover to warm backups; backups must already be warm (Phase 2 doesn't ensure this) |
| Month 6 | "Did this work?" | Most likely answer per both models: technically functional, ad-rev <$100/mo, 1-3 unresolved claims, exit value $0–$2,400 |

### 0F: Mode selection confirmation

Autoplan defaulted to SELECTIVE EXPANSION. Both voices implicitly recommend **SCOPE REDUCTION** instead — drop Scout/Optimizer/ElevenLabs from Phase 2; defer until validation data justifies them. This is a premise-level reframe, not a scope cherry-pick, so it goes to the premise gate (next section).

### Phase 1 dual voices

**CODEX SAYS (CEO — strategy challenge):** SEND BACK. Plan is implementation-heavy and strategy-light. Three project docs contradict each other in load-bearing ways. Revenue math at $0.04 RPM requires 7M–50M monetized views/month. Crowded competitive landscape (OpusClip, StoryShort, AutoFeed, ShortFast) — no moat defined. Account-sale exit violates ToS of all three primary platforms. Recommends: 14-day manual-mode validation pilot on real posts; gate Phase 2 on <2% Content ID claim rate + measurable affiliate RPV.

**CLAUDE SUBAGENT (CEO — strategic independence):** SEND BACK. Same diagnosis. Math at $0.04 RPM = 12.5M monthly views needed — moonshot at 7 clips/day. Music detection is load-bearing but treated as half-day afterthought. 30 min/day budget is fantasy given manual TikTok cadence. No competitive analysis. Most likely month-6 regret: "we should have built a single hand-curated affiliate-monetized newsletter+TikTok on one creator with affiliate links, treated the agentic architecture as a year-2 problem." Recommends: stripped Phase 2 (6 agents, not 10) ONLY after 14-day validation pilot passes.

### Phase 1 CEO consensus table

```
CEO DUAL VOICES — CONSENSUS TABLE:
═════════════════════════════════════════════════════════════════════════
  Dimension                              Claude       Codex      Consensus
  ─────────────────────────────────────  ──────────   ──────────  ─────────
  1. Premises valid?                     NO           NO          CONFIRMED — three docs contradict each other
  2. Right problem to solve?             NO (reframe) NO (reframe) CONFIRMED — both recommend B2B pivot or
                                                                  validation-first manual pilot
  3. Scope calibration correct?          OVERBUILT    OVERBUILT   CONFIRMED — defer Scout/Optimizer/ElevenLabs
  4. Alternatives sufficiently explored? NO           NO          CONFIRMED — no "Alternatives considered" section
  5. Competitive/market risks covered?   NO           NO          CONFIRMED — no competitor named in plan
  6. 6-month trajectory sound?           NO           NO          CONFIRMED — revenue math doesn't survive Shorts RPM
═════════════════════════════════════════════════════════════════════════
6/6 CONFIRMED — both voices agree on every dimension. No disagreements.
Single high-confidence verdict: SEND BACK before Phase 2 wiring.
```

### Phase 1 "NOT in scope" + deferred items

To `TODOS.md`:
- B2B creator-permission clipping pivot (Codex's 10x reframe)
- Affiliate-first monetization model (subagent's reframe)
- 14-day validation pilot on 1 creator + 1 platform + manual editing
- Competitor analysis (OpusClip, StoryShort, AutoFeed, ShortFast, Submagic, Tammy AI)
- Kill criteria definition: claim rate >2%, RPV <$0.001, time/day >45min

### Phase 1 Failure Modes Registry

| ID | Failure mode | Probability | Detection | Mitigation status in plan |
|----|---|---|---|---|
| F-CEO-1 | Content ID cluster on a single creator's source music | HIGH | strikes table + dispute log | NO — plan defers to "publish anyway, dispute on claim" |
| F-CEO-2 | yt-dlp broken by anti-bot update | HIGH | doctor liveness ping | PARTIAL — proxy budget but no fallback acquisition source |
| F-CEO-3 | TikTok manual queue exceeds 30 min/day budget | HIGH | operator self-report | NO — plan acknowledges but doesn't size |
| F-CEO-4 | Compliance music-detector false-negative | MEDIUM | Content ID claims surface it | NO — music detection is "half-day sub-problem" |
| F-CEO-5 | Optimizer acts on insufficient sample (n<500) | MEDIUM | rollback rate >0 | PARTIAL — auto-rollback exists but Optimizer should not run on noise |
| F-CEO-6 | Anthropic API spend overruns $40 line item | MEDIUM | monthly burn check (now in doctor) | YES — runtime cap can be added like Seedance |
| F-CEO-7 | Account sale buyer pool collapses on AI-content label | HIGH | exit attempt | NO — `exit_strategy.md:126` notes this; plan doesn't respond |

### Phase 1 Completion Summary

| Mode | SELECTIVE EXPANSION default, but voices recommend SCOPE REDUCTION + premise reframe |
|---|---|
| Premises validated | 0 of 5 |
| Scope expansions proposed | 0 (both voices recommend REDUCTION instead) |
| Scope reductions proposed | 3 (defer Scout, drop ElevenLabs, defer Optimizer) |
| Critical gaps | 5 (premise contradictions P1–P5, all critical or high) |
| Dual voices | Both ran, both recommend SEND BACK |
| User Challenge candidate | YES — both models recommend changing the user's stated direction (10-day full wiring → validation-first 14-day manual pilot) |
| Final phase verdict | **BLOCKED on premise gate** |

**Premise gate decision (2026-05-18):** Operator chose "Continue — finish full review" to surface every issue before deciding rethink scope. Contradictions acknowledged; review continued without modification to the plan.

## Phase 2 — Design Review

**SKIPPED.** No UI scope detected. The plan describes a 10-agent backend pipeline. Surface "component" / "layout" matches in the doc trigger from architecture nouns (agent components, repo layout), not user-facing UI work. No screens, dashboards, modals, forms, or interactive surfaces in Phase 2. Re-evaluate if a Phase 3+ adds a digest UI or operator dashboard.

## Phase 3 — Eng Review (Architecture, Tests, Performance, Security)

### Step 0: Scope challenge with actual code

Both voices read every `agents/*.py` file and the schema, not just the plan. The plan's 10-day sizing assumes "wire it up" complexity. The voices found the plan understates **6 distinct sub-projects** hiding inside the agent labels:

| Hidden sub-project | Plan slot | True size |
|---|---|---|
| Music-detection validation harness (50+ labeled clips, ≥95% precision) | "Editor 1.5 days" | +1 day |
| Caption word-timing forced alignment (WhisperX or similar) | "Compositor 1.5 days" | +1 day |
| LUFS-correct sidechain ducking | "Compositor 1.5 days" | +1-2 days |
| yt-dlp PoToken/SABR ongoing maintenance | "Editor 1.5 days" | 2-4 hrs/week ongoing |
| Typed Atlas/fal.ai response parser + face-filter classifier | "Visuals 0.5 days" | +1 day |
| LLM eval suite (golden outputs, drift detection) | not in plan | +2 days |

**Realistic re-sizing: 14-17 days, not 10.**

### Phase 3 dual voices

**CODEX SAYS (eng — architecture challenge):** SEND BACK. The plan introduces distributed side effects, paid APIs, mutable configs, prompt-fed scraped data, media timing, and platform quota behavior without the control plane those things require. Highest-risk fixes: stage leases/versioned artifacts (every stage transition conditional on `artifact_version`), pre-call cost reservations under `BEGIN IMMEDIATE` (not after-the-fact logging), typed provider response handling (replace `response.get("video_url")` with a proper parser yielding `submitted/processing/succeeded/face_filter_rejected/rate_limited/provider_error`), real music + no-speech eval suite, atomic validated config writes (temp-file + rename + version hash on every Optimizer mutation).

**CLAUDE SUBAGENT (eng — independent review):** SEND BACK. Found 6 architecture findings + 7 edge-case findings + 7 security findings + 6 hidden-complexity findings. Plan needs +3-5 days for atomic-claim hardening across Scout/Curator/Compositor, music-detection validation, prompt-injection sanitization layer, daily/per-call cost caps, response-shape verification against real Atlas Cloud, and a basic LLM eval suite. Without these, "wire it up and ship" produces a system whose Compliance gate has tested code on top of untested evidence inputs — the exact failure mode the gate was built to prevent.

### Phase 3 eng consensus table

```
ENG DUAL VOICES — CONSENSUS TABLE:
═════════════════════════════════════════════════════════════════════════
  Dimension                              Claude       Codex      Consensus
  ─────────────────────────────────────  ──────────   ──────────  ─────────
  1. Architecture sound?                 NO           NO          CONFIRMED — race conditions, no stage leases
  2. Test coverage sufficient?           NO           NO          CONFIRMED — music detection unvalidated,
                                                                  no LLM eval suite, race tests absent
  3. Performance risks addressed?        PARTIAL      PARTIAL     CONFIRMED — WAL checkpoint, MTD scan,
                                                                  faster-whisper throughput not benchmarked
  4. Security threats covered?           NO           NO          CONFIRMED — OAuth rotation, prompt injection,
                                                                  per-call caps, source_url validation
  5. Error paths handled?                NO           NO          CONFIRMED — yt-dlp partial, Whisper no-speech,
                                                                  ElevenLabs 429, Atlas response shape
  6. Deployment risk manageable?         NO           NO          CONFIRMED — manual TikTok load understated,
                                                                  no kill criteria, no rollback drill
═════════════════════════════════════════════════════════════════════════
6/6 CONFIRMED. Same verdict: SEND BACK.
```

### Architecture (ASCII dependency graph as actually wired)

```
            ┌─────────────────────────────────────────────────────────────────┐
            │                    External services                            │
            │  Twitch│YouTube│TikTok scrape │ Anthropic │ Coqui │ ElevenLabs   │
            │  yt-dlp│Whisper│ Atlas Cloud │  fal.ai  │ ffmpeg │ YT/IG APIs   │
            └──────────────────────────────┬──────────────────────────────────┘
                                           │
                                           ▼
   clips_candidate (status: discovered → curated → processing → ready/blocked/published)
                          │
   ┌──────────────────────┼──────────────────────┐
   │ Scout                │ Curator              │
   │ (race-prone)         │ (race-prone)         │
   └──────────────────────┴──────────────────────┘
                          │
                          ▼
   clip_artifacts (shared mutable row, written by Editor + Writer + Voice + Visuals + Compositor)
   • No artifact_version → torn writes possible
   • No stage lease → multiple workers can run same stage
   • has_music_in_source_segment  ← Editor sets (unvalidated detector)
   • has_real_face_reference      ← Visuals sets (not set today!)
                          │
   ┌──────────────────────┼──────────────────────┐
   │ Editor               │ Writer               │
   │ (yt-dlp + Whisper)   │ (LLM, prompt-inject  │
   │ no partial-DL check  │  via trending.md)    │
   └──────────────────────┴──────────────────────┘
                          │
                          ▼
   ┌──────────────────────┼──────────────────────┐
   │ Voice (TTS)          │ Visuals (Seedance)   │
   │ no 429 backoff       │ wrong response shape │
   │ no daily cost cap    │ check-then-spend race│
   └──────────────────────┴──────────────────────┘
                          │
                          ▼
                    Compositor (race-prone — same output path; no version pin)
                          │
                          ▼
                    Compliance gate (already wired and tested ✓)
                          │
                          ▼
                    Publisher → clips_ready (already atomic ✓)
                          │
                          ▼
                    Analyst → performance_metrics + learnings.jsonl
                          │
                          ▼
                    Optimizer → auto_changes + config/*.yaml (no write-scope whitelist)
                          │
                          ▼
                    `config/*.yaml` mutated → Publisher's lru_cache stale → torn schedule
```

### Eng findings (severity-ranked)

| ID | Severity | Finding | File:line | Fix |
|----|----------|---------|-----------|-----|
| E-1 | CRITICAL | No stage lease / artifact_version. Concurrent workers tear writes across `clip_artifacts` | `agents/editor.py:71`, `writer.py:192`, `voice.py:84`, `visuals.py:194`, `compositor.py:69` | Add `pipeline_runs` table or per-stage `stage`, `claimed_by`, `lease_expires_at`, `artifact_version` columns. Every transition: `WHERE clip_id=? AND status=? AND artifact_version=?` |
| E-2 | CRITICAL | Cost caps are check-then-spend, not reservations. Concurrent workers can all pass MTD then overspend | `agents/visuals.py:110-138` | Reserve in `costs` under `BEGIN IMMEDIATE` before external call; mark `pending/succeeded/failed`; enforce against reservations + actuals |
| E-3 | CRITICAL | Atlas Cloud response handler checks wrong key path. Will quarantine every successful generation | `agents/visuals.py:294-309` | Implement typed parser returning `submitted/processing/succeeded(url)/face_filter_rejected/rate_limited/provider_error`. Verify real response shape via Phase 0.7 docs |
| E-4 | CRITICAL | Curator has no atomic claim; concurrent runs double-promote | `agents/curator.py:66-103` | Wrap in `BEGIN IMMEDIATE` + conditional `UPDATE ... WHERE status='discovered'` |
| E-5 | CRITICAL | Scout idempotency key includes minute-precision timestamp → duplicates on retry / minute-boundary | `agents/scout.py:63-67` | Drop timestamp from dedupe key, or add `UNIQUE (creator, source_url)` constraint |
| E-6 | CRITICAL | Music detection has no validation harness or fixture; load-bearing for Compliance fail-closed posture | `docs/phase2_plan.md:61` "1.5d" lump sum | Build labeled dataset (50 clips ½ w/ music, ½ w/o), require ≥95% precision before shipping. Add to test suite. |
| E-7 | CRITICAL | Prompt injection: Writer ingests raw `data/trending.md` (scraped Reddit/KYM/X) directly into LLM prompt | `agents/writer.py:84,223,233` | Parse trends into structured records; strip non-ASCII control chars; quote as data, never instructions; eval suite for "ignore previous" attacks |
| E-8 | CRITICAL | yt-dlp PoToken/SABR ongoing burden understated 5-10x; no fallback when extractor breaks | `docs/phase2_plan.md:45,60` | Define supported sources + fallback acquisition path; pin yt-dlp version; doctor liveness check against known URL; auto-pause ingestion on extractor failure |
| E-9 | CRITICAL | Compositor races on same output path `OUTPUT_DIR/{clip_id}.mp4`; last-write-wins | `agents/compositor.py:69,93` | Compose to run-scoped temp path; atomic rename after ffmpeg success; claim before composition |
| E-10 | HIGH | Compositor accepts nondeterministic ffmpeg duration; >2s drift flips Compliance verdict | `agents/compositor.py:120-141` | Post-compose `ffprobe -show_entries format=duration`; assert \|measured-returned\| < 0.5s; quarantine on mismatch |
| E-11 | HIGH | OAuth refresh tokens in plain `.env`; no rotation cadence, scope docs, revocation drill | `.env.example`, plan §"Publisher" | macOS Keychain or `pass`-backed secrets; quarterly rotation; doctor check on token age; alert on `.env` mtime change |
| E-12 | HIGH | Anthropic API has no per-call ceiling; rewrite loop in Writer can burn budget in hours | `docs/phase2_plan.md:172`, `agents/writer.py:248-258` (TODO) | Per-clip token cap + max-iterations bound on rewrite loop + per-day cost cap (mirror Seedance `_month_to_date_*`) |
| E-13 | HIGH | Atlas Cloud has MTD cap but no per-day cap; single bad day burns $37/$50 line item | `agents/visuals.py:322`, `config/budget.yaml` | Add `daily_budget_usd` to budget.yaml + gate at top of `run_visuals` |
| E-14 | HIGH | `source_url` not validated at Scout insert; potential RCE if Phase 2 passes to yt-dlp via `shell=True` | `agents/scout.py:96-114` | Schema-validate `source_url` against allowed platform URL regexes at insert; never `shell=True` |
| E-15 | HIGH | Whisper no-speech path undefined; empty transcript yields placeholder punch segment + caption-less clip | `agents/editor.py:41-46`, `agents/compositor.py:104` | Quarantine on `len(transcript)==0 or sum(seg.duration)<5s`; Compliance check caption presence |
| E-16 | HIGH | yt-dlp partial download is unguarded; truncated mp4 drives downstream pipeline | `agents/editor.py:106-115` | Download to `.part`; `ffprobe` validate (duration, format, audio stream); atomic rename only on success |
| E-17 | HIGH | ElevenLabs 429 silent fallback to Coqui changes persona without logging the swap | `agents/voice.py:117-123` | Honor `Retry-After`; on 429 → fallback to Coqui + log persona swap in `feature_records` |
| E-18 | HIGH | `posting_schedule.yaml` mid-edit during Publisher run = partial YAML parse error kills loop | `agents/publisher.py:245`, `agents/config.py:14` lru_cache | Atomic config swap (temp-file + rename); try/except around `load()`; bust lru_cache per scheduler tick |
| E-19 | HIGH | Two Compositors targeting same `clip_id` race on file write + DB upsert | `agents/compositor.py:69-81,93` | Advisory lock (sentinel `compositor_lock_pid` column) or per-clip file lock |
| E-20 | HIGH | Optimizer write-scope undefined; can theoretically touch `persona.yaml.do_not` or `budget.yaml` caps | `CLAUDE.md:302-311`, `agents/optimizer.py:58` | Hard-code allowed `(file, key)` whitelist per `change_type`; CI test asserts Optimizer cannot mutate compliance/budget/credentials |
| E-21 | MEDIUM | Caption timing drift: Whisper word timestamps drift ±200ms; ffmpeg drawtext filter chain is 75+ items for 30s | `agents/compositor.py:105`, `CLAUDE.md:192` | Forced alignment pass (WhisperX); normalize times to final timeline; reject captions outside segment bounds; snapshot tests |
| E-22 | MEDIUM | LUFS-correct sidechain ducking is a 1-2d sub-project, not a bullet | `agents/compositor.py:41-43` | One audio pipeline: measure source → normalize voice → sidechaincompress source under voice → true-peak limit → verify integrated LUFS |
| E-23 | MEDIUM | SQLite WAL checkpoint unmanaged; can balloon under sustained writes + long Analyst reads | `data/schema.sql:5`, `agents/db.py:40` | `PRAGMA wal_autocheckpoint=200`; nightly `wal_checkpoint(TRUNCATE)`; SQLite online backup for offsite |
| E-24 | MEDIUM | `seedance_generations` MTD scan grows linearly each month; ~50ms tax by month 3 | `agents/visuals.py:110-138` | Composite index `(tier, status, ts)`; short-circuit cache MTD per `run_visuals` invocation |
| E-25 | MEDIUM | `@lru_cache` on config.load wedges Publisher to first-run YAML forever | `agents/config.py:14` | Cache by mtime; or bust cache per scheduler tick |
| E-26 | MEDIUM | Coqui XTTS-v2 first-call model download (~1.8GB) can wedge on partial download | plan §Voice | Download via checksum-verified curl; document prewarm path |
| E-27 | MEDIUM | faster-whisper M-series throughput not benchmarked; could exceed 30-min/day operator window | `docs/phase2_plan.md:57` | Benchmark on target hardware with real 30-60min VOD; pick `large-v3-turbo` or `medium`; add queue backpressure |

### Phase 3 test diagram → coverage matrix

| Codepath | Current coverage | Required test type | Status |
|----------|------------------|---------------------|--------|
| Compliance rules (all 10) | 40+ tests | Unit | ✅ |
| Publisher atomic claim + JOIN + tz | 10 tests (commit 0344a3e) | Unit + integration | ✅ |
| Writer hard-violation quarantine (placeholder) | 7 tests | Unit | ✅ |
| Doctor live-API + budget + strikes + warming | 16 tests | Unit (mocked) | ✅ |
| Curator hybrid heuristic+LLM | none | Unit + mocked LLM | ❌ |
| Editor punch-segment selection | none | Unit + sample transcript | ❌ |
| **Music detection** | **none** | **Validation harness (50+ labeled clips, ≥95% precision)** | **❌ CRITICAL** |
| Writer hard-violation on real LLM output | none | Unit + mock LLM with do_not hit | ❌ |
| Voice Coqui synthesis | none | Integration | ❌ |
| Voice ElevenLabs 429 fallback | none | Unit + mocked 429 | ❌ |
| Visuals face-filter rejection | none | Unit + Atlas fixture | ❌ |
| Visuals daily/MTD reservation | none | Unit + DB fixture | ❌ |
| Compositor ffmpeg integration | none | Integration + ffprobe verify | ❌ |
| Concurrent Curator runs | none | Threading test | ❌ |
| Concurrent Compositor on same clip | none | Threading test | ❌ |
| Publisher YAML mid-edit | none | Process + filesystem | ❌ |
| Analyst metrics pull | none | Mocked API responses | ❌ |
| Optimizer auto-rollback condition | none | Time-series fixture | ❌ |
| Optimizer config write-scope | none | Whitelist enforcement test | ❌ |
| **LLM eval suite** (drift detection) | **none** | **Golden-output regression** | **❌ CRITICAL** |
| Prompt injection on trending.md | none | Unit + adversarial fixtures | ❌ |

### Phase 3 test plan artifact

Test plan written to `~/.gstack/projects/amishra883-agentic-clipper/test-plan-phase2-{datetime}.md` (separate file, link below).

### Phase 3 Failure Modes Registry

| ID | Failure mode | Probability | Detection | Mitigation in plan |
|----|---|---|---|---|
| F-ENG-1 | Torn writes to clip_artifacts under concurrent stages | HIGH | data quality alarms post-hoc | NO — needs stage leases |
| F-ENG-2 | Atlas response misparse quarantines every generation | CERTAIN if response shape is nested | quarantine count explosion | NO — needs typed parser before Phase 2 |
| F-ENG-3 | yt-dlp broken by anti-bot update | HIGH (recurring) | doctor live-API ping | PARTIAL — proxy budget, no fallback source |
| F-ENG-4 | Music false-negative → Content ID claim | HIGH | platform claim notification | NO — no validation harness for detector |
| F-ENG-5 | Compositor race corrupts final video | LOW (today), MEDIUM under load | manual inspection | NO — needs file lock + atomic rename |
| F-ENG-6 | Prompt-injection in trending.md poisons script | MEDIUM | eval suite (doesn't exist) | NO — needs sanitization + eval |
| F-ENG-7 | Anthropic rewrite loop burns $40 budget in hours | MEDIUM | monthly burn check | PARTIAL — doctor only flags after the fact |
| F-ENG-8 | OAuth token rotation lapses; all 3 accounts compromised on single .env leak | LOW (today), HIGH on .env exposure | account ToS alert | NO — keep secrets in keychain |
| F-ENG-9 | TikTok manual queue exceeds 30min/day budget | HIGH | operator self-report | NO — no time tracking |
| F-ENG-10 | Optimizer auto-applies change on insufficient sample (n<500) | HIGH first 3mo | rollback rate | PARTIAL — auto-rollback exists; needs significance gate |

### Phase 3 Completion Summary

| | |
|---|---|
| Files actually read | 13 agent files + 3 docs + schema + tests/ |
| Concrete findings | 27 (10 critical, 11 high, 6 medium) |
| Architecture failure modes | 10 |
| Test coverage gaps | 18 codepaths uncovered (2 CRITICAL: music detection, LLM eval) |
| Security gaps | 7 (4 critical) |
| Hidden complexity revealed | 6 sub-projects, plan understates total work by 4-7 days |
| Dual voices | Both ran, both recommend SEND BACK |
| Final phase verdict | **BLOCKED on engineering hardening** (separate from premise gate) |

## Phase 3.5 — DX Review (Operator Surface)

### Step 0: Persona + initial DX completeness

**The "developer" is the operator** — a chief medical officer running this side-business with a 30-min/day attention budget. Primary daily surfaces: Makefile, doctor CLI output, `.env` / config files, daily digest, manual TikTok upload queue. They write code occasionally but operate the system daily.

Initial DX completeness rating: **3-4/10** based on first pass through `Makefile`, `scripts/doctor.py`, `runbook.md`. Strong in: `make help` segmentation, `_PHASE2_NOT_WIRED` pattern, `generate_avatar.py` idempotency model. Weak in: daily digest unspecified, TikTok flow needs interactive command, no migration mechanism, no `make morning`-style single entry point.

### Phase 3.5 dual voices

**CODEX SAYS (DX — developer experience challenge):** SEND BACK. "Phase 2 is written like an engineer's integration plan, not an operator system." Core DX issue is not one bad command — it is that the plan assumes the operator can mentally join state across Makefile targets, SQLite tables, drop folders, config files, and runbook prose. For a 30-min/day CMO, this is not viable. Phase 2 should not proceed until operator surface is first-class scope: `make morning`, `make tiktok-list`, real `make tiktok-confirm`, `make migrate`, config/env validation, and a digest schema before wiring more external APIs. Composite verdict: 2.9/10.

**CLAUDE SUBAGENT (DX — independent review):** SEND BACK with three named blockers. (1) Daily digest schema undefined despite 11+ mentions in spec — it IS the operator product. (2) Manual TikTok flow needs `make tiktok-flow` interactive command, not 90 invocations/month of `make tiktok-confirm CLIP_ID=... POST_ID=...`. (3) No schema migration mechanism (schema_version exists in schema.sql:293 but no `migrations/`, no `make migrate`, no applied-version ledger). Composite verdict: 3.6/10. Holds up `scripts/generate_avatar.py` as the gold standard the rest of the operator surface should aspire to (idempotency, dry-run, audit best-effort, post-run "next steps").

### Phase 3.5 DX consensus table

```
DX DUAL VOICES — CONSENSUS TABLE:
═══════════════════════════════════════════════════════════════════════════════════
  Dimension                              Claude       Codex      Consensus
  ─────────────────────────────────────  ──────────   ──────────  ─────────
  1. Getting started < 5 min (TTHW)?     NO (4-6h+)   NO (3-6h)   CONFIRMED — multi-hour realistic, multi-day with backup warming
  2. API/CLI naming guessable?           PARTIAL      PARTIAL     CONFIRMED — Makefile help good, but missing operator-grade targets
  3. Error messages actionable?          PARTIAL      PARTIAL     CONFIRMED — doctor budget burn is good model;
                                                                  agent NotImplementedError + YAML parse failure are not
  4. Docs findable & complete?           NO           NO          CONFIRMED — daily digest spec missing entirely
  5. Upgrade path safe?                  NO           NO          CONFIRMED — no migrations/ + no make migrate + no doctor delta
  6. Dev environment friction-free?      NO           NO          CONFIRMED — yt-dlp/ffmpeg/Coqui not in setup script;
                                                                  90 manual TikTok uploads/mo not ergonomized
═══════════════════════════════════════════════════════════════════════════════════
6/6 CONFIRMED — both voices identify the SAME three blockers (digest, TikTok flow, migration).
Single high-confidence verdict: SEND BACK.
```

### DX Scorecard

| # | Dimension | Claude | Codex | Avg |
|---|-----------|-------:|-------:|----:|
| 1 | Time-to-first-run | 3 | 2 | 2.5 |
| 2 | Operator error legibility | 6 | 4 | 5.0 |
| 3 | Makefile ergonomics | 7 | 5 | 6.0 |
| 4 | Daily digest design | 2 | 1 | **1.5** |
| 5 | Manual TikTok workflow | 3 | 3 | 3.0 |
| 6 | Configuration surface | 4 | 3 | 3.5 |
| 7 | Upgrade / migration story | 1 | 2 | **1.5** |
| 8 | Observability under failure | 3 | 3 | 3.0 |
| | **Composite** | **3.6** | **2.9** | **3.25 / 10** |

### Developer journey map (operator's day-1 through month-3)

| Stage | Today's experience | Friction | Fix |
|---|---|---|---|
| Discover | Read CLAUDE.md (468 lines) | High cognitive load; no "start here" landing | Add quickstart at top of README |
| Sign up | Visit 7 provider portals, copy keys | ~3hr; OAuth consent screens for 3 services | `make setup-keys` interactive walker |
| Install | yt-dlp + ffmpeg + Coqui XTTS-v2 (1.8GB) | Not in runbook; first-call model download wedge | `make setup` checksum-verified prewarm |
| Init DB | `make init-db` (works) | OK | — |
| Lock avatar | `make generate-avatar` (works) | Excellent example of operator UX | Hold as template |
| Verify health | `make doctor` (works) | Pull-only, four-place observability | `make morning` consolidated entry |
| First manual post | `make tiktok-confirm CLIP_ID= POST_ID=` (stub) | Phase 2 not wired; AirDrop unspecified | `make tiktok-flow` interactive |
| Daily run | `make morning` (doesn't exist) | Operator improvises queries against SQLite | Build this |
| Strike investigation | `sqlite3 data/main.db ...` | Operator needs SQL skill | `make strikes` |
| Schema upgrade | `git pull` → silent compliance fail | No migration mechanism | `make migrate` + doctor delta |

### Developer empathy narrative (first-person)

> Day 1, 7:30am. Coffee. I cloned the repo last night. Now I'm trying to follow `runbook.md` step 3b — Google Cloud Console, OAuth consent screen, "what's an authorized redirect URI?" I'm a CMO, not a software engineer. The runbook says I'll be done in "60 min" but I've been at this for two hours and I haven't even reached step 4. I switch to step 4 (Atlas Cloud) just to feel progress. That works. Step 6 generates the avatar — that one's *beautiful*, the script is friendly and even has a `--force` flag I didn't know I needed. If only the rest of the operator surface felt like this script.
>
> Two weeks later: my warm-backup accounts aren't actually warm yet. The runbook said "30 day warming protocol" but didn't sequence it Day 1 with everything else. The pipeline can't really run safely. I have one primary account on each platform with no failover. I'm posting 7 clips/day with no safety net.
>
> Month 2: I'm posting. TikTok is hell — every morning I `ls data/clips/output/manual_upload/tiktok/`, eyeball which clip is which, AirDrop the video to my phone (which fails twice this week because files are >200MB), open the caption.txt and hashtags.txt in separate windows, paste each into TikTok, remember to toggle the AI-content label, schedule, come back to the laptop, type `make tiktok-confirm CLIP_ID=2026-06-...-xyz POST_ID=...`. Three times a day. Some days I forget the AI label. I posted the same clip twice last Tuesday because I lost track.
>
> Month 3: yt-dlp broke. YouTube updated something. I don't know how to fix it. The doctor says "Atlas Cloud reachable", "YouTube Data API reachable" — all green. But Editor crashes on yt-dlp. There's no doctor check for that. I open Cursor and start reading `agents/editor.py`. I'm a CMO. It's Tuesday morning.

### TTHW assessment

| | |
|---|---|
| Today's TTHW (clone to `make doctor` all-green, realistic operator) | **3-6 hours over 2-3 days** |
| Plus backup-account warming to safe-to-publish | **+30 days** |
| Target | <60 min to first manual clip; <30 days to autonomous safety |
| Gap | ~3-5x longer than the operator's effective day-1 budget |

### DX Implementation Checklist (must land before Phase 2 wiring)

**Blockers (SEND BACK until these exist):**

- [ ] **Daily digest schema specified.** Concrete sections per `phase2_plan.md` review (alerts, manual queue, yesterday's publish, performance, budget, auto-changes, "what needs you"). `make digest` writes to `data/digest/YYYY-MM-DD.md` + prints to stdout.
- [ ] **`make morning` (or equivalent single entry point) built.** Subsumes `make digest`, `make tiktok-flow`, `make quarantine-report`, `make doctor --brief`. The 30-min/day operator surface.
- [ ] **`make tiktok-flow` interactive command.** Iterates queue, copies caption+hashtags to clipboard, opens video in Finder, prompts for POST_ID, forces AI-label confirmation for Seedance clips, detects duplicate posts.
- [ ] **Migration mechanism.** Add `migrations/` directory, numbered SQL files, `make migrate` target with backup-then-apply pattern, doctor check comparing `schema_version` table to expected version, downgrade documentation.

**Strong recommendations (should land):**

- [ ] `make setup` for yt-dlp/ffmpeg/Coqui prewarm (checksum-verified download)
- [ ] `make config-validate` with JSON Schema / pydantic validation
- [ ] `make quarantine-report` printing quarantined clips + reasons
- [ ] `make tiktok-list` showing pending queue with timestamps + slot windows
- [ ] `make strikes` with dispute templates per platform
- [ ] `make config-diff` showing Optimizer auto-changes since last review
- [ ] Per-line-item burn breakdown in doctor's monthly budget check
- [ ] YAML parse failures show offending line + context (not `str(exc)`)
- [ ] `Phase 2 NOT YET WIRED` Makefile messages link to `docs/phase2_plan.md` (not `runbook.md`)
- [ ] Backup-warming day-by-day sequencing in runbook Step 0

### DX magical moment: `make morning`

Both voices converged on the same proposal. From the Claude subagent's draft:

```
$ make morning
=== agentic-clipper · 2026-05-18 (Mon) 06:47 ET ===

HEALTH (last 5 checks)
  ✓ APIs reachable    ✓ budget headroom    ✓ schema v7
  ✓ 0 strikes         ✓ warm backups OK

WHAT NEEDS YOU TODAY (3 items, ~12 min)
  1. POST 3 TIKTOK CLIPS — press [enter] to start the upload flow
     a) 07:00 slot — Kai Cenat reaction (2026-05-17_a8b2.mp4)
        ↳ caption+tags copied to clipboard
        ↳ opening video.mp4 in Finder…
        ↳ REMEMBER: toggle "AI-generated content" ON
  2. ONE QUARANTINED CLIP — review reason
     [r] re-process  [s] skip  [v] view video  [q] keep quarantined
  3. AUTO-CHANGE APPLIED OVERNIGHT
     Optimizer shifted TikTok 12:00 → 12:23 (engagement +14% vs baseline)
     [u] undo  [k] keep  [d] details

YESTERDAY
  6 published · 1 quarantined · 0 strikes · $4.20 spent (MTD $87 / $200)
  Top clip: 2026-05-16_x1y9 — 47k views in 18h (above persona baseline)

[enter] to start manual upload flow, or [q] to quit
```

This is the operator's daily entry point. Build this and DX composite jumps from 3.25 to ~7.

### Phase 3.5 Completion Summary

| | |
|---|---|
| Operator persona | Solo CMO, 30-min/day attention budget, daily TikTok + weekly Optimizer review |
| DX composite | **3.25 / 10** (Claude 3.6, Codex 2.9) |
| Blockers identified | 4 (daily digest spec, `make morning`, `make tiktok-flow`, migration mechanism) |
| Strong recommendations | 9 (setup script, config-validate, quarantine-report, etc.) |
| TTHW today | 3-6 hours over 2-3 days, +30d backup warming |
| TTHW target | <60min to first manual clip; <30d to autonomous safety |
| Dual voices | Both ran, both recommend SEND BACK with same 4 blockers |
| Final phase verdict | **BLOCKED on operator surface design** |

---

## Phase 4 — Cross-phase synthesis

### Cross-phase themes (issues that surfaced in 2+ phases independently)

1. **THE OPERATOR'S TIME BUDGET IS FANTASY.** Surfaced in Phase 1 (P4 premise) and Phase 3.5 (Dim 5 + Dim 8). Both find: 30 min/day collides with 90 manual TikTok uploads/month, strike monitoring, weekly Optimizer review, quarterly clipper-program re-verification, doctor health checks. Realistic floor is 60-90 min/day for the first 3 months, settling to maybe 45min/day at steady state. The whole product premise rests on a budget the system as designed cannot honor.

2. **THE COMPLIANCE GATE IS A SHELL OVER UNTESTED EVIDENCE.** Surfaced in Phase 1 (P3 zero-strikes-vs-fair-use contradiction) and Phase 3 (E-6 music detection has no test, E-15 Whisper no-speech path is undefined, E-3 Atlas response handler is wrong). The gate code itself has 40+ passing tests, but the upstream evidence columns it consumes (`has_music_in_source_segment`, `has_real_face_reference`, voice_id whitelist) depend on stages that are either unwired (Editor music detection) or wrongly wired (Visuals face-filter response shape). Compliance shipping safely requires Phase 2's evidence-producers to be as rigorously tested as Compliance itself.

3. **THREE PROJECT DOCS DISAGREE.** Surfaced across all three phases. CLAUDE.md says "open clipper programs" are the wedge; fair_use_position.md says operator dropped them; exit_strategy.md says ToS violation. Each phase inherits the contradiction without resolving it. Until one strategic doc is normative, every plan downstream is internally inconsistent.

4. **THE PLAN ASSUMES OPERATOR INFRASTRUCTURE THAT DOESN'T EXIST.** Surfaced in Phase 3 (no migration mechanism, no config validation, no per-call cost cap on Anthropic) and Phase 3.5 (no `make morning`, no daily digest spec, no quarantine report). The plan is engineer-shaped, not operator-shaped. The 10-agent wiring layer is well-specified; the human interface layer is "assumed to exist."

### Pre-gate verification

**Phase 1 (CEO) outputs:**
- [x] Premise challenge (5 premises rated, all critical or high)
- [x] All review sections with findings or noted N/A
- [x] Failure Modes Registry (F-CEO-1 through F-CEO-7)
- [x] "NOT in scope" section
- [x] "What already exists" section (0B Existing code leverage map)
- [x] Dream state delta (0C)
- [x] Completion Summary
- [x] Dual voices ran (Codex + Claude subagent)
- [x] CEO consensus table (6/6 CONFIRMED)

**Phase 2 (Design):**
- [x] Skipped, no UI scope (noted)

**Phase 3 (Eng) outputs:**
- [x] Scope challenge with actual code analysis (6 hidden sub-projects identified)
- [x] Architecture ASCII diagram
- [x] Test diagram mapping codepaths to coverage (22-row matrix)
- [x] Test plan artifact written to `~/.gstack/projects/amishra883-agentic-clipper/test-plan-phase2-20260518.md`
- [x] "NOT in scope" section (in CEO phase, applies to Eng too)
- [x] "What already exists" section (in CEO phase)
- [x] Failure modes registry (F-ENG-1 through F-ENG-10)
- [x] Completion Summary
- [x] Dual voices ran
- [x] Eng consensus table (6/6 CONFIRMED)

**Phase 3.5 (DX) outputs:**
- [x] All 8 DX dimensions evaluated with scores
- [x] Developer journey map (10-stage table)
- [x] Developer empathy narrative
- [x] TTHW assessment (3-6 hrs + 30d warming vs <60min target)
- [x] DX Implementation Checklist (4 blockers + 9 recommendations)
- [x] Dual voices ran
- [x] DX consensus table (6/6 CONFIRMED)

**Cross-phase:**
- [x] Cross-phase themes section (4 themes)

**Audit trail:** see Decision Audit Trail below.

---

# Revised Phase 2 Plan (post-/autoplan, 2026-05-18)

**This section supersedes the original "Phase 2 Wiring Plan" at the top of the file.** The original is retained for diff/audit purposes. Operator approved "Revise with auto-decided fixes" at the Final Approval Gate; this revision incorporates all 27 Eng findings + 4 DX blockers + 4 cross-phase theme remediations.

**Top-level changes vs original:**
- Sizing: 10d → **16-18d** (closing the 4-7d understatement Eng review found)
- Structure: adds Day 0-2 **Pre-wiring blockers** before any agent work
- New explicit work blocks: **Operator surface** (make morning, tiktok-flow, migrations, digest), **Compliance evidence harnesses** (music detection, prompt-injection sanitization, LLM eval suite), **Cost reservation layer** across all paid-call agents, **Stage lease + artifact_version** schema migration before any stage transitions
- Premise contradictions (CEO P1-P5) NOT resolved by this revision — operator declined the rethink at the premise gate; revision proceeds with eyes open. Validation-pilot recommendation (User Challenge 1) recorded in TODOS as the gating safety net at Day 14 if early posts trip claims

## Day-by-day re-sizing

| Day | Block | Owner | Deliverable | Gates |
|-----|-------|-------|-------------|-------|
| 0 | Schema migration mechanism | infra | `migrations/`, `make migrate`, `schema_version` table with applied-ledger, doctor schema-delta check | Doctor: `make migrate` → green |
| 0 | Operator setup script | infra | `make setup` installs yt-dlp/ffmpeg/Coqui (checksum-verified), prewarms XTTS model | `make setup && make doctor` green |
| 1 | **Music-detection validation harness (E-6 BLOCKER)** | compliance | 50+ labeled clip fixtures (25 w/ music, 25 w/o); detector achieves ≥95% precision before Editor wiring | Validation set passes |
| 1 | **Prompt-injection sanitization layer (E-7 BLOCKER)** | compliance | `agents/trending_sanitizer.py` with adversarial test fixtures; structured parser extracts only `meme:`/`slang:`/`sound:` refs; strips control chars / "ignore previous" patterns | Adversarial fixtures blocked 100% |
| 1 | **LLM eval suite (E-loose BLOCKER)** | compliance | 20 golden Writer outputs; new persona prompt must match ≥18 within similarity threshold | Golden set in `tests/evals/` |
| 2 | Stage lease + artifact_version migration (E-1) | infra | New `pipeline_runs` table + `clip_artifacts.artifact_version`; every stage transition conditional on `WHERE clip_id=? AND status=? AND artifact_version=?` | All 6 writing stages updated |
| 2 | Pre-call cost reservation layer (E-2) | infra | `costs` row written under `BEGIN IMMEDIATE` before external call; marked `pending/succeeded/failed`; caps enforce against reservations+actuals | Visuals + Curator + Writer + Voice updated |
| 2 | **Daily digest schema + `make morning` (DX-1, DX-2 BLOCKER)** | operator | `make morning` is the single daily entry point; subsumes digest, tiktok queue, quarantine report, doctor; key-binding-inline action prompts | Operator can run one command for the full daily picture |
| 3-4 | Scout + Curator (with hardening) | pipeline | Scout idempotency without timestamp key (E-5); Curator atomic claim (E-4); source_url validation (E-14); typed retry/backoff | Concurrent-run threading tests pass |
| 5-6 | Editor (with hardening) | pipeline | yt-dlp partial-download validation via ffprobe (E-16); Whisper no-speech quarantine (E-15); music-detection populates `has_music_in_source_segment` from Day 1 harness; PoToken/SABR fallback acquisition (E-8) | Quarantine-report shows partial-DL and no-speech edge cases caught |
| 7 | Writer + LLM evals (with hardening) | pipeline | Anthropic per-clip token cap (E-12); rewrite-loop max-iterations bound; trending sanitizer enforced; LLM eval suite gates persona prompt changes | Eval suite passes; cost cap triggers under load test |
| 8 | Voice (with hardening) | pipeline | Coqui checksum-verified prewarm (E-26); ElevenLabs 429 + persona-swap logging (E-17); daily cost cap; benchmark on M-series CPU (E-27) | First synthesis on day-8; per-day-cost gate verified |
| 9-10 | Visuals (with hardening) | pipeline | **Typed Atlas response parser** (E-3, classifies submitted/processing/succeeded/face-filter/rate-limit/error); succeeded path writes `has_real_face_reference=0`; daily Atlas cap (E-13); MTD scan composite index (E-24) | Mocked-response test for each state passes; real call against Atlas Cloud succeeds end-to-end |
| 11-12 | Compositor (with hardening) | pipeline | Run-scoped temp path + atomic rename (E-9); per-clip advisory lock (E-19); post-compose ffprobe duration verification (E-10); WhisperX forced alignment for captions (E-21); sidechain LUFS ducking pipeline (E-22) | Two-compositor concurrency test passes; caption drift <50ms; LUFS measured -14 ±0.5 |
| 13 | Publisher live mode + atomic config (with hardening) | pipeline | YouTube/Instagram upload wiring; `make tiktok-flow` interactive command (DX-3); atomic YAML swap (E-18); `@lru_cache` mtime invalidation (E-25); per-day quota tracking | First live post; manual TikTok flow tested |
| 14 | **Validation gate: 30-clip pilot** | safety | Posts to single platform (Instagram Reels default — least claim-prone), measures Content ID claim rate + RPV + operator time. <2% claim rate AND >$0.001 RPV AND <45min/day → unblock Day 15 | Validation pilot passes |
| 15 | Analyst (with hardening) | pipeline | Metrics pull for YT + IG; OAuth token rotation drill documented (E-11); timestamp format standardized (one helper, ISO 8601 + offset everywhere) | First learnings.jsonl entries written |
| 16 | Optimizer with write-scope whitelist (E-20) | pipeline | Hard-coded `(file, key)` allowlist per `change_type`; CI test asserts Optimizer cannot mutate `persona.yaml.do_not` / `budget.yaml` caps / credentials; significance gate (n≥500 before applying); auto-rollback on >15% degradation over 72h | Whitelist enforcement test passes |
| 17 | SQLite WAL checkpoint policy (E-23) + offsite backup | infra | `PRAGMA wal_autocheckpoint=200`; nightly `wal_checkpoint(TRUNCATE)`; SQLite online backup API to S3 | WAL size stays <50MB under load; backup verified |
| 18 | Operator surface polish (DX recommendations) | operator | `make config-validate` (JSON Schema); `make quarantine-report`; `make tiktok-list`; `make strikes`; YAML parse error context; backup-warming day-by-day in runbook | Operator surface composite ≥7/10 |

## Pre-wiring blockers (Day 0-2, must all be green before Day 3)

These are NEW blocks that did not exist in the original plan. Each closes a critical gap surfaced by /autoplan.

### Block A: Schema migration mechanism (Day 0)
- `migrations/000_baseline.sql` → applied baseline (matches current `data/schema.sql`)
- `migrations/001_artifact_version.sql` → adds `clip_artifacts.artifact_version` + `pipeline_runs` table
- `make migrate` runs pending migrations in a transaction with `data/main.db.bak` first
- Doctor check: compares `schema_version.version` to latest `migrations/*.sql`; FAILS with the exact command to run
- Documented downgrade: each migration ships with rollback SQL

### Block B: Operator setup script (Day 0)
- `make setup` installs system dependencies (yt-dlp, ffmpeg, sqlite3) via Homebrew on macOS
- Downloads Coqui XTTS-v2 weights with SHA256 verification; idempotent on re-run
- Verifies Python deps from `requirements.txt`
- Walks operator through any missing `.env` vars interactively (one prompt per missing key, pointing to the provider portal)

### Block C: Compliance evidence harnesses (Day 1)
- Music detection: labeled fixture set under `tests/fixtures/music/` with `manifest.json` listing 50 clips × ground-truth; `tests/test_music_detection.py` requires ≥95% precision, ≥90% recall before merge
- Prompt-injection sanitizer: `agents/trending_sanitizer.py` produces structured `TrendingRefs` from raw scraped text; `tests/test_trending_sanitizer.py` covers adversarial fixtures (control chars, "ignore previous", URLs, HTML)
- LLM eval suite: `tests/evals/writer_persona/golden_*.json` — 20 golden outputs from the locked persona prompt; new prompts must match ≥18 within similarity threshold (sentence embeddings)

### Block D: Stage lease + artifact_version schema (Day 2)
- New `pipeline_runs` table: `clip_id`, `stage` (enum: editor/writer/voice/visuals/compositor), `claimed_by` (process PID + hostname), `lease_expires_at`, `attempt`, `input_artifact_version`, `output_artifact_version`
- Every stage's `connect()` block reads + locks via `BEGIN IMMEDIATE`, asserts current artifact_version matches expected, increments on success
- Janitor sweep: clears expired leases every 5 min; emits `stage_lease_expired` event

### Block E: Pre-call cost reservation layer (Day 2)
- `agents/costs.py.reserve(category, amount_usd) -> reservation_id` — writes a `pending` row under `BEGIN IMMEDIATE`, returns id
- `agents/costs.py.settle(reservation_id, actual_amount, status='succeeded'|'failed')` — finalizes
- All MTD aggregates sum `pending + succeeded` to prevent the check-then-spend race
- Anthropic, Atlas Cloud, fal.ai, ElevenLabs, proxy all route paid calls through this

### Block F: Operator daily surface (Day 2)
- `make morning` interactive command — see the magical-moment spec in the DX section above
- `make digest` — same data, non-interactive, writes to `data/digest/YYYY-MM-DD.md`
- `make tiktok-flow` — iterates the manual queue, clipboard copy, Finder open, AI-label confirmation, POST_ID prompt, duplicate detection
- Daily digest schema: `Alerts | Manual queue | Yesterday | Performance | Budget | Auto-changes | What needs you (max 3)`

## Cost ceiling additions (closes E-12, E-13, E-22)

Adds to `config/budget.yaml`:
```yaml
per_call_caps:
  anthropic_input_tokens_max: 8000        # per-clip Writer call
  anthropic_output_tokens_max: 2000
  rewrite_loop_max_iterations: 3          # Writer self-rewrite ceiling
  atlas_cloud_daily_usd_max: 5            # closes the per-day-cap gap
  elevenlabs_daily_usd_max: 1.50          # selective escalation hard daily ceiling
```

Doctor surfaces each as a separate check (similar to monthly budget burn).

## Test coverage required for merge (closes E-test-matrix)

73 existing tests + ≥26 new tests from the test plan artifact at `~/.gstack/projects/amishra883-agentic-clipper/test-plan-phase2-20260518.md`. Critical-priority tests block their corresponding agent's wiring.

## What this revision does NOT do

These are the deferred items. Operator decision 2026-05-18: accept risks, no manual pilot for UC1, defer the rest.

1. **Validation-first 14-day manual pilot BEFORE wiring** (UC1): operator decided not to run a pre-wiring manual pilot. UC1 is resolved by the **Day 14 in-pipeline validation gate** (single platform, 30 clips, <2% claim rate AND >$0.001 RPV AND <45min/day) which gates the multi-platform ramp. Validation arrives 13 days into wiring rather than before it; risk is recorded and accepted.
2. **B2B creator-permission reframe** (UC2): deferred. The plan ships the B2C ad-rev + account-sale model unchanged. Reopen as a TODO if the Day 14 gate fails or if B2C unit economics underperform after month 1.

**Premise contradictions (CP-3) — RESOLVED.** CLAUDE.md updated 2026-05-18 to align with `fair_use_position.md` and `exit_strategy.md`:
- P1: Clipper-program gating dropped from the Mission; fair-use-only is the legal posture
- P2: $500/mo target reframed as blended revenue (ad-rev + affiliate), not pure ad-rev
- P3: Claims vs strikes distinguished — zero strikes on primary, occasional claims expected
- P4: Attention budget revised to ≤45min/day steady-state, ≤60min/day first 30 days
- P5: Exit multiple revised to 3-6x conditional on ToS-tolerant buyer + clean strike record

Other PARTIAL items (E-8 yt-dlp fallback, E-11 OAuth Keychain, E-27 Whisper threshold) and sequencing concerns (V-1..V-4) deferred to engineer-at-execution-time.

## Cycle 2 verification (single-voice, bounded compression)

Independent verifier checked the revised plan against the 31 cycle-1 findings. Verdict:

```
CLOSED:  26 | PARTIAL: 4 | OPEN: 1 (expected) | NEW: 0
```

**PARTIAL items (acceptance criterion soft; logged as known risk, not blocking):**

| Finding | Issue | Recommended follow-up |
|---|---|---|
| E-8 yt-dlp fallback acquisition | "PoToken/SABR fallback" named but concrete source (Twitch Helix VOD? yt-dlp-impersonate?) unspecified; doctor liveness check + auto-pause behavior missing | Pick concrete fallback source before Day 5 |
| E-11 OAuth rotation | "Documented drill" is weaker than the moved-secrets-store (Keychain / `pass`) the finding required | Decide Keychain vs `pass` before any Day 15 OAuth wiring |
| E-27 faster-whisper M-series | Benchmark named but model-selection threshold (large-v3-turbo vs medium) and queue-backpressure trigger soft | Set the threshold from real benchmark numbers Day 8 |
| CP-1 operator time budget | Day 14 validation gate is fail-late detector, not Day 0 prevention | Track operator daily time in `events` table from Day 3; alert if >45min |

**Previously OPEN item — NOW RESOLVED (2026-05-18, post-cycle-2):**
- CP-3 three-doc premise contradictions. Operator chose to resolve after the cycle-2 verification. CLAUDE.md updated in the same change-set to align with `fair_use_position.md` (fair-use-only posture, clipper programs dropped) and `exit_strategy.md` (3-6x multiple, not 6-12x), plus a clarification distinguishing CLAIMS from STRIKES, plus realistic attention-budget revision (≤45min/day steady-state from the original ≤30min/day). Premises P1, P2, P3, P4, P5 all reconciled to a single normative framing across the three docs.

**NEW concerns surfaced by verification (not in cycle 1):**

| ID | Concern | Where it lands |
|----|---------|----------------|
| V-1 | Day 1 over-stuffed: 3 BLOCKER items (music harness + sanitizer + LLM eval suite) compressed into a single day | Realistic floor is Day 1-2 split |
| V-2 | Block C says "detector achieves ≥95% precision before Editor wiring" but the detector itself is in Day 5-6, not Day 1 | Either the detector is Day 1 (and Day 5-6 becomes wiring-only) or Block C is a fixtures-only milestone (and the ≥95% gate moves to Day 5-6) — pick one |
| V-3 | Day 14 validation gate uses Instagram Reels as default ("least claim-prone") but the post-pilot plan needs to ramp to 3 platforms in only Days 15-18 with no slot for the multi-platform ramp | Add a Day 19-20 multi-platform ramp, or accept that initial autonomous run stays IG-only for first 30 days |
| V-4 | Day 18 "operator surface polish" packs 6 separate make-targets into 1 day | Realistically 2 slip to Day 19-20 |

V-1 through V-4 are sequencing concerns, not architectural defects. Logged as known-risks; do not block approval. Engineer-at-execution-time can adjust the day budget when the load is visible.

**Verifier recommendation (and now applied):** Accept revised plan with the 4 PARTIALs and 4 sequencing concerns as known-risk. Third autoplan cycle has diminishing returns. Start Day 0 with these eight items tracked in TODOS.

## Decision Audit Trail

| # | Phase | Decision | Classification | Principle | Rationale | Rejected? |
|---|-------|----------|----------------|-----------|-----------|-----------|
| 1 | Setup | Run dual voices for every phase | Mechanical | P6 | Codex available; always include independent voice | No |
| 2 | Setup | Skip Phase 2 Design review | Mechanical | n/a | No UI scope detected; 7 grep matches were architectural false-positives | No |
| 3 | Phase 1 | Surface premise contradictions to operator | Required gate | n/a | Premises are the one user-facing decision per autoplan spec | No (deferred to user) |
| 4 | Phase 1 | Mode SELECTIVE EXPANSION → recommend SCOPE REDUCTION | Taste | P1, P2 | Both voices recommend reducing scope (drop Scout-auto/Optimizer/ElevenLabs from initial wiring) | Surface to operator at final gate |
| 5 | Phase 1 | UC1 (validation-first pilot) vs operator's 10-day wiring direction | User Challenge | n/a | Both models recommend changing direction | Deferred to operator final gate |
| 6 | Phase 1 | UC2 (B2B reframe) vs operator's B2C direction | User Challenge | n/a | Codex strongly recommends; subagent neutral | Deferred to operator final gate |
| 7 | Phase 3 | Write test plan artifact to disk | Mechanical | P1 | Skill requires it; landed at `~/.gstack/projects/.../test-plan-phase2-20260518.md` | No |
| 8 | Phase 3 | Add stage leases / artifact_version (E-1) | Auto-decided | P5 | Explicit over clever; race-conditions are silent corruption | Approved |
| 9 | Phase 3 | Pre-call cost reservations (E-2) | Auto-decided | P2 | Boil the lake; check-then-spend is the same anti-pattern as the Publisher race we already fixed | Approved |
| 10 | Phase 3 | Typed Atlas response parser (E-3) | Auto-decided | P5 | Wrong key path would quarantine every generation; not a taste call | Approved |
| 11 | Phase 3 | Music-detection validation harness (E-6) | Auto-decided | P1 | Load-bearing for Compliance fail-closed; cannot ship without it | Approved |
| 12 | Phase 3 | Prompt-injection sanitizer (E-7) | Auto-decided | P1 + P5 | Two known injection vectors live (trending.md, source_url); cannot ship without | Approved |
| 13 | Phase 3 | LLM eval suite (golden outputs) | Auto-decided | P1 | Phase 2 ships first real LLM; no drift detection = uncontrolled blast radius on any prompt change | Approved |
| 14 | Phase 3 | Defer Optimizer to Day 16 (after validation gate) | Auto-decided | P5 | 210 clips/mo is below significance; Optimizer on noise causes harm | Approved |
| 15 | Phase 3.5 | `make morning` consolidated entry point (DX-2) | Auto-decided | P5 + P1 | 30-min/day operator surface; both voices proposed same target | Approved |
| 16 | Phase 3.5 | Migration mechanism (DX-4) | Auto-decided | P5 | Schema is going to change; current state silently fails on older clones | Approved |
| 17 | Verification | Accept revised plan with 4 PARTIALs as known-risk | Taste | P6 + P3 | Diminishing returns from cycle 3; engineer-at-execution can adjust soft items | Approved |
| 18 | Verification | Log V-1 through V-4 sequencing concerns to TODOS | Auto-decided | P5 | Sequencing is execution-time concern, not design-time blocker | Approved |







