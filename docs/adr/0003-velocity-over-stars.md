# 0003 — Rank on velocity, not absolute stars

- **Status:** accepted
- **Date:** 2026-10-04

## Context

The literal request was "filter new GitHub repos with the most stars". Taken
literally, sorting by `stars` returns large, long-established projects — which are
neither new nor newly interesting.

## Decision

Rank on **stars per day**, not absolute stars, and keep a history of observations
so acceleration becomes measurable from the second run onward.

## Why

- **Stars accumulate; they do not indicate motion.** A repo that gained 10,000
  stars over three years and a repo that gained 3,000 last week rank identically
  under `sort=stars`. Only the second one is worth interrupting someone for.
- **A repo's first sighting has no delta**, so run 1 ranks on age-derived velocity
  and later runs can reward real acceleration. This is why `observations` exists
  as a separate table rather than overwriting a single star count.
- **The digest surfaces the delta** (`+69` next to the star count), which makes
  the momentum visible to the reader instead of hiding it in the score.

## Alternatives rejected

- **Absolute stars.** The literal reading, and it produces a list of famous
  projects. Kept only as one of several query slices, never as the ranking key.
- **Watchers or forks instead.** Both correlate with stars and both can be
  gamed; stars-per-day is the most direct measure of attention and is what the
  request asked for.
- **A time-series model.** Real acceleration needs more than two observations to be
  meaningful, and a proper model is unjustified for a heuristic ranking whose
  weights are user-tuned anyway.

## Consequences

- Discovery quality is coupled to run cadence: a repo that appears and vanishes
  between runs is invisible. The window filter bounds this.
- The minimum-star floor is still needed. Without it, a repo created ten minutes
  ago with three stars tops the velocity table.
- Fresh repos carry no acceleration signal, which is an accepted blind spot
  rather than something the scoring pretends to solve.
