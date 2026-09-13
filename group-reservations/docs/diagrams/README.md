# Diagrams

Rendered reference diagrams for this repository. Each is verified against the
current source, not just the docs — see `ARCHITECTURE.md` for the prose
explanation and lighter Mermaid versions that render inline on GitHub.

- **[`architecture.png`](./architecture.png)** — System architecture (UML
  component / deployment view): actors, the FastAPI service on Amazon ECS,
  the Bedrock AgentCore Runtime, and the boto3 call that connects them.
- **[`technical-stack.png`](./technical-stack.png)** — Technical stack
  (layered view): every technology currently running, from the frontend
  down to the third parties it calls, with planned-but-not-built items
  marked explicitly.
- **[`request-flow.png`](./request-flow.png)** — Request flow (UML sequence
  diagram): one worked example, with sample data, from guest survey answers
  through AgentCore to a ranked recommendation.

These were generated from a Claude Design canvas and exported as static
images; they are not auto-regenerated from the diagrams themselves, so they
can drift from the code over time like any other doc — check them against
`src/groupreservations/` when in doubt.
