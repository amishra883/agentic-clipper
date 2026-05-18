# CLAUDE.md

## Mission

Build an autonomous, agentic clipping operation that produces AI-narrated reaction/commentary short-form videos derived from the highest-aggregate-view creators across Twitch, YouTube, TikTok, Instagram (and Kick, if relevant). Distribute across YouTube Shorts, TikTok, and Instagram Reels. Monetize via platform ad rev share and affiliate links. Exit via account sale once metrics qualify.

**Legal posture (updated 2026-05-14):** This operation is fair-use-defended (see `docs/fair_use_position.md`), not licensed. The original Phase 0 mission gated creator selection on "open, publicly-documented clipper programs" — operator dropped this requirement on 2026-05-14 after finding that program coverage was too thin to be load-bearing. Creator selection is now by view-velocity + content type only. The legal shield is the fair-use defense + the Compliance gate's structural rules (≤30s source, ≥50% commentary, AI-content disclosure), not a creator license. This is a more contested legal posture; the Compliance gate carries the entire defense.

The system runs on a local server, orchestrated by Claude Code, with sub-agents handling scouting, editing, scripting, voicing, composition, compliance, publishing, and learning.

-----

## Operating principles

1. **Legality first, virality second.** Every output must pass a fair-use checklist or operate under a documented clipper-program license. Anything that fails goes to `/quarantine/`, not the upload queue.
1. **Transformative > derivative.** Commentary is the product; the source clip is reference material. Scripts must add analysis, humor, criticism, or context — never just narrate what's happening on screen.
1. **Cheap-first stack.** Self-host where it's free (Whisper, Coqui TTS, ffmpeg, SQLite). Pay only when an API meaningfully moves retention numbers.
1. **Agentic but auditable.** Every autonomous action logs to a SQLite event log with rationale. A daily digest summarizes what ran, what published, what failed, and what was skipped on legal grounds.
1. **Solo-operator-safe.** No action runs without circuit breakers — post quotas, cost caps, strike monitors. Default: at most one human approval slot per day.

-----

## Hard constraints

- Budget: **≤ $200/month** in external services (Claude Max 20x covers LLM usage from inside Claude Code sessions)
- Local server orchestration; cloud only for posting endpoints and offsite backup
- Solo operator. Attention budget: **≤45 min/day at steady state, ≤60 min/day during the first 30 days** (revised 2026-05-18 from the original ≤30 min/day; reality-checked against ~90 manual TikTok uploads/mo + weekly Optimizer review + strike monitoring)
- **Zero copyright STRIKES tolerated** on primary accounts (failover to backups on first claim). Distinct from CLAIMS — Content ID *claims* (revenue redirected) are expected at low rates and not failure events. A *strike* (policy violation) is.
- AI commentary must be **≥50% of audio runtime** per clip
- Source footage **≤30s** per clip; total output ≤60s for Shorts/Reels/TikTok
- Written description on every video discloses transformative commentary purpose and includes affiliate disclosure when applicable

-----

## Phase 0 — Research & validation (week 1, BEFORE writing pipeline code)

Claude Code must complete and produce artifacts for all of the following, then **stop and present findings for human approval** before scaffolding the pipeline.

### 0.1 ~~Verify open clipper programs~~ (HISTORICAL — operator dropped this requirement 2026-05-14)

This Phase 0 step was completed but its output is now historical. Operator's 2026-05-14 decision (`docs/fair_use_position.md:7`) moves the project to fair-use-only posture; clipper programs are no longer required for creator selection. The section is retained below for the audit trail.

Web-search and confirm **currently-active, publicly-documented** clipper agreements. Likely candidates to investigate (verify each — programs open and close frequently):

- Kai Cenat, IShowSpeed, Adin Ross, Sketch, Jynxzi, Plaqueboymax, Agent00, Stableronaldo, Duke Dennis, Fanum

For each verified program, capture:

- Program rules and allowed platforms
- Attribution / tagging requirements
- Exclusivity or non-compete clauses
- Revenue split or payout terms (if any)
- Prohibited content list
- Source URL and date captured

Output: `/config/clipper_programs.yaml`

### 0.2 Compute aggregate views and pick the top 5

Sum public view counts across Twitch (last 30d), YouTube (last 90d), TikTok (last 30d), Instagram (last 30d) for every creator with a verified open program. Rank by total. Pick the top 5. Cache rankings; re-run monthly.

Output: `/config/creators.yaml` (top 5 with view evidence, program terms, primary platforms)

### 0.3 Verify posting APIs

- **YouTube Data API v3** (Shorts upload): confirm current quota and any Shorts-specific endpoint requirements
- **TikTok Content Posting API**: requires approval — submit application day 1; document fallback plan (e.g., scheduled Playwright posting via TikTok Studio Desktop) if denied
- **Instagram Graph API** (Reels): requires Business/Creator account linked to a Facebook Page; document the linking process
- Note rate limits, quota costs per call, and any "draft only" restrictions

Output: `/docs/posting_apis.md`

### 0.4 Fair use self-assessment

Produce a written 4-factor analysis (purpose, nature, amount, market effect) describing how this operation will satisfy each factor, plus the specific operational rules that enforce it (length cap, commentary ratio, overlay requirements, etc.). Re-evaluate quarterly.

Output: `/docs/fair_use_position.md`

### 0.5 Exit strategy reality check

Research FameSwap, Social Tradia, Empire Flippers current rules and the YouTube, TikTok, Instagram ToS clauses on account transfer. **Flag clearly: account transfer violates the ToS of all three primary platforms. The exit is plausible but not guaranteed and carries termination risk for the buyer.**

Output: `/docs/exit_strategy.md`

### 0.6 Backup-account warming plan

Identify the 30+ day warming protocol for 2 backup accounts per platform. Schedule a content trickle for them now so they exist before the primary accounts need failover.

Output: `/docs/backup_warming.md`

### 0.7 Seedance 2.0 access path

ByteDance's Seedance 2.0 (released Feb 2026) is the primary generative-video engine for avatars, B-roll, and scene-matched graphics. Verify current access and price before committing:

- Compare current pricing across BytePlus ModelArk (official international), fal.ai, PiAPI, Atlas Cloud, Segmind, OpenRouter
- Default target: Seedance 2.0 **Fast tier** (~$0.022/sec) for the bulk of generations; **Pro tier** (~$0.15–$0.25/sec) reserved for clips passing a virality-projection threshold
- Confirm: real-human face restrictions on `first_frame_url` (this is a feature for us — it enforces our no-impersonation rule), commercial use rights for the chosen provider, failed-generation billing policy
- Free-tier evaluation: use BytePlus 2M free tokens or Dreamina daily credits to validate prompt fit before any spend

Output: `/docs/seedance_access.md` with chosen provider, API contract, fallback provider, and the virality-threshold rule that promotes a clip from Fast to Pro.

-----

## Architecture

### Tech stack

- **Orchestration**: Python 3.12, asyncio
- **LLM**: Anthropic Claude via Claude Code session (primary); Anthropic API for sub-agents that run outside a session (charged against the $200 budget buffer, not the Max plan)
- **Transcription**: `faster-whisper` (local, GPU if available, CPU fallback)
- **TTS**: Coqui XTTS-v2 (local, free) as default for narrator-style commentary; **ElevenLabs Creator tier (~$22/mo)** escalation only for clips projected to outperform a defined threshold
- **Generative video (avatars, scene-matched graphics, B-roll)**: **Seedance 2.0** via the provider chosen in Phase 0.7. Fast tier as default; Pro tier on threshold escalation. Native audio-video joint generation can replace TTS entirely for avatar-delivered intros and outros.
- **Video composition**: ffmpeg for cuts/overlays/captions; MoviePy for programmatic composition; word-level captions derived from Whisper, burned in
- **B-roll & stock fallback**: Pexels API and Pixabay (free tiers) when Seedance generation isn't justified by cost
- **Source ingestion**: yt-dlp for VODs/clips; Twitch Helix API for live clip discovery; YouTube Data API for trending; TikTok Creative Center for trending discovery
- **Database**: SQLite (single file, nightly backups to local + offsite S3-compatible)
- **Queue**: SQLite-backed task queue (no Redis at this scale)
- **Scheduler**: APScheduler or systemd timers
- **Posting**: official APIs preferred; Playwright fallback for TikTok if API access is denied
- **Affiliate link tracking**: short links via Cloudflare Worker + KV; attribution stored per clip

### Agent topology

Sub-agents run as Claude Code-invoked scripts or background jobs:

1. **Scout** — every 4h: pulls trending clips from Twitch Helix, YouTube, TikTok Creative Center; scores virality potential (heuristics + LLM judgment)
1. **Curator** — ranks Scout output by predicted view-to-effort ratio; picks top N for processing
1. **Editor** — downloads source via yt-dlp; identifies the 15–30s "punch" segment using Whisper transcript + LLM scene analysis
1. **Writer** — generates commentary script (Claude); enforces commentary-ratio and substance rules; structures hook → take → payoff. Also emits a **shot list** — a structured spec of visual moments the script implies (avatar reactions, scene-matched graphics, transition stingers).
1. **Voice** — runs TTS for narrator commentary; normalizes audio levels; selects persona
1. **Visuals** — consumes the shot list and generates Seedance 2.0 assets:
- **Avatar shots**: a consistent AI persona (locked via reference image stored in `/config/avatars/`) delivering reaction beats. Use Seedance's reference-image character lock to keep the avatar consistent across clips.
- **Scene-matched graphics**: short generative clips that visualize concepts the commentary references (e.g., commentary says "this is a 1-in-a-million play" → Visuals generates a stylized probability-graphic stinger).
- **Transitions and stingers**: 1–2s generative cuts between source segments.
- Cost gate: Visuals never exceeds the per-clip cost budget in `/config/budget.yaml`. Uses Fast tier by default; escalates to Pro only when Curator's virality projection passes threshold.
- **No real-face references.** Never feeds a creator's image into Seedance. Enforced by Compliance.
1. **Compositor** — assembles final video: source segment with optional zoom/overlay, commentary audio mixed over ducked source audio, generated avatar inserts at marked beats, generative B-roll/stinger cutaways, burned word-level captions, intro/outro
1. **Compliance** — hard gate. Blocks any clip failing: source ≤30s, commentary ≥50%, no detectable copyrighted music in source segment, no real-face Seedance references, attribution present in description, fair-use checklist passed
1. **Publisher** — uploads per platform with platform-specific captions, hashtags, titles, thumbnails (where applicable)
1. **Analyst** — daily: pulls performance metrics; attributes results to variables (creator, topic, hook style, time-of-day, hashtags, avatar variant, graphic density, etc.); appends findings to `/data/learnings.jsonl`
1. **Optimizer** — weekly: reads learnings; proposes config changes; auto-applies low-risk changes within bounds (see "Self-improvement loop" below), surfaces higher-risk diffs in `/proposals/` for human approval

### Data flow

```
Twitch / YT / TikTok / IG APIs
            │
        Scout (4h)
            │
     Curator queue (SQLite: clips_candidate)
            │
   Editor → Writer ──┬──► Voice (commentary audio)
                     │
                     └──► Visuals (Seedance avatars + scene graphics)
                              │
                          Compositor (merges source + voice + visuals)
                              │
                     Compliance gate ──fail──► /quarantine/
                              │ pass
                     Publisher queue (clips_ready)
                              │
                  Scheduled post (per-platform optimal time)
                              │
                          Analyst (daily)
                              │
                         Optimizer (continuous + weekly)
```

-----

## Posting cadence (initial defaults; Optimizer adjusts)

Defaults based on current short-form best practices; Phase 0 should re-verify and Optimizer refines from real data.

- **TikTok**: 3 posts/day at roughly 7am, 12pm, 8pm in the target audience timezone. Test a 4-post day on weekends.
- **Instagram Reels**: 2 posts/day at roughly 11am, 7pm.
- **YouTube Shorts**: 2 posts/day at roughly 12pm, 9pm.

Total: ~7 clips/day across platforms. The same clip is re-formatted per platform (aspect ratio, caption density, hashtag set) rather than producing entirely separate pipelines.

Target audience timezone default: **US Eastern**. Override in `/config/posting_schedule.yaml`.

-----

## Commentary style guidelines

- **Hook in the first 1.5 seconds** — question, claim, or stake. Never "today we're looking at…"
- **Voice persona** defined in `/config/persona.yaml`. Default persona: **"manic reactor"** — high-energy, over-the-top, slapstick-leaning, fast cadence, exaggerated highs and lows. Secondary persona introduced after day 30 for A/B testing (test a contrasting tone like "unhinged hype-man" or "chaotic gremlin," not deadpan).
- **Humor profile (enforced by Writer)**:
  - **Over-the-top, never flat.** Stakes are always inflated — a mediocre play is "the worst decision in human history," a good play is "career-defining." Energy is the floor, not the ceiling.
  - **Slapstick-coded language.** Sound-effect words ("BOOM," "SPLAT," "WHOMP"), physical-comedy verbs ("absolutely launched," "got dummied," "ate dirt"), reaction interjections ("NO WAY," "BRO," "I'M CRYING").
  - **Trending references required.** Every script must include at least one currently-trending meme format, slang term, sound reference, or running joke from `/data/trending.md` (refreshed daily — see "Trending intake" below). If `/data/trending.md` is stale (>48h old), Writer blocks and triggers a refresh.
  - **Punch density.** At least one quotable beat (joke, hot take, or absurd comparison) every 8–10 seconds. The Writer scores its own output on punch density and rewrites if it falls below threshold.
- **Substance still required.** Even with slapstick energy, every script must contain at least one of — prediction, comparison, counter-take, contextual fact, character analysis, pattern observation. Funny ≠ empty. Substance is what keeps the clip fair-use-defensible; humor is what makes it spread.
- **Length**: target 45–55s; never exceed 58s (algorithm cutoff sensitivities).
- **Captions**: word-level, burned-in, high-contrast, never covering subjects' faces. Use animated emphasis (scale-pop, color-shift, shake) on punch words — captions are part of the humor, not just accessibility.
- **Never AI-clone a creator's voice** or impersonate them. Use neutral AI personas only.
- **Lines to not cross.** Over-the-top about the play, the moment, the absurdity — never about a creator's race, sexuality, mental health, family, or appearance. Roast the game; never roast the person in a way that could be defamation or harassment. Writer prompt enforces this hard.

-----

## Trending intake

"Trending" is a moving target, so the system actively refreshes its sense of what's currently funny.

### Sources (Scout extension, runs every 12h)

- **TikTok Creative Center**: trending sounds, hashtags, effects, and creator-spotlight memes
- **YouTube Shorts trending tab** + Google Trends rising queries (entertainment + gaming verticals)
- **Twitter/X trending topics** filtered to the gaming/streamer cluster
- **Reddit**: top-of-week from `r/LivestreamFail`, `r/Kenji`, `r/Twitch`, plus the named creators' subreddits
- **Twitch clip leaderboards** (most-viewed clips of the week per creator and overall)
- **Know Your Meme** recent entries (sanity check on whether something is rising or already cringe-cooked)

### Output

- `/data/trending.md` — a Claude-summarized, dated, ranked list:
  - **Hot** (peaking, use freely)
  - **Rising** (use sparingly, may peak this week)
  - **Cooked** (overused, avoid — drives algorithmic suppression)
  - Slang glossary with usage examples
  - Sound IDs (TikTok-specific) with the emotion they convey
- `/data/trending.md` is regenerated every 12h. The Writer and Visuals agents both consume it.

### Decay

- Items move from Hot → Cooked automatically if the trend's velocity inverts (declining views/uses across two consecutive intakes).
- The Optimizer also tracks per-trend performance from learnings — a trend that consistently underperforms our average gets demoted regardless of platform-wide signal.

-----

## Scene-aware visuals (Seedance 2.0)

The Visuals agent operates on the **shot list** produced by the Writer. Every shot is one of these types:

| Type                  | Purpose                                                                                                                  | Length | Tier |
|-----------------------|--------------------------------------------------------------------------------------------------------------------------|--------|------|
| **Avatar reaction**   | Persona delivers a reaction beat (laugh, eyebrow raise, point) over commentary                                           | 2–4s   | Fast |
| **Concept graphic**   | Visualizes a noun the script references ("the chair flew across the room" → generated stinger of a stylized chair flying)| 1–3s   | Fast |
| **Stat card**         | Animated overlay graphic with the number the script just spoke                                                           | 1–2s   | Fast |
| **Transition stinger**| Glitch / zoom / whoosh between source segments                                                                           | 0.5–1s | Fast |
| **Hero shot**         | Cinematic generative clip used for the hook or close — only on Curator's high-virality picks                             | 4–6s   | Pro  |

### Avatar consistency

- One canonical avatar per persona, defined by a locked **reference image** in `/config/avatars/{persona}.png` and a locked **seed**.
- Every avatar shot uses the reference + seed to keep face/style stable across clips.
- The reference image is AI-generated and clearly non-human-real (the goal is a recognizable mascot, not a deepfake). Documented in `/config/avatars/README.md`.
- Avatars must never reference, resemble, or be derived from a real creator's likeness.

### Avatar performance style

- **Expressive cartoon-coded design.** The default avatar reads as a stylized character (think Saturday-morning cartoon or modern animated short), not a photoreal person. This makes exaggerated faces and movement land instead of looking uncanny.
- **Slapstick reaction vocabulary.** The Writer's shot list specifies reactions from a fixed library that match the humor profile: jaw-drop, exaggerated double-take, head-explosion stinger, comically-slow blink, dramatic point, faint-and-recover, "this is fine" deadpan-with-fire (used sparingly), spit-take, monocle-pop. Each maps to a tested Seedance prompt template in `/config/avatar_reactions.yaml`.
- **Energy floor.** Avatar reactions are always dialed past 100%. A neutral or slightly-amused face is never an acceptable output — Compliance flags it and Visuals re-generates.
- **Sync to punch beats.** Avatar shots are placed by the Compositor on the punch words/moments identified by the Writer, not at fixed intervals. The reaction lands with the joke.

### Generation strategy (cost-aware)

- Default: ≤15s of total Seedance-generated content per output clip, on Fast tier
- Cache aggressively: identical concept graphics and stingers are stored in `/data/generated_cache/` and reused. The Visuals agent checks the cache before generating new assets.
- Budget guard: per-clip Seedance cost cap defined in `/config/budget.yaml`. The agent fails the clip (sends to /quarantine/) if cost exceeds cap rather than skipping visuals silently.
- Pro-tier escalation: only when Curator's virality projection score exceeds the threshold in `/config/budget.yaml`. Logged with rationale.

### Compliance for generated visuals

- No real human faces as `first_frame_url` or in reference images (Seedance enforces this too; we double-check).
- No likeness or voice imitation of the source creator.
- Generated content checked for hate/violence/sexual content via a lightweight classifier before composition.
- All generated assets retain provenance metadata in the database (model version, prompt, seed, cost, timestamp) for audit.

-----

## Self-improvement loop

The system learns from itself continuously. Two cadences:

### Continuous (per-clip)

Every clip carries a structured **feature record** when published:

```yaml
clip_id: 2026-05-13-0742
creator: <name>
source_platform: twitch
target_platform: tiktok
hook_template_id: HT-04
persona_id: P-01
avatar_variant: A-02
avatar_reactions_used: [jaw-drop, monocle-pop, spit-take]
script_substance_tags: [counter-take, prediction]
trending_refs_used: [meme:XYZ, slang:bussin, sound:tt_487291]
trending_freshness: hot   # hot | rising | cooked
punch_density: 0.18      # punches per second
seedance_seconds_used: 9
seedance_tier: fast
b_roll_density: 0.4
caption_style: pop-bold-yellow
posted_at: 2026-05-13T20:00:00-04:00
hashtags: [...]
```

After 48h, the Analyst joins this record with platform metrics (views, watch time %, likes, shares, follows, click-throughs). Records are appended to `/data/learnings.jsonl`. The Optimizer specifically tracks performance-per-trend-reference and performance-per-reaction-type so the system learns which jokes and which physical bits are actually moving numbers.

### Continuous (auto-tuning, low-risk)

The Optimizer is allowed to **auto-apply** these changes without human review, bounded by guardrails in `/config/optimizer_bounds.yaml`:

- Posting time shifts (±60 min from current schedule, based on engagement-vs-hour regression)
- Hashtag set rotations (from a pre-approved bank)
- Caption style (from the approved style bank)
- B-roll density (within 0.2–0.6 range)
- Hook template weighting (existing templates only, no new templates)

Every auto-applied change writes a row to `/data/auto_changes.jsonl` and is summarized in the daily digest. If a change underperforms baseline by >15% over a 72h window, it's auto-rolled back.

### Weekly (human-reviewed, higher-risk)

Changes the Optimizer cannot apply alone, surfaced as diffs in `/proposals/`:

- New persona introduction
- New hook template
- New creator added to or removed from the top-5 rotation
- Seedance tier-escalation threshold change
- Budget allocation shifts
- Compliance rule changes (**never auto-applied**, always reviewed)

### Experiments

The Optimizer can open **bandit-style experiments**: e.g., split traffic across two hook templates for 50 clips each, then promote the winner. Active experiments live in `/data/experiments/` with start state, current state, and termination conditions. Max 3 concurrent experiments to keep variance interpretable.

### Knowledge persistence

- `/data/learnings.jsonl` — append-only fact log (the source of truth)
- `/data/playbook.md` — Optimizer-maintained summary of what works, regenerated weekly from learnings. Read by the Writer and Visuals agents to bias their generations toward winning patterns.
- `/data/anti-patterns.md` — Optimizer-maintained list of what consistently flopped. Writer and Visuals avoid these explicitly.

-----

## Legal & risk register

**Read this section in full. Everything else assumes these risks are accepted.**

- **AI commentary is not automatically fair use.** Courts and platforms evaluate the *substance* of commentary. Generic AI narration over footage has been claimed and removed at scale. Mitigation: enforce substantive-commentary rules in Compliance; keep clips short; overlay original visuals; document transformative intent per clip.
- **Clipper programs are revocable.** Re-verify monthly; cache terms with timestamps; pause a creator immediately if their program closes.
- **Platform ToS supersedes fair use within platforms.** YouTube Content ID, Meta Rights Manager, and TikTok's copyright tools apply independently of legal fair use. Expect occasional claims. Have a dispute template; do not dispute frivolously — disputes can escalate strikes.
- **Account sale violates ToS** of YouTube, TikTok, and Instagram. Marketplaces exist but operate in a gray zone. A single strike kills resale value. Treat exit as a plausible upside, not a planned guarantee.
- **Music in source clips = instant Content ID claim.** Compliance must detect and mute or replace music segments.
- **Likeness/voice rights.** Some jurisdictions (CA, NY, TN, others) restrict commercial use of a person's likeness or voice. AI-cloning a creator's voice is off-limits, full stop.
- **Defamation.** Critical commentary must be opinion-framed or fact-based. Writer prompt must enforce this. No accusations of crimes; no false statements of fact.
- **FTC affiliate disclosure.** Every video and description with affiliate links must include "#ad" or an equivalent disclosure.
- **Backup accounts.** Maintain 2 warm backup accounts per platform, aged ≥30 days with native content before any failover.
- **AI-generated visuals must disclose.** TikTok, YouTube, and Instagram each require labeling of AI-generated/synthetic content in some contexts. Publisher applies the platform-specific AI-content label on every clip that includes Seedance assets.
- **Seedance ToS.** Provider terms can change; re-verify commercial-use rights quarterly. Failed-generation billing varies by provider — log every failure and dispute charges where the provider's policy allows.
- **Cost runaway.** Budget guard with a hard kill switch on any month >$200 external spend. Optimizer cannot increase paid-tier usage without explicit human approval.

-----

## Budget allocation (target $200/mo)

| Item                                                           | Monthly   | Notes                                       |
|----------------------------------------------------------------|-----------|---------------------------------------------|
| Seedance 2.0 Fast tier (default visuals)                       | ~$50      | ≈10s/clip × 7 clips/day × 30d at ~$0.022/sec|
| Seedance 2.0 Pro tier (selective escalations)                  | ~$20      | High-virality projection clips only         |
| ElevenLabs Creator (selective)                                 | ~$22      | Cap'd; default TTS is local Coqui           |
| Anthropic API buffer (sub-agents outside Claude Code sessions) | ~$40      |                                             |
| Proxy pool (if needed for scraping)                            | ~$30      |                                             |
| Storage (local + cheap S3-compatible offsite backup)           | ~$10      |                                             |
| Domain + Cloudflare (affiliate tracker)                        | ~$2       |                                             |
| Pexels / Pixabay                                               | $0        | Used when Seedance not cost-justified       |
| Reserve                                                        | ~$26      |                                             |
| **Total**                                                      | **~$200** |                                             |

A monthly cost report runs on the 1st. If projected month-end exceeds $200, the Optimizer auto-pauses paid-tier features in this priority order: Seedance Pro escalations → ElevenLabs → Seedance Fast (cap reduced) → Anthropic API overflow (force back into Claude Code session). Surfaces in the daily digest.

-----

## Affiliate strategy

- **Apply to**: Amazon Associates, Impact, ShareASale, gaming peripherals direct programs (Razer, SteelSeries, HyperX). Many require existing audience — re-apply at 1k / 5k / 10k follower thresholds.
- **Link insertion**: only when relevant to the clipped content. Don't paste generic affiliate sprays — algorithmic and audience-trust cost is real.
- **Tracking**: short links via Cloudflare Worker + KV; attribution stored per clip and surfaced in the daily digest.

-----

## Success metrics & exit

- **Month 1**: pipeline produces ≥5 clips/day with zero strikes. At least 1 video crosses 100k views.
- **Month 3**: one platform monetized (YPP eligibility, TikTok Creativity Program eligibility, or equivalent). Affiliate applications opened.
- **Month 6**: two platforms monetized. ≥$500/mo **blended revenue (ad-rev + affiliate + creator-fund where applicable)**. Net-positive operation. Note: pure Shorts ad-rev at ~$0.04 RPM cannot reach $500/mo at solo-operator scale; the $500 target is realistic only as a blended figure where affiliate revenue carries the majority. Track per-revenue-source breakdown.
- **Month 9–12**: account-sale candidacy. Bundled valuation across YT + TikTok + IG. Target multiple: **3–6x monthly profit** on private marketplaces (revised 2026-05-18 from the original 6–12x after `docs/exit_strategy.md:7` documented that account transfer violates ToS of all three platforms — realistic multiples sit lower because the buyer pool is narrower and AI-content disclosure shrinks it further). Conditional on: ToS-tolerant buyer, clean strike record, marketplace policy unchanged at sale time.

-----

## What Claude Code should do first

1. Read this file end-to-end.
1. Execute **Phase 0** (sections 0.1–0.7).
1. Stop and present a single human digest with:
- Verified top-5 creators with sourced view counts
- Confirmed posting API availability + any gaps
- Fair-use self-assessment summary
- Exit-strategy reality summary
- **Chosen Seedance 2.0 provider with current pricing and a 30-day cost projection**
- **Initial avatar persona spec (reference image generated, seed locked)**
- Any red flags from research that should change this spec
1. **Wait for human approval before scaffolding pipeline code.**

-----

## Target repo layout

```
/agents/         scout.py, curator.py, editor.py, writer.py, voice.py,
                 visuals.py, compositor.py, compliance.py, publisher.py,
                 analyst.py, optimizer.py
/config/         creators.yaml, persona.yaml, posting_schedule.yaml,
                 clipper_programs.yaml, budget.yaml, optimizer_bounds.yaml,
                 avatar_reactions.yaml,
                 /avatars/ (locked reference images + seeds)
/docs/           fair_use_position.md, posting_apis.md, exit_strategy.md,
                 backup_warming.md, seedance_access.md, runbook.md
/data/           main.db (SQLite), learnings.jsonl, auto_changes.jsonl,
                 playbook.md, anti-patterns.md, trending.md,
                 /clips/raw, /clips/output, /generated_cache, /experiments, /quarantine
/proposals/      Optimizer's weekly suggested diffs (human-reviewed before merge)
/scripts/        one-offs, backups, cost monitor, doctor
/tests/          compliance gate unit tests, end-to-end smoke tests
.env.example
README.md
CLAUDE.md        this file
```

-----

## Commands Claude Code should expose

- `make phase0` — run all Phase 0 research and produce digest
- `make scout` — run Scout once
- `make trending` — refresh `/data/trending.md` from all trending sources
- `make process N=5` — run the full pipeline on the next N candidates
- `make visuals CLIP_ID=<id>` — re-run only the Visuals stage for a clip (useful for iterating on avatars/graphics without regenerating audio)
- `make publish` — flush the ready queue, respecting schedule
- `make digest` — produce today's human digest (cost, posts, performance, alerts, auto-changes applied)
- `make optimize` — run Optimizer manually
- `make experiment NAME=<name>` — open a new bandit experiment from the experiments registry
- `make rollback CHANGE_ID=<id>` — manually roll back an auto-applied Optimizer change
- `make doctor` — health-check APIs, budget burn, strike count, account warming status, Seedance provider availability

-----

## Open questions for the operator (defaults applied)

The spec runs on defaults. Override in `/config/` as needed:

- Target audience timezone (default: US Eastern)
- Tolerance for controversial / drama-heavy clips (default: skip)
- Preferred commentary persona (default: deadpan analyst)
- Backup accounts already created (default: assume no — Phase 0 produces warming plan)
- Kick included in scouting (default: no, start with the four named platforms; add later if a top-5 creator's open program covers it)

-----

## Final note to Claude Code

This project sits in a real legal gray zone even with the AI-commentary fair-use posture. The defaults in this file are tuned to minimize risk, not eliminate it. If at any point during execution you find:

- A creator's program terms conflict with this spec
- A platform API change makes a compliance rule impossible
- A budget or strike threshold is about to breach

**Stop and surface it in the daily digest rather than working around it.** The operator would rather know than discover the workaround later.
