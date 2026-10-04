# Scoring model

How a repository becomes a number, and how that number changes when you say
"yes" or "no" to it.

Source of truth: [`src/signalhub/scoring.py`](../src/signalhub/scoring.py).
Every formula and constant below is transcribed from that file.

## The four components

A repo's `total` is a weighted sum of four normalised components, each in `0..1`,
multiplied by `penalty_scale`. There is no fifth component: *momentum* is a
property of the `velocity` component (see below), and *health* is the name of the
HTTP `/health` endpoint, not a score input.

```python
@dataclass
class ScoreWeights:
    velocity: float = 0.40
    engagement: float = 0.20
    relevance: float = 0.25
    developer: float = 0.15
    penalty_scale: float = 1.0
```

| Component | Weight | Question it answers |
|---|---|---|
| `velocity` | 0.40 | Is this repo moving *now*? |
| `engagement` | 0.20 | Do people use it, or just star it? |
| `relevance` | 0.25 | Is it on-topic for the hubs we watch? |
| `developer` | 0.15 | Is it a serious project or a weekend repo? |

`penalty_scale` is excluded from normalisation on purpose: it multiplies a
penalty rather than competing with the positive terms, so folding it into the sum
would change the meaning of the other four.

## Why velocity, not stars

Sorting GitHub search by `stars` surfaces repos that have been accruing stars for
years. What is actually useful is *what is moving right now* — a 900-star MCP
server created six days ago is a better signal than a 30k-star toolchain from
2019.

The hub therefore works from **stars per day**. A repo's first sighting has no
delta, because there is nothing to compare it against; it still scores, using its
absolute age. From the second sighting onward, `observations` carries the previous
star count and the ranking can reward acceleration.

This is why the digest starts showing a `+N` next to the star count once a repo
has been seen twice:

```
⭐ 5,703 (+69) · TypeScript · MIT · 1008⭐/día ▰▰▰▰▰
```

The `+69` is the star delta since the previous run, and it is what turns a static
ranking into a feed.

## Normalisation

`ScoreWeights.normalised()` returns a copy whose positive weights sum to `1.0`.
Two details matter:

1. **Only positive weights are summed.** A weight driven to `0.0` or below stops
   competing, so the remaining terms keep their relative influence.
2. **The all-zero case returns a flat `0.25` each — not the class defaults.**
   Returning `0.40/0.20/0.25/0.15` there would silently swap in a profile nobody
   asked for, which would read as a bug in the learner rather than the intended
   "I don't care" signal.

## The feedback learner

`FeedbackLearner` nudges weights from verdicts. The loop is deliberately
conservative: **a multiplicative nudge bounded by ±25% per verdict, on the
component the verdict is about.** A dozen verdicts can re-shape the ranking; a
single accidental click cannot destroy it.

### Verdicts and their targets

```python
VERDICT_COMPONENT = {
    "star_per_day": ("velocity", +1),
    "relevance":    ("relevance", +1),
    "quality":      ("developer", +1),
    "noise":        (None,        +1),  # raises penalty_scale
}
```

| Verdict | Component affected | Note |
|---|---|---|
| `star_per_day` | `velocity` | "worth more for moving fast" |
| `relevance` | `relevance` | "worth more for being on-topic" |
| `quality` | `developer` | "worth more for being a serious project" |
| `noise` | `penalty_scale` | spam report; `None` component, so it scales the penalty instead |

An unknown verdict raises `ValueError` — there is no silent fallback.

### The algorithm

```
strength   = step * clamp(confidence, 0, 1)          # step defaults to 0.06
direction  = base_sign * (+1 if sign >= 0 else -1)
baseline   = the weight value at construction time
ceiling    = baseline * (1 + MAX_RELATIVE_GROWTH)    # MAX_RELATIVE_GROWTH = 0.60
floor      = baseline * (1 - MAX_RELATIVE_GROWTH)
target     = current * (1 + direction * strength)    # multiplicative
new        = clamp(target, floor, ceiling)
weights    = normalised()
```

`sign` carries polarity when the caller resolved a `yes`/`no` pair. Getting it
backwards is invisible in the output but silently trains the ranking away from
the user.

Two details that are easy to get wrong and were:

- **The nudge is multiplicative, and the result is re-normalised afterwards.**
  Applying the bump to an already-normalised weight set and *not* re-normalising
  is what makes the clamp relative to the baseline meaningful. The docstring is
  explicit that bumping a raw weight and then normalising is a no-op once the
  weights already sum to 1.0 — the bump gets divided straight back out.
- **The clamp is relative to the original baseline, not to the current value.**
  A long run of identical verdicts therefore approaches the cap asymptotically
  instead of saturating on the first one, and negative verdicts get the
  mirror-image floor.

When the component's baseline is `0`, the nudge falls back to an additive
`current ± strength`, since a multiplicative step on zero can never move.

Pinned by `tests/test_scoring.py::TestFeedbackLearner`.

### Resetting

```bash
signalhub --db DB weights --reset
```

Restores the four defaults. Feedback history is not deleted; only the derived
weights are.

## Verdict semantics at the hub level

The CLI exposes the human-facing short form and resolves it to the components
above:

```bash
signalhub --db DB decide owner/repo yes
signalhub --db DB decide owner/repo no
signalhub --db DB decide owner/repo noise
```

A bare `yes`/`no` carries no component information, so the hub resolves the
component from the repo's own weakest dimension. `sign` is propagated from the
hub, which is what makes `no` reliably *lower* a weight.

Two defects made bare verdicts useless before this was fixed, and both are now
pinned by tests:

- `hub.py::_to_scored` invented zero components, so a repo that had not been
  scored yet looked maximally weak everywhere.
- The verdict path forced the `relevance` component for bare verdicts, which made
  `yes` and `no` indistinguishable in their effect on a fast repo.

Pinned by `tests/test_hub_cli.py` and `tests/test_scoring.py`.

## Reading a ranked line

```
1. [KKKKhazix/AIHOT](https://github.com/KKKKhazix/AIHOT)
   一个自己找热点、自己写日报的网站框架。...
   ⭐ 5,637 (+3) · TypeScript · MIT · 1018⭐/día ▰▰▰▰▰
   💡 fast: 1018 stars/day; accelerating: +3 stars in 1h; active use: 26% fork ratio; 5637 watchers
   #ai #chinese #content-curation #daily-digest
```

| Fragment | Meaning |
|---|---|
| `⭐ 5,637` | current stars |
| `(+3)` | delta since the previous run; absent on first sighting |
| `· TypeScript · MIT` | dominant language, license |
| `1018⭐/día` | velocity, the primary ranking signal |
| `▰▰▰▰▰` | normalised velocity as a five-cell bar |
| `💡 fast: …` | the component breakdown behind the total |

## Known limits

- **The learning is heuristic, not ML.** It is a bounded multiplicative nudge on
  four weights. It cannot express anything the components do not already encode,
  and a few unrepresentative verdicts can still skew it.
- **Relevance is keyword- and topic-driven**, so a good repo in an unwatched
  domain scores low. That is the intended trade-off, but it means the topic
  slices in `collector.py` matter more than the weights.
- **A repo's first sighting carries no acceleration signal.** Fresh discovery is
  the weakest case, which is why the window and minimum-star filters exist.
- **Deltas need two runs.** One `collect` gives stars-per-day derived from age;
  real acceleration only appears from the second sighting onward.
