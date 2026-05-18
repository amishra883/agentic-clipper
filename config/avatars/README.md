# Avatars

This directory holds locked reference images and seed values that keep our AI commentator avatar visually consistent across every clip. Read this BEFORE generating any avatar shot.

## Hard rules

1. **Cartoon-coded, never photoreal.** The avatar reads as a stylized character (Saturday-morning cartoon / modern animated short). No uncanny-valley humans.
2. **Never derived from any real creator's likeness.** No reference image, prompt, or seed in this directory may be based on a real person — clipped creator, public figure, anyone.
3. **One canonical avatar per persona.** Locked reference image + locked seed. Every avatar shot in production passes both.
4. **Seedance enforces the no-real-face rule on `first_frame_url`.** That's a feature for our compliance posture. We layer our own check on top.

## Persona → avatar mapping

| Persona ID | Persona name     | Reference image           | Locked seed          | Status   |
|------------|------------------|---------------------------|----------------------|----------|
| P-01       | manic_reactor    | `manic_reactor.png` (TBD) | `8376739915435003287`| pending  |

The seed is locked NOW so the avatar identity is reproducible the moment a Seedance account exists. Do not change this value once a reference image is generated — changing the seed produces a different character.

## Why the reference image is deferred

Reference image generation is gated on Phase 0.7 finishing (Seedance provider chosen + account funded). Generating it earlier on a different model risks visual drift when we re-generate on Seedance later. See `docs/phase0_digest.md` for the action item.

## Reference image prompt (manic_reactor)

When the Seedance account exists, generate a single static reference image with this prompt and the locked seed above. Save as `manic_reactor.png` next to this README, then commit.

```
A stylized cartoon character designed as a podcast/video reactor mascot.
Friendly but unhinged energy. Big expressive eyes (cartoon-large, not anime),
wide flexible mouth capable of exaggerated faces. Round-ish head, simple
shape language, one signature accessory (a chunky pair of headphones around
the neck). Bright primary palette, thick clean linework, modern flat-shaded
animation style — think contemporary animated short, not 1990s Saturday
morning. Front-facing, neutral pose, looking slightly off-camera, expression
mid-grin. Plain neutral background. NOT a real person, NOT a celebrity, NOT
based on any specific human likeness. Mascot quality.
```

Required generation parameters (locked):

- `seed: 8376739915435003287`
- `aspect_ratio: 1:1`
- `resolution: 2048x2048` (Seedream v5.0-lite minimum is 3,686,400 px ≈ 1920×1920; 2048×2048 clears that with headroom)
- `reference_image_url: null` (this IS the reference)

## Reaction shots

Every reaction shot in production passes `reference_image_url: manic_reactor.png` and `seed: 8376739915435003287`. The reaction prompts live in `/config/avatar_reactions.yaml`.

## Compliance audit log

Every avatar generation writes a row to `/data/main.db` with: model version, prompt, seed, reference_image_hash, cost, timestamp. This is the audit trail if we ever need to defend the no-real-face posture.

## Changing the avatar later

A new persona requires a new entry in `/config/persona.yaml` (status: planned), a new reference image generated against a fresh locked seed, and a row added to the table above. Changing the existing P-01 reference once production has shipped breaks brand continuity — propose via `/proposals/` for human review.
