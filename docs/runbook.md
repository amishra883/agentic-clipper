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

## 2. TikTok Content Posting API audit (~10 min to submit; 4–8 wk review)

This is the **long pole**. Submit today.

1. Go to **https://developers.tiktok.com/** and sign in (use the email tied to your Business/Creator TikTok identity, NOT a personal account).
2. Click **Manage apps** → **Connect an app** (or **Create an app** if none exists).
3. App-creation form — paste these answers:

   | Field | Paste this |
   |---|---|
   | App name | `Agentic Clipper` |
   | Category | `Content Creation Tools` |
   | Description | `Short-form commentary/reaction video pipeline. Source clips ≤30s undergo AI-narrated commentary that occupies ≥50% of audio runtime. Each clip is transformative under U.S. fair-use (17 U.S.C. § 107). Publisher applies TikTok's AI-content label on every clip that uses generative visuals. Output ≤60s, vertical 9:16.` |
   | Website URL | `https://agentic-clipper.dev` (placeholder — replace with your real domain when you provision it) |
   | Privacy Policy URL | (host a one-pager describing data handling — template below) |
   | Terms of Service URL | (host a one-pager — template below) |

4. After app creation, request the **Content Posting API** product:
   - **Products → Add product → Content Posting API → Request**.
   - You will need to request **"Direct Post"** scope explicitly. The default `SELF_ONLY` sandbox is useless for distribution.
5. Audit submission form — paste these answers:

   | Field | Paste this |
   |---|---|
   | Use case | `Autonomous publishing of transformative commentary clips to a TikTok account owned by the operator. Each posting decision is gated by our internal Compliance hard-gate which enforces fair-use rules (source ≤30s, commentary ≥50%, AI-content disclosure, attribution to source creator). No third-party user content is published.` |
   | Posting frequency | `≤4 posts/day per account` |
   | Content moderation approach | `Hard-gated by automated Compliance rules before any post call. Manual review on first 50 clips. Backup-account failover on first copyright claim.` |
   | AI-content disclosure | `Every clip with AI-generated visuals or AI-narration is labeled via the platform's AI-content toggle on the upload call.` |
   | Privacy / data handling | `No third-party user data collected. Source clip metadata cached locally in SQLite. No data sent to TikTok beyond the post payload itself.` |
6. Submit. Save your **Client Key** and **Client Secret** to `.env`:
   ```
   TIKTOK_CLIENT_KEY=...
   TIKTOK_CLIENT_SECRET=...
   ```
7. Audit review typically takes 4–8 weeks. While you wait, the codebase runs in TikTok's Playwright-on-TikTok-Studio-Desktop fallback mode (suppressed reach — see `docs/posting_apis.md`).

**Privacy Policy / ToS one-pagers (host on your domain, both required for app submission):**

Privacy Policy template:
```
agentic-clipper does not collect personal data from third parties. The
operator's own platform credentials are stored locally and never transmitted
to any service other than the platforms they authenticate against
(YouTube, TikTok, Instagram). Source content metadata (URLs, view counts,
public titles) is cached locally for analytics. No user is tracked.
```

ToS template:
```
agentic-clipper is operated solely by the named account holder. Content
posted via this pipeline is transformative commentary on publicly-available
source material under U.S. fair-use doctrine (17 U.S.C. § 107). All AI-
generated visuals are labeled per platform requirements. The operator
warrants that they comply with each platform's community guidelines and
copyright policies.
```

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

The avatar's locked seed is `8376739915435003287` (`config/avatars/README.md`). The exact request payload is below — paste it into `curl` once you have your Atlas Cloud key.

```bash
# Set the key first
export ATLAS_CLOUD_API_KEY=sk-...

# Make the request. Atlas Cloud's image endpoint generates a single
# static reference frame; we use it as the locked reference_image_url
# for every avatar shot going forward.
curl -X POST https://api.atlascloud.ai/v1/images/generations \
  -H "Authorization: Bearer $ATLAS_CLOUD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "seedance-image-2.0",
    "prompt": "A stylized cartoon character designed as a podcast/video reactor mascot. Friendly but unhinged energy. Big expressive eyes (cartoon-large, not anime), wide flexible mouth capable of exaggerated faces. Round-ish head, simple shape language, one signature accessory (a chunky pair of headphones around the neck). Bright primary palette, thick clean linework, modern flat-shaded animation style — think contemporary animated short, not 1990s Saturday morning. Front-facing, neutral pose, looking slightly off-camera, expression mid-grin. Plain neutral background. NOT a real person, NOT a celebrity, NOT based on any specific human likeness. Mascot quality.",
    "seed": 8376739915435003287,
    "aspect_ratio": "1:1",
    "resolution": "1024x1024"
  }' > /tmp/avatar_response.json

# Inspect — if it's HTTP 200 with no image_url, the face-filter rejected
# the prompt. (Shouldn't happen here — the prompt is explicitly non-photoreal —
# but the Visuals agent handles this case in production.)
cat /tmp/avatar_response.json

# Download the image to the locked path
IMAGE_URL=$(jq -r '.image_url' /tmp/avatar_response.json)
curl -o config/avatars/manic_reactor.png "$IMAGE_URL"

# Verify it looks right (open in any image viewer)
ls -la config/avatars/manic_reactor.png

# Commit
git add config/avatars/manic_reactor.png
git commit -m "Lock manic_reactor avatar reference image (seed 8376739915435003287)"
git push
```

**Verify the result before committing:**
- Is it clearly cartoon-coded (not photoreal)?
- Does it have the headphones-around-neck accessory?
- Front-facing, mid-grin expression?

If it doesn't look right, **do not** change the seed. Re-write the prompt slightly while keeping the seed locked, regenerate. Once the right image is in `config/avatars/manic_reactor.png`, every subsequent avatar shot will lock to it.

**If the response was an empty body (face-filter rejection):** the prompt accidentally implied a real person. Strip any name, real-place reference, or photoreal qualifier and retry.

After commit, re-run `make doctor` — it should now show all-green.

---

## Status checklist

- [x] Step 1: Rename repo
- [ ] Step 2: TikTok audit submitted *(long pole — submit today)*
- [ ] Step 3a: Facebook Page created and linked to IG
- [ ] Step 3b: Google Cloud project + YouTube API keys in `.env`
- [ ] Step 4: Atlas Cloud account funded, key in `.env`
- [x] Step 5: `make init-db && make test && make doctor` *(36/36 pass; 1 expected fail awaiting step 6)*
- [ ] Step 6: Avatar reference image generated and committed

Once all boxes are ticked, **Phase 2** (replacing `NotImplementedError` stubs with live wiring) is unblocked.
