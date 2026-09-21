# Internet Troubleshooting Agent

A proof-of-concept conversational agent for a fictional ISP. It helps customers with basic
internet connectivity problems, and hands over to a human representative when the problem goes
beyond what it is allowed to solve.

> **All data in this project is synthetic.** The router models (R100, R200), the company
> policy, and every scenario were generated for this exercise. No real customer data, no real
> company systems, and no external services are involved. Files under `data/` carry a synthetic
> banner.

---

## Setup

```bash
pip install -r requirements.txt

cp .env.example .env          # then add your Google API key
python -m cli.main
```

`.env` needs one real value:

```
GOOGLE_API_KEY=<your key from Google AI Studio>
ISP_MODEL=gemini-flash-3.6
```

### Terminal commands

| Command | What it does |
|---|---|
| `/state` | Show the tracked troubleshooting record |
| `/summary` | Show the handover summary as it currently stands |
| `/reset` | Start a fresh conversation |
| `/quit` | Exit |

`/state` is the interesting one: it shows what the **code** believes, which is deliberately
independent of what the model believes.

---

## The problem this design solves

The hard part of this assignment is not the chat loop. It is that several rules must hold
**even when the customer argues, repeats themselves, or asks the agent to skip ahead**:

- Don't give model-specific steps before the model is confirmed.
- Don't repeat a step the customer already did.
- Hand over after two distinct failed steps — not one, not three.
- Never claim a handover succeeded when it didn't.

A system prompt can *ask* a model to follow these. It cannot *ensure* it. Over a long, tense
conversation a helpful model will eventually round a corner: the customer says "just assume
it's an R200", and being helpful looks like agreeing.

So the design splits responsibility.

### What the model decides

- Reading vague symptoms and judging which approved step fits.
- Whether a customer's message is genuinely a statement of their router model.
- Wording instructions warmly, in its own voice.
- When a report is ambiguous enough to need a clarifying question.
- Tone, pacing, and everything else that makes it feel like support rather than a form.

### What the code decides

- Whether the model is confirmed (`model_confirmed`, set only by an explicit confirmation).
- Which step comes next, and whether any step is available at all.
- Whether a step has already been completed.
- How many **distinct** steps have failed.
- Whether a handover is required, and why.
- Whether a handover actually succeeded.

The second list is enforced in `core/policy.py` and `tools/`, which return structured errors the
model cannot argue with. The prompt restates these rules so the agent can explain a refusal
gracefully — not because the prompt is what makes them true.

---

## Architecture

```
cli/main.py            terminal chat loop
      │
agent/graph.py         create_react_agent + gates the model cannot influence
      │  ├─ agent/prompt.py      system prompt (behaviour, not enforcement)
      │  ├─ agent/providers.py   Gemini connection, error classification
      │  └─ agent/state.py       transcript + troubleshooting record
      │
tools/                 8 tools, built per-conversation as closures over live state
      │
core/                  pure logic — no LLM, no network, no framework
         ├─ models.py           the data shapes
         ├─ knowledge_base.py   the only source of instruction text
         ├─ policy.py           THE rule enforcer
         └─ handover.py         builds the summary, attempts the send
      │
data/                  synthetic router docs, policy, scenarios
```

`core/` imports nothing from `agent/` or LangGraph. That is what makes "the agent didn't invent
this instruction" checkable by reading one folder rather than trusting a docstring.

### Three structural guarantees

**1. Tools are closures, not functions with parameters.**

`tools/__init__.py` exposes `build_tools(state, kb)`, not a static list. Each tool closes over
the live conversation state. The consequence shows up in the tool schema:

```
get_next_troubleshooting_step   (no arguments)
review_progress                 (no arguments)
check_if_handover_needed        (no arguments)
```

There is no `model_id` parameter on the step tool. "Use the R200 instructions instead" is not a
request that gets rejected — it is *unrepresentable*, because the schema has nowhere to put it.
The step always comes from whichever model is confirmed in state.

**2. Suggested, completed, and outcome are three separate facts.**

`StepAttempt` keeps them apart. A step the agent offered but the customer never did doesn't
count toward the handover threshold. A step done with an ambiguous result blocks progress until
clarified rather than being rounded to a pass or fail.

Records are keyed by `step_id`, so a customer repeating "I told you, restarting didn't help"
updates one record instead of creating three. `failed_step_count()` counts **distinct** step
ids — that one word is what makes the repeat scenario behave.

Everything carries `source="customer_reported"`. This system cannot observe anyone's equipment,
and the data shape doesn't let later code pretend otherwise.

**3. The handover's success is a real value, not an intention.**

`build_summary()` is pure and always works. `send_handover()` is the part that can fail. On
failure it returns an error code and **no ticket id at all** — there is nothing in the result
that could be misread as success.

Two further gates sit in `agent/graph.py`, outside the model's control:

- A message explicitly asking for a human is detected **before** the model runs, so even if the
  model tries "let me just try one more thing", every tool call that turn already sees
  `customer_requested_human=True`.
- After the model replies, the reply is checked for a handover claim. If it claims one that
  didn't happen, the text is replaced. This is the only place a model output is overridden, and
  it is deliberate: telling someone help is coming when it isn't means they stop looking for it.

---

## The eight tools

| Tool | Purpose | What it structurally prevents |
|---|---|---|
| `confirm_router_model` | Record the model | Requires an explicit "the customer stated this" flag; a request to assume records an unconfirmed guess |
| `get_model_identification_help` | Approved identifying questions | Keeps identification wording inside approved text |
| `get_next_troubleshooting_step` | The next approved step | No model argument; never a completed step; blocked before confirmation, after two failures, or while awaiting a result |
| `review_progress` | What's been tried | — (read-only) |
| `record_step_outcome` | What the customer reported | Idempotent per step; rejects unknown or never-suggested steps |
| `record_prior_attempt` | Something done before the chat | Marks it completed so it is never suggested |
| `check_if_handover_needed` | Ask the policy | — (read-only) |
| `escalate_to_human` | Hand over | Relays the real result; no ticket on failure |

---

## Evaluation scenarios

`data/scenarios.md` contains eight multi-turn scenarios covering everything the brief asked
for: successful resolution, two-failures-then-handover, a customer who already restarted,
a customer repeating the same report, an unconfirmed model, a request to bypass confirmation,
plus a handover failure and an explicit request for a human.

Each gives the customer's messages, expected behaviour, expected state, forbidden behaviour,
and pass/fail criteria. They are written to be walked through by hand in the terminal, using
`/state` to check the tracked record.

To exercise the handover-failure path deliberately, include the phrase **"queue full"** in a
message — `core/handover.py` fails the simulated send on that trigger, so rule 8's failure
behaviour can be demonstrated on demand rather than waiting for a random outage.

---

## Assumptions

- **State is in-memory, per process.** A restart loses the conversation. Moving to Redis or
  Postgres means replacing `agent/state.py`; nothing above it reads those fields directly.
- **One conversation per process.** No multi-user session routing.
- **Handover is simulated.** `send_handover()` is where a real ticketing integration would go.
- **`applies_when` is judged by the model, not evaluated in code.** It is prose about symptoms,
  which is a genuine judgement call. Code owns the hard guarantee instead: never returning a
  step that was already completed.
- **Symptoms are captured from raw messages** rather than requiring a tool call, since they are
  free-form context for the summary and no policy gate turns on them.

## Limitations

- **The model ID `gemini-flash-3.6` has not been verified against a live API.** If it is
  rejected on first run, it is a one-line fix in `.env`.
- **No automated tests or eval runner.** Out of scope for this exercise by agreement; the
  scenarios serve as a manual test script. The gates in `core/policy.py` are pure functions and
  can be exercised in a REPL with no API key.
- **Whether a message is genuinely a model confirmation is the model's judgement.** This is the
  one soft spot. The `customer_stated_model` flag turns a vague duty into one explicit claim,
  which is meaningfully more robust than a prompt instruction — but a model that asserts it
  falsely gets through. Everything downstream of confirmation is hard-enforced. Worst case: an
  R100 owner receives R200 steps, which are still approved and low-risk, and the handover
  summary prints `(NOT CONFIRMED by customer)` so the human sees it.
- **Human-request and safety detection are regex-based.** Deliberately narrow to avoid false
  positives, so an unusual phrasing may not trip the pre-turn gate — the model is still
  instructed to escalate, and `escalate_to_human` is never blocked.
- **No API-level auth, rate limiting, or CORS**, because there is no server. These would all
  need addressing before this was reachable beyond localhost.
- **Logs go to `isp_agent.log`** with full exception detail. Fine locally; a real deployment
  would want field allowlisting so message text can't reach the logs.

## Out of scope

No web UI, no server endpoints, no database, no vector store, no authentication.
A local structured knowledge base was sufficient — with two router models and eight steps,
a vector database would add operational weight and a retrieval-miss failure mode in exchange
for nothing.
