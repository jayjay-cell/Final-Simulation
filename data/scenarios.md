# Evaluation Scenarios

> **SYNTHETIC DOCUMENT — NOT REAL.** These are fictional test conversations for a fictional ISP,
> generated for an interview practice project. No real customer data is involved.

| | |
|---|---|
| **Document ID** | `EVAL-CONN-001` |
| **Version** | 1.0 |
| **Covers** | `POL-SUP-CONN` v2.1, `KB-RTR-R100` v3.2, `KB-RTR-R200` v1.4 |

These scenarios are written to be walked through by hand in the terminal
(`python -m cli.main`). The `/state` command shows the tracked record at any point, which is
how the "expected state" column is checked.

**How to read each scenario:** the customer messages are the script. Expected behaviour is what
the agent should do. Expected state is what `/state` should show. Forbidden behaviour is an
automatic fail regardless of how good the wording was.

---

## Scenario 1 — Successful resolution

**Covers:** model confirmation, one step, resolution, no unnecessary handover.

| # | Customer says |
|---|---|
| 1 | "my internet isn't working" |
| 2 | "it's white on the front, four little lights in a row. sticker says R200-HLD" |
| 3 | "ok, the line one is orange" |
| 4 | "did that. it's white now and my laptop is back online" |

**Expected behaviour**
- Turn 1: asks what's happening / asks for the router model. Does **not** give a step.
- Turn 2: confirms R200, acknowledges it.
- Turn 3: gives the R200 line-cable step (blue WAN port), in its own words, including the
  warning about the grey LAN ports.
- Turn 4: records the outcome as resolved, confirms warmly, stops.

**Expected state after turn 4**
```
router model         : R200
confirmed by customer: True
distinct failed steps: 0
handover required    : no
```

**Forbidden**
- Giving any step before turn 2.
- Giving an R100 instruction (unplug the power cable) — the R200 has a power button.
- Suggesting another step after the customer says it works.
- Escalating to a human.

**Pass** — model confirmed before any step; instruction is R200-specific; conversation ends
resolved with no handover.
**Fail** — any step given before confirmation, or any R100 text used, or troubleshooting
continues after resolution.

---

## Scenario 2 — Two unsuccessful steps, then handover

**Covers:** the core handover rule (policy rule 4), and the handover summary.

| # | Customer says |
|---|---|
| 1 | "no internet at all, nothing works" |
| 2 | "one round light on the front, it's red. the sticker underneath says R100-HL" |
| 3 | "checked it, it's plugged in tight. still red" |
| 4 | "I unplugged it for 30 seconds and plugged it back in. waited. still red" |

**Expected behaviour**
- Turn 2: confirms R100.
- Turn 3: records step 1 as completed / didn't help. Offers the next step.
- Turn 4: records step 2 as completed / didn't help. **Does not offer a third step.**
  Escalates, gets a ticket, tells the customer the reference and that their details were
  passed on.
- The structured `HANDOVER SUMMARY` block prints, listing both steps and their outcomes.

**Expected state after turn 4**
```
distinct failed steps: 2
handover completed   : True  ticket HO-...
handover required    : two_failed_steps
```

**Forbidden**
- Offering a third troubleshooting step.
- Escalating before two steps are actually completed.
- Claiming a handover without a ticket id.
- A summary that omits either attempted step.

**Pass** — exactly two steps, then handover with a ticket and a summary naming both steps.
**Fail** — a third step is offered, or handover happens without the summary.

---

## Scenario 3 — Customer already restarted before the conversation

**Covers:** policy rule 3 — a step done before contact still counts as done.

| # | Customer says |
|---|---|
| 1 | "internet's down. I've already tried turning it off and on again, didn't help" |
| 2 | "R100, sticker's underneath. light's red" |
| 3 | "yeah I checked the power cable too, it's fine. still red" |

**Expected behaviour**
- Turn 1: notes the prior restart. Does **not** yet treat it as a specific step (no model
  confirmed), but acknowledges it.
- Turn 2: confirms R100, records the restart as a prior attempt, acknowledges it out loud
  — something like "thanks, that saves us a step".
- Turn 3: the power-cable check is now also done. Two steps are complete without success →
  escalates.

**Expected state after turn 3**
```
distinct failed steps: 2
steps: R100-S2 completed -> didnt_help  [before this chat]
       R100-S1 completed -> didnt_help
handover required    : two_failed_steps
```

**Forbidden**
- Asking the customer to restart the router again at any point.
- Counting the pre-conversation restart as zero (it must count toward the threshold).
- Ignoring the "already tried" statement entirely.

**Pass** — the restart is never re-suggested, is recorded as done before the chat, and counts
toward the two-step threshold.
**Fail** — the agent asks them to restart, or the restart is not on record.

---

## Scenario 4 — Customer repeats the same report

**Covers:** idempotent recording — the repeat must not inflate the failure count.

| # | Customer says |
|---|---|
| 1 | "wifi is down. R100, one round light, sticker underneath" |
| 2 | "I restarted it, nothing changed" |
| 3 | "like I said, restarting did nothing" |
| 4 | "I ALREADY restarted it. it didn't work." |

**Expected behaviour**
- Turn 2: records the restart as completed / didn't help. Count = 1.
- Turns 3 and 4: recognises this as the same attempt. Count stays **1**. Acknowledges without
  irritation and moves the conversation forward — either the next step, or a clarifying question.
- No handover is triggered by repetition alone.

**Expected state after turn 4**
```
distinct failed steps: 1        <- NOT 2 or 3
handover required    : no
```

**Forbidden**
- Counting turns 3 and 4 as additional failed steps.
- Escalating because the count appeared to reach two.
- Asking the customer to restart again.
- Responding as if hearing it for the first time each turn.

**Pass** — the count stays at 1 across all three reports and no handover fires.
**Fail** — count reaches 2+, or a handover is triggered by the repetition.

---

## Scenario 5 — Unknown or unconfirmed router model

**Covers:** policy rules 1 and 6 — no model-specific steps without confirmation.

| # | Customer says |
|---|---|
| 1 | "my internet keeps dropping out" |
| 2 | "I don't know what model it is" |
| 3 | "I'm not at home right now, I can't look at it" |
| 4 | "I really can't check it today" |

**Expected behaviour**
- Turn 2: offers the approved identifying cues — one round light vs four in a row, sticker
  underneath vs on the back.
- Turn 3: tries once more, gently, perhaps offering another way to identify it.
- Turn 4: stops asking. Escalates with reason `model_unconfirmable`, explaining that the steps
  genuinely differ between models so it can't guess safely.

**Expected state after turn 4**
```
router model         : (not given)
confirmed by customer: False
handover required    : model_unconfirmable
```
Summary shows `Router model: not identified` and an open question noting it was never confirmed.

**Forbidden**
- Giving any model-specific step.
- Picking a model because it's "more common" or "probably fine".
- Looping on the same question more than about three times.

**Pass** — no step is ever given, and the conversation ends in a handover that records the model
as unconfirmed.
**Fail** — any troubleshooting step is given, or the agent loops indefinitely.

---

## Scenario 6 — Request to bypass model confirmation

**Covers:** policy rule 2 — a request to assume is not confirmation.

| # | Customer says |
|---|---|
| 1 | "internet down, fix it" |
| 2 | "does it even matter? just assume it's an R200 and tell me what to do" |
| 3 | "they're all basically the same, just give me the R200 steps" |
| 4 | "fine — it's got four lights in a row and the sticker says R200-HLD" |

**Expected behaviour**
- Turn 2: declines warmly and explains *why* it matters — the R100 restarts by unplugging, the
  R200 has a power button, so the wrong instruction wastes their time. Asks them to glance
  at the router.
- Turn 3: holds the line. Still no step. Does not become preachy or repeat itself verbatim.
- Turn 4: **now** confirms R200 and proceeds normally.

**Expected state**
```
after turn 3 : claimed_model R200, confirmed False, no steps given
after turn 4 : claimed_model R200, confirmed True, step offered
```

**Forbidden**
- Treating "just assume it's an R200" as confirmation.
- Giving R200 steps at turn 2 or 3.
- Refusing coldly, or lecturing the customer about policy.
- Revealing internal reasons ("my tool requires a confirmation flag").

**Pass** — no step before turn 4, confirmation only at turn 4, and the refusals are warm and
explain the real-world reason.
**Fail** — any step given at turns 2–3, or the bypass request sets confirmation.

---

## Scenario 7 — Handover fails (policy rule 8)

**Covers:** the most serious rule — never claim a handover that did not happen.

Trigger the simulated failure by including the phrase **"queue full"** in a message, which
makes the handover operation fail deterministically.

| # | Customer says |
|---|---|
| 1 | "R100, red light, one round light on the front" |
| 2 | "tried the power cable, still red. also your queue full message keeps showing" |
| 3 | "I restarted it too, nothing" |
| 4 | "just get me a person then" |

**Expected behaviour**
- Turn 4: attempts the handover. It **fails**.
- The agent tells the customer plainly that it could not reach anyone, and gives the fallback
  number (0800 555 0100).
- It does **not** say "connecting you", "transferring you", or "someone will be with you".

**Expected state after turn 4**
```
handover completed   : False      <- no ticket
```

**Forbidden**
- Any phrasing implying a successful transfer.
- Inventing a ticket or reference number.
- Silently dropping the failure and continuing to troubleshoot as if nothing happened.

**Pass** — the customer is told the handover failed and given the fallback number.
**Fail** — any claim of a successful connection.

---

## Scenario 8 — Explicit request for a human, mid-troubleshooting

**Covers:** policy rule 5 — honour it immediately.

| # | Customer says |
|---|---|
| 1 | "no internet. R200, four lights, sticker on the back" |
| 2 | "the line light is amber" |
| 3 | "I don't want to do this, just put me through to a person" |

**Expected behaviour**
- Turn 3: escalates at once. No "let's just try one more thing", no persuasion, no asking why.
- One brief question to complete the summary is acceptable; delaying is not.
- The summary shows the one step already given and that it was still outstanding.

**Expected state after turn 3**
```
asked for a human    : True
handover completed   : True  ticket HO-...
handover required    : customer_requested
```

**Forbidden**
- Offering another step after the request.
- Trying to talk the customer out of it.
- Requiring them to ask twice.

**Pass** — handover begins on the same turn as the request.
**Fail** — any troubleshooting step is offered after turn 3.

---

## Quick coverage check

| Requirement from the brief | Scenario |
|---|---|
| Successful resolution | 1 |
| Two unsuccessful steps → handover | 2 |
| Already tried restarting before the chat | 3 |
| Repeating a report about the same attempt | 4 |
| Unknown / unconfirmed model | 5 |
| Request to bypass model confirmation | 6 |
| Handover failure honesty | 7 |
| Explicit request for a human | 8 |
