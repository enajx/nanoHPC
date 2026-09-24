> **Template: replace this content.** This file is a scaffold meant to be copied into
> each repo and then kept up to date. Everything below describes what each section should contain; it is *instructions to be replaced* with the project's actual content, not text to keep.

PROJECT.md holds the project overview, technical specifications, and relevant information (i.e., what the project is, how it's built, and where it stands).

PROJECT.md headers and content to be instantiated:

- Project gist:
  - Gist and Goal: what the project is, what it does, and why it exists (the problem it solves, or simply what it's for), and who it's for.
  - Plan: a high-level implementation/roadmap plan: the phases/milestones and the arc of the work. Keep it high level: the concrete, actionable, task-level items live in TODO.md, not here. Updatable as the project progresses.
    - The plan carries a standing `mermaid` diagram: phases/milestones linked in sequence, status-marked per [`DIAGRAMS.md`](md/instructions/DIAGRAMS.md), kept current as the plan evolves.
- Technical specifications: the tech stack and architecture: the main components, how they fit together, and how the project is run/built/deployed. Keep it high level and cross-reference detail into `md/` via plain markdown links (`[text](md/filename.md)`), fetched on demand.
  - The section carries a standing `mermaid` architecture diagram: the main components and how they fit together, high level per [`DIAGRAMS.md`](md/instructions/DIAGRAMS.md).
- Status: project status, how it's going, what has been built, what works, what doesn't, known issues.
- Notes: diverse things relevant that don't belong to the other points.
- References: name of relevant zotero collection. (optional)
