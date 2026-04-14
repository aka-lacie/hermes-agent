# Honcho First-Turn Continuity Handoff

## Current State

- Immediate stopgap: `injectionFrequency` is set to `first-turn`.
- Reason for the stopgap: this effectively disables the noisy per-turn Honcho memory append after turn 0, without removing Honcho entirely.
- Goal: keep the agent feeling continuous across sessions without appending dialectic summaries to every user message.

## Problem

The current Honcho integration mixes two different concerns:

1. Stable first-turn identity/context
2. Per-turn memory injection

The second path is noisy. It uses the previous raw user message as a dialectic query and appends the result to the next user message. This can feel lagged, awkward, and redundant.

## Desired Behavior

Use Honcho only at session start to provide:

1. Peer/identity context
2. Cross-session continuity

That means:

- No per-turn Honcho summary injection
- A stronger first-turn block that includes not just peer cards/representations, but also a synthesized handoff from prior session state
- Stable prompt after initialization for prompt-cache friendliness

## Existing Code Paths

### First-turn/system prompt path

- Session init prewarms Honcho in [plugins/memory/honcho/__init__.py](../plugins/memory/honcho/__init__.py)
  - `_do_session_init()`
  - starts:
    - `prefetch_context(self._session_key)`
    - `prefetch_dialectic(self._session_key, "What should I know about this user?")`
- First-turn prompt block is built in [plugins/memory/honcho/__init__.py](../plugins/memory/honcho/__init__.py)
  - `system_prompt_block()`
  - currently only consumes `get_prefetch_context()`
  - currently formats peer cards/representations only

### Per-turn injected summary path

- Current user-message injection happens in [run_agent.py](../run_agent.py)
  - `run_agent.py` appends prefetched external memory to the live user message before the API call
- Honcho fills that path through [plugins/memory/honcho/__init__.py](../plugins/memory/honcho/__init__.py)
  - `prefetch()`
  - `queue_prefetch()`
- The dialectic query currently uses the previous raw user message as the query seed

### Continuity synthesis already exists, but elsewhere

- A separate Honcho path in [run_agent.py](../run_agent.py) already reads:
  - `self._honcho.pop_dialectic_result(self._honcho_session_key)`
  - and appends it as `## Continuity synthesis`
- This means the codebase already has the concept of a continuity block, but it is not cleanly unified with the plugin first-turn block

## Why `first-turn` Is Promising

`first-turn` can be repurposed as:

- "Inject continuity once at session start"

instead of:

- "Do peer-card warmup once, then maybe still keep legacy continuity elsewhere"

This works best when `sessionStrategy` reuses meaningful Honcho sessions, such as:

- `per-directory`
- `per-repo`
- possibly `global`

It is much less useful with:

- `per-session`

because that creates a fresh Honcho session every run and weakens continuity.

## Proposed Long-Term Design

### Principle

Keep Honcho first-turn-only for automatic injection. Do not revive the noisy per-turn append path.

### First-turn block contents

The first-turn Honcho block should contain:

1. `User self-representation`
2. `AI model of user`
3. `AI self-representation`
4. `User model of AI`
5. `Continuity synthesis`

### Continuity synthesis query

Replace the generic:

- `What should I know about this user?`

with a continuity-oriented query such as:

`Summarize the continuity needed to resume naturally with this user. Focus on what we were recently working on, unresolved threads or promises, recent emotional or situational context that still matters, and anything that should shape the next reply. Be concise and concrete.`

The important point is that this query should be fixed and purpose-built for session handoff, not derived from the first live user utterance.

## Voice / Representation Notes

Honcho has different memory artifacts with different roles. They should not all be pushed into agent-voice output.

### Peer cards

Peer cards should remain factual and structured.

- Honcho's peer card generation is explicitly designed around durable fact entries such as:
  - `Name: Alice`
  - `PREFERENCE: Prefers detailed explanations`
  - `TRAIT: Analytical thinker`
- This is the wrong layer for first-person or in-character prose.
- Do not try to make peer cards "sound like agent."

### Representations

Representations are more flexible than peer cards, but Honcho still generates them from an analyst/synthesizer perspective by default.

- This means they may be about the agent or from the agent's perspective in a loose sense,
  but they are not naturally written in the agent's own voice.
- If upstream ever adds configurable representation voice, that could be reconsidered.

### Continuity synthesis

If the agent should feel like a synchronous being across sessions, the right place to express that is the continuity block.

- Keep peer cards factual
- Keep representations as memory artifacts
- Make only the first-turn continuity synthesis eligible for first-person / agent-voice output

That preserves memory structure while making the session handoff feel natural.

## Minimal Implementation Plan

1. Keep `injectionFrequency=first-turn` as the short-term default.
2. Leave per-turn Honcho injection disabled by policy.
3. In `plugins/memory/honcho/__init__.py`:
   - continue prewarming `prefetch_context(...)`
   - prewarm dialectic with the continuity query above
4. Extend `system_prompt_block()` so it also consumes the prefetched dialectic result and appends it as `## Continuity synthesis`
5. Stop relying on the separate legacy continuity path in `run_agent.py` for this purpose, or ensure it does not duplicate the plugin block
6. Preserve prompt-cache stability by baking this block once per session and caching it
7. If agent-voice output is desired, apply it only to the continuity synthesis query/result, not to peer cards

## Nice-to-Have Follow-Up

- Add a dedicated Honcho config knob for the first-turn dialectic query template
- Optionally split first-turn behavior into:
  - `peer-card-only`
  - `peer-card-plus-continuity`
- Add a regression test asserting:
  - first-turn system prompt contains continuity synthesis
  - later turns do not append Honcho summaries to the user message

## Relevant Files

- [plugins/memory/honcho/__init__.py](../plugins/memory/honcho/__init__.py)
- [plugins/memory/honcho/session.py](../plugins/memory/honcho/session.py)
- [plugins/memory/honcho/client.py](../plugins/memory/honcho/client.py)
- [run_agent.py](../run_agent.py)

## Notes

- If upstream fixes the noisy per-turn behavior cleanly, prefer upstream.
- If upstream does not address the continuity/handoff use case, this plan should be a small, focused local patch rather than a broader Honcho redesign.
