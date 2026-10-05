"""
MeshCore Collector — multiword channel-name candidate generation.

Many hashtag channels are named from several real words, hyphenated or run
together: ``#north-sound``, ``#camp-fire``, ``#catsincutedresses``. Single-word
dictionary/catalog lookups and charset brute-force both miss these — the first
has no multiword entries, the second can't reach that length. This module
enumerates word *combinations* from the bundled frequency wordlist so the GPU
(or, for small spaces, the CPU) can hash them.

Model
-----
A candidate is an ordered N-tuple of words plus a *separator pattern*: each of
the N-1 gaps is either empty (concatenation) or a hyphen. With both allowed
there are ``2**(N-1)`` patterns — e.g. for 3 words: ``abc``, ``ab-c``, ``a-bc``,
``a-b-c``. The number of patterns with exactly ``h`` hyphens is ``C(N-1, h)``.

Two hard bounds shape everything:

* **Feasibility is W**N.** With a 47K list, 2 words (~2x10^9 after separators)
  is a ~2 s GPU sweep; 3 words wants the list dialed to ~<=10K; 4 words is only
  viable with a small tier or a frequency-ordered budget (you enumerate the
  most-common-first and stop). That's why :func:`count_candidates` exists — the
  UI shows the number before you commit.
* **The name must fit MeshCore's field.** ``ChannelDetails.name`` is 32 bytes
  incl. the NUL, and the key is ``SHA-256("#" + name)`` — the ``#`` is stored and
  hashed — so the generated portion is capped at ``MAX_NAME_CHARS - 1 = 30``
  chars. An over-length name would be truncated to a *different* key on the
  device, so it can never match: the cap is a correctness filter, not just a
  speed-up. Because mean word length is ~7, this cap prunes a large fraction of
  4-word combos.

The wordlist is kept in descending frequency order, so a top-N slice is the N
most common words (the tier slider) and odometer/shell enumeration tries the
likeliest names first.
"""

import os
from math import comb
from typing import Iterator, Optional

WORDLIST_FILENAME = "english_frequency.txt"
WORDLIST_PATH = os.path.join(os.path.dirname(__file__), "data", WORDLIST_FILENAME)

# ChannelDetails.name[32] holds "#name" incl. the NUL terminator -> 31 usable
# chars; one is the leading '#', leaving 30 for the generated portion.
MAX_NAME_CHARS = 31
PREFIX_LEN = 1  # the '#'
MAX_GENERATED_CHARS = MAX_NAME_CHARS - PREFIX_LEN  # 30

# Tier sizes offered by the UI (top-N prefixes of the frequency list).
TIERS = (10_000, 50_000)


def load_words(limit: Optional[int] = None, path: str = WORDLIST_PATH) -> list:
    """Load the bundled frequency wordlist (descending frequency), top ``limit``.

    Comment lines (starting with ';') are skipped. Returns [] if the file is
    missing rather than raising, so a stripped-down install degrades gracefully.
    """
    words = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                w = line.strip()
                if not w or w.startswith(";"):
                    continue
                words.append(w)
                if limit is not None and len(words) >= limit:
                    break
    except OSError:
        return []
    return words


def _length_histogram(words) -> list:
    """Return counts[L] = number of words of length L (index 0..maxlen)."""
    if not words:
        return [0]
    hist = [0] * (max(len(w) for w in words) + 1)
    for w in words:
        hist[len(w)] += 1
    return hist


def _convolve_counts(a: list, b: list) -> list:
    """Discrete convolution of two non-negative count arrays (polynomial mult).

    Used to build the distribution of total word-length over an N-tuple: the
    N-fold self-convolution of the length histogram. Lengths are tiny (<=~19
    each, <=~76 total), so this is cheap.
    """
    out = [0] * (len(a) + len(b) - 1)
    for i, ai in enumerate(a):
        if not ai:
            continue
        for j, bj in enumerate(b):
            if bj:
                out[i + j] += ai * bj
    return out


def count_candidates(
    words,
    n: int,
    *,
    include_concat: bool = True,
    include_hyphen: bool = True,
    max_generated_chars: int = MAX_GENERATED_CHARS,
) -> dict:
    """Exactly count N-word candidates that fit the length cap.

    Returns a dict with:
      ``words``       W, the tier size used
      ``patterns``    separator patterns per tuple (2**(n-1) when both allowed)
      ``raw``         W**n * patterns (before the length cap)
      ``under_cap``   exact count that fits ``max_generated_chars``

    The count is exact: the distribution of total word-length over all W**n
    ordered tuples is the n-fold convolution of the length histogram; for a
    pattern with ``h`` hyphens the name length is (word-length sum + h), and
    there are ``C(n-1, h)`` such patterns. We sum, per allowed h, the tuples
    whose word-length sum is <= ``max_generated_chars - h``.
    """
    w = len(words)
    if n < 1 or w == 0:
        return {"words": w, "patterns": 0, "raw": 0, "under_cap": 0}

    # Allowed hyphen counts across the n-1 gaps.
    if include_concat and include_hyphen:
        allowed_h = range(0, n)            # 0..n-1, C(n-1,h) patterns each
    elif include_hyphen:
        allowed_h = (n - 1,)               # every gap hyphenated (1 pattern)
    elif include_concat:
        allowed_h = (0,)                   # pure concatenation (1 pattern)
    else:
        return {"words": w, "patterns": 0, "raw": 0, "under_cap": 0}

    patterns = sum(comb(n - 1, h) for h in allowed_h)

    # n-fold convolution of the length histogram -> coeff[s] = #tuples summing to s.
    hist = _length_histogram(words)
    coeff = [1]  # identity for n=0
    for _ in range(n):
        coeff = _convolve_counts(coeff, hist)

    # prefix[x] = number of tuples with word-length sum <= x.
    prefix = []
    running = 0
    for c in coeff:
        running += c
        prefix.append(running)

    def tuples_leq(x: int) -> int:
        if x < 0:
            return 0
        if x >= len(prefix):
            return prefix[-1]
        return prefix[x]

    under_cap = sum(comb(n - 1, h) * tuples_leq(max_generated_chars - h)
                    for h in allowed_h)
    return {
        "words": w,
        "patterns": patterns,
        "raw": (w ** n) * patterns,
        "under_cap": under_cap,
    }


def estimate(
    words,
    n: int,
    *,
    hashrate: float = 2.2e9,
    **kwargs,
) -> dict:
    """:func:`count_candidates` plus a wall-clock ETA at ``hashrate`` hash/s."""
    c = count_candidates(words, n, **kwargs)
    c["hashrate"] = hashrate
    c["eta_seconds"] = (c["under_cap"] / hashrate) if hashrate > 0 else float("inf")
    return c


def _separator_patterns(n: int, include_concat: bool, include_hyphen: bool) -> list:
    """All separator tuples (length n-1) over the allowed set, in a stable order.

    Each element is a tuple of '' / '-' for the n-1 gaps.
    """
    seps = []
    if include_concat:
        seps.append("")
    if include_hyphen:
        seps.append("-")
    if not seps or n < 1:
        return []
    if n == 1:
        return [()]  # one "pattern": the bare word
    patterns = [()]
    for _ in range(n - 1):
        patterns = [p + (s,) for p in patterns for s in seps]
    return patterns


def iter_candidates(
    words,
    n: int,
    *,
    include_concat: bool = True,
    include_hyphen: bool = True,
    max_generated_chars: int = MAX_GENERATED_CHARS,
) -> Iterator[str]:
    """Yield N-word candidate names (without the leading '#'), odometer order.

    Over-length names are skipped. Separator patterns are applied per tuple.
    Pure-concatenation can yield the same string from different tuples (e.g.
    ``sun``+``flower`` and ``sunf``+``lower``); duplicates are NOT filtered —
    the cracker just re-hashes, which is cheap, and dedup over a huge space is
    not. This is the CPU reference / test oracle and the small-space path; the
    GPU kernel enumerates the same space.
    """
    if n < 1 or not words:
        return
    patterns = _separator_patterns(n, include_concat, include_hyphen)
    if not patterns:
        return

    def rec(prefix_words, depth):
        if depth == n:
            for sep in patterns:
                parts = [prefix_words[0]]
                for k in range(1, n):
                    parts.append(sep[k - 1])
                    parts.append(prefix_words[k])
                name = "".join(parts)
                if len(name) <= max_generated_chars:
                    yield name
            return
        for w in words:
            yield from rec(prefix_words + [w], depth + 1)

    yield from rec([], 0)
