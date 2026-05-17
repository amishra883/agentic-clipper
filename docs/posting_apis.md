# Posting APIs — Phase 0.3 verification

> Captured 2026-05-14. Re-verify before pipeline scaffolding and quarterly thereafter.
> Primary docs `developers.google.com`, `developers.tiktok.com`, and `developers.facebook.com` returned HTTP 403 to direct fetches from this environment, so secondary references are cited alongside primary doc URLs. The primary URLs are the authoritative source — re-verify them manually before the build phase.

> ### Operator decision (2026-05-14): TikTok deferred to manual mode
>
> The operator chose **not** to submit the TikTok Content Posting API audit application on Day 1. Rationale: the audit is brutal for solo operators (4–8 weeks, high rejection rate), and automated TikTok uploads are widely reported as algorithmically suppressed. Instead, the pipeline runs TikTok in **manual mode**: Publisher produces the finished MP4 + caption + hashtags in `data/clips/output/manual_upload/tiktok/`, and the operator uploads via the native TikTok app each morning (~5 min/day). See `docs/runbook.md` Step 7 for the daily workflow.
>
> Re-evaluate at **month 3** once we have a track record to point at in the audit application. The TikTok section below is preserved as reference for that re-evaluation.

## Summary table

| Platform           | API                          | Status                          | Approval needed                                                                                  | Daily limit                                                                          | Notes |
|--------------------|------------------------------|---------------------------------|--------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------|-------|
| YouTube Shorts     | YouTube Data API v3          | available_with_approval         | Default quota usable immediately; YouTube API Compliance Audit required to lift 10k-unit/day cap | ~6 uploads/day on default quota (10,000 units / 1,600 per `videos.insert`)           | No dedicated Shorts endpoint — use `videos.insert` and tag `#Shorts` in title or description. |
| TikTok             | Content Posting API          | available_with_approval (gated) | OAuth client must be approved + then **audited** to publish PUBLIC content                       | Unaudited: 5 users / 24h, all posts forced to `SELF_ONLY`. Audited: ~6 req/min/token | Sandbox does **not** cover Content Posting for public videos. |
| Instagram Reels    | Instagram Graph API          | available_with_approval         | Meta App Review for `instagram_content_publish` + IG Business/Creator linked to a Facebook Page  | 50 published posts / 24h per IG account (rolling)                                    | Container-then-publish pattern; Reels capped at 90s via API. |

---

## YouTube Shorts (Data API v3)

### Status
**available_with_approval.** A Google Cloud project with OAuth 2.0 credentials can call `videos.insert` immediately on the default quota of 10,000 units/day, but that ceiling supports only ~6 uploads/day. To stay within the spec's 2 Shorts/day per primary account the default quota is sufficient; if multiple accounts share a single Cloud project, or if metadata reads (search/list) are also burning quota, an audit + quota extension is required.[^yt-quota][^yt-getting-started]

### Quota and cost
- Default quota: **10,000 units/day** per Cloud project.[^yt-quota]
- `videos.insert` cost: **1,600 units per upload**.[^yt-quota] At 2 Shorts/day this consumes 3,200 units; remaining 6,800 units cover read endpoints (`search.list` = 100 units, `videos.list` = 1 unit, `channels.list` = 1 unit).
- `captions.insert` cost: **400 units per call**.[^yt-captions]
- Quota increases require completing the **YouTube API Services Audit and Quota Extension Form**; manual review usually returns within ~3–4 business days after the audit completes, but the audit itself is multi-week.[^yt-audit-form][^yt-audit-guide]

### Shorts-specific requirements
- **No dedicated endpoint** — use `videos.insert` with the same payload as a long-form upload.[^yt-shorts-guide]
- **Hashtag signal**: include `#Shorts` in `snippet.title` or `snippet.description`. This is the reliable programmatic signal YouTube reads to route a vertical short clip into the Shorts feed.[^yt-shorts-guide]
- **Aspect / dimensions**: 9:16 vertical, recommended 1080×1920.[^yt-shorts-guide]
- **Duration**: ≤3 minutes is the platform limit (raised from 60s in Oct 2024); the spec already caps clips at ≤60s, which is comfortably inside the well-supported Shorts-feed window.[^yt-shorts-guide]
- **Safe-area**: keep critical content inside the central ~4:5 region to avoid YouTube UI overlay (subscribe button, like counter).[^yt-shorts-guide]

### Authentication
- OAuth 2.0 user consent flow (the channel owner authorizes the Cloud project). Refresh tokens persist; the Publisher agent can run unattended once the channel grants `youtube.upload`.

### Captions
- Programmatic upload **is supported** via `captions.insert` (sbv/scc/srt/ttml/vtt, ≤100 MB). 400 quota units per call.[^yt-captions]
- The pipeline already burns word-level captions into the video frame, so caption-track upload is optional. Recommend skipping it to conserve quota.

### Action items
1. Create a Google Cloud project, enable YouTube Data API v3, generate OAuth 2.0 client credentials. (Day 1)
2. Authorize the primary YT channel via OAuth consent screen and store the refresh token.
3. Validate end-to-end with a single test upload (set `status.privacyStatus=private`).
4. Track daily quota burn in the Publisher agent; alert at 80% to avoid silent upload failures.
5. **Defer the audit/quota-extension request** unless metadata-read traffic exceeds the default quota — at 2 uploads/day per channel the default ceiling is enough.

### Sources
- [YouTube Data API Overview / Getting Started](https://developers.google.com/youtube/v3/getting-started) (capture date 2026-05-14, returned 403 to fetch — verify manually)
- [YouTube Data API videos.insert reference](https://developers.google.com/youtube/v3/docs/videos/insert) (capture date 2026-05-14, returned 403 to fetch — verify manually)
- [YouTube Data API quota cost calculator](https://developers.google.com/youtube/v3/determine_quota_cost) (capture date 2026-05-14)
- [Quota and Compliance Audits](https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits) (capture date 2026-05-14)
- [Audit and Quota Extension Form](https://support.google.com/youtube/contact/yt_api_form?hl=en) (capture date 2026-05-14)
- [Captions: insert](https://developers.google.com/youtube/v3/docs/captions/insert) (capture date 2026-05-14)

---

## TikTok (Content Posting API)

### Status
**available_with_approval (gated by audit).** An OAuth client can be created today and call the Content Posting API in unaudited mode, but uploaded content is forced to `SELF_ONLY` (private) and capped at 5 distinct posting users per 24h. Public posting requires passing TikTok's audit.[^tt-getting-started][^tt-direct-post]

### Application path
1. Register at `developers.tiktok.com` and create an app.
2. Add the Content Posting API product; request scopes:
   - `video.publish` — required for **Direct Post** (publishes immediately to the user's profile).[^tt-direct-post]
   - `video.upload` — required for **Upload as Draft** (lands in the user's TikTok inbox for manual finalization).[^tt-direct-post]
3. Submit the app for review. Standard review: **5–10 business days** for clean submissions; common path is multiple rounds of feedback over **several days to ~2 weeks** for the initial scope grant.[^tt-review][^tt-zernio]
4. After scope grant, request **audit** of the app for production public posting. Audit is multi-week (≈2–4 weeks with feedback rounds).[^tt-review]

### Common rejection patterns
TikTok rejects apps that don't expose the required pre-post UX. The Publisher agent must surface (or, for an autonomous agent, log/record) the following, because TikTok's reviewers verify it:[^tt-review]
- Creator's username and avatar shown before the post.
- Privacy selector (public / friends / private).
- Per-post duet, stitch, comment toggles.
- Clear, legitimate use case description in the app submission.

### Sandbox vs production
- Sandbox mode lets you wire up the OAuth + endpoint contracts and supports up to 10 invited target users.[^tt-sandbox-blog]
- **Sandbox does NOT cover Content Posting API for public videos** — it's restricted to the unaudited path. Full production posting requires audit.[^tt-sandbox]

### Direct Post vs Draft Upload (which is auto-publishable)
| Endpoint                              | Scope            | Effect                                                  | Autonomous-publish friendly?                                   |
|---------------------------------------|------------------|---------------------------------------------------------|---------------------------------------------------------------|
| `/v2/post/publish/video/init/` (Direct Post) | `video.publish`  | Publishes the video to the creator's profile.            | **Yes — once the client is audited.** Unaudited: forced `SELF_ONLY`. |
| `/v2/post/publish/inbox/video/init/` (Upload as Draft) | `video.upload`   | Drops video into the creator's TikTok inbox for manual finishing. | No — requires manual tap-through in the TikTok app each time. |

So for an autonomous pipeline: only **Direct Post + audited client** is acceptable. Upload-as-Draft defeats the "no-human-in-the-loop" goal because every clip needs a manual finalize on the phone.[^tt-direct-post]

### Rate limits
- Per `access_token`: **6 requests/minute**.[^tt-zernio]
- Per creator: **~15 posts/day** (platform-side cap reported in 2026 community guides; not explicitly numbered in primary docs — treat as a soft ceiling and re-verify under load).[^tt-zernio]
- Unaudited cap: **5 distinct posting users / 24h**, all `SELF_ONLY`.[^tt-direct-post][^tt-sandbox]

### Fallback plan (Playwright + TikTok Studio Desktop)
If the audit is denied or stalls > 4 weeks, fall back to **TikTok Studio** (TikTok's official desktop creator app) driven by Playwright:
- TikTok Studio supports native scheduling up to 10 days out from the desktop web client, which is the surface a Playwright bot can drive.[^tt-studio]
- Existing OSS reference: `wanghaisheng/tiktoka-studio-uploader` (Playwright branch) and `haziq-exe/TikTokAutoUploader` (updated April 2026) demonstrate the working flow.[^tt-playwright-osspw][^tt-playwright-haziq]
- **Critical risk**: multiple operators report uploads via Playwright/desktop automation get **0 views** vs. mobile uploads of the same content, suggesting active suppression of automated desktop uploads.[^tt-playwright-suppression] The Compositor should produce mobile-shaped artifacts and the Playwright fallback should mimic real human pacing (random delays, scrolls, mouse jitter) using something like Phantomwright (a hardened Playwright fork) rather than raw Playwright.[^tt-playwright-osspw]
- Recommended posture: API path is primary; Playwright is an emergency-only fallback, and if it triggers visibility suppression, escalate to a hand-held mobile posting path (i.e., human queue) rather than burning the account.

### Action items
1. **Day 1 of build phase**: register developer account, create the app, submit for `video.publish` + `video.upload` scopes. Approval clock starts immediately.
2. While waiting, build the unaudited test path (sandbox + 5-user `SELF_ONLY`) so wiring is finished before scope grants land.
3. The moment scopes are granted, queue the **audit** request — this is the long pole; assume 4–6 weeks total wall-clock from day 1 to publishable Direct Post.
4. In parallel, scaffold the Playwright/TikTok Studio fallback path so Day 0 of pipeline launch isn't blocked on TikTok audit.
5. Make the Publisher agent's TikTok output structurally include the username, avatar reference, privacy selector logic, and duet/stitch/comment defaults — required for audit success.

### Sources
- [TikTok Content Posting API — Get Started](https://developers.tiktok.com/doc/content-posting-api-get-started) (capture date 2026-05-14, returned 403 to fetch — verify manually)
- [TikTok Content Posting API — Direct Post Reference](https://developers.tiktok.com/doc/content-posting-api-reference-direct-post) (capture date 2026-05-14)
- [TikTok Content Sharing Guidelines](https://developers.tiktok.com/doc/content-sharing-guidelines) (capture date 2026-05-14)
- [Introducing Sandbox Mode for TikTok Developers](https://developers.tiktok.com/blog/introducing-sandbox) (capture date 2026-05-14)
- [TikTok Add a Sandbox docs](https://developers.tiktok.com/doc/add-a-sandbox/) (capture date 2026-05-14)
- [TikTok Content Posting API: Developer Guide 2026 — TokPortal](https://www.tokportal.com/learn/tiktok-content-posting-api-developer-guide) (capture date 2026-05-14, secondary)
- [TikTok Content Posting API Developer Guide — Zernio 2026](https://zernio.com/blog/tiktok-developer-api) (capture date 2026-05-14, secondary)
- [tiktoka-studio-uploader (Playwright branch)](https://github.com/wanghaisheng/tiktoka-studio-uploader/blob/playwright/how-to-upload-tiktok.md) (capture date 2026-05-14)
- [haziq-exe/TikTokAutoUploader (updated Apr 2026)](https://github.com/haziq-exe/TikTokAutoUploader) (capture date 2026-05-14)
- [Playwright TikTok auto-upload visibility suppression report — BlackHatWorld](https://www.blackhatworld.com/seo/0-views-on-tiktok-when-uploading-from-pc-automation-but-mobile-posts-get-views-anyone-else.1813686/) (capture date 2026-05-14)

---

## Instagram Reels (Graph API)

### Status
**available_with_approval.** Requires (a) an Instagram **Business or Creator** account, (b) linked to a **Facebook Page** the operator can administer, (c) a Meta app reviewed for `instagram_content_publish`. Once those are in place, publishing is fully automatable.[^ig-overview][^ig-publish]

### Account + Page linking flow
1. Convert the IG account to **Professional → Business** (or Creator) in the IG mobile app.
2. Create or use an existing Facebook Page; link it to the IG account in the IG account settings → Linked Accounts.
3. In Meta Business Suite, confirm the Page and IG account both appear under the same Business asset.
4. In Meta Developers, create an app of type **Business**; add the **Instagram Graph API** product (using the Facebook Login for Business flow that succeeds the older Instagram Basic Display path).[^ig-login]
5. Configure OAuth redirect; request the scope set:
   - `instagram_basic`
   - `instagram_content_publish` ← required for Reels publishing
   - `pages_show_list`
   - `pages_read_engagement`
   - `business_management`[^ig-overview][^ig-scopes-secondary]
6. Submit for App Review with a screencast demonstrating the publishing flow. Approval is typically days, but Meta's review is opaque and can extend to a couple of weeks for content-publishing scopes.

### Container-then-publish pattern
Two-step for Reels:[^ig-publish][^ig-media-ref]
1. **Create container**: `POST /{ig-user-id}/media` with `media_type=REELS`, `video_url=<https URL the Meta servers can fetch>`, `caption=...`, optional `share_to_feed=true`.
   - Returns a container ID. The container moves through statuses: `IN_PROGRESS` → `FINISHED` (or `ERROR`). Poll with `GET /{ig-container-id}?fields=status_code` until `FINISHED`.
2. **Publish**: `POST /{ig-user-id}/media_publish` with `creation_id=<container_id>`.
   - Idempotent on the container ID; the published media ID is returned.

Reels-specific media constraints:[^ig-reels-specs]
- Container: MOV or MP4 (MPEG-4 Part 14), `moov` atom at front, no edit lists.
- Codec: H.264 or HEVC, progressive scan, closed GOP, 4:2:0 chroma, 23–60 FPS.
- Audio: AAC, ≤48 kHz, mono or stereo.
- Duration: **3–90 seconds** for API-published Reels (the spec's ≤60s output sits cleanly inside this).
- Aspect ratio: 9:16 strongly recommended (technically 0.01:1–10:1 accepted).
- Resolution: 1080×1920 recommended.
- File size: ≤4 GB hard cap; ≤500 MB recommended for stable upload.

### Rate limits and scopes
- General: 200 calls / hour / IG account (rolling).[^ig-rate-limits]
- Publishing-specific: **50 published posts / 24h** per IG account, queryable via `GET /{ig-user-id}/content_publishing_limit`.[^ig-publish-limit] (Historical 25/day was raised — verify the live value via the `content_publishing_limit` endpoint at runtime; some sources still cite 25.[^ig-publish-limit-conflict])
- Reels and Stories may have their own caps that aren't separately published — treat the 50/day total as the binding ceiling and run `content_publishing_limit` checks before each publish.[^ig-publish-limit][^ig-rate-limits-secondary]
- Required scopes (Facebook Login for Business): `instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement`, `business_management`.[^ig-overview]

### Action items
1. **Today** if not already done: convert the primary IG account to a Business/Creator account and create + link a Facebook Page. (No Page = no API access.)
2. Stand up a Meta Developer app, get it through Business Verification (driver's license / business doc upload) — this is its own ~3–7 day step.
3. Submit App Review for `instagram_content_publish` with a 60-second screencast of the create-container → publish flow.
4. Implement container-status polling (do **not** assume ready immediately on container creation — Meta queues video transcode for several seconds).
5. Wire `content_publishing_limit` checks into the Publisher agent before every scheduled post.
6. Host the source video on an HTTPS endpoint Meta can fetch (Cloudflare R2, S3, Worker → R2). Local-only paths will not work.

### Sources
- [Instagram Platform Overview](https://developers.facebook.com/docs/instagram-platform/overview/) (capture date 2026-05-14, returned 403 to fetch — verify manually)
- [Publish Content using the Instagram Platform](https://developers.facebook.com/docs/instagram-platform/content-publishing/) (capture date 2026-05-14)
- [Instagram Graph API — IG User /media reference](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media/) (capture date 2026-05-14)
- [Instagram Graph API — Content Publishing Limit endpoint](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/content_publishing_limit/) (capture date 2026-05-14)
- [Instagram API with Instagram Login (2024+ flow)](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/) (capture date 2026-05-14)
- [Instagram Reels API Complete Guide 2026 — Phyllo](https://www.getphyllo.com/post/a-complete-guide-to-the-instagram-reels-api) (capture date 2026-05-14, secondary — reels specs)
- [Instagram API Limits & Scaling 2026 — Phyllo](https://www.getphyllo.com/post/instagram-api-rate-limits-explained----and-how-to-scale-beyond-them-2026) (capture date 2026-05-14, secondary)

---

## Gaps and risks

- **TikTok audit is the long pole.** Without a passed audit, every TikTok post is `SELF_ONLY` (effectively useless). Realistic wall-clock from app submission to first public Direct Post is **4–8 weeks** if reviews go cleanly, longer if reviewers send back the standard UX-compliance feedback. The pipeline must launch with TikTok in fallback mode (Playwright + TikTok Studio) and migrate to API once audited.
- **TikTok Playwright fallback may be silently throttled.** Operator reports of 0-view automated desktop uploads mean the fallback is not a reliable substitute for API access, just an emergency continuity measure. Build it but assume degraded distribution until API audit clears.
- **Instagram requires a Facebook Page that may not exist yet.** Without a Page tied to the IG Business/Creator account, there is no Graph API access — and Page creation triggers Meta's Business Verification (KYC docs, ~3–7 days). Add this to Day 1 of the build phase.
- **YouTube quota is sufficient for posting but not for heavy Scout/Curator metadata reads.** If the Scout or Analyst agents start scanning trending or competitor channels via the same Cloud project, the 10k/day budget will burn fast (1 search.list = 100 units). Either separate Cloud projects per agent or front-load an audit + extension request.
- **Primary developer-doc fetches (developers.google.com, developers.tiktok.com, developers.facebook.com) returned HTTP 403 from this environment.** All API parameters and quotas in this report are corroborated by 2026 secondary sources, but the operator should personally re-verify the linked primary docs before scaffolding code, and again quarterly per the spec.
- **Per-post UX requirements on TikTok constrain agent autonomy.** Even after audit, TikTok requires creator username + avatar + privacy + duet/stitch/comment selectors per post. An autonomous Publisher must encode default selections explicitly per post and log them — TikTok's review requires the *capability* to surface them; for an internal tool the defaults can be hard-coded but must be auditable.
- **Reels publishing limit conflict in secondary sources.** Some 2026 secondary sources still cite the older 25/day figure even though the official `content_publishing_limit` endpoint now returns 50/day for most accounts. Treat the live `content_publishing_limit` query as the source of truth at runtime; do not hard-code.
- **No platform-side guarantee on AI-content labeling parity.** All three platforms now require labeling of AI-generated content in some contexts (per the spec's risk register). The Publisher agent must apply the platform-specific AI-content label on every clip carrying Seedance assets — this is a per-platform field, not a single shared parameter, and is tracked separately from posting-API quota.

---

[^yt-quota]: YouTube Data API v3 default project quota: 10,000 units/day; `videos.insert` cost: 1,600 units. [Quota cost reference](https://developers.google.com/youtube/v3/determine_quota_cost), [getting started](https://developers.google.com/youtube/v3/getting-started), and 2026 secondary [Phyllo guide](https://www.getphyllo.com/post/youtube-api-limits-how-to-calculate-api-usage-cost-and-fix-exceeded-api-quota). Captured 2026-05-14.
[^yt-getting-started]: [YouTube Data API v3 Getting Started](https://developers.google.com/youtube/v3/getting-started). Captured 2026-05-14.
[^yt-captions]: `captions.insert` cost is 400 units; supports sbv/scc/srt/ttml/vtt up to 100 MB. [Captions: insert](https://developers.google.com/youtube/v3/docs/captions/insert). Captured 2026-05-14.
[^yt-audit-form]: [YouTube API Services Audit and Quota Extension Form](https://support.google.com/youtube/contact/yt_api_form?hl=en). Captured 2026-05-14.
[^yt-audit-guide]: [Quota and Compliance Audits guide](https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits). Captured 2026-05-14.
[^yt-shorts-guide]: Shorts use `videos.insert` (no dedicated endpoint); `#Shorts` in title or description is the programmatic signal; 9:16 1080×1920; ≤3 min platform max (≤60s reliably routes to the Shorts feed). [Phyllo Shorts API guide 2026](https://www.getphyllo.com/post/unlocking-success-with-youtube-api-for-youtube-shorts), [Veed Shorts API guide](https://www.veed.io/learn/youtube-shorts-api). Captured 2026-05-14.
[^tt-getting-started]: [TikTok Content Posting API — Get Started](https://developers.tiktok.com/doc/content-posting-api-get-started). Captured 2026-05-14.
[^tt-direct-post]: [TikTok Content Posting API — Direct Post reference](https://developers.tiktok.com/doc/content-posting-api-reference-direct-post). `video.publish` scope drives Direct Post; `video.upload` scope drives Upload-as-Draft (lands in user inbox). Unaudited clients are forced to `SELF_ONLY` viewership and capped at 5 posting users per 24h. Captured 2026-05-14.
[^tt-sandbox-blog]: [Introducing Sandbox Mode for TikTok Developers](https://developers.tiktok.com/blog/introducing-sandbox). Captured 2026-05-14.
[^tt-sandbox]: [TikTok Add a Sandbox docs](https://developers.tiktok.com/doc/add-a-sandbox/) — Sandbox does not cover Content Posting API for public videos. Captured 2026-05-14.
[^tt-review]: TikTok app review timeline 5–10 business days for clean apps; resubmissions add 1–2 weeks; audit is 2–4 weeks with feedback rounds. Common rejection reasons (UX missing username/avatar, privacy selector, duet/stitch/comment controls). Sources: [TikTok Content Posting API Developer Guide — TokPortal 2026](https://www.tokportal.com/learn/tiktok-content-posting-api-developer-guide), [Posteverywhere TikTok API guide 2026](https://posteverywhere.ai/blog/post-to-tiktok-api). Captured 2026-05-14.
[^tt-zernio]: [TikTok Content Posting API — Zernio 2026 guide](https://zernio.com/blog/tiktok-developer-api): per-token rate limit 6 req/min; per-creator daily cap ~15 posts. Captured 2026-05-14.
[^tt-studio]: TikTok Studio (desktop) supports native scheduling up to 10 days out. [SocialBee — schedule TikTok posts 2026](https://socialbee.com/blog/how-to-schedule-tiktok-posts/), [Buffer schedule TikTok posts](https://buffer.com/resources/how-to-schedule-tiktok-posts/). Captured 2026-05-14.
[^tt-playwright-osspw]: [tiktoka-studio-uploader (Playwright branch)](https://github.com/wanghaisheng/tiktoka-studio-uploader/blob/playwright/how-to-upload-tiktok.md) — uses Phantomwright (hardened Playwright fork) for bot evasion. Captured 2026-05-14.
[^tt-playwright-haziq]: [haziq-exe/TikTokAutoUploader](https://github.com/haziq-exe/TikTokAutoUploader) — Python TikTok scheduler using Playwright; updated April 2026. Captured 2026-05-14.
[^tt-playwright-suppression]: Operator report: Playwright/desktop automated TikTok uploads return 0 views vs. mobile uploads of identical content. [BlackHatWorld discussion](https://www.blackhatworld.com/seo/0-views-on-tiktok-when-uploading-from-pc-automation-but-mobile-posts-get-views-anyone-else.1813686/). Captured 2026-05-14. (Anecdotal but consistent with multiple operator reports.)
[^ig-overview]: [Instagram Platform Overview](https://developers.facebook.com/docs/instagram-platform/overview/). Account must be IG Business/Creator linked to a Facebook Page; required scopes for publishing include `instagram_basic`, `instagram_content_publish`, `pages_show_list`, `business_management`. Captured 2026-05-14.
[^ig-publish]: [Instagram content publishing guide](https://developers.facebook.com/docs/instagram-platform/content-publishing/). Container-then-publish: `POST /media` then `POST /media_publish`. Captured 2026-05-14.
[^ig-login]: [Instagram API with Instagram Login (2024+)](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/). Captured 2026-05-14.
[^ig-scopes-secondary]: Secondary confirmation of scope set for content publishing: [Elfsight Instagram Graph API guide 2026](https://elfsight.com/blog/instagram-graph-api-complete-developer-guide-for-2026/), [StackoverflowTips IG publishing setup](https://www.stackoverflowtips.com/2025/08/how-to-set-up-facebook-graph-api-for.html). Captured 2026-05-14.
[^ig-media-ref]: [Instagram Graph API — IG User /media endpoint reference](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media/). Captured 2026-05-14.
[^ig-reels-specs]: Reels specs via the Graph API (3–90s, 9:16, 1080×1920, H.264/HEVC, AAC, MP4/MOV, ≤4 GB). [Phyllo Instagram Reels API guide 2026](https://www.getphyllo.com/post/a-complete-guide-to-the-instagram-reels-api), [Somake Instagram Reel size guide 2026](https://www.somake.ai/blog/instagram-reel-size-guide). Captured 2026-05-14.
[^ig-rate-limits]: Instagram Graph API: 200 calls/hour/account rolling. [Phyllo Instagram API integration guide 2026](https://www.getphyllo.com/post/instagram-api-integration-101-for-developers-of-the-creator-economy). Captured 2026-05-14.
[^ig-publish-limit]: [Content Publishing Limit endpoint](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/content_publishing_limit/) — query at runtime; current cap 50 published posts / 24h per IG account. Captured 2026-05-14.
[^ig-publish-limit-conflict]: Some 2026 secondary sources still cite 25/day; reconcile by querying `content_publishing_limit` at runtime. [Phyllo IG rate limits guide](https://www.getphyllo.com/post/instagram-api-rate-limits-explained----and-how-to-scale-beyond-them-2026). Captured 2026-05-14.
[^ig-rate-limits-secondary]: [Elfsight Instagram Graph API guide 2026](https://elfsight.com/blog/instagram-graph-api-complete-developer-guide-for-2026/). Captured 2026-05-14.
