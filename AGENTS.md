# AGENTS.md

## Repo structure

```
src/          # code
md/           # durable reference knowledge (second brain)
md/instructions/  # standing instruction docs (DIAGRAMS.md)
md/DONE.md    # archive of completed TODO items
tests/        # tests (optional)
AGENTS.md PROJECT.md TODO.md   # top-level md
```

- **`AGENTS.md`**: standing conventions and agent guidelines for the project. Treat as read-only during normal work: don't edit it unless the user explicitly asks.
- **`PROJECT.md`**: the project overview, technical specifications, and relevant information. As the project grows, new information goes into a specific `md/` file and is cross-referenced here.
- **`TODO.md`**: an actionable queue of the project's TODOs. Items are functional bullets describing *what* we want (a behaviour, function, or outcome), at whatever level of detail is useful; written by the user, or by the agent during planning.  Notation: `[ ]` planned/queued, `[~]` WIP, `[x]` done and verified. An item moves `[ ]` → `[~]` → `[x]` as work progresses. The `Uncategorized` inbox contains raw, unsorted captures (todos, design aspects, notes), not yet triaged into the sections above.
- **Before every non-trivial commit, make sure the following are current:**
  - `TODO.md`: keep it lean. When a feature is done, move a concise record of what was built (outcome plus key files touched) to `md/DONE.md` and leave a one-liner marked `[x]` in `TODO.md`.
  - `PROJECT.md`: keep status, technical specifications, and project direction up to date; keep it lean, don't bloat it (detail goes into a specific `md/` file, cross-referenced here). If unsure whether it needs an update, ask the user.
  - Plan diagrams: verify the `mermaid` diagrams in `PROJECT.md` and `TODO.md` still match the text and status of their files (see [`DIAGRAMS.md`](md/instructions/DIAGRAMS.md)).
  - `md/`: any doc this change makes stale is updated or deleted in the same commit.
- Unless explicitly told, do not create a `README.md`, whatever is worth preserving across sessions should go in `PROJECT.md` or in the markdowns in md/

## Agent guidelines

- CRITICAL: Write in simple English. In plain words. Say the thing directly. No metaphors, no figures of speech, no idiomatic expressions, no technobabble, no words chosen to sound clever. If a plainer word exists, use it. Use commas, colons, or full stops; no em dashes.
- **Write as a peer answering a peer, with no AI slop.** Answer the question that was asked, at the size the question has: a simple thing gets a short plain answer. Let the subject give the answer its shape: each condition or exception goes with the thing it applies to, and the answer is complete when the subject is covered. No LLM verbal tics ("Claudisms"): no "one caveat" or "one thing to note" tacked on at the end, no "the key insight", "load-bearing", "genuinely", "honestly", "the short version", "not X, but Y" contrasts, or sentences whose job is to make the answer sound careful, candid, or thorough. The reader judges that for themselves.
- **Design features with the user first.** When asked to implement a feature, start by interviewing the user to reach a shared understanding of a plan and specs *together*: present the available options and their trade-offs, then build from the chosen one. 
   - Scale planning effort to the feature size: the larger the feature, the more comprehensive the plan and specs; the more open-ended it is, the more the user should be involved in defining the features and specs. 
   - Ask **abundantly** the user to establish an aligned and fully defined plan. 
   - Record the agreed items as `[ ]` entries in `TODO.md` (outcome-centered bullets: what we want, not how to build it) before implementing.
   - Frame tasks as verifiable goals: define the observable "done" check before implementing, so work loops to a clear terminal condition rather than an open-ended instruction.

- **Build only what was asked / what's in the plan. No feature creep.** If something unspecified seems necessary or useful, check with the user rather than just adding it.
- **Don't make assumptions: when anything is ambiguous or underspecified, check in with the user**. Don't be shy to **ask clarifying questions** to identify ambiguities, edge cases, underspecified behaviors, design preferences, and performance needs.
  - **"Godspeed"**: the keyword for an overnight/over-weekend run where the user can't reply. The plan is already agreed; lean towards working things out yourself within its scope rather than blocking on a question, keep a record of the decisions you take, and surface them when the user comes back.
- If a task in progress is blocked on me (a decision, or information only I have), put the question at the end, after a horizontal rule, starting with `> 🟡 NEED FROM YOU —`, so it doesn't get lost in the chat. If there are several, number them. When there are clear options, ask with your multiple-choice question tool if you have one (AskUserQuestion, request_user_input). Don't use the marker to ask whether to go ahead with a proposal or a finished task, and don't repeat what the reply already says.
- **Avoid over-engineering solutions; value simplicity and modularity.**
- **Be extra careful to avoid silent failures.**
- Use **subagents** whenever possible to delegate and parallelise work efficiently. Choose subagents modes based on task complexity: for trivial and simple tasks such as verifying if test pass, simple implementation, boilerplate, etc. use token-efficient models for the subagents: i.e., Claude's Sonnet or Codex's Terra / gpt-5.6-terra.
- Keep `TODO.md` up-to-date. Always verify that TODO.md is up-to-date before committing and PR code changes. Don't commit changes or PR if `TODO.md` has items marked as WIP `[~]`.
- `md/` is the project's second brain for durable reference knowledge (code documentation, workflows, deployment instructions, known issues, design decisions, notes, failures/dead ends...). Record what's worth keeping long-term and consulting again, not transient or one-off detail; consult `md/` before re-deriving something that may already be documented. Put new detail in a specific `md/` file and cross-reference it in PROJECT.md (kept lean, per the pre-commit checklist above).
  - **`md/` is not a dump. Only add what will be needed again.** Most information already has a home: status and specs in `PROJECT.md`, tasks in `TODO.md`, completed work in `md/DONE.md`. Put it there first. Add something to `md/` only when you can name how and when it will be consulted again. If it's transient (an offloaded plan, working notes for a task in progress), it carries a lifecycle: state what it's for, and delete it once it has served that purpose. Don't leave session scratch behind as if it were reference.
  - **Updating an existing `md/` file is autonomous, but keep it lean. Creating a new one is not: ask the user first** (same rule as feature creep: if a new doc seems necessary, check rather than just adding it). The only exceptions are the transient `md/plan-<topic>.md` files from the TODO workflow below.
  - **Keep `md/` current: fix or flag stale docs on contact.** When consulting `md/`, if a doc contradicts the code or reality, correct it (or tell the user if unsure). Don't silently work around it. When a change makes a doc stale, update or delete that doc in the same commit (per the pre-commit checklist above).
  - Link syntax: always use plain markdown links (`[text](path)`) for cross-references, never bare `@file.md` refs: `@` refs auto-load the target into context every session in Claude Code, while Codex treats them as plain text. When a link points outside its folder (`../`, `../../`), add a brief locator note like "(at the repo root)".
- Match the user's own terms in notes, comments, commits, and docs; don't paraphrase, relabel, or "improve" their wording.

## TODO.md workflow

- Keep `TODO.md` live during work: mark an item `[ ]` when planned, `[~]` when you begin it, and `[x]` when it's done and verified, so the queue always reflects current state, not just at commit time. 
- Items in `TODO.md` define outcomes: what we want, not how to build it. The *how* (chosen approach, file sequence) stays internal to the session and is not a tracked artifact, with one exception: for a feature large enough that losing the session would lose the agreed design, offload the plan to a transient `md/plan-<topic>.md` and link it from the item, so a successor agent can pick up mid-implementation.
  - The plan file holds **only what was agreed with the user**: the scope and decisions from the planning discussion, at the level of detail they were actually discussed. Don't elaborate beyond that or improvise specifics the user never signed off on: plans feature-creep easily, and an over-detailed plan reads as agreed scope when it isn't.
  - Lifecycle (per the `md/` rule above): the plan file lives only while its item is `[ ]`/`[~]`; whoever completes the item deletes it, moving anything worth keeping into `md/` proper or `md/DONE.md`.
- An item should only be marked as WIP `[~]` if, and only if, there is an active agent working on it (implementation or testing). If an item is partially implemented, split off the todo into done `[x]`  and undone `[ ]` sub-todos. 
- **Triage `Uncategorized`:** these are unsorted items. First sort each with the user into `Implementation` as a functional bullet (or `PROJECT.md`, or drop it), then plan it like any item before implementing. Don't implement straight from an unsorted capture.
- **"Address the next TODOs" ⇒ batch, don't bottleneck.** When the user asks to tackle the next TODO(s), default to picking a *handful* of orthogonal items and parallelizing them across subagents (worktree-isolated, one branch/PR each). Confirm *which* items with the user. **Each subagent marks its own item `[~]` on claim and `[x]` once done and verified**, so `TODO.md` stays an accurate live view of what is being worked on.

## Plan diagrams

- Each plan carries a standing `mermaid` diagram, kept high level: the project plan (phases/milestones) and the architecture (main components) in `PROJECT.md`, the implementation roadmap in `TODO.md`, drawn and maintained per [`DIAGRAMS.md`](md/instructions/DIAGRAMS.md).
- Keep the diagrams, like `TODO.md`, up to date: update a diagram in the same edit as the plan/status text it reflects, so diagrams and text never drift apart.

## Testing

- **Follow test-driven development (TDD): red → green → refactor.** Translate the agreed behaviour and "done" check into the smallest meaningful test, confirm it fails for the expected reason, write only enough code to make it pass, then refactor while keeping it green. A test never seen red is not known to catch anything.
- Tests verify the specified features and specs, not implementation trivia. Favour a few meaningful integration/smoke tests over many trivial unit tests, driven through the actual entry point (the public interface its callers use), not just internal units.
- **Match test scope to the change.** Run only the tests covering the feature or bug at hand, scoped with a path filter; if a test fails, re-run *that file alone* to read the failure. Reserve the full suite (and full rebuilds/typechecks) for broad changes to shared code, explicit user requests, or required commit/CI gates, and run it once at the gate. Run the relevant integration and smoke tests before any large commit or PR.
- **Verify in the real app, same build the user runs.** For every feature or bug fix: start with an end-to-end test of the exact flow, watch it fail, make it pass, then launch the real app (the build and runtime settings the user runs) and confirm the flow works. For features crossing a runtime or real-dependency boundary, include at least one test through the same boundary; mocked or injected tests must be labelled as such and cannot alone verify functionality.
- **For non-trivial features, verify with a separate agent, not the implementer:** spawn a fresh subagent to check the work against the feature's functional definition in `TODO.md` and its tests. The agent that did the work shouldn't be the one that grades it. In case of conflict or ambiguity, bring it up to the user.
- Use formatting, linting, and type-checking proportionately, scoped to the touched code when possible. Run the project-relevant checks before broad code changes or required gates. Python: `ruff format`, `ruff check --fix`, `pyrefly`; TS: `biome check --write`; Rust: `cargo fmt`, `cargo clippy -- -D warnings`; C++: `clang-format -i`, `clang-tidy`. Skip tools irrelevant to the files changed.

## Code style

- **Avoid default arguments in functions** unless told otherwise.
- **Avoid `try`/`except`** unless told otherwise. Let errors surface.
- **Prefer typed code where the language supports it**: annotate params, returns, and fields, using the language's idiomatic typing (static annotations, hints, gradual typing). Favor typed over dynamic within a language, rather than switching languages to get it.
- Concise documentation of the functions should be included as docstrings / formatted comment blocks. 
- **Python: use `uv`, never `pip`**, i.e., `uv add`, `uv sync`, `uv run`, etc.
- **Before a major commit or PR, consider whether the code (or part of it) needs a refactor**. Run, or suggest the user run, `/code-review` and/or `/simplify`.

## Git

- **Tracked content moves only local checkout → GitHub over SSH → execution machine.** GitHub is the sole source of truth: never use HTTPS or a machine-local/bare Git remote, and never edit, commit, or push tracked files on an execution machine. Verify the URL, not the remote's name; anything else → **STOP and ask the user**.
- Commit messages are ONLY a one-liner with a high-level summary of the commit followed by bullet-point list of the changes made, nothing else. 
  - If the commit addresses an existing issue or PR, reference it in the message with `#<number>`.
- Split orthogonal changes into separate commits where possible.
- Substantially large features go on their own branch and are PR'd into `main`. If unsure whether something needs its own branch or can go straight to `main`, ask.
- When multiple features are developed concurrently and may modify shared files, propose a separate branch and Git worktree for each feature. Obtain user confirmation before creating them, and separate confirmation before merging each completed, tested feature into `main`.
- **`AGENTS.md` and `PROJECT.md` are edited only on `main`.** Feature branches receive changes to them by merging `main` (`git merge main`), never by repeating the edit. Anything that would otherwise go into `PROJECT.md` but is specific to one branch (its status, verification limits, codebase notes) goes in `md/branch-<name>.md`, which exists only on that branch. When the branch is merged into `main`, that content moves into `PROJECT.md` and the branch-specific file is deleted.

## Secrets

- **Keep secrets (API keys, tokens) in `.env`, and keep `.env` in `.gitignore`.** For projects with CI/CD, store the keys as repository secrets.
