# Day-1 operator runbook

> Per CLAUDE.md target layout. This document is the operator's hands-on
> playbook for the manual steps Phase 1 cannot automate (account creation,
> identity verification, payment). Every section is pre-filled — you should
> not have to author new prose, only paste-and-confirm.
>
> Sequence matters. The TikTok audit is the long pole (4–8 weeks) — submit
> step 2 today, then proceed with the others in parallel. None of these
> blocks the codebase scaffold; they unblock Phase 2 live wiring.

---

## 1. Rename repo (~2 min) — *currently still `datasciencecoursera`*

1. Go to **https://github.com/amishra883/datasciencecoursera/settings**
2. In the **Repository name** field, change `datasciencecoursera` → `agentic-clipper`
3. Click **Rename**.
4. Update your local clone:
   ```bash
   cd /home/user/datasciencecoursera   # current path
   git remote set-url origin git@github.com:amishra883/agentic-clipper.git
   git remote -v   # verify
   # Optionally rename the local working directory too:
   cd ..
   mv datasciencecoursera agentic-clipper
   cd agentic-clipper
   ```

GitHub auto-redirects from the old URL, so anything else that references the repo keeps working.

---

## 2. TikTok — DEFERRED until month 3 *(operator decision 2026-05-14)*

**You do not need to submit anything to TikTok today.** No developer account, no audit, no API setup.

Why: TikTok's Content Posting API audit (4–8 weeks) is gated tightly against solo operators, automated-upload reach is widely reported as suppressed, and the rejection rate is high. We get all of TikTok's distribution upside today by uploading the finished MP4s manually each morning — the pipeline still produces them end-to-end (Compliance gate, AI commentary, generative visuals, captions burned in). See **Step 7** for the daily manual-upload playbook (~5 min/day).

Re-evaluate at **month 3** once you have a track record to point at in an audit application — at that point the rejection cost is lower.

**If you change your mind earlier**, the previous walkthrough lives in this file's git history (commit `4818d8d`). The fields and prompts there are still accurate.

---

## 3a. Facebook Page (~5 min) — required by Instagram Graph API

The IG Graph API for Reels publishing requires an Instagram Business or Creator account **linked to a Facebook Page**. The Page is just a hand-off mechanism; you don't have to post anything on it.

1. Go to **https://www.facebook.com/pages/create**
2. Fill in:
   - **Page name:** `agentic-clipper` (or your channel brand if you've picked one)
   - **Category:** `Video creator`
   - **Description:** *(short, e.g.)* `AI-narrated commentary clips. Short-form video.`
3. Click **Create Page**.
4. From the Page's **Settings → Linked accounts**, click **Connect Instagram account** and authorize the Instagram identity you'll publish from (must be a Business or Creator IG account — convert via IG mobile app if it's currently a personal account: **Settings → Account → Switch to professional account → Creator**).
5. Note the **Page ID** (Settings → About) — you'll need it for the IG Graph API setup.

---

## 3b. Google Cloud project + YouTube Data API v3 (~10 min)

1. Go to **https://console.cloud.google.com/** and sign in.
2. Top bar → project dropdown → **New Project**:
   - **Project name:** `agentic-clipper`
3. Once the project is created, click **APIs & Services → Library** and enable:
   - **YouTube Data API v3**
4. Click **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - First time: it'll prompt you to configure the **OAuth consent screen**. Pick **External** (you can leave it in "Testing" mode and add yourself as a test user — no app verification needed for personal use). Fill in app name `agentic-clipper`, support email = your email, leave the rest defaults.
   - Back to **Create OAuth client ID** → application type **Desktop app** → name `agentic-clipper-cli` → **Create**.
   - Download the JSON. Extract `client_id` and `client_secret` and put them in `.env`:
     ```
     YOUTUBE_OAUTH_CLIENT_ID=...
     YOUTUBE_OAUTH_CLIENT_SECRET=...
     ```
5. Also create an **API key** (Credentials → Create credentials → API key) for the read-only Scout calls (trending video discovery). Restrict it to the YouTube Data API v3. Put it in `.env`:
   ```
   YOUTUBE_API_KEY=...
   ```
6. Default quota is 10,000 units/day — enough for ~6 uploads + reads. Phase 2 will request a quota increase only if we hit the ceiling.

---

## 4. Atlas Cloud account + $50 starter (~5 min)

Atlas Cloud is our Seedance 2.0 provider at $0.022/sec Fast — see `docs/seedance_access.md`.

1. Go to **https://www.atlascloud.ai/**
2. Sign up (use a separate email per backup-account hygiene — see `docs/backup_warming.md`).
3. After verification, go to **Billing → Add funds** and add **$50** (one calendar month of Fast-tier visuals at our budgeted rate; you can top up monthly).
4. **Account → API keys → Create new key** named `agentic-clipper-prod`. Copy the key (you'll see it only once).
5. Add to `.env`:
   ```
   ATLAS_CLOUD_API_KEY=sk-...
   ```
6. Sanity-check your funded balance under **Account → Usage**. The doctor health check (Phase 2) will start pinging the billing endpoint daily.

**Important — re-verify quarterly:** Atlas Cloud's commercial-use terms can shift if the ByteDance / MPA dispute escalates. Calendar the next review: **2026-08-14** (`docs/seedance_access.md` already records this).

---

## 5. (DONE on your behalf 2026-05-14)

- `make init-db` — created `data/main.db` from `data/schema.sql`
- `make test` — 36/36 pass
- `make doctor` — reports one expected failure (#6 below); everything else green

---

## 6. Generate the manic_reactor reference image (~3 min, once #4 is done)

The avatar's locked seed is `8376739915435003287` (`config/avatars/README.md`). Run the script that submits the locked prompt + seed to Atlas Cloud's Seedream image API:

```bash
# Verify the dry-run prints expected payload (no network call, no spend)
python3 scripts/generate_avatar.py --dry-run

# Run it for real (~3s generation, ~$0.032)
python3 scripts/generate_avatar.py
```

The script:

1. Reads `ATLAS_CLOUD_API_KEY` from `.env` (or env).
2. POSTs to `https://api.atlascloud.ai/api/v1/model/generateImage` with model `seedream-v5.0-lite`, the locked prompt, and seed `8376739915435003287`.
3. Polls the prediction endpoint until status=`completed`.
4. Downloads the result to `config/avatars/manic_reactor.jpg`.
5. Writes provenance + cost rows to `data/main.db` (`seedance_generations`, `costs`).

**Verify the result before committing:**

- Open `config/avatars/manic_reactor.jpg` in any image viewer.
- Is it clearly cartoon-coded (NOT photoreal)?
- Headphones-around-neck signature accessory visible?
- Front-facing, mid-grin expression?
- Plain neutral background?

If it looks right:

```bash
git add config/avatars/manic_reactor.jpg
git commit -m "Lock manic_reactor avatar reference image"
git push
```

If it does NOT look right, **do not** change the seed (`8376739915435003287` is final). Tweak the `PROMPT` constant in `scripts/generate_avatar.py` and re-run — the script overwrites `manic_reactor.jpg` each call. Once you're happy with the image, commit, and the seed stays locked forever after.

**Why a script instead of `curl`:** Atlas Cloud's Seedream API is async — submit returns a prediction ID, you poll for completion, then download. Easier to wrap once than to type out three curl-and-jq incantations every time you want to iterate on the prompt. The script is also the basis for Phase 2's Visuals image-gen wiring.

**If the request fails with an empty `outputs` array on a "completed" response:** that's the Seedance video API's face-filter rejection signature; per recent reporting Atlas Cloud's third-party version may not apply this filter at all on the image endpoint, but if it does, the prompt contains a real-person signal somewhere. Strip names, real places, photoreal qualifiers and re-run. After commit, re-run `make doctor` — the `avatar reference image` check should pass.

---

## 7. Daily TikTok manual upload (~5 min/day, recurring)

Replaces step 2's API automation. Each morning:

1. Check `data/clips/output/manual_upload/tiktok/` for the previous day's finished MP4s.
2. For each clip directory `<date>_<clip_id>/`, you'll find:
   - `video.mp4` — the final composited clip
   - `caption.txt` — the description (already Compliance-approved: includes attribution, "Commentary on...", AI-visuals disclosure, `#ad` if applicable)
   - `hashtags.txt` — the hashtag set (3–5 tags per TikTok config)
3. Open the TikTok mobile app or TikTok Studio Desktop.
4. Tap **+** to upload → select the `video.mp4` from your phone (AirDrop / Google Drive / Dropbox the file over if you're uploading from the server).
5. Paste `caption.txt` into the caption field, append `hashtags.txt`.
6. **Toggle "AI-generated content"** ON if the clip used any Seedance visuals (required by TikTok ToS; Compliance already verified the description discloses this).
7. Set posting time to match the slot it was scheduled for in `config/posting_schedule.yaml` (TikTok's "Schedule" feature accepts up to 10 days out).
8. Post (or schedule). Then run:
   ```bash
   make tiktok-confirm CLIP_ID=<clip_id> POST_ID=<tiktok_post_id>
   ```
   This marks `clips_ready.status = 'posted'` and lets the Analyst pick it up after 48h.

The pipeline produces ~3 TikTok-targeted clips/day per `config/posting_schedule.yaml`. Total daily time: 5–10 min.

> *(The `make tiktok-confirm` target is wired in Phase 2 alongside live API publishers. For now, Publisher writes the files to `data/clips/output/manual_upload/tiktok/` and marks the queue row `manual_pending`.)*

---

## 7.4 Local tool dependencies

The pipeline uses three external binaries and two Python packages, all
free and open-source. Each stage degrades gracefully (falls back to
scaffold mode) when its tool isn't installed, but `make doctor` will
surface every missing one.

Install once on your operator machine:

```bash
# macOS — Homebrew bundles ffmpeg + ffprobe
brew install ffmpeg

# Python packages (use the same Python that runs the pipeline)
pip install yt-dlp faster-whisper TTS
```

What each tool does:

| Tool | Stage | Phase 1 fallback if missing |
|------|-------|------------------------------|
| `ffmpeg` / `ffprobe` | Compositor (video composition, LUFS measurement) + Editor (download validation) | scaffold mode — final video is a placeholder, fails Compliance on duration check |
| `yt-dlp` | Editor (source clip download) | scaffold mode — no source file, downstream stages run with empty input |
| `faster-whisper` | Editor (transcription, word-level timestamps) | scaffold mode — empty transcript, no-speech gate auto-fails the clip |
| `TTS` (Coqui XTTS-v2) | Voice (TTS synthesis) | scaffold mode — placeholder audio track |

Verify the install:

```bash
make doctor
```

You're looking for `[OK]` next to every `binary:` and `python:` line in
the `check_pipeline_dependencies` section. Anything that's `[FAIL]`
will degrade the corresponding stage to scaffold mode.

---

## 7.5 Running the pipeline end-to-end

`make process N=5` picks up to N candidates in `'curated'` status and runs them through Editor → Writer → Voice → Visuals → Compositor → Compliance → enqueue. Each clip that passes Compliance gets one `clips_ready` row per platform configured in `config/posting_schedule.yaml` (Instagram Reels, YouTube Shorts, TikTok), scheduled for the next available time slot in `America/New_York`.

Prereqs: there has to be something to process. The orchestrator does NOT run Scout or Curator — those have their own cadences and cost models. Top up the queue first:

```bash
# Phase 2 wire-up still pending for these two — once they ship:
make scout      # discover new candidates
make curator    # promote discovered → curated

# Then run the pipeline:
make process N=5
```

The orchestrator prints a per-clip outcome table:

```
Requested:           5
Processed:           5
  → ready:           4
  → quarantined:     1
  → compliance fail: 0
  → lease conflict:  0
  → errored:         0

Per-clip:
  2026-05-18-0001-abc   ready
  2026-05-18-0002-def   ready
  2026-05-18-0003-ghi   quarantined  (editor: no_speech)
  ...
```

Outcomes:
- **ready** — clip passed Compliance and is in `clips_ready` waiting for `make publish`
- **quarantined** — an upstream stage routed it to `/data/quarantine/`
- **compliance fail** — composed successfully but the legal-defense gate blocked it (most common: commentary-ratio under 50%, music detected, missing attribution)
- **lease conflict** — another agent had the lease; next run picks it up
- **errored** — unexpected exception; check the digest for stack

Re-running on the same clip is safe: stage leases handle idempotency, and `_enqueue_for_publish` skips platforms where a queued row already exists.

---

## 8. Day 14 validation pilot gate (~30 days, recurring during pilot)

This is the safety gate before multi-platform ramp. Per `docs/phase2_plan.md:755`, the pipeline must post 30 clips to a single platform and clear three independent thresholds before Day 15 unblocks:

| Gate | Threshold | Why |
|------|-----------|-----|
| Content ID claim rate | **< 2%** | Fair-use defense holding; below "regular flagging" baseline |
| Revenue per view (RPV) | **> $0.001** | Proves the funnel is *actually monetized*, not just visible |
| Operator time | **< 45 min/day** | Per CLAUDE.md hard constraint; if higher, this isn't solo-operable |

Default platform is **Instagram Reels** — historically the least claim-prone of the three. Override with `PLATFORM=youtube_shorts` (or `tiktok`) if you have a reason.

### 8.1 Open the pilot

```bash
make pilot-start             # 30 clips to Instagram Reels, defaults
# or
make pilot-start PLATFORM=youtube_shorts CLIPS=20
```

This creates one row in `pilot_runs` with the gate thresholds baked in. At most one pilot can be active at a time — if you need to start over, `make pilot-finalize VERDICT=abandon` first.

### 8.2 Daily operator loop during the pilot

Each day (alongside step 7 manual TikTok uploads), spend 5–10 min logging what happened. The pilot has no automatic ingestion — Phase 2 has no live revenue API and no claim push listener, so the operator reads creator dashboards and records what they see.

```bash
# End of day — what was your total time on the pipeline?
make pilot-record-time MINUTES=32 NOTE="manual IG upload + reviewed digest"

# When the IG / YT / TikTok dashboard updates with revenue (often daily-ish):
make pilot-record-revenue AMOUNT=0.42 SOURCE=ad_rev DETAIL="2026-05-19 IG payout"
make pilot-record-revenue AMOUNT=1.10 SOURCE=affiliate DETAIL="3 clicks, 1 conversion"

# If any clip gets a Content ID claim notification:
make pilot-record-claim CLIP_ID=2026-05-19-1200-abc DETAIL="music match: Sony / Track X"
```

`SOURCE` values: `ad_rev` | `affiliate` | `creator_fund` | `other`.

### 8.3 Check progress

```bash
make pilot-status
```

Shows: clips posted vs. target, total claims, current RPV, minutes/day rolling average. You're looking for "all three gates trending green" — if RPV is collapsing or claim rate is creeping up, you'd rather know on day 7 than day 30.

### 8.4 Render verdict

```bash
make pilot-verdict
```

Three outcomes:

- **PASS** — all three gates clear at ≥ target clip count. Day 15 unblocked; proceed to multi-platform ramp.
- **FAIL** — gate criteria are at full sample but at least one failed. Verdict prints which gates failed. **Do not ramp.** Diagnose the failing dimension before retrying.
- **INCONCLUSIVE** — sample too small. Keep posting.

Exit code is 0 on PASS or INCONCLUSIVE, 2 on FAIL, so this can be chained from CI / cron.

### 8.5 Close the pilot

```bash
make pilot-finalize VERDICT=pass NOTES="Day 14 cleared; ready for ramp"
# or
make pilot-finalize VERDICT=fail NOTES="RPV held at $0.0006; reconsidering hook templates"
# or, if the pilot stalled (operator interruption, accounts paused, etc.):
make pilot-finalize VERDICT=abandon NOTES="paused for 2 weeks; will restart fresh"
```

Closing the pilot frees the active slot for a new run. The closed row stays in `pilot_runs` for the audit trail.

### 8.6 If the pilot FAILS

Don't loop. The gate exists to surface load-bearing problems before they get expensive:

- **Claim rate breach** → music detection is failing or fair-use commentary ratio is too low. Tighten the Compliance gate; review quarantine reasons; consider raising commentary-ratio threshold above 50%.
- **RPV breach** → either monetization isn't enabled (YPP/MMF status) or the niche isn't paying enough. Re-check eligibility status, hook templates, and CTR on affiliate links. Consider switching primary platform.
- **Operator-time breach** → automation gaps are showing. Profile your daily time, find the >10min step, automate or batch it before re-running.

Open a fresh pilot only after addressing the specific failed dimension.

---

## Status checklist

- [ ] Step 1: Rename repo
- [x] ~~Step 2: TikTok audit~~ — *deferred to month 3 (see above); replaced by Step 7 daily upload*
- [ ] Step 3a: Facebook Page created and linked to IG
- [ ] Step 3b: Google Cloud project + YouTube API keys in `.env`
- [ ] Step 4: Atlas Cloud account funded, key in `.env`
- [x] Step 5: `make init-db && make test && make doctor` *(36/36 pass; 1 expected fail awaiting step 6)*
- [ ] Step 6: Avatar reference image generated and committed
- [ ] Step 7: (recurring) daily TikTok manual upload — kicks in once Phase 2 runs the pipeline end-to-end
- [ ] Step 8: (recurring during pilot) Day 14 validation pilot gate — 30 clips, single platform, three thresholds

Once steps 1, 3a, 3b, 4, 6 are ticked, **Phase 2** (replacing `NotImplementedError` stubs with live wiring) is unblocked. Once the Phase 2 pipeline runs end-to-end, **step 8** gates the expansion to multi-platform.
