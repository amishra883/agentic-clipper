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
