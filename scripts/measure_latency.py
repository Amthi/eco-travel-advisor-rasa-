"""Measures end-to-end response time of the running bot (REST channel).

Run Rasa + the action server first, then:
    python scripts/measure_latency.py                      # local, 5 conversations
    python scripts/measure_latency.py --url https://<user>-<space>.hf.space --runs 10

Each run is one full trip-planning conversation, so the slowest turn (the one
that chains fetch -> carbon -> rank) is included. Quote the p95 and max in the
report, together with where you ran it (local CPU vs Hugging Face free Space).
"""
import argparse
import statistics
import time
import uuid

import requests

TURNS = [
    ("greet", "hi"),
    ("destination", "I want to go to Lisbon"),
    ("origin", "I'm travelling from Berlin"),
    ("dates", "12-18 December 2026"),
    ("budget", "my budget is 1500 euros"),
    ("preference", "balanced"),          # <- triggers fetch + carbon + rank
    ("carbon question", "what is the carbon footprint of flying to Rome?"),
    ("experiences", "what local experiences do you recommend?"),
    ("offsets", "how can I offset my emissions?"),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:5005")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--limit", type=float, default=3.0, help="latency target in seconds")
    args = ap.parse_args()
    endpoint = args.url.rstrip("/") + "/webhooks/rest/webhook"

    samples = {name: [] for name, _ in TURNS}
    for _ in range(args.runs):
        sender = "latency-" + uuid.uuid4().hex[:8]
        for name, text in TURNS:
            start = time.perf_counter()
            r = requests.post(endpoint, json={"sender": sender, "message": text}, timeout=30)
            r.raise_for_status()
            samples[name].append(time.perf_counter() - start)

    everything = [t for v in samples.values() for t in v]
    print(f"{'turn':<18}{'median':>8}{'max':>8}   (seconds, n={args.runs})")
    for name, vals in samples.items():
        print(f"{name:<18}{statistics.median(vals):>8.2f}{max(vals):>8.2f}")
    everything.sort()
    p95 = everything[min(len(everything) - 1, int(0.95 * len(everything)))]
    print(f"\nall turns: median {statistics.median(everything):.2f}s, p95 {p95:.2f}s, max {max(everything):.2f}s")
    print("PASS" if p95 <= args.limit else "FAIL", f"(p95 vs {args.limit:.0f}s target)")


if __name__ == "__main__":
    main()
