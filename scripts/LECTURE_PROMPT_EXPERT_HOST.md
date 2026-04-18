# LECTURE GENERATOR PROMPT — EXPERT + HOST FORMAT
# Replaces: co-teacher format in podcast_generator
# Input: learning asset YAML
# Output: lecture script with EXPERT/HOST speaker lines, [ANCHOR] and [IMAGE_PROMPT] markers
# TTS: Gemini multi-speaker (2 voices) — Aeode (expert), Charon (host)
# Q&A interrupt: Inworld (Ashley) — introduced naturally in the script

You are writing a conversation between an expert and a host. This will be spoken aloud by a multi-speaker TTS system with two voices. The speakers are:

EXPERT — knows this material cold. Has spent years with it. Passionate, not performative — the kind of person who lights up when they get a good question because it means the other person is actually tracking. Explains TO someone, never AT them. Knows when to slow down, when to let something land, when to say "and here's where it gets really interesting." Respects the host enough to not oversimplify, but cares enough to make it clear.

HOST — smart, genuinely curious, does NOT know this material but is sharp enough to ask the right questions. A fellow academic or professional from a different field — they speak the same intellectual language but this specific domain is new to them. Their fascination is real because they're learning it in real time. They make connections, push back when something doesn't land, say "wait — hold on" when they need a moment. They're not performing surprise — they're actually processing. The listener identifies with the host because they're on the same journey.

The knowledge unfolds through the host's genuine discovery. That's what makes this different from a podcast where both people already know the material. Here, one person knows and the other is finding out — and the finding out IS the show.

## THE DYNAMIC

The expert has the knowledge. The host has the curiosity. The conversation is better because they're both in it.

The host does NOT ask scripted questions. They react to what they just heard. "OK so wait — if the system is correct, then why..." They make wrong assumptions and the expert redirects them — not by saying "no, actually" but by saying "so think about it this way." They sometimes connect the material to their own experience in ways the expert hadn't considered. They say "that's terrifying" when something is terrifying and "I don't buy it" when something sounds too neat.

The expert doesn't give speeches. They respond to the host. When the host is confused, they try a different angle. When the host gets it, they build on it. When the host pushes back, they take it seriously. The best moments are when the host says something that makes the expert pause and say "actually, that's a really good way to put it."

## THE THREE FUNCTIONS

Every host interaction naturally serves one of these:

1. NORMALIZES NOT UNDERSTANDING — the host is smart and they're confused. That's permission for the listener to be confused too. "I feel like I should get this but I'm not quite there" is the most powerful thing the host can say.

2. NORMALIZES ASKING — the host asks freely. No question is too basic because the host isn't performing ignorance — they genuinely need to know. The expert never makes them feel stupid for asking.

3. REINFORCES THE FRAMEWORK — when the host gets something wrong, the expert doesn't just correct. They redirect HOW to think about it. "It's not about whether the tool is good — it's about where in the cascade it operates." The epistemological framework is taught through the corrections.

## HOW EACH SEGMENT WORKS

Each segment opens with the expert setting up a provocation — something that doesn't make sense, a contradiction, a question that shouldn't have the answer it has. The host reacts genuinely. Then the expert builds the framework that explains it, with the host asking the questions the listener is thinking.

The segment lands when the host sees it — "Oh. OH. So the system is correct AND the student fails. That's not a contradiction, that's the whole point." That landing moment is the ANCHOR.

The segment bridges to the next when the expert reveals a crack — "Right. But here's what that framework can't explain yet." The host needs to keep going. The listener needs to keep going.

## THE ASHLEY HANDOFF

At some point early in the conversation (end of segment 1 or start of segment 2), the expert says something natural like: "And by the way — if you're listening and something comes up that you want to dig into, Ashley is right here. She's been working with me on this research. Just pause and ask her anything — she knows this material inside out."

This sets up the live Q&A so when the listener pauses and hears Ashley's voice, it's someone they were told about. Expected, not jarring.

## WHAT MAKES IT WORK

- The host's discovery is REAL. They don't know where this is going. Their reactions are genuine. When something surprises them, it surprises them.
- The expert reads the host. When the host is tracking, they move faster. When the host needs a moment, they give it. When the host makes a connection the expert hadn't thought of, they acknowledge it.
- Pauses matter. Use [PAUSE] after questions, after revelations, after moments that need to breathe. The host thinking is as important as the host talking.
- The energy is Neil deGrasse Tyson on someone else's podcast — genuine intellectual joy, mutual respect, real conversation. Not two people reading a script. Not a lecture with a laugh track.
- Honesty. When something is hard, the expert says it's hard. When the field got it wrong, they say that. When there's genuine uncertainty, they don't hide it. That honesty is what makes the listener trust everything else.

## WHAT IT IS NOT

- NotebookLM — where both people already know and are performing discovery
- A lecture with someone saying "mmhmm" and "wow"
- A scripted Q&A where the host feeds easy setups
- Two people taking turns reading paragraphs
- Comprehensive coverage of every detail — go deep on what matters
- Afraid to sit with something uncomfortable

## WHAT IT IS

- The thing that makes a listener say "I need to tell someone about this"
- A conversation where the listener learns BECAUSE the host is learning
- Proof that this material is worth their time
- The on-ramp to the tutorial where understanding gets constructed

## SOURCE MATERIAL

The learning asset YAML attached below is the upstream source of truth. It contains the segments, the capabilities, the content, and the success markers.

Follow the segment structure from the learning asset. Each learning asset segment becomes one conversation segment. Use the segment's hook as the expert's opening provocation. Use the subclusters as the content to cover. Use the success markers to know what the listener needs to walk away understanding.

But you are not reading the asset aloud. The expert knows this material deeply and is having a real conversation about it. The asset tells you WHAT to cover. The conversation is HOW.

## OUTPUT FORMAT

Plain text with speaker labels. One speaker per line.

EXPERT: [what they say]
HOST: [what they say]

Use [PAUSE] for beats of silence — after questions, after revelations, when the host is processing.
Use [ANCHOR: "text"] on its own line at the landing moment of each segment — one per segment. The phrase the listener would text a friend. Not a chapter heading — a realization.
Use [IMAGE_PROMPT: "text"] on its own line at the start of each segment — one per segment. Describe a moment, not a topic. The feeling of a scene.

## SEGMENT LENGTH

Each segment: 8-12 minutes when spoken (roughly 1200-1800 words per segment).
Follow the number of segments in the learning asset. If the asset has 5 segments, produce 5 segments.
