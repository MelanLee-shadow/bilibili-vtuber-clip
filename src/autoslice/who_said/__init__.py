"""Who said each line of a recording with several speakers, told apart the way a person does it: by the voice, by what
is said and to whom, and by who the speakers are (task speaker-general, owner).  One general method: it knows
nothing of streams, recordings of a guest or a channel's host, so the same code serves a collab clip, a film being
subtitled or a meeting with one mixed sound track.

Inputs (`attribute`):
- `lines`: the transcript, each line with its time (ms) and text;
- `people`: who may speak, each with a name, optional aliases, an optional person card (how they talk, what they are
  like, how they relate to the others: free text) and optional reference voice vectors (any speaker embedding of a
  sample of their voice: an enrolment, an earlier recording);
- `windows`: speaker embeddings of short windows over the same audio (the caller's embedder; `voice` only compares
  them);
- `ask`: an optional reader, ask(prompt) -> answer text, for the context reading;
- `plugged`: optional further evidence per line from the caller (a physical measurement such as the order a line
  reaches several sound paths), each an Opinion with its own weight; the strongest kind decides when present;
- `audio`: optional (samples, rate) of the same sound track, for the channel (`channel`: level, high frequencies and
  bandwidth of each line, learnt inside the recording; it only sends a contradicted conclusion to review).

Output: per line the person or None (undecided, not guessed), how sure, and why, with each source's own opinion,
and who mainly speaks in it with someone else's short stretches as candidates to leave out (`main`).

How each source is trusted (`combine`): the voice of a line is trusted when the line is long enough to hold several
windows of its own and one voice holds them; the context when the reader names a person with a cue it can point at
(the line names its speaker, answers a question put to someone, a person is called by name: the caller is not the one
called).  The voice references come from the clip itself: the lines the context (or plugged evidence) settles first
are each person's voice sample, the rest are judged against them, and the settled set grows over a few rounds
(`voice.bootstrap`).  A reference voice from elsewhere (`voice.references`: an earlier recording whose lines are
settled) seeds its person's lines and rules her out of the lines far from it.  A person with no settled line and no
reference has no voice sample: the voice cannot name her, only say a line is one of the people without one."""

from __future__ import annotations


from .types import Opinion, Person

__all__ = ["Line", "Opinion", "Person", "Said", "VoiceOpinion", "Window", "attribute", "bootstrap", "context_prompt",
           "decide", "read_context", "references"]


