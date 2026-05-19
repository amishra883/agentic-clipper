"""Dataclasses for records flowing between agents.

Mirror the SQLite schema but stay decoupled — the agents pass these around
without each having to know SQL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

SourcePlatform = Literal["twitch", "youtube", "tiktok", "kick", "instagram"]
TargetPlatform = Literal["tiktok", "instagram_reels", "youtube_shorts"]
Tier = Literal["fast", "pro"]
TrendingFreshness = Literal["hot", "rising", "cooked"]
ShotType = Literal[
    "avatar_reaction",
    "concept_graphic",
    "stat_card",
    "transition_stinger",
    "hero_shot",
]


@dataclass
class CandidateClip:
    id: str
    creator: str
    source_platform: SourcePlatform
    source_url: str
    source_title: str | None = None
    source_duration_s: float | None = None
    source_view_count: int | None = None
    virality_score: float | None = None
    predicted_views: int | None = None


@dataclass
class TranscriptSegment:
    start_s: float
    end_s: float
    text: str
    words: list[dict] = field(default_factory=list)  # word-level for caption burn-in
    # faster-whisper exposes per-segment no_speech_prob (0..1) — the model's
    # estimate that the segment is non-speech. Editor's no-speech quarantine
    # (E-15) checks whether every segment exceeds NO_SPEECH_THRESHOLD; if so,
    # the source is audio-only / instrumental / unintelligible and the clip
    # is shunted to /quarantine/ rather than wasting downstream paid calls.
    no_speech_prob: float | None = None


@dataclass
class ShotListEntry:
    """One visual moment the script implies."""
    shot_type: ShotType
    start_s: float
    duration_s: float
    prompt: str
    reaction_id: str | None = None        # for avatar reactions
    punch_word: str | None = None         # for sync alignment


@dataclass
class Script:
    text: str
    runtime_s: float                       # estimated voice runtime
    substance_tags: list[str]              # prediction / comparison / counter_take / ...
    trending_refs: list[str]
    trending_freshness: TrendingFreshness
    punch_density: float                   # punches per second
    punch_beats: list[float]               # timestamps (s) of punch moments
    shot_list: list[ShotListEntry]
    hook_template_id: str | None = None


@dataclass
class AudioTrack:
    path: str
    runtime_s: float
    loudness_lufs: float
    engine: str                            # coqui_xtts_v2 | elevenlabs
    # Persona's approved voice identifier (matches an entry in
    # persona.yaml voice.approved_voice_ids). Compliance requires this to
    # be both non-empty and a member of the active persona's whitelist —
    # the structural guard against accidental source-creator cloning.
    voice_id: str | None = None


@dataclass
class GeneratedAsset:
    path: str
    duration_s: float
    cost_usd: float
    tier: Tier
    provider: str
    prompt: str
    seed: int | None = None


@dataclass
class CompositedClip:
    """Output of the Compositor, input to Compliance."""
    clip_id: str
    final_video_path: str
    final_duration_s: float
    source_segment_duration_s: float
    commentary_audio_duration_s: float
    audio_track: AudioTrack
    visuals_used: list[GeneratedAsset]
    description: str
    # True = detected (will fail compliance). False = explicitly checked and
    # absent (passes). None = not yet checked (treated as unknown → fail
    # closed in compliance). Compositor MUST NOT hardcode False; it reads
    # the upstream evidence from clip_artifacts.
    has_music_in_source_segment: bool | None
    has_real_face_reference: bool | None
    source_creator: str                    # for attribution check


@dataclass
class ComplianceResult:
    passed: bool
    blocked_reason: str | None
    rule_results: dict[str, dict]          # {rule_id: {passed: bool, detail: str}}
