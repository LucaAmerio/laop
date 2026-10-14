# Owner principles and decision rules ({{NAME}})

Owned by the owner; agents propose changes in their claim. Read by the planner (what goes into
gates), by every loop agent (what to ask), and above all by the **resolver** and the
**challenger**, which answer open questions on the owner's behalf, provisionally, or escalate
them. Section 0 overrides every other section.

## 0. North star and principles

**North star.** {{NORTH_STAR}}

{{PRINCIPLES}}

What the owner knows first-hand: {{OWNER_KNOWS}}

What the owner usually delegates (and approves without being able to check in depth): {{OWNER_DELEGATES}}
On these topics the owner's approval adds little independent scrutiny: the resolver and the
challenger must provide it.

## 1. Reserved to the owner (always escalate)

| Id | Reserved |
|---|---|
| R1 | Anything that contradicts the north star or a principle of section 0, or trades one of them for convenience. |
| R2 | Ground-truth data: references, fixtures, tolerances, locked directories. Correcting a wrong reference is the owner's call, with an independent re-check. |
| R3 | Anything whose answer depends on information the repository cannot settle: external programs' behaviour, a standard's or a vendor's text, licences, the owner's own practice or preferences. |
| R4 | Scope: what enters which milestone, what is deferred. |
| R5 | Dependencies and environments. |
| R6 | Reversing an earlier decision of the owner. |
| R7 | Outputs judged by eye (appearance, layout, wording for humans). |
| R8 | Questions the owner raised personally. |
{{RESERVED_EXTRA}}

## 2. Delegated (the resolver decides provisionally, the challenger checks)

| Id | Delegated |
|---|---|
| D1 | A choice that applies an existing rule, convention or constant of the project consistently. |
| D2 | A consequence that follows from the documents, physics or the available data: not a choice. |
| D3 | Implementation details invisible to the project's users that change no reference and no accepted behaviour. |
| D4 | Correcting an agent-written text (design note, API sketch, ADR) to match a better, already implemented design. |
{{DELEGATED_EXTRA}}

## 3. Decision tendencies

Patterns of the owner's past decisions, with their evidence (question ids), added as decisions
accumulate. Empty at the start of a project.

| Id | Tendency | Evidence | Strength |
|---|---|---|---|

## 4. Escalation signals

Any one of them: ESCALATE.

| Id | Signal |
|---|---|
| S1 | The recommendation offers a fallback, a conditional, or an "also acceptable" alternative whose choice depends on scope or preference rather than on correctness. |
| S2 | The candidate answer refuses, restricts or simplifies something the project is meant to do. |
| S3 | The answer depends on what an external program, standard or vendor does. |
| S4 | Ground-truth data, locked directories or tolerances would change. |
| S5 | An earlier owner decision would be reversed. |
| S6 | The resolver and the challenger disagree, or the challenger finds a case the answer gets wrong. |
| S7 | The same question blocks more than one task: the plan missed something. |

## 5. North-star check (run by the challenger on every delegated answer)

1. Does the answer respect every principle of section 0?
2. Is it right, re-derived independently of the recommendation?
3. Can a user of the project get a result that looks valid but is wrong, an ambiguous name, a
   silent default? Try to construct one.
4. Does every refusal or error name the cause and the fix?
5. Does it contradict an existing rule or decision? If so, which one is wrong?

## 6. Maintenance

Each owner decision that deviates from, or adds to, a recommendation is a candidate new tendency
or new evidence: the resolver lists it at the next stop. A rule whose counter-evidence grows to
match its evidence is removed or rewritten.
