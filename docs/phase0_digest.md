# Phase 0 — Human approval digest

> **Captured 2026-05-14. This is the single document the operator needs to read to decide whether to approve Phase 1 pipeline scaffolding.** Linked artifacts in `docs/` and `config/` carry the supporting detail.

## Operator decisions resolved 2026-05-14

| # | Red flag | Decision | Where written |
|---|---|---|---|
| 1 | Adin Ross drama conflict | **Override skip-drama default for Adin only**, with elevated Compliance scrutiny | `config/creators.yaml` (drama_override block) |
| — | Clipper-program engagement | **Do NOT enroll in any clipper program** — fair-use-only posture | `config/clipper_programs.yaml`, `config/creators.yaml`, `docs/fair_use_position.md` |
| — | Top-5 composition | **Keep current top-5** (re-rank candidate at next monthly recompute) | `config/creators.yaml` |
| 4 | Budget overage from backup warming | **Authorize bump to $240/mo** | (to land in `config/budget.yaml` during Phase 1) |
| 5 | Seedance MPA cease-and-desist | **Agree** — quarterly re-verify of commercial-use terms | `docs/seedance_access.md` (operator-confirmed note) |
| 6 | Whop brief capture | **Dropped** — no Whop engagement under fair-use-only posture | `config/clipper_programs.yaml` (informational only) |

Implications of the fair-use-only posture (load-bearing):

- **Compliance is now the SOLE legal defense.** There is no "we have a license" fallback. The Compliance gate must enforce every rule in `docs/fair_use_position.md` "Operational summary" without exception. If the gate cannot enforce a rule, the pipeline pauses publishing.
- **Strike-risk envelope is higher.** Backup-account warming becomes operationally required, not just defensive — see `docs/backup_warming.md`.
- **Sketch's economic concern dissolves.** No Whop dependency means his earnings come from ad-rev share on our channels like every other creator's clips; pilot-cadence approach is preserved for the Twitch-supply concern.

## Still-open items (need operator action, not decisions)

| Item | Owner | When |
|---|---|---|
| Rename repo `datasciencecoursera` → `agentic-clipper` via GitHub web UI; run `git remote set-url origin <new-url>` | operator | before Phase 1 commits |
| ~~Submit TikTok Content Posting API audit application~~ | ~~operator~~ | **Dropped 2026-05-14** — TikTok runs in manual mode (Publisher writes to drop dir; operator uploads via native app). Re-evaluate month 3. |
| Create Facebook Page (needed by IG Graph API linkage) | operator | Phase 1 Day 1 |
| Initialize Google Cloud project + YouTube Data API key | operator | Phase 1 Day 1 |
| Provision Atlas Cloud account + fund $50 starter balance + API key | operator | Phase 1 Day 1 |
| Resolve Red Flag 3 (Kai Cenat Twitch supply alert) | me, in Phase 1 | implements as Scout "no fresh source in N days" alert |
| Resolve TikTok/IG data gap before 2026-06-13 recompute | operator decision | approve Social Blade Premium ($4–10/mo) or Playwright scrape |


## TL;DR

Phase 0 finished. Five of seven sections returned actionable decisions; two surfaced material conflicts with the spec that the operator should resolve before code is written. **Recommended posture: do not start Phase 1 scaffolding yet — three of the red flags below want a 5-minute conversation first.**

The verified-open creator pool is exactly 5 (no elimination round needed), Atlas Cloud is the recommended Seedance provider at the spec's target price, and total Phase-0-projected monthly external spend lands at **~$226–$246/mo** versus the **$200/mo** cap — driven by backup-account warming infra not anticipated in the spec's budget table.

## What got built this turn

| Artifact | Path | Status |
|---|---|---|
| Spec (source of truth) | `CLAUDE.md` | committed |
| Repo overview | `README.md` | committed |
| Persona definition | `config/persona.yaml` | committed |
| Avatar reaction library | `config/avatar_reactions.yaml` | committed |
| Avatar approach + locked seed | `config/avatars/README.md` | committed |
| 0.1 Clipper programs | `config/clipper_programs.yaml` | committed |
| 0.2 Top-5 creator ranking | `config/creators.yaml` | committed |
| 0.3 Posting APIs | `docs/posting_apis.md` | committed |
| 0.4 Fair-use position | `docs/fair_use_position.md` | committed |
| 0.5 Exit strategy | `docs/exit_strategy.md` | committed |
| 0.6 Backup warming plan | `docs/backup_warming.md` | committed |
| 0.7 Seedance access path | `docs/seedance_access.md` | committed |
| **This digest** | `docs/phase0_digest.md` | **you are here** |

No agent code (`agents/`), no Makefile, no `.env.example`, no dependency installs — Phase 1 is gated on your approval of this document.

## 1. Top-5 creators (verified-open clipper programs only)

The Phase 0.1 pool of "verified-open" programs is exactly 5, so the ranking is a prioritization exercise, not a cut. Aggregate views are verified-only (unverified platform numbers excluded from totals). Full evidence trail in `config/creators.yaml`.

| Rank | Creator     | Aggregate (verified) | Primary platforms      | Program payout (per 0.1)             |
|------|-------------|----------------------|------------------------|--------------------------------------|
| 1    | IShowSpeed  | ~1.685B              | YouTube, Twitch        | Free Whop community; CPM unknown     |
| 2    | Kai Cenat   | ~230M                | YouTube Live, Twitch   | Whop Content Rewards, ~$2 / 1k views |
| 3    | Sketch      | ~17.7M               | YouTube, Twitch        | Whop, ~$0.35 / 1k views (low)        |
| 4    | Jynxzi      | ~6.95M               | Twitch, TikTok*        | Whop "Jynxziclips" community         |
| 5    | Adin Ross   | ~2.79M               | Kick (primary), YouTube| Discord-gated, $40–$50 / 100k views  |

*Jynxzi has 10.2M TikTok followers but TikTok 30d view aggregates are unverified for all 5 creators (see "data quality" below). Re-rank in the next monthly cycle is likely to move Jynxzi up.

**Operator decisions wanted on the creator pool — see Red Flags 1, 2, 3.**

## 2. Posting APIs

Full detail in `docs/posting_apis.md`.

| Platform        | API                  | Status                     | Lead time         | Notes                                                 |
|-----------------|----------------------|----------------------------|-------------------|-------------------------------------------------------|
| YouTube Shorts  | YouTube Data API v3  | available_with_approval    | days              | 10k-unit/day default quota = ~6 uploads/day; ample    |
| TikTok          | Content Posting API  | available_with_approval    | **4–8+ weeks**    | Audit-gated; sandbox is `SELF_ONLY` (5 users/24h)     |
| Instagram Reels | Graph API            | available_with_approval    | 1–3 weeks         | Needs Business/Creator IG + linked Facebook Page      |

**Worst-case wall-clock to all three publishing publicly: ~6–8 weeks**, paced by the TikTok audit. The Playwright + TikTok Studio Desktop fallback is documented but operators report 0-view suppression on automated desktop uploads.

**Critical Day-1 actions for the operator:**
1. Submit the TikTok Content Posting API audit application (long pole)
2. Create the Facebook Page that Instagram needs to link to
3. Initialize a Google Cloud project for the YouTube Data API key

## 3. Fair-use position

Full analysis in `docs/fair_use_position.md`. Summary:

The posture rests on **substantive transformative commentary** + enforced operational rules: source ≤30s, total ≤60s, commentary ≥50% of audio, ≥1 substance tag per script, no music passthrough, attribution in every description, AI-content label per platform, no AI voice cloning of source creator, no creator-face references to Seedance. Rules are mapped to enforcing agents (Compliance hard gate for the load-bearing ones).

Defensible against U.S. § 107 fair use post-*Warhol*. Weakest against platform-level Content ID / Rights Manager / TikTok copyright tools, which operate independently of the legal doctrine — expect occasional claims, dispute calmly. Re-evaluate quarterly; next review **2026-08-14**.

## 4. Exit strategy reality

Full detail in `docs/exit_strategy.md`. Bottom line:

Account transfer **violates the ToS of all three primary platforms** (YouTube, TikTok, Instagram). Marketplaces (FameSwap, Social Tradia, Empire Flippers) exist and operate in a gray zone; FameSwap is the only one that fits short-form-video bundles in practice. **Realistic valuation multiple is 3–6×**, not the 6–12× the spec hoped for. YouTube's October 2025 "Second Chance" reinstatement program **excludes copyright-terminated channels** — making the spec's zero-strike rule even more load-bearing.

Treat exit as plausible upside, not a planned guarantee. Re-evaluate at month 9 with fresh marketplace + ToS check.

## 5. Seedance 2.0 — chosen provider + cost projection

Full detail in `docs/seedance_access.md`.

- **Primary provider: Atlas Cloud** — $0.022/sec Fast, $0.247/sec Pro, "failed generations never billed", clear commercial-use rights. Hits the spec's target price exactly.
- **Fallback provider: fal.ai** — ByteDance's named enterprise partner with async webhooks, but Fast tier is **~$0.24/sec (≈10× Atlas)**. Failover cannot run at full volume — must cap to ≤2 clips/day or breach budget.
- **Real-face restriction:** **CONFIRMED** by Seedance documentation and third-party tests. Critical quirk: the API returns HTTP 200 with an empty body on face-filter rejection — the Visuals agent must explicitly treat "succeeded but no video_url" as a face-filter rejection.
- **Reference-image character lock:** natively supported (Omni Reference, up to 9 reference images + locked seed). Avatar consistency works out of the box.

**30-day cost projection** at planned volume (210 clips/mo × 10s avg Fast tier):

| Line item                          | Projected | Spec budget |
|------------------------------------|-----------|-------------|
| Seedance Fast (default)            | ~$46      | ~$50        |
| Seedance Pro (escalations)         | ~$20      | ~$20        |
| **Total Seedance**                 | **~$66**  | **~$70**    |

Virality-threshold rule for Pro promotion (encoded for Phase 1): `Curator score ≥ 0.85 AND projected views ≥ 50k AND hero shot in shot list AND month-to-date Pro spend ≤ $20`.

## 6. Avatar persona spec

- **Persona ID P-01 ("manic_reactor")** defined in `config/persona.yaml`.
- **Reaction library** of 9 cartoon-coded reactions (jaw-drop, exaggerated double-take, head-explosion stinger, comically-slow blink, dramatic point, faint-and-recover, deadpan-with-fire, spit-take, monocle-pop) defined in `config/avatar_reactions.yaml` with placeholder Seedance prompt templates.
- **Locked seed:** `8376739915435003287` (recorded in `config/avatars/README.md`).
- **Reference image: NOT YET GENERATED** — deferred until Atlas Cloud account exists. Generating it on a different model would cause visual drift when re-generated on Seedance later. Generation prompt is locked and ready in `config/avatars/README.md`.

**Action item:** generate `config/avatars/manic_reactor.jpg` during Phase 1's first Seedance call, commit, and we're brand-locked.

## Red flags that want operator decisions before Phase 1

### 🚩 1. Adin Ross conflicts with the "skip drama-heavy" default

His content includes gambling promos, political takes, and feud cycles — directly conflicts with the spec's "skip controversial / drama-heavy clips (default: skip)" override and may be Shorts/Reels monetization-ineligible. Options:
- (a) Drop him from the rotation (top-5 becomes top-4 until next monthly recompute)
- (b) Override the default — accept drama-coded clips for Adin only, with extra Compliance scrutiny
- (c) Include him but only for non-drama segments (gameplay, IRL non-political moments) — Curator filter required

### 🚩 2. Sketch's economics are weak

Reported clipper payout of ~$0.35/1k views combined with collapsed Twitch viewership (rank #1179) means his clips may not earn their pipeline cost. Options:
- (a) Pilot only — process at low cadence (1 clip/week), evaluate after month 1
- (b) Drop from rotation, monitor for program rate changes
- (c) Keep at full cadence — economics work if Shorts ad-rev share carries the unit economics independent of Whop payout

### 🚩 3. Kai Cenat's Twitch supply is currently zero

He had no streams in the last 30-day window. Pipeline depends entirely on YouTube Live VOD ingest for him this month. Add a Scout alert for "no fresh source in N days" so we don't silently drop creators when their supply dries up. **Recommend implementing in Phase 1.**

### 🚩 4. Backup-warming infra blows the budget

Phase 0.6 requires real-SIM MVNO ($18/mo for 6 lines), residential proxies (~$30/mo, fits existing line item), antidetect browser (free tier), and **$200–300 one-time for two used Android devices** for mobile-only TikTok flows. Ongoing infra is **~$60–80/mo** vs. the spec's $30/mo proxy line. Total monthly external spend after warming starts: **~$226–$246/mo** vs. the $200 cap. Options:
- (a) Authorize the budget bump to ~$240/mo (phone provisioning is a one-time setup cost amortized; ongoing settles closer to $230)
- (b) Trim somewhere else — e.g., delay ElevenLabs Creator (~$22/mo) until month 3
- (c) Defer backup warming — accept higher single-strike risk for the first 60 days while monetization ramps

### 🚩 5. ByteDance MPA cease-and-desist on Seedance 2.0

The MPA issued a cease-and-desist against ByteDance over Seedance 2.0 (copyright pushback from major studios). Commercial-use rights are clear today, but ToS could shift. **Action: re-verify Atlas Cloud's commercial-use terms before any spend, and add quarterly re-verify to the doctor health check.**

### 🚩 6. Whop campaign briefs are operator-must-fetch

Four of the five verified-open programs (Kai Cenat, IShowSpeed, Sketch, Jynxzi) live on Whop, which is Cloudflare-gated to automated fetching. The agent could not capture the per-campaign attribution / exclusivity / prohibited-content rules. **Action: operator should manually visit each Whop campaign page logged in and paste the brief into `config/clipper_programs.yaml` before the pipeline starts ingest on that creator.**

### 🚩 7. Repo rename — manual operator step

The plan called for renaming the repo from `datasciencecoursera` to `agentic-clipper`. The GitHub MCP scope provided to this session does **not** expose a rename tool (only create/fork/etc.). **Action: the operator should rename via the GitHub web UI (Settings → General → Rename) and run `git remote set-url origin <new-url>` locally. GitHub auto-redirects from the old name so existing clones keep working.**

## Data-quality caveats (read before trusting numbers)

- **Almost every WebFetch in this Phase 0 returned HTTP 403** (developer doc portals, Cloudflare-gated analytics aggregators, marketplace sites, ToS pages). Findings were corroborated through search-result snippets and secondary 2026 sources. **The operator should manually re-verify primary sources before committing real money or publishing any of these decisions externally.** Specifically:
  - Posting API quota numbers (re-check Google / TikTok / Meta dev portals)
  - ToS quotes for the exit-strategy doc (re-check on YouTube / TikTok / Instagram help centers)
  - Atlas Cloud's commercial-use language for Seedance (re-check before paid use)
  - Whop campaign briefs for the four affected creators
- **TikTok and Instagram 30d view aggregates are unverified for all 5 creators.** Social Blade Premium ($4–10/mo) or Playwright scraping would close this gap on the next monthly cycle. Decide before the 2026-06-13 recompute.

## What I'd do next if you approve

In rough order:
1. Operator: rename repo to `agentic-clipper`, run `git remote set-url`.
2. Operator: resolve Red Flags 1, 2, 4 (creator pool decisions + budget bump). I can write the resulting changes into `config/creators.yaml` and `config/budget.yaml` once you've decided.
3. Operator: submit TikTok API audit application + create Facebook Page + initialize Google Cloud project for YT API.
4. Operator: provision Atlas Cloud account, fund $50 starter balance, provision API key in `.env`.
5. **Phase 1 scaffolding** (waits for explicit approval):
   - `agents/` skeleton with the 11 agents from CLAUDE.md (Scout → Optimizer)
   - `config/budget.yaml`, `config/optimizer_bounds.yaml`, `config/posting_schedule.yaml`
   - SQLite schema
   - Compliance gate unit tests (write these FIRST — they encode the load-bearing rules)
   - `Makefile` with the commands from CLAUDE.md
   - `.env.example`, `requirements.txt`, `make doctor` health-check script
6. **First real generation pass:** generate the manic_reactor reference image on Atlas Cloud, commit, brand-lock.
7. **First end-to-end smoke test** on one IShowSpeed clip (highest-volume verified creator) before opening the firehose.

## Approval

If everything above looks right: reply with which Red Flags you've decided on and the budget posture, and I'll start Phase 1.

If anything is wrong: point at the section and I'll re-research / re-write before Phase 1 starts.
