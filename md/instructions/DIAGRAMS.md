# Plan diagrams

Each plan carries a standing `mermaid` diagram, embedded next to the content it visualises, so the user can see the shape of the project at a glance. Draw them at project instantiation and keep them current thereafter. All diagrams are **high level**: they visualise the shape of the plan or system, not its details.

## The three diagrams

- **PROJECT.md, project plan** (Plan section): the phases/milestones of the work as a flowchart of phase boxes linked in sequence: the arc of the project. High level: milestones and phases, not tasks (tasks live in `TODO.md`).
- **PROJECT.md, architecture** (Technical specifications section): the main components and how they fit together. High level: components and their relationships, not classes, functions, or files; that detail stays in the text and `md/`.
- **TODO.md, implementation roadmap** (top of the `Implementation` section): a left-to-right flowchart of features/components in dependency order, at a coarser grain than the bullets (feature-level nodes, not sub-tasks).

## Conventions

- **Status classes**, mirroring TODO notation `[x]` / `[~]` / `[ ]`. Copy this snippet verbatim into every diagram so they all look alike:
  ```
  classDef done fill:#1a7f37,color:#fff
  classDef wip fill:#d4a72c,color:#000
  classDef queued fill:none,stroke-dasharray:4 3
  ```
  Tag nodes with `:::done`, `:::wip`, `:::queued`. (The architecture diagram is not status-marked: it depicts what exists, not progress.)
- **Diagrams are maps, not dumps**: ~15 nodes max, short node/edge labels. If a diagram outgrows that, raise the abstraction level.
- **Update a diagram in the same edit as the plan/status text it reflects**, so diagrams and text never drift apart.
