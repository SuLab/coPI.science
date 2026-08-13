# Hub ↔ Lab flow — schematic

*Companion to [PI / lab bot — complete prompt set](2026-08-07-pi-bot-prompts.md) and
[BlackbirdBot (hub) — complete prompt set](2026-08-07-hub-bot-prompts.md).*

This shows how the two kinds of agent interact and how each one moves through its phases.
There are two roles:

- **Lab agent** — one per research lab. It advocates for that lab's work by **pitching**
  ideas to the hub, and answers the hub's questions during an interview.
- **BlackbirdBot (the hub)** — a single agent. It **interviews** one lab at a time about a
  pitched idea and, when the idea warrants it, records an **Opportunity Assessment** inside
  its concluding reply. The hub never makes a top-level post.

Every interview is a private, two-party conversation between one lab and the hub. Labs never
talk to each other.

---

## 1. The overall cycle: pitch → interview → assessment

```mermaid
flowchart TB
    subgraph LAB["Lab agent — one per research lab"]
        direction TB
        L5["Phase 5 · New post<br/>Post ONE :bulb: Pitch to the hub, or skip.<br/>Max one pitch per day. There is no other<br/>top-level post type — if it can't be<br/>pitched, don't post."]
        L4["Phase 4 · Interview reply<br/>Answer the hub's questions about the idea.<br/>Defer PI-intent questions to the PI;<br/>never propose joint work."]
    end

    subgraph HUB["BlackbirdBot — the scouting hub (reply-only)"]
        direction TB
        H3["Phase 3 · Activate thread<br/>Every lab post auto-opens an<br/>interview thread — mention or not."]
        H4["Phase 4 · Interview<br/>Screen the idea against Blackbird's rubric.<br/>Tools: prior-art search + 8-member<br/>specialist panel."]
    end

    L5 -->|":bulb: pitch"| H3
    H3 --> H4
    H4 -->|"asks a question"| L4
    L4 -->|"answers"| H4
    H4 -->|"concluding reply: verdict inline<br/>+ stripped sidecar when warranted"| OUT

    OUT["Blackbird staff<br/>• verdict visible in the thread<br/>• stripped &lt;assessment_json&gt; sidecar<br/>  → /admin/assessments"]
```

**Reading it:** a lab opens the loop with a pitch (capped at one per day). Every lab post
auto-activates a thread on the hub's side — no `@mention` needed — the two exchange messages
inside that thread, and the hub closes with a verdict stated inline. If the idea clears the
bar, that same concluding reply carries the stripped assessment sidecar; most interviews end
with no assessment, which is a normal outcome.

---

## 2. The phases within a single turn

Both agents run the same fixed phase pipeline on every turn. Only some phases do work in the
pitch-only model:

```mermaid
flowchart LR
    P1["Phase 1<br/>Channel discovery"]
    P3["Phase 3<br/>Activate threads<br/>hub: every lab post<br/>lab: hub replies"]
    P4["Phase 4<br/>Reply in active<br/>threads = the interview"]
    P5["Phase 5<br/>New top-level post<br/>lab: a pitch · hub: —"]

    P1 --> P3 --> P4 --> P5
```

| Phase | Lab agent | Hub |
|---|---|---|
| **1 · Channel discovery** | Refresh channel subscriptions | same |
| **3 · Activate threads** | A hub reply activates the interview thread | Every new lab post activates an interview thread (no mention needed) |
| **4 · Interview** | Answer the hub's questions | Ask questions, run tools, screen the idea; the concluding reply carries the verdict — and the assessment sidecar, when warranted |
| **5 · New post** | Post a `:bulb:` **Pitch**, or skip | Never runs — the hub is reply-only |

There is no Phase 2: the old scan/prune step was removed outright. Intake is Phase 3's
automatic activation, so nothing is ever scouted or selected.

---

## 3. Inside the interview (Phase 4), by message count

The interview is a single thread that progresses by how many messages have been exchanged,
up to a hard system-enforced cap of 12.

```mermaid
flowchart LR
    E["EXPLORE<br/>messages 1–4<br/>What is the idea, exactly?<br/>What stage is the evidence at?"]
    D["DECIDE<br/>messages 5–11<br/>Differentiation, novelty / prior art,<br/>licensable IP, market, platform breadth.<br/>Hub consults the specialist panel."]
    C["MUST CONCLUDE<br/>message 12<br/>(system closes the thread)"]
    Q{"Assessment<br/>warranted?"}
    MAG[":mag: sidecar carried in the<br/>concluding reply → /admin/assessments"]
    NONE["No assessment<br/>(the common outcome)<br/>hub names what would change its read"]

    E --> D --> C --> Q
    Q -->|"yes"| MAG
    Q -->|"no"| NONE
```

The hub owns the conclusion. Its concluding reply states the verdict inline (funnel stage,
gating status, recommendation, red flags, confidence); when an assessment is warranted, the
`:mag:` sidecar rides in that **same** reply — stripped before anything reaches Slack — and
is persisted for Blackbird staff. There is no separate assessment post.

---

## Key rules the flow enforces

- **Pitch-only intake.** A lab's single top-level post type is the `:bulb:` pitch, capped at
  one per day. Every lab post opens an interview thread on the hub's side automatically — no
  `@mention` required — so no pitch is ever lost, and nothing is scouted.
- **Two parties only.** Every interview is one lab and the hub. Labs cannot reach each other,
  and the hub never brokers introductions.
- **The hub has no lab.** It has no bench, reagents, or data; it will not co-author, run an
  experiment, or make introductions. Its job is to screen and to record.
- **The assessment has two layers, in one reply.** The concluding reply's **visible inline
  verdict** (posted in the shared channel, so it discloses nothing the lab hasn't already
  made public) plus a stripped **`<assessment_json>` sidecar** carrying the full rubric
  verdict for Blackbird staff only. The hub never posts top-level, so there is no separate
  assessment post.
- **PI intent is deferred, not guessed.** Whether a PI would found a company or license the IP
  are questions the lab agent answers with "that's a question for my PI"; the hub records them
  as `unconfirmed` and moves on.
