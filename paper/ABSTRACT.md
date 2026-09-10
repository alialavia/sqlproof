# VeriCodeGen @ NeurIPS 2026 — abstract submission

**Track:** Research papers (4–9 pages main text, refs + appendix uncapped)
**Abstract deadline:** 2026-09-11 · **Paper deadline:** 2026-09-13
**Anonymize:** yes (no author names, no `sqlproof.com` / GitHub links in the submitted PDF)
**Target topics:** specification generation and quality · program verification and repair · verification of and for AI

---

## Title (primary)

**Who Checks the Agent's Checks? Mutation-Scored Property Specifications for LLM-Written Database Code**

### Alternates

- *Executable Specifications for Agent-Written SQL: Schema-Derived Falsification with Mutation-Based Adequacy*
- *Vacuous by Construction: Measuring and Repairing the Specifications LLM Agents Write for Database Code*

---

## Abstract (198 words)

LLM coding agents increasingly author the database layer — row-level security
policies, SQL and PL/pgSQL functions, triggers — together with the tests meant
to check it. This is a specification-quality problem: when one model writes both
the artifact and its oracle, example-based tests tend to encode the
implementation's behavior rather than the intended invariant, and pass on code
that is wrong. Deductive verification is a poor fit here, because the semantics
of interest live inside a dialect-rich, stateful engine.

We present SqlProof, a falsification layer that makes database specifications
executable and, critically, *auditable*. A specification is a property over
datasets synthesized from the schema itself: a constraint-aware generator
compiles types, foreign keys, CHECK, UNIQUE, and partial-unique indexes into
strategies, so every dataset is admissible by construction rather than by
rejection, and counterexamples shrink to minimal repro cases. To test whether a
generated specification constrains anything at all, we score it by mutation over
function bodies and RLS predicates, yielding a machine-checkable adequacy signal
that closes a generate–falsify–repair loop for agents.

We evaluate on InboxBench, a multi-tenant application carrying ten seeded
authorization, aggregation, trigger, and vector-search defects, and report
deployment experience on a production SaaS codebase.
