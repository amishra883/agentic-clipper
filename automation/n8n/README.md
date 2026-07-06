# n8n Publishing Rail

Operator decision 2026-07-06: **n8n is the posting layer** (Zapier rejected).
The producer (this repo's pipeline, driven by a Claude Code session) generates
and compliance-gates clips; n8n owns credentials and performs the actual
platform posts on its own infrastructure.

## Workflow: `clip-poster` (Clip Poster — Autonomous Publishing Rail)

Source: `clip_poster.workflow.js` (n8n Workflow SDK script, validated 19 nodes).

Deployment status: **validated but NOT yet created** — the n8n MCP connector
disconnected mid-deploy on 2026-07-06. To deploy: reconnect the n8n connector
in the Claude session and ask Claude to "deploy the clip-poster n8n workflow
from automation/n8n/clip_poster.workflow.js", then publish it.

## What it does

```
POST /webhook/clip-poster
        │
  Normalize payload
        │
  Compliance Gate (compliance_passed must be true)
        ├─ false ─► Outlook email alert to operator (nothing posted)
        └─ true ──┬─► platforms contains "youtube"       → download mp4 → YouTube upload (Short)
                  ├─► platforms contains "instagram"     → IG Reels container → wait/poll → publish
                  └─► platforms contains "tiktok_manual" → Outlook email to operator with
                                                            download link + caption + AI-label checklist
```

## Payload contract

```json
{
  "clip_id": "2026-07-06-0001",
  "video_url": "https://.../final.mp4",
  "title": "He did WHAT?! #Shorts",
  "description": "Reaction commentary... (must satisfy agents/compliance.py description rules)",
  "hashtags": ["#gaming", "#reaction"],
  "platforms": ["youtube", "instagram", "tiktok_manual"],
  "ig_user_id": "17841400000000000",
  "yt_category_id": "20",
  "yt_privacy": "public",
  "operator_email": "amishra883@gmail.com",
  "compliance_passed": true
}
```

`video_url` must be a publicly fetchable HTTPS mp4 (Higgsfield CDN URLs work).
`compliance_passed` is set exclusively by `agents/compliance.py` output — the
n8n gate is a second, structural copy of the repo's hard gate, not a
replacement for it.

## Credentials

| Credential | n8n type | Status |
|---|---|---|
| Microsoft Outlook account | `microsoftOutlookOAuth2Api` | ✅ already connected (id `5K3bclggRALuCjcH`) |
| YouTube account | `youTubeOAuth2Api` | ❌ operator must create: GCP project + YouTube Data API v3 + OAuth client, connect in n8n |
| Facebook Graph account | `facebookGraphApi` | ❌ operator must create: long-lived Page token with `instagram_basic` + `instagram_content_publish`; IG account must be Business/Creator linked to a FB Page |

TikTok has **no autonomous path** here by design — Zapier/n8n have no organic
TikTok posting integration, consistent with the 2026-05-14 manual-mode
decision (`config/posting_schedule.yaml`). The rail instead emails the
operator a ready-to-post package per clip (~2 min/clip, 4/day = ~8 min/day).
