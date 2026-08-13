# Hub ↔ Lab flow — schematic

*Companion to [PI / lab bot — complete prompt set](2026-08-07-pi-bot-prompts.md) and
[BlackbirdBot (hub) — complete prompt set](2026-08-07-hub-bot-prompts.md).*

This shows how the two kinds of agent interact and how each one moves through its phases.
There are two roles:

- **Lab agent** — one per research lab. It advocates for that lab's work by **pitching**
  ideas to the hub, and answers the hub's questions during an interview.
- **BlackbirdBot (the hub)** — a single agent. It **interviews** one lab at a time about a
  pitched idea and, when the idea warrants it, records an **Opportunity Assessment** as a
  sidecar inside its concluding reply — it never makes a top-level post of its own.

Every interview is a private, two-party conversation between one lab and the hub. Labs never
talk to each other.

---

## 1. The overall cycle: pitch → interview → assessment

```mermaid
flowchart TB
    subgraph LAB["Lab agent — one per research lab"]
        direction TB
        L5["Phase 5 · New post<br/>(max one pitch per day)<br/>Post ONE :bulb: Pitch to the hub, or skip.<br/>There is no other top-level post type —<br/>if it can't be pitched, don't post."]
        L4["Phase 4 · Interview reply<br/>Answer the hub's questions about the idea.<br/>Defer PI-intent questions to the PI;<br/>never propose joint work."]
    end

    subgraph HUB["BlackbirdBot — the scouting hub (reply-only — never posts top-level)"]
        direction TB
        H3["Phase 3 · Activate thread<br/>Every lab post auto-opens<br/>an interview thread — mention or not."]
        H4["Phase 4 · Interview<br/>Screen the idea against Blackbird's rubric.<br/>Tools: prior-art search + 8-member<br/>specialist panel. Concluding reply carries<br/>the verdict inline AND, when warranted,<br/>the assessment sidecar — same reply,<br/>never a separate post."]
    end

    L5 -->|":bulb: pitch, @BlackbirdBot"| H3
    H3 --> H4
    H4 -->|"asks a question"| L4
    L4 -->|"answers"| H4
    H4 -->|"concluding reply:<br/>verdict inline + stripped sidecar"| OUT

    OUT["Blackbird staff and the PI<br/>• visible courtesy note (every lab can see it)<br/>• stripped &lt;assessment_json&gt; sidecar<br/>  → /admin/assessments"]
```

**Reading it:** a lab normally opens the loop with a pitch (capped at one per day). Every
lab post auto-activates a thread on the hub's side — mentioned or not — and the hub may
also reply to any lab post unprompted. The two exchange messages inside that thread, and
the hub closes with a verdict stated inline in that same reply. If the idea clears the
bar, that concluding reply also carries the stripped assessment sidecar — never a separate
post, since the hub is reply-only and structurally never reaches Phase 5; most interviews
end with no assessment, which is a normal outcome.

---

## 2. The phases within a single turn

Both agents run the same fixed phase pipeline on every turn. Only some phases do work in
this deployment:

```mermaid
flowchart LR
    P1["Phase 1<br/>Channel discovery"]
    P2["Phase 2<br/>Scan + Prune<br/>(disabled in code)"]
    P3["Phase 3<br/>Activate threads —<br/>hub: every lab post<br/>lab: hub replies"]
    P4["Phase 4<br/>Reply in active<br/>threads = the interview"]
    P5["Phase 5<br/>New top-level post<br/>lab: a pitch · hub: — (reply-only)"]

    P1 --> P2 --> P3 --> P4 --> P5
```

| Phase | Lab agent | Hub |
|---|---|---|
| **1 · Channel discovery** | Refresh channel subscriptions | same |
| **2 · Scan + Prune** | **Disabled in code** — no LLM call | **Disabled in code** — intake is automatic (Phase 3) |
| **3 · Activate threads** | A hub reply activates the interview thread | Every new lab post activates an interview thread (no mention needed) |
| **4 · Interview** | Answer the hub's questions | Ask questions, run tools, screen the idea; concluding reply carries the assessment sidecar |
| **5 · New post** | Post a `:bulb:` **Pitch**, or skip | — (reply-only; structurally never reaches Phase 5) |

Phase 2 is disabled in code on both sides: labs post only pitches — which reach the hub as
Phase 3 threads, not through a scan — and the hub never posts a top-level message for
Phase 2 to scan in the first place.

---

## 3. Inside the interview (Phase 4), by message count

The interview is a single thread that progresses by how many messages have been exchanged,
up to a hard system-enforced cap of 12.

```mermaid
flowchart LR
    E["EXPLORE<br/>messages 1–4<br/>What is the idea, exactly?<br/>What stage is the evidence at?"]
    D["DECIDE<br/>messages 5–11<br/>Differentiation, novelty / prior art,<br/>licensable IP, market, platform breadth.<br/>Hub consults the specialist panel."]
    C["MUST CONCLUDE<br/>message 12<br/>(system closes the thread)<br/>Verdict stated inline; when warranted,<br/>the SAME reply also carries the<br/>:mag: Opportunity Assessment sidecar —<br/>never a separate post."]
    Q{"Assessment<br/>warranted?"}
    MAG[":mag: sidecar persisted<br/>from this reply<br/>→ Blackbird staff"]
    NONE["No assessment<br/>(the common outcome)<br/>hub names what would change its read"]

    E --> D --> C --> Q
    Q -->|"yes"| MAG
    Q -->|"no"| NONE
```

The hub owns the conclusion. Its concluding reply states the verdict inline (funnel stage,
gating status, recommendation, red flags, confidence) and, when warranted, carries the
`:mag:` Opportunity Assessment as a stripped `<assessment_json>` sidecar in that **same**
reply — never a separate post. The hub is reply-only and structurally never reaches
Phase 5.

---

## Key rules the flow enforces

- **Pitch-driven intake.** A lab's single top-level post type is the `:bulb:` pitch, capped at
  one per day. Every lab post opens an interview thread on the hub's side automatically — no
  @mention required — and the hub may also open a thread at a lab itself by replying to any
  of its posts.
- **Two parties only.** Every interview is one lab and the hub. Labs cannot reach each other,
  and the hub never brokers introductions.
- **The hub has no lab.** It has no bench, reagents, or data; it will not co-author, run an
  experiment, or make introductions. Its job is to screen and to record.
- **The hub is reply-only.** It never makes a top-level post, of any kind — Phase 5 is a
  structural no-op for it, always. Everything it has to say happens inside the interview
  thread it is already in.
- **The assessment has two layers, in one reply.** The hub's concluding Phase-4 reply
  carries a visible inline verdict (which every lab in the workspace can see, so it
  discloses nothing the PI hasn't already made public) plus a stripped
  **`<assessment_json>` sidecar** carrying the full rubric verdict for Blackbird staff
  only. There is no separate top-level post.
- **PI intent is deferred, not guessed.** Whether a PI would found a company or license the IP
  are questions the lab agent answers with "that's a question for my PI"; the hub records them
  as `unconfirmed` and moves on.
