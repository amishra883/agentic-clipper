# Autonomous Publishing — Architecture v2 (Higgsfield + n8n)

Status: **staged, not yet live.** Written 2026-07-06 after the operator's
directives: (1) use Higgsfield for generation, (2) use a recording of the
operator's own voice for commentary, (3) autonomous posting to the designated
socials, (4) four clips per day.

This supersedes the Phase-2 assumption that Atlas Cloud + local Coqui/Piper
would carry generation. Atlas Cloud + local TTS remain wired as fallbacks
(`agents/visuals.py`, `agents/voice.py`).

## Architecture

```
[cron 4×/day: 08:00, 12:00, 16:00, 20:00 ET]
        │
Claude Code session (producer — this repo)
        │  Scout/Curator pick source moment (config/creators.yaml)
        │  Writer generates script + shot list (persona P-01)
        ├─ Higgsfield personal_clipper / media_import_url → source clip (≤30s)
        ├─ Higgsfield generate_audio (OPERATOR'S cloned voice) → commentary track
        ├─ Higgsfield generate_video → avatar reactions / stingers (budget-capped)
        ├─ local ffmpeg compose (agents/compositor.py) → final ≤58s vertical mp4
        ├─ agents/compliance.py HARD GATE (10 rules) → fail ⇒ /quarantine/
        │
        └─ POST → n8n webhook `clip-poster` (automation/n8n/)
                    ├─ YouTube Shorts upload (immediate)
                    ├─ Instagram Reels publish (immediate)
                    └─ TikTok manual-upload email to operator (~2 min/clip)
```

Why this split: Higgsfield tools are only reachable from a Claude session
(MCP), so the creative/compliance brain stays here; n8n holds the platform
OAuth credentials and runs 24/7 independent of any session, so posting is
durable and auditable in its execution log.

## Voice (operator directive: "use a recording of my own voice")

- The commentary voice is a Higgsfield voice clone created from the
  **operator's own self-recorded audio** via Higgsfield `create_voice`.
- Legal posture: first-party consented voice use — categorically different
  from the banned practice of cloning a *creator's* voice. The
  `approved_voice_ids` whitelist in `config/persona.yaml` remains the
  structural chokepoint; the real Higgsfield voice_id replaces
  `operator_voice_higgsfield_PENDING` once created.
- Speech generation measured at **~1 credit per commentary track** — the
  cheap part of the stack.

## Session findings (2026-07-06)

| Fact | Value | Consequence |
|---|---|---|
| Higgsfield plan | **free, 10 credits** | Blocks everything; paid plan required |
| Shorts Studio cost | 180 credits / 60s short | Too expensive as the default path (4/day ≈ 21,600 credits/mo) |
| Speech (seed_audio) | ~1 credit / track | Operator-voice commentary is cheap |
| Cheap per-clip path | clipper + speech + local ffmpeg + a few seconds of avatar video | Estimated tens of credits/clip, not 180 — price the plan against this |
| Zapier TikTok | no organic posting app exists | Confirms manual mode for TikTok |
| Zapier | **rejected by operator** — n8n chosen | Posting rail lives in n8n |
| n8n credentials | Outlook ✅ / YouTube ❌ / Facebook Graph ❌ | Two OAuth setups pending (see automation/n8n/README.md) |
| n8n workflow | validated (19 nodes), deploy interrupted by connector drop | Re-deploy from `automation/n8n/clip_poster.workflow.js` |

Budget note: the Higgsfield subscription must fit inside the $240/mo cap
(`config/budget.yaml`). It largely *replaces* the budgeted Seedance +
ElevenLabs lines (~$92/mo combined), so a plan up to ~$90/mo is
budget-neutral. Verify actual plan pricing at purchase time and record it in
`config/budget.yaml`.

## Go-live checklist

Operator steps (one-time, ~30–45 min total):

1. **Reconnect connectors** in the Claude session: Higgsfield + n8n (both
   dropped mid-session on 2026-07-06).
2. **Record your voice**: say "create my voice" — Claude opens the Higgsfield
   voice widget; record/upload ~1–2 min of clean speech. Claude then writes
   the returned voice_id into `config/persona.yaml`.
3. **Buy Higgsfield credits/plan** sized for ~4 clips/day on the cheap path
   (Claude will compute the exact monthly credit need from a priced pilot
   clip before you commit).
4. **n8n YouTube credential**: GCP project → enable YouTube Data API v3 →
   OAuth client → connect as `YouTube account` in n8n.
5. **n8n Instagram credential**: IG account to Business/Creator, link to a
   Facebook Page, create long-lived Page token with `instagram_basic` +
   `instagram_content_publish`, add as `Facebook Graph account` in n8n; note
   the IG user ID.
6. **Deploy the rail**: ask Claude to deploy + publish
   `automation/n8n/clip_poster.workflow.js`.

Claude steps (after 1–6):

7. Produce **one pilot clip end-to-end** with real costs logged; operator
   reviews it before anything is posted publicly.
8. On operator approval ("go live"): schedule the 4 daily production runs
   (08:00/12:00/16:00/20:00 ET) and start the daily digest.

Standing guards (unchanged from CLAUDE.md): compliance gate on every clip,
AI-content disclosure on every post, ≤30s source / ≥50% commentary, zero
tolerance for strikes, hard monthly cost kill switch.
