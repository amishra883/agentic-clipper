// Clip Poster — Autonomous Publishing Rail (n8n Workflow SDK script)
//
// Status: VALIDATED against the n8n MCP builder (19 nodes, valid=true) on
// 2026-07-06. Deployment was interrupted when the n8n MCP connector dropped
// mid-call. To deploy: reconnect the n8n connector in Claude and re-run
// create_workflow_from_code with this file's contents, then publish.
//
// Contract: the producer (Claude Code session in this repo) POSTs one JSON
// payload per finished clip to webhook path `clip-poster`. See
// automation/n8n/README.md for the payload schema and credential setup.

import { workflow, node, trigger, sticky, newCredential, ifElse, expr, nodeJson } from '@n8n/workflow-sdk';

const clipWebhook = trigger({
  type: 'n8n-nodes-base.webhook',
  version: 2.1,
  config: {
    name: 'Clip Webhook',
    parameters: {
      httpMethod: 'POST',
      path: 'clip-poster',
      responseMode: 'onReceived'
    },
    position: [-600, 300]
  },
  output: [{ body: { clip_id: '2026-07-06-0001', video_url: 'https://cdn.example.com/final.mp4', title: 'He did WHAT?! #Shorts', description: 'Reaction commentary. AI-generated visuals.', hashtags: ['#gaming', '#reaction'], platforms: ['youtube', 'instagram', 'tiktok_manual'], ig_user_id: '17841400000000000', yt_category_id: '20', yt_privacy: 'public', operator_email: 'amishra883@gmail.com', compliance_passed: true } }]
});

const normalize = node({
  type: 'n8n-nodes-base.set',
  version: 3.4,
  config: {
    name: 'Normalize Clip Payload',
    parameters: {
      mode: 'manual',
      includeOtherFields: false,
      assignments: {
        assignments: [
          { id: 'f-clip-id', name: 'clip_id', value: expr('{{ $json.body?.clip_id ?? $json.clip_id ?? "unknown" }}'), type: 'string' },
          { id: 'f-video-url', name: 'video_url', value: expr('{{ $json.body?.video_url ?? $json.video_url ?? "" }}'), type: 'string' },
          { id: 'f-title', name: 'title', value: expr('{{ $json.body?.title ?? $json.title ?? "" }}'), type: 'string' },
          { id: 'f-desc', name: 'description', value: expr('{{ $json.body?.description ?? $json.description ?? "" }}'), type: 'string' },
          { id: 'f-hash-text', name: 'hashtags_text', value: expr('{{ ($json.body?.hashtags ?? $json.hashtags ?? []).join(" ") }}'), type: 'string' },
          { id: 'f-yt-tags', name: 'yt_tags', value: expr('{{ ($json.body?.hashtags ?? $json.hashtags ?? []).map(h => h.replace("#", "")).join(",") }}'), type: 'string' },
          { id: 'f-caption', name: 'full_caption', value: expr("{{ ($json.body?.description ?? $json.description ?? '') + '\\n\\n' + ($json.body?.hashtags ?? $json.hashtags ?? []).join(' ') }}"), type: 'string' },
          { id: 'f-platforms', name: 'platforms', value: expr('{{ $json.body?.platforms ?? $json.platforms ?? [] }}'), type: 'array' },
          { id: 'f-ig-user', name: 'ig_user_id', value: expr('{{ $json.body?.ig_user_id ?? $json.ig_user_id ?? "" }}'), type: 'string' },
          { id: 'f-yt-cat', name: 'yt_category_id', value: expr('{{ $json.body?.yt_category_id ?? $json.yt_category_id ?? "20" }}'), type: 'string' },
          { id: 'f-yt-priv', name: 'yt_privacy', value: expr('{{ $json.body?.yt_privacy ?? $json.yt_privacy ?? "public" }}'), type: 'string' },
          { id: 'f-op-email', name: 'operator_email', value: expr('{{ $json.body?.operator_email ?? $json.operator_email ?? "amishra883@gmail.com" }}'), type: 'string' },
          { id: 'f-compliance', name: 'compliance_passed', value: expr('{{ $json.body?.compliance_passed ?? $json.compliance_passed ?? false }}'), type: 'boolean' }
        ]
      }
    },
    position: [-360, 300]
  },
  output: [{ clip_id: '2026-07-06-0001', video_url: 'https://cdn.example.com/final.mp4', title: 'He did WHAT?! #Shorts', description: 'Reaction commentary. AI-generated visuals.', hashtags_text: '#gaming #reaction', yt_tags: 'gaming,reaction', full_caption: 'Reaction commentary. AI-generated visuals.\n\n#gaming #reaction', platforms: ['youtube', 'instagram', 'tiktok_manual'], ig_user_id: '17841400000000000', yt_category_id: '20', yt_privacy: 'public', operator_email: 'amishra883@gmail.com', compliance_passed: true }]
});

const complianceGate = ifElse({
  version: 2.3,
  config: {
    name: 'Compliance Gate',
    parameters: {
      conditions: {
        options: { caseSensitive: true, leftValue: '', typeValidation: 'strict' },
        conditions: [{ id: 'c-compliance', leftValue: expr('{{ $json.compliance_passed }}'), operator: { type: 'boolean', operation: 'equals' }, rightValue: true }],
        combinator: 'and'
      }
    },
    position: [-120, 300]
  }
});

const markApproved = node({
  type: 'n8n-nodes-base.set',
  version: 3.4,
  config: {
    name: 'Mark Approved',
    parameters: {
      mode: 'manual',
      includeOtherFields: true,
      assignments: {
        assignments: [
          { id: 'f-gate-ts', name: 'gate_passed_at', value: expr('{{ $now.toISO() }}'), type: 'string' }
        ]
      }
    },
    position: [140, 180]
  },
  output: [{ clip_id: '2026-07-06-0001', video_url: 'https://cdn.example.com/final.mp4', title: 'He did WHAT?! #Shorts', full_caption: 'Reaction commentary.\n\n#gaming #reaction', platforms: ['youtube', 'instagram', 'tiktok_manual'], ig_user_id: '17841400000000000', yt_category_id: '20', yt_privacy: 'public', yt_tags: 'gaming,reaction', description: 'Reaction commentary.', operator_email: 'amishra883@gmail.com', compliance_passed: true, gate_passed_at: '2026-07-06T12:00:00.000Z' }]
});

const emailBlockAlert = node({
  type: 'n8n-nodes-base.microsoftOutlook',
  version: 2,
  config: {
    name: 'Email Compliance Block Alert',
    parameters: {
      resource: 'message',
      operation: 'send',
      toRecipients: expr('{{ $json.operator_email }}'),
      subject: expr('[agentic-clipper] BLOCKED at posting rail — {{ $json.clip_id }}'),
      bodyContent: expr('Clip {{ $json.clip_id }} arrived at the n8n posting rail WITHOUT compliance_passed=true and was NOT posted anywhere.<br><br>Video: {{ $json.video_url }}<br>Title: {{ $json.title }}<br><br>This should never happen — the producer must run the Compliance gate before calling this webhook. Investigate before re-queueing.'),
      additionalFields: { bodyContentType: 'html' }
    },
    credentials: { microsoftOutlookOAuth2Api: { id: '5K3bclggRALuCjcH', name: 'Microsoft Outlook account' } },
    position: [140, 480]
  },
  output: [{ success: true }]
});

const wantsYouTube = ifElse({
  version: 2.3,
  config: {
    name: 'Wants YouTube?',
    parameters: {
      conditions: {
        options: { caseSensitive: true, leftValue: '', typeValidation: 'strict' },
        conditions: [{ id: 'c-yt', leftValue: expr('{{ $json.platforms }}'), operator: { type: 'array', operation: 'contains' }, rightValue: 'youtube' }],
        combinator: 'and'
      }
    },
    position: [420, 20]
  }
});

const downloadVideo = node({
  type: 'n8n-nodes-base.httpRequest',
  version: 4.4,
  config: {
    name: 'Download Video File',
    parameters: {
      method: 'GET',
      url: expr('{{ $json.video_url }}'),
      options: {
        timeout: 120000,
        response: { response: { responseFormat: 'file', outputPropertyName: 'data' } }
      }
    },
    position: [680, -60]
  },
  output: [{ fileName: 'final.mp4', mimeType: 'video/mp4' }]
});

const uploadYouTubeShort = node({
  type: 'n8n-nodes-base.youTube',
  version: 1,
  config: {
    name: 'Upload YouTube Short',
    parameters: {
      resource: 'video',
      operation: 'upload',
      title: nodeJson(markApproved, 'title'),
      regionCode: 'US',
      categoryId: nodeJson(markApproved, 'yt_category_id'),
      binaryProperty: 'data',
      options: {
        description: nodeJson(markApproved, 'full_caption'),
        tags: nodeJson(markApproved, 'yt_tags'),
        privacyStatus: nodeJson(markApproved, 'yt_privacy'),
        selfDeclaredMadeForKids: false,
        notifySubscribers: false
      }
    },
    credentials: { youTubeOAuth2Api: newCredential('YouTube account') },
    position: [940, -60]
  },
  output: [{ uploadId: 'abc123XYZ' }]
});

const wantsInstagram = ifElse({
  version: 2.3,
  config: {
    name: 'Wants Instagram?',
    parameters: {
      conditions: {
        options: { caseSensitive: true, leftValue: '', typeValidation: 'strict' },
        conditions: [{ id: 'c-ig', leftValue: expr('{{ $json.platforms }}'), operator: { type: 'array', operation: 'contains' }, rightValue: 'instagram' }],
        combinator: 'and'
      }
    },
    position: [420, 300]
  }
});

const igCreateContainer = node({
  type: 'n8n-nodes-base.facebookGraphApi',
  version: 1,
  config: {
    name: 'IG Create Reel Container',
    parameters: {
      authType: 'accessToken',
      hostUrl: 'graph.facebook.com',
      httpRequestMethod: 'POST',
      graphApiVersion: 'v23.0',
      node: expr('{{ $json.ig_user_id }}'),
      edge: 'media',
      options: {
        queryParameters: {
          parameter: [
            { name: 'media_type', value: 'REELS' },
            { name: 'video_url', value: expr('{{ $json.video_url }}') },
            { name: 'caption', value: expr('{{ $json.full_caption }}') },
            { name: 'share_to_feed', value: 'true' }
          ]
        }
      }
    },
    credentials: { facebookGraphApi: newCredential('Facebook Graph account') },
    position: [680, 240]
  },
  output: [{ id: '17900000000000000' }]
});

const waitForIgProcessing = node({
  type: 'n8n-nodes-base.wait',
  version: 1.1,
  config: {
    name: 'Wait For IG Processing',
    parameters: { resume: 'timeInterval', amount: 90, unit: 'seconds' },
    position: [900, 240]
  },
  output: [{ id: '17900000000000000' }]
});

const igCheckStatus = node({
  type: 'n8n-nodes-base.facebookGraphApi',
  version: 1,
  config: {
    name: 'IG Check Container Status',
    parameters: {
      authType: 'accessToken',
      hostUrl: 'graph.facebook.com',
      httpRequestMethod: 'GET',
      graphApiVersion: 'v23.0',
      node: expr('{{ $json.id }}'),
      options: {
        fields: { field: [{ name: 'status_code' }, { name: 'id' }] }
      }
    },
    credentials: { facebookGraphApi: newCredential('Facebook Graph account') },
    position: [1120, 240]
  },
  output: [{ status_code: 'FINISHED', id: '17900000000000000' }]
});

const igContainerReady = ifElse({
  version: 2.3,
  config: {
    name: 'IG Container Ready?',
    parameters: {
      conditions: {
        options: { caseSensitive: true, leftValue: '', typeValidation: 'strict' },
        conditions: [{ id: 'c-ig-ready', leftValue: expr('{{ $json.status_code }}'), operator: { type: 'string', operation: 'equals' }, rightValue: 'FINISHED' }],
        combinator: 'and'
      }
    },
    position: [1340, 240]
  }
});

const waitExtraForIg = node({
  type: 'n8n-nodes-base.wait',
  version: 1.1,
  config: {
    name: 'Wait Extra For IG',
    parameters: { resume: 'timeInterval', amount: 120, unit: 'seconds' },
    position: [1560, 360]
  },
  output: [{ status_code: 'IN_PROGRESS', id: '17900000000000000' }]
});

const igPublishReel = node({
  type: 'n8n-nodes-base.facebookGraphApi',
  version: 1,
  config: {
    name: 'IG Publish Reel',
    parameters: {
      authType: 'accessToken',
      hostUrl: 'graph.facebook.com',
      httpRequestMethod: 'POST',
      graphApiVersion: 'v23.0',
      node: nodeJson(markApproved, 'ig_user_id'),
      edge: 'media_publish',
      options: {
        queryParameters: {
          parameter: [
            { name: 'creation_id', value: nodeJson(igCreateContainer, 'id') }
          ]
        }
      }
    },
    credentials: { facebookGraphApi: newCredential('Facebook Graph account') },
    position: [1800, 240]
  },
  output: [{ id: '17900000000000001' }]
});

const wantsTikTokManual = ifElse({
  version: 2.3,
  config: {
    name: 'Wants TikTok Manual?',
    parameters: {
      conditions: {
        options: { caseSensitive: true, leftValue: '', typeValidation: 'strict' },
        conditions: [{ id: 'c-tt', leftValue: expr('{{ $json.platforms }}'), operator: { type: 'array', operation: 'contains' }, rightValue: 'tiktok_manual' }],
        combinator: 'and'
      }
    },
    position: [420, 580]
  }
});

const emailTikTokManual = node({
  type: 'n8n-nodes-base.microsoftOutlook',
  version: 2,
  config: {
    name: 'Email TikTok Manual Upload',
    parameters: {
      resource: 'message',
      operation: 'send',
      toRecipients: expr('{{ $json.operator_email }}'),
      subject: expr('[agentic-clipper] TikTok upload ready — {{ $json.clip_id }}'),
      bodyContent: expr('A clip passed Compliance and is ready for manual TikTok upload (~2 min).<br><br><b>Download:</b> <a href="{{ $json.video_url }}">{{ $json.video_url }}</a><br><b>Title:</b> {{ $json.title }}<br><b>Caption to paste:</b><br>{{ $json.full_caption }}<br><br><b>Checklist:</b><br>1. Upload via TikTok app or TikTok Studio Desktop.<br>2. Paste the caption above (hashtags included).<br>3. Toggle ON the "AI-generated content" label — required, every clip has AI visuals/voice.<br>4. Post, then reply DONE with the TikTok post URL for the analytics log.'),
      additionalFields: { bodyContentType: 'html' }
    },
    credentials: { microsoftOutlookOAuth2Api: { id: '5K3bclggRALuCjcH', name: 'Microsoft Outlook account' } },
    position: [680, 580]
  },
  output: [{ success: true }]
});

const contractNote = sticky(
  '## Clip Poster — payload contract\n\nPOST JSON to this webhook:\n\n- clip_id, video_url (public HTTPS mp4)\n- title, description, hashtags[]\n- platforms[]: any of youtube | instagram | tiktok_manual\n- ig_user_id (IG Business account ID)\n- yt_category_id (20=Gaming), yt_privacy\n- operator_email\n- compliance_passed: MUST be true — set only by the producer\'s Compliance gate\n\nProducer: Claude Code session (agentic-clipper repo). Cadence: 4 clips/day.',
  [clipWebhook, normalize],
  { color: 4 }
);

const setupNote = sticky(
  '## One-time credential setup\n\n1. **YouTube account** (youTubeOAuth2Api): Google Cloud project with YouTube Data API v3 enabled, OAuth client, connect on the Upload YouTube Short node.\n2. **Facebook Graph account** (facebookGraphApi): long-lived Page access token with instagram_basic + instagram_content_publish, IG account must be Business/Creator linked to the FB Page.\n3. Outlook is already connected (TikTok manual emails + block alerts).',
  [uploadYouTubeShort, igPublishReel],
  { color: 5 }
);

export default workflow('clip-poster', 'Clip Poster — Autonomous Publishing Rail')
  .add(clipWebhook)
  .to(normalize)
  .to(complianceGate
    .onTrue(markApproved)
    .onFalse(emailBlockAlert))
  .add(markApproved)
  .to(wantsYouTube
    .onTrue(downloadVideo.to(uploadYouTubeShort)))
  .add(markApproved)
  .to(wantsInstagram
    .onTrue(igCreateContainer.to(waitForIgProcessing).to(igCheckStatus).to(igContainerReady
      .onTrue(igPublishReel)
      .onFalse(waitExtraForIg.to(igPublishReel)))))
  .add(markApproved)
  .to(wantsTikTokManual
    .onTrue(emailTikTokManual))
  .add(contractNote)
  .add(setupNote);
