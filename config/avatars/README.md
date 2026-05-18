# Avatars

This directory holds locked reference images and seed values that keep our AI commentator avatar visually consistent across every clip. Read this BEFORE generating any avatar shot.

## Hard rules

1. **Cartoon-coded, never photoreal.** The avatar reads as a stylized character (Saturday-morning cartoon / modern animated short). No uncanny-valley humans.
2. **Never derived from any real creator's likeness.** No reference image, prompt, or seed in this directory may be based on a real person — clipped creator, public figure, anyone.
3. **One canonical avatar per persona.** Locked reference image + locked seed. Every avatar shot in production passes both.
4. **Seedance enforces the no-real-face rule on `first_frame_url`.** That's a feature for our compliance posture. We layer our own check on top.

## Persona → avatar mapping

| Persona ID | Persona name     | Reference image    | Locked seed          | Status |
|------------|------------------|--------------------|----------------------|--------|
| P-01       | manic_reactor    | `manic_reactor.jpg`| `8376739915435003287`| locked |

The seed is locked so the avatar identity is reproducible. Do not change this value — changing the seed produces a different character.

## Reference image generation history

Generated 2026-05-17 on Atlas Cloud's `bytedance/seedream-v5.0-lite` image API (Seedance's image sibling — Seedance itself is video-only). Three iterations on prompt; operator accepted the third. The committed `manic_reactor.jpg` IS the canonical reference. Do not change the seed or the prompt below without a `/proposals/` review per the "Changing the avatar later" section.

**Style decision (operator, 2026-05-17):** the v3 prompt below explicitly says "NOT anime", but Seedream's training bias produced an anime/manhwa-leaning result anyway. Operator accepted the anime-leaning aesthetic as on-trend for short-form virality. The "NOT anime" clause is preserved in the prompt verbatim because it IS what was passed to the model — re-running this exact `(seed, prompt)` pair against Seedream v5.0-lite is the only way to deterministically recover this image, and changing the prompt to match the output would break that guarantee.

## Reference image prompt (manic_reactor)

The exact prompt used to produce the committed `manic_reactor.jpg`. To reproduce, pass this verbatim with the locked seed and parameters below to `bytedance/seedream-v5.0-lite` via Atlas Cloud's `POST /api/v1/model/generateImage`. The canonical runner is `scripts/generate_avatar.py`.

```
A stylized animated-feature illustration of an original young-adult character
(early-to-mid 20s, NOT a child, NOT middle-aged) designed as a podcast/video
reactor mascot for short-form gaming and reaction content. Realistic-leaning
humanoid proportions — modern animated-feature build (head roughly 1/7 of
body, NOT chibi, NOT toddler-coded), but still clearly illustrated and
cartoon-styled, NOT photoreal, NOT 3D render, NOT anime. Spider-Verse / Arcane
/ Soul-style aesthetic: bold confident linework, modern flat shading with
subtle gradient lighting, on-trend streaming-mascot look optimized for
short-form video virality. Default expression is warm and approachable —
confident, slightly amused. This is the REST state, NOT a peak reaction.
Expressive eyes (cartoon-proportioned but not oversized), soft neutral or
slightly-raised eyebrows. Mouth in a relaxed closed-mouth half-smile — NOT a
wide teeth-bare grin, NOT a grimace. Front-facing torso, eyes drifting
slightly off-camera in a relaxed gaze. Contemporary casual outfit (modern
streetwear or graphic tee), bright contemporary palette. ONE signature
accessory: a chunky pair of headphones around the neck. Plain neutral
background. NOT a real person, NOT a celebrity, NOT a streamer likeness, NOT
based on any specific human. Original mascot character.
```

Required generation parameters (locked):

- `seed: 8376739915435003287`
- `aspect_ratio: 1:1`
- `resolution: 2048x2048` (Seedream v5.0-lite minimum is 3,686,400 px ≈ 1920×1920; 2048×2048 clears that with headroom)
- `reference_image_url: null` (this IS the reference)

## Reaction shots

Every reaction shot in production passes `reference_image_url: manic_reactor.jpg` and `seed: 8376739915435003287`. The reaction prompts live in `/config/avatar_reactions.yaml`.

## Compliance audit log

Every avatar generation writes a row to `/data/main.db` with: model version, prompt, seed, reference_image_hash, cost, timestamp. This is the audit trail if we ever need to defend the no-real-face posture.

## Changing the avatar later

A new persona requires a new entry in `/config/persona.yaml` (status: planned), a new reference image generated against a fresh locked seed, and a row added to the table above. Changing the existing P-01 reference once production has shipped breaks brand continuity — propose via `/proposals/` for human review.
