# Seedance 2.0 access — Phase 0.7

> Captured 2026-05-14. Re-verify quarterly per CLAUDE.md "Legal & risk register".
>
> **Operator-confirmed risk register item (2026-05-14):** MPA has issued a
> cease-and-desist against ByteDance over Seedance 2.0 (copyright pushback
> from major studios). Commercial-use rights are clear today, but Atlas
> Cloud's and ByteDance's terms could shift abruptly if the dispute
> escalates. Quarterly re-verification of commercial-use language is
> mandatory and will be enforced via `make doctor` once Phase 1 ships.
> Next due: 2026-08-14.
>
> **Revision 2026-05-14 (Step 6 research):** secondary reporting indicates
> Atlas Cloud's third-party hosting of Seedance/Seedream may NOT enforce
> the upstream "no real-person uploads" filter that ByteDance's own
> platforms (Jimeng) apply. Phase 0.7 listed the upstream filter as a
> compliance backstop — that backstop is unreliable on Atlas Cloud.
>
> **Operational consequence:** the `_rule_no_real_face_seedance_reference`
> rule in `agents/compliance.py` is the SOLE enforcer of "no creator-face
> references", not a defense-in-depth layer. The Visuals agent must
> independently verify that no `first_frame_url` or reference-image
> argument contains a real human face before submission — Atlas Cloud
> will not refuse the request for us. Test coverage on this rule is
> already in `tests/test_compliance.py::test_real_face_reference_blocks`.
>
> Re-verify Atlas Cloud's actual filter behavior on first paid use; if
> they DO apply the filter, this revision can be reverted.
>
> Companion finding: Atlas Cloud also serves Seedream (ByteDance's image
> sibling) at $0.032/image for v5.0 Lite. Used by `scripts/generate_avatar.py`
> to produce the locked reference image — same API key, async submit +
> poll pattern documented below.

## Decision

- **Primary provider:** **Atlas Cloud** — lowest verified Fast-tier price ($0.022/sec), explicit "no charge for failed generations" policy, transparent per-second billing with no waitlist, and clear commercial-use terms.
- **Fallback provider:** **fal.ai** — different infrastructure (ByteDance's named enterprise partner for the Seedance 2.0 rollout), excellent developer experience, async webhook support out of the box. Pricier ($0.2419/sec Fast at 720p), but reliable when Atlas Cloud has an outage or shifts pricing.

Note on the spec target ($0.022/sec Fast): only Atlas Cloud currently advertises this rate publicly. The official BytePlus ModelArk Standard tier benchmarks against an internal $0.14/sec reference (per Volcengine's launch disclosure). PiAPI is the next-cheapest verified option at $0.08/sec Fast.

## Provider comparison

| Provider             | Fast $/sec       | Pro $/sec        | Failed-gen billing            | Commercial use   | Free tier                          | Docs URL |
|----------------------|------------------|------------------|-------------------------------|------------------|------------------------------------|----------|
| BytePlus ModelArk    | unverified (Lite tier $0.010/sec quoted; Fast/Standard not on public per-second sheet) | ~$0.14/sec (Volcengine launch disclosure) | unverified; resource packs explicitly non-refundable | Clear (commercial use included on paid plans) | 2M free vision-model tokens at signup | https://docs.byteplus.com/en/docs/ModelArk/1520757 |
| fal.ai               | $0.2419/sec at 720p (×0.6 multiplier on reference-to-video with video input → ~$0.1452/sec) | $0.2419/sec standard at 720p (same headline; quality endpoint priced higher per call) | unverified on public page | Clear (fal commercial terms; ByteDance enterprise partner) | Free credits on signup | https://fal.ai/models/bytedance/seedance-2.0/fast/text-to-video |
| PiAPI                | $0.08/sec (seedance-2-fast) | $0.10/sec (seedance-2 / quality) | unverified | Clear on paid plans | Free signup credits | https://piapi.ai/seedance-2-0 |
| Atlas Cloud          | **$0.022/sec**   | **$0.247/sec** (Pro/v1.5 Pro) — Standard $0.10/sec, Fast-Standard $0.081/sec also offered | **Not billed** (explicit policy) | Clear (transparent per-second, no waitlist) | No free tier; pay-as-you-go | https://www.atlascloud.ai/seedance-2 |
| Segmind              | unverified per-second (per-call: ~$0.73 for 5s 720p text-to-video → ~$0.146/sec) | unverified per-second (per-call: ~$0.91 for 5s 720p → ~$0.182/sec) | unverified | Clear (pay-as-you-go, commercial OK) | Free credits on signup | https://www.segmind.com/models/seedance-2.0-fast/pricing |
| OpenRouter           | unverified per-second (page lists $0/M input + $0/M output; real cost is upstream provider, opaque on the public page) | unverified per-second (same caveat) | unverified | Clear (OpenRouter ToS; passes through provider terms) | Routes to upstream provider free tiers | https://openrouter.ai/bytedance/seedance-2.0 |

(Cells marked "unverified" are not on the provider's public per-second pricing sheet today; values are not extrapolated.)

## Primary provider — API contract

**Atlas Cloud** exposes Seedance 2.0 through a unified per-model REST endpoint. Detailed contract (capture date 2026-05-14):

- **Endpoint URL pattern:** `https://api.atlascloud.ai/v1/models/bytedance/seedance-2.0-fast/{text-to-video | image-to-video | reference-to-video}` (model slugs visible at https://www.atlascloud.ai/models/bytedance/seedance-2.0-fast/reference-to-video)
- **Authentication:** Bearer token. Header: `Authorization: Bearer $ATLAS_API_KEY`
- **Sync vs. async:** Async — POST returns a `task_id`; client polls a `GET /v1/tasks/{task_id}` (or webhook callback if configured) for `status: succeeded` and the output video URL. Atlas matches the BytePlus task-lifecycle pattern (create → retrieve → MP4 URL on success).
- **Required parameters:**
  - `prompt` — string, the scene description
  - `duration` — integer seconds (4–15)
  - `resolution` — `"480p"` or `"720p"` (Fast tier max is 720p)
  - `aspect_ratio` — `"16:9"`, `"9:16"`, `"1:1"`, etc.
  - `seed` — integer; required for character-consistency lock across our avatar shots
  - `first_frame_url` — optional, https URL to a reference still (used for image-to-video). Mutually exclusive with `reference_images` per the underlying ByteDance API contract.
  - `reference_images` — optional list of up to 9 image URLs for omni-reference character lock (use this instead of `first_frame_url` for the avatar consistency case)
- **Response shape (poll result):**
  ```json
  {
    "task_id": "tsk_01hx...",
    "status": "succeeded",
    "model": "bytedance/seedance-2.0-fast",
    "output": {
      "video_url": "https://cdn.atlascloud.ai/.../out.mp4",
      "duration_seconds": 6,
      "resolution": "720p",
      "seed": 42
    },
    "usage": {
      "billed_seconds": 6,
      "amount_usd": 0.132
    }
  }
  ```
- **Sample request body (generic):**
  ```json
  {
    "prompt": "stylized cartoon mascot doing an exaggerated jaw-drop reaction, cinematic lighting",
    "duration": 3,
    "resolution": "720p",
    "aspect_ratio": "9:16",
    "seed": 730421,
    "reference_images": [
      "https://assets.example.com/avatars/manic_reactor_v1.png"
    ]
  }
  ```
- **Documentation URL:** https://www.atlascloud.ai/seedance-2 — captured 2026-05-14
- **Model collection:** https://www.atlascloud.ai/collections/seedance2 — captured 2026-05-14

## Real-face restriction

- **Confirmed: yes.** Seedance 2.0 runs face detection on uploaded reference images **before** the prompt filter activates. Real photographs of identifiable human faces are rejected at that stage. Quote (vicsee.com test report, 2026): *"Seedance 2.0 scans uploaded images for identifiable real faces before the prompt is processed... if an uploaded image contains a clearly visible face, it can trigger a rejection before the model processes any text at all."* (https://vicsee.com/blog/seedance-content-filter)
- What passes the filter: AI-generated portraits, illustrated characters, 3D renders, stylized faces, side profiles with limited facial detail. This matches our avatar spec — the canonical avatar in `/config/avatars/{persona}.png` is AI-generated and clearly non-photoreal, so it passes.
- **This is a feature for us.** It enforces our "no real-face references" rule from CLAUDE.md (Compliance gate, Visuals agent constraints) at the model layer, in addition to our own check.
- **API response on detection:** Quirk — the API may return HTTP 200 with an empty body (no video URL, no error string), per Segmind's error-guide testing. A more explicit form is `OutputVideoSensitiveContentDetected.PolicyViolation`. **Implication for our pipeline:** the Visuals agent must treat `status=succeeded` with no `video_url` as a face-filter rejection, log it to `/data/main.db` with the input asset reference, and route the clip to `/quarantine/` rather than retrying with the same image. (Source: https://blog.segmind.com/seedance-2-0-error-guide-every-error-explained-with-fixes/)

## Reference-image character lock

- **Yes — natively supported.** Seedance 2.0's "Omni Reference" mode accepts up to 9 images, 3 videos, and 3 audio files (12 total) as reference inputs with role-based `@mention` syntax in the prompt.
- Recommended practice (per WaveSpeed AI guide, 2026): a "reference pack" of 3 stills (one straight-on, one three-quarter, one profile, same lighting/session) plus a locked seed produces the most stable character continuity across clips.
- **Our usage:** one canonical avatar reference image per persona in `/config/avatars/{persona}.png` + locked seed in `/config/persona.yaml`, fed via the `reference_images` field on every avatar-reaction shot. No fallback workaround needed.

## Failed-generation billing

- **Atlas Cloud (primary):** Failed generations are **not billed**. Quoted: *"failed generations are never billed"* (https://www.atlascloud.ai/blog/case-studies/seedance-2.0-pricing-full-cost-breakdown-2026).
- **fal.ai (fallback):** Public page does not state a refund-on-failure policy; assume billed unless we confirm otherwise during integration.
- **BytePlus ModelArk:** Resource packs explicitly **non-refundable**; failed-generation policy not surfaced on the public docs.
- **Our handling:** the Visuals agent logs **every** failure (including the silent face-filter "200 with empty body" case) to `/data/main.db` with timestamp, prompt, reference asset, model version, and provider response. The monthly cost report (1st of each month per CLAUDE.md budget guard) reconciles logged failures against provider invoices and flags any failure-billing discrepancies for dispute.

## Cost projection — 30 days

- **Fast tier:** 210 clips/month × 10 sec/clip avg × $0.022/sec = **$46.20/month** (target: ≤$50). Within the spec line item.
- **Pro tier escalation budget:** $20/mo at Atlas Cloud Pro $0.247/sec buys ~80 seconds = **~13 hero shots @ 6s each** (the Hero shot row from CLAUDE.md "Scene-aware visuals" table is 4–6s). Within the spec range of 10–20 hero shots/mo.
- **Virality-threshold rule for Pro promotion:** A clip is promoted from Fast → Pro **only if all of the following hold**:
  1. Curator virality projection score **≥ 0.85** (on the 0–1 scale already produced by the Curator agent)
  2. Projected views **≥ 50,000** (from the Curator's view-prediction regression)
  3. Hero shot is in the Writer-emitted shot list (Pro tier is never used for transitions/stat cards)
  4. The current month's Pro spend is ≤ $20 (hard cap; once hit, Optimizer auto-disables Pro escalation per the budget guard)

  Coded threshold lives in `/config/budget.yaml` so the Optimizer can adjust it as a higher-risk weekly proposal (per the self-improvement loop rules, threshold changes are human-reviewed, not auto-applied).

- **Total Seedance line item:** **$46.20 + $20.00 ≈ $66/month** (target: ≤$70). Fits the budget table ($50 Fast + $20 Pro = $70).

If Atlas Cloud raises Fast pricing or has an outage, the fallback math at fal.ai's $0.2419/sec is **$508/month** for the same Fast-tier volume — that is **8x over budget**. So failover to fal.ai must be paired with an immediate volume cap (≤2 clips/day) until we either restore Atlas Cloud or add a third provider. This is documented as a runbook item in the daily digest's "Seedance provider availability" doctor check.

## Free-tier evaluation plan

- **BytePlus 2M free vision-model tokens:** new BytePlus ModelArk accounts get 2,000,000 free tokens for every vision model (Seedance counts). Token math: a 15-second video consumes ~308,880 tokens (per Volcengine launch disclosure), so 2M tokens ≈ **~6 fifteen-second generations** or ~10 ten-second generations — enough to validate prompt templates and the avatar reference pack end-to-end before any paid spend on Atlas Cloud. Claim by signing up at https://www.byteplus.com/en/product/modelark and provisioning an ARK_API_KEY.
- **Dreamina daily credits:** 225 shared tokens/day per account (shared across all Dreamina tools, not just Seedance). Worth ~1–2 short Seedance clips per day. Use this as a side channel to sanity-check prompts on the consumer interface (web UI, no API) before encoding them as templates. Claim at https://dreamina.capcut.com (or the BytePlus-hosted Dreamina entry).
- **Sequence:** before the first paid Atlas Cloud call, run the canonical avatar reference image and the 9 reaction-prompt templates from `/config/avatar_reactions.yaml` through the BytePlus free 2M tokens to confirm: (a) the avatar passes the face filter, (b) seed-locked output stays consistent across the reference pack, (c) reaction prompts produce the expected expression library. Outputs are archived under `/data/generated_cache/eval_2026-05/` for future regression testing.

## Sources

- Atlas Cloud Seedance 2.0 pricing breakdown — https://www.atlascloud.ai/blog/case-studies/seedance-2.0-pricing-full-cost-breakdown-2026 (captured 2026-05-14)
- Atlas Cloud Seedance 2.0 product page — https://www.atlascloud.ai/seedance-2 (captured 2026-05-14)
- Atlas Cloud "Cheapest provider" comparison — https://www.atlascloud.ai/blog/guides/seedance-2.0-api-pricing-the-cheapest-provider-compared (captured 2026-05-14)
- Atlas Cloud Seedance 2.0 Fast reference-to-video model page — https://www.atlascloud.ai/models/bytedance/seedance-2.0-fast/reference-to-video (captured 2026-05-14)
- Atlas Cloud PR Newswire launch announcement — https://www.prnewswire.com/news-releases/atlas-cloud-makes-seedance-2-0-available-to-global-developers--transparent-per-second-pricing-no-waitlist-302746999.html (captured 2026-05-14)
- BytePlus ModelArk Seedance 2.0 API reference — https://docs.byteplus.com/en/docs/ModelArk/1520757 (captured 2026-05-14)
- BytePlus ModelArk pricing page — https://docs.byteplus.com/en/docs/ModelArk/1544106 (captured 2026-05-14)
- BytePlus ModelArk product page — https://www.byteplus.com/en/product/modelark (captured 2026-05-14)
- BytePlus free trial activity — https://www.byteplus.com/en/activity/free (captured 2026-05-14)
- TechNode: Seedance 2.0 ~$0.14/sec per Volcengine disclosure — https://technode.com/2026/03/05/bytedances-seedance-2-0-video-model-costs-about-0-14-per-second/ (captured 2026-05-14)
- fal.ai Seedance 2.0 Fast text-to-video model page — https://fal.ai/models/bytedance/seedance-2.0/fast/text-to-video (captured 2026-05-14)
- fal.ai Seedance 2.0 launch page — https://fal.ai/seedance-2.0 (captured 2026-05-14)
- fal.ai Seedance 2.0 user guide — https://fal.ai/learn/tools/how-to-use-seedance-2-0 (captured 2026-05-14)
- PiAPI Seedance 2.0 — https://piapi.ai/seedance-2-0 (captured 2026-05-14)
- PiAPI Seedance 2 docs — https://piapi.ai/docs/seedance-api/seedance-2 (captured 2026-05-14)
- Segmind Seedance 2.0 pricing — https://www.segmind.com/models/seedance-2.0/pricing (captured 2026-05-14)
- Segmind Seedance 2.0 Fast pricing — https://www.segmind.com/models/seedance-2.0-fast/pricing (captured 2026-05-14)
- Segmind Seedance 2.0 error guide — https://blog.segmind.com/seedance-2-0-error-guide-every-error-explained-with-fixes/ (captured 2026-05-14)
- OpenRouter Seedance 2.0 — https://openrouter.ai/bytedance/seedance-2.0 (captured 2026-05-14)
- OpenRouter Seedance 2.0 Fast — https://openrouter.ai/bytedance/seedance-2.0-fast (captured 2026-05-14)
- vicsee real-face restriction test report — https://vicsee.com/blog/seedance-content-filter (captured 2026-05-14)
- aividpipeline real-human-face rules — https://aividpipeline.com/blog/seedance-real-human-face-rules-2026 (captured 2026-05-14)
- WaveSpeed character-consistency reference-pack guide — https://wavespeed.ai/blog/posts/blog-character-consistency-seedance-2-0/ (captured 2026-05-14)
- vicsee Omni Reference guide — https://vicsee.com/blog/seedance-2-0-omni-reference (captured 2026-05-14)
- Dreamina daily credits guide — https://www.glbgpt.com/hub/seedance-2-0-free-credits-2026-guide/ (captured 2026-05-14)
- promeai commercial-use posture — https://www.promeai.pro/blog/seedance-2-0-commercial-use-licensing/ (captured 2026-05-14)
- Miller Shah note on MPA cease-and-desist (re-verify quarterly) — https://millershah.com/blog/seedance-ai-copyright-litigation/ (captured 2026-05-14)
