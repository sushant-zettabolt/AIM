# Copyright © Advanced Micro Devices, Inc., or its affiliates.
#
# SPDX-License-Identifier: MIT
"""Renders bench.py summaries as a human-readable Markdown report."""

from collections import OrderedDict
from datetime import datetime
from typing import List, Optional


def _num(v, digits=1) -> str:
    if v is None:
        return "–"
    if isinstance(v, float) and v != v:
        return "–"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    if abs(v) >= 100:
        return f"{v:.0f}"
    return f"{v:.{digits}f}"


def _pct(d: dict, q: int, scale: float = 1.0, digits=1) -> str:
    if not d or not d.get("n"):
        return "–"
    s = _num(d[f"p{q}"] * scale, digits)
    return s if d.get(f"p{q}_reliable", True) else s + "*"


def _stat(d: dict, key: str, scale: float = 1.0, digits=1) -> str:
    if not d or not d.get("n") or d.get(key) is None:
        return "–"
    return _num(d[key] * scale, digits)


def _table(headers: List[str], rows: List[List[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(out)


def _server_prefill(s: dict) -> Optional[float]:
    if s.get("server") and s["server"].get("prefill_tokens_per_s"):
        return s["server"]["prefill_tokens_per_s"]
    d = (s.get("distributions") or {}).get("server_prefill_tokens_per_s")
    return d.get("p50") if d and d.get("n") else None


def _server_decode(s: dict) -> Optional[float]:
    if s.get("server") and s["server"].get("decode_tokens_per_s"):
        return s["server"]["decode_tokens_per_s"]
    d = (s.get("distributions") or {}).get("server_decode_tokens_per_s")
    return d.get("p50") if d and d.get("n") else None


def _workload_line(s: dict) -> str:
    p = s["params"]
    if s["mode"] == "closed_loop":
        load = f"closed loop, {p['users']} concurrent user(s)"
        if p.get("duration_s"):
            load += f", {p['duration_s']}s"
        if p.get("num_requests"):
            load += f", up to {p['num_requests']} requests"
        think = p.get("think_time_s") or [0, 0]
        if think[1]:
            load += f", think time {think[0]}-{think[1]}s"
        if p.get("ramp_up_s"):
            load += f", ramp-up {p['ramp_up_s']}s"
    else:
        rate = p["request_rate"]
        load = f"open loop, {p['num_requests']} requests at {rate} req/s (burstiness {p['burstiness']})"
        if p.get("max_concurrency"):
            load += f", max concurrency {p['max_concurrency']}"
    tokens = f"{p['input_tokens']} input / {p['output_tokens']} output tokens"
    if p.get("shared_prefix_tokens"):
        tokens += f", {p['shared_prefix_tokens']}-token shared prefix at {p['cache_hit_rate']:.0%} hit rate"
    extra = f"ignore_eos={p['ignore_eos']}, temperature={p['temperature']}, {p['warmup_requests']} warmup request(s)"
    return f"{load}; {tokens}; {extra}"


def csv_row(s: dict) -> "OrderedDict[str, object]":
    d = s.get("distributions") or {}
    tp = s.get("throughput") or {}
    row = OrderedDict(
        target=s["target"], engine=s["engine"], model=s["model"], scenario=s["scenario"], point=s["point"],
        requests_ok=s["requests_ok"], requests_failed=s["requests_failed"],
        duration_s=tp.get("duration_s"), requests_per_s=tp.get("requests_per_s"),
        input_tokens_per_s=tp.get("input_tokens_per_s"), output_tokens_per_s=tp.get("output_tokens_per_s"),
        mean_concurrency=(s.get("concurrency") or {}).get("mean"),
    )
    for metric in ("ttft_ms", "tpot_ms", "itl_ms", "e2e_ms", "decode_tokens_per_s",
                   "effective_prefill_tokens_per_s", "prompt_tokens", "completion_tokens"):
        m = d.get(metric) or {}
        for k in ("mean", "p50", "p90", "p95", "p99", "max"):
            row[f"{metric}_{k}"] = m.get(k)
    row["server_prefill_tokens_per_s"] = _server_prefill(s)
    row["server_decode_tokens_per_s"] = _server_decode(s)
    row["prefix_cache_hit_rate"] = (s.get("server") or {}).get("prefix_cache_hit_rate")
    return row


def _glance(summaries: List[dict]) -> str:
    rows = []
    for s in summaries:
        d = s.get("distributions") or {}
        tp = s.get("throughput") or {}
        rows.append([
            s["target"], s["scenario"], s["point"], f"{s['requests_ok']}/{s['requests_total']}",
            _num(tp.get("requests_per_s"), 2), _num(tp.get("input_tokens_per_s")),
            _num(tp.get("output_tokens_per_s")),
            _pct(d.get("ttft_ms"), 50, digits=0), _pct(d.get("ttft_ms"), 99, digits=0),
            _pct(d.get("tpot_ms"), 50), _pct(d.get("tpot_ms"), 99),
            _pct(d.get("decode_tokens_per_s"), 50), _num(_server_prefill(s)), _num(_server_decode(s)),
            _pct(d.get("e2e_ms"), 50, 1 / 1000), _pct(d.get("e2e_ms"), 99, 1 / 1000),
        ])
    return _table(["Target", "Scenario", "Point", "OK", "Req/s", "In tok/s", "Out tok/s",
                   "TTFT p50 ms", "TTFT p99 ms", "TPOT p50 ms", "TPOT p99 ms",
                   "Decode tok/s p50", "Prefill tok/s (srv)", "Decode tok/s (srv)",
                   "E2E p50 s", "E2E p99 s"], rows)


HEAD_TO_HEAD = [
    # label, getter, higher_is_better
    ("Output throughput (tok/s)", lambda s: (s.get("throughput") or {}).get("output_tokens_per_s"), True),
    ("Request throughput (req/s)", lambda s: (s.get("throughput") or {}).get("requests_per_s"), True),
    ("TTFT p50 (ms)", lambda s: (s["distributions"].get("ttft_ms") or {}).get("p50"), False),
    ("TTFT p99 (ms)", lambda s: (s["distributions"].get("ttft_ms") or {}).get("p99"), False),
    ("TPOT p50 (ms)", lambda s: (s["distributions"].get("tpot_ms") or {}).get("p50"), False),
    ("TPOT p99 (ms)", lambda s: (s["distributions"].get("tpot_ms") or {}).get("p99"), False),
    ("E2E p50 (ms)", lambda s: (s["distributions"].get("e2e_ms") or {}).get("p50"), False),
    ("E2E p99 (ms)", lambda s: (s["distributions"].get("e2e_ms") or {}).get("p99"), False),
    ("Decode speed p50 (tok/s per request)",
     lambda s: (s["distributions"].get("decode_tokens_per_s") or {}).get("p50"), True),
    ("Prefill speed (server, tok/s)", _server_prefill, True),
    ("Decode speed (server, tok/s)", _server_decode, True),
    ("Failed requests", lambda s: s["requests_failed"], False),
]


def _head_to_head(summaries: List[dict]) -> str:
    groups: "OrderedDict[tuple, List[dict]]" = OrderedDict()
    for s in summaries:
        if s.get("distributions"):
            groups.setdefault((s["scenario"], s["point"]), []).append(s)
    parts = []
    for (scenario, point), group in groups.items():
        targets = OrderedDict((s["target"], s) for s in group)
        if len(targets) < 2:
            continue
        names = list(targets)
        base = targets[names[0]]
        headers = ["Metric"] + names + [f"{n} vs {names[0]}" for n in names[1:]]
        rows = []
        for label, get, higher in HEAD_TO_HEAD:
            vals = [get(targets[n]) for n in names]
            row = [label] + [_num(v) for v in vals]
            b = get(base)
            for v in vals[1:]:
                if v is None or b is None or b == 0:
                    row.append("–")
                    continue
                ratio = v / b
                better = (ratio > 1) == higher if ratio != 1 else None
                tag = "" if better is None else (" better" if better else " worse")
                row.append(f"{ratio:.2f}×{tag}")
            rows.append(row)
        parts.append(f"### {scenario} · {point}\n\n{_table(headers, rows)}")
    return "\n\n".join(parts)


def _dist_rows(d: dict, specs) -> List[List[str]]:
    rows = []
    for label, key, scale, digits in specs:
        m = d.get(key)
        if not m or not m.get("n"):
            continue
        ci = m.get("p50_ci95")
        p50 = _pct(m, 50, scale, digits)
        if ci:
            p50 += f" [{_num(ci[0] * scale, digits)}–{_num(ci[1] * scale, digits)}]"
        rows.append([label, m["n"], _stat(m, "mean", scale, digits), _stat(m, "std", scale, digits),
                     _stat(m, "min", scale, digits), p50, _pct(m, 90, scale, digits), _pct(m, 95, scale, digits),
                     _pct(m, 99, scale, digits), _stat(m, "max", scale, digits)])
    return rows


DIST_HEADERS = ["Metric", "n", "mean", "std", "min", "p50 [95% CI]", "p90", "p95", "p99", "max"]


def _details(s: dict) -> str:
    out = [f"### {s['target']} · {s['scenario']} · {s['point']}", "", f"*Workload:* {_workload_line(s)}", ""]
    if not s.get("distributions"):
        out.append(f"No successful requests. Errors: {s.get('errors')}")
        out += [_checks(s)]
        return "\n".join(out)
    d, tp = s["distributions"], s["throughput"]
    conc = s.get("concurrency") or {}
    out += ["**Throughput**", "", _table(["Duration", "Requests ok / failed", "Req/s", "Input tok/s",
                                            "Output tok/s", "Total tok/s", "Input / output tokens",
                                            "Mean / peak in-flight"], [[
        f"{tp['duration_s']:.1f}s", f"{s['requests_ok']} / {s['requests_failed']}",
        _num(tp["requests_per_s"], 3), _num(tp["input_tokens_per_s"]), _num(tp["output_tokens_per_s"]),
        _num(tp["total_tokens_per_s"]), f"{tp['input_tokens_total']:,} / {tp['output_tokens_total']:,}",
        f"{conc.get('mean', 0):.2f} / {conc.get('max', 0)}"]]), ""]

    out += ["**Token sizes (per request)**", "", _table(["", "n", "mean", "min", "p50", "max"], [
        [label, m["n"], _stat(m, "mean"), _stat(m, "min", digits=0), _stat(m, "p50", digits=0),
         _stat(m, "max", digits=0)]
        for label, m in (("Prompt tokens", d.get("prompt_tokens")),
                         ("Generated tokens", d.get("completion_tokens")),
                         ("Cached prompt tokens", d.get("cached_tokens")))
        if m and m.get("n")]), ""]

    out += ["**Latency (ms)**", "", _table(DIST_HEADERS, _dist_rows(d, [
        ("TTFT", "ttft_ms", 1, 1), ("TPOT", "tpot_ms", 1, 1), ("ITL", "itl_ms", 1, 1),
        ("E2E", "e2e_ms", 1, 0), ("Client-side queue", "client_queue_ms", 1, 1),
        ("Server prompt processing (llama.cpp)", "server_prompt_ms", 1, 1)])), ""]

    out += ["**Speed (tokens/s)**", "", _table(DIST_HEADERS, _dist_rows(d, [
        ("Decode, per request (client)", "decode_tokens_per_s", 1, 1),
        ("Effective prefill, per request (client)", "effective_prefill_tokens_per_s", 1, 1),
        ("Prefill, per request (server)", "server_prefill_tokens_per_s", 1, 1),
        ("Decode, per request (server)", "server_decode_tokens_per_s", 1, 1)])), ""]

    if s.get("by_kind"):
        rows = []
        for kind, k in s["by_kind"].items():
            rows.append([kind, k["n"], _pct(k["ttft_ms"], 50, digits=0), _pct(k["ttft_ms"], 95, digits=0),
                         _pct(k["e2e_ms"], 50, digits=0), _stat(k["cached_tokens"], "mean", digits=0)])
        out += ["**Prefix cache: hit vs miss requests**", "",
                _table(["Kind", "n", "TTFT p50 ms", "TTFT p95 ms", "E2E p50 ms", "Mean cached tokens"], rows), ""]

    srv = s.get("server")
    if srv:
        g = s.get("server_gauges") or {}

        def ms(v):
            return _num(v * 1000) if v is not None else "–"
        rows = [
            ["Mean queue time (ms)", ms(srv.get("mean_queue_s"))],
            ["Mean prefill time (ms)", ms(srv.get("mean_prefill_s"))],
            ["Mean decode time (ms)", ms(srv.get("mean_decode_s"))],
            ["Mean TTFT (ms)", ms(srv.get("mean_ttft_s"))],
            ["Mean TPOT (ms)", ms(srv.get("mean_tpot_s"))],
            ["Mean E2E (ms)", ms(srv.get("mean_e2e_s"))],
            ["Prefill speed (uncached tok/s)", _num(srv.get("prefill_tokens_per_s"))],
            ["Decode speed (tok/s per request)", _num(srv.get("decode_tokens_per_s"))],
            ["Prefix cache hit rate", f"{srv['prefix_cache_hit_rate']:.1%}"
             if srv.get("prefix_cache_hit_rate") is not None else "–"],
            ["Cached prompt tokens", _num(srv.get("cached_prompt_tokens"), 0)],
            ["Preemptions", _num(srv.get("preemptions"), 0)],
            ["Running requests mean / max", f"{_num(g.get('running_mean'))} / {_num(g.get('running_max'), 0)}"],
            ["Waiting requests mean / max", f"{_num(g.get('waiting_mean'))} / {_num(g.get('waiting_max'), 0)}"],
            ["KV cache usage max", f"{g['kv_cache_usage_max']:.1%}" if g.get("kv_cache_usage_max") is not None
             else "–"],
        ]
        out += ["**Server-side (vLLM /metrics, delta over the measured window)**", "",
                _table(["Metric", "Value"], rows), ""]
    out.append(_checks(s))
    return "\n".join(out)


def _checks(s: dict) -> str:
    rows = [[status, name, detail] for status, name, detail in s.get("checks") or []]
    return "**Measurement checks**\n\n" + _table(["Status", "Check", "Detail"], rows) + "\n"


GLOSSARY = """
## How each number is measured

All client timestamps use `time.perf_counter()` in the load generator. Every request streams
(`stream: true`) with `temperature` and `ignore_eos` from the config. Token counts come from the
server's own `usage` report, not a client-side estimate. Warmup requests run before every
measurement window and are excluded.

| Metric | Definition |
|---|---|
| TTFT | Request sent → first generated-token chunk received. Includes network, server queueing and prefill. |
| E2E | Request sent → end of stream. |
| TPOT | (E2E − TTFT) / (generated tokens − 1), per request. Unaffected by how tokens are grouped into chunks. |
| ITL | Gap between consecutive generated-token chunks (including chunks whose text is empty because the token doesn't complete a UTF-8 character yet), pooled across all requests. Per token only if the "one token per stream chunk" check passes. |
| Decode speed (client) | (generated tokens − 1) / (E2E − TTFT), per request: tokens/s one user sees while streaming. |
| Effective prefill speed (client) | prompt tokens / TTFT, per request. A lower bound on real prefill speed: TTFT also includes queueing and network, and cached tokens are counted as if computed. |
| Prefill speed (server) | vLLM: Σ uncached prompt tokens (`request_prefill_kv_computed_tokens`) / Σ per-request prefill time. llama.cpp: `timings.prompt_n / prompt_ms` per request (uncached tokens only). Per-request rate: when several prompts are prefilled in the same batch their prefill times overlap, so system prefill throughput is higher than this. |
| Decode speed (server) | vLLM: (Σ generated tokens − Σ requests) / Σ per-request decode time. llama.cpp: `timings.predicted_n / predicted_ms` per request. |
| Input / output tok/s | Σ prompt (or generated) tokens of successful requests / (first request sent → last request finished). System throughput, not per user. |
| Mean in-flight | Time-weighted average number of concurrent requests over the run. |
| p50/p90/p95/p99 | Linear interpolation between closest ranks (numpy's default, same as `vllm bench serve`). `*` marks a percentile computed from too few samples to mean anything: p90 needs ≥10, p95 ≥20, p99 ≥100. |
| 95% CI | Bootstrap (1000 resamples) confidence interval of the p50. A wide interval means run longer before trusting a difference. |
| Server-side (vLLM) | Deltas of the engine's Prometheus counters/histograms scraped from every pod behind the Service, before and after the measured window. Gauges sampled every 2s. |
"""


def render(metas: List[dict], summaries: List[dict]) -> str:
    out = ["# LLM serving benchmark report", "",
           f"Rendered {datetime.now().isoformat(timespec='seconds')}.", ""]
    for m in metas:
        out.append(f"- Run from `{m.get('config')}` on `{m.get('host')}`, started {m.get('started_at')}"
                   f"{', finished ' + m['finished_at'] if m.get('finished_at') else ''}; "
                   f"client CPUs {m.get('client_cpu_affinity') or 'unpinned'}")
    out.append("")
    out += ["## Targets", "", _table(["Target", "Engine", "Model", "Instances", "Endpoint", "Status"], [
        [t["name"], t.get("engine"), t.get("model"), t.get("instances") or "?", t.get("base_url"),
         "ok" if not t.get("error") else f"skipped: {t['error']}"]
        for m in metas for t in m.get("targets", [])]), ""]
    if not summaries:
        out.append("No completed runs.")
        return "\n".join(out)

    worst = {}
    for s in summaries:
        for status, name, detail in s.get("checks") or []:
            if status in ("FAIL", "WARN"):
                worst.setdefault((status, name), []).append(f"{s['target']}/{s['scenario']}/{s['point']}")
    if worst:
        out += ["## Measurement warnings", ""]
        for (status, name), where in sorted(worst.items()):
            out.append(f"- **{status} {name}** in {len(where)} run(s): {', '.join(where[:6])}"
                       f"{' …' if len(where) > 6 else ''}")
        out.append("")

    out += ["## Results at a glance", "",
            "Latencies in ms except E2E (s). `*` = percentile from too few samples (see definitions).", "",
            _glance(summaries), ""]
    h2h = _head_to_head(summaries)
    if h2h:
        out += ["## Head-to-head", "", "Ratios are relative to the first target listed.", "", h2h, ""]
    out += ["## Details", ""]
    out += [_details(s) for s in summaries]
    out.append(GLOSSARY)
    return "\n".join(out)
