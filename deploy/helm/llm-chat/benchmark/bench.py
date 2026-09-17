#!/usr/bin/env python3
# Copyright © Advanced Micro Devices, Inc., or its affiliates.
#
# SPDX-License-Identifier: MIT
"""YAML-driven load benchmark for OpenAI-compatible LLM servers (vLLM, llama.cpp).

    python3 bench.py run -c bench_config.yaml            # run everything in the config
    python3 bench.py run -c bench_config.yaml --targets vllm-96c --scenarios concurrency
    python3 bench.py report results/<run-dir>            # re-render a report
    python3 bench.py compare results/<run-a> results/<run-b> -o compare.md

See README.md for metric definitions and how each number is measured.
"""

import argparse
import copy
import csv
import http.client
import itertools
import json
import math
import os
import platform
import random
import resource
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

import yaml

import bench_report
from bench_stats import describe, time_weighted_concurrency

SCRIPT_DIR = Path(__file__).resolve().parent

SWEEPABLE = {
    "users", "duration_s", "num_requests", "ramp_up_s", "request_rate", "burstiness",
    "max_concurrency", "input_tokens", "output_tokens", "shared_prefix_tokens", "cache_hit_rate",
}

DEFAULTS = {
    "input_tokens": 1024,
    "output_tokens": 128,
    "ignore_eos": True,
    "temperature": 0.0,
    "shared_prefix_tokens": 0,
    "cache_hit_rate": 0.0,
    "warmup_requests": 2,
    "seed": 42,
    "think_time_s": [0.0, 0.0],
    "ramp_up_s": 0.0,
    "burstiness": 1.0,
    "max_concurrency": None,
}

SYSTEM_HEADER = "You are a benchmark assistant. Reference material:"
USER_HEADER = "Context:"

# Candidate filler words. Only those the target's own tokenizer encodes as
# exactly one token (with a leading space) are used, so a prompt of N words
# is exactly N tokens and can be sized precisely -- see PromptBuilder.
WORDS = """
time year people way day man thing woman life child world school state family student group
country problem hand part place case week company system program question work government number
night point home water room mother area money story fact month lot right study book eye job word
business issue side kind head house service friend father power hour game line end member law car
city community name president team minute idea kid body information back parent face others level
office door health person art war history party result change morning reason research girl guy
moment air teacher force education foot boy age policy process music market sense nation plan
college interest death experience effect use class control care field development role effort rate
heart drug show leader light voice wife police mind price report decision son view relationship town
road arm difference value building action model season society tax director position player record
paper space ground form event official matter center couple site project activity star table need
court oil situation cost industry figure street image phone data picture practice piece land product
doctor wall patient worker news test movie north love support technology step baby computer type
attention film tree source organization hair window evidence population truth song camera river
bank army stage stock wind ocean island summer winter spring autumn garden kitchen chair bridge
engine signal memory network server cluster request token queue cache model layer weight vector
green blue red black white yellow brown silver golden quiet simple strong bright early late small
large short long fast slow warm cold clear dark open close high low deep wide free full real
""".split()


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------


def expand_scenarios(cfg: dict) -> List[dict]:
    """Merge defaults into each scenario and expand list-valued sweep params."""
    base = dict(DEFAULTS)
    base.update(cfg.get("defaults") or {})
    runs = []
    for sc in cfg.get("scenarios") or []:
        merged = dict(base)
        merged.update(sc)
        if merged.get("mode") not in ("closed_loop", "open_loop"):
            raise ValueError(f"scenario {sc.get('name')}: mode must be closed_loop or open_loop")
        sweep_keys = [k for k in merged if k in SWEEPABLE and isinstance(merged[k], list)]
        combos = itertools.product(*[merged[k] for k in sweep_keys]) if sweep_keys else [()]
        for combo in combos:
            run = dict(merged)
            run.update(dict(zip(sweep_keys, combo)))
            run["sweep"] = {k: v for k, v in zip(sweep_keys, combo)}
            run["point"] = ",".join(f"{k}={v}" for k, v in run["sweep"].items()) or "-"
            validate_run(run)
            runs.append(run)
    return runs


def validate_run(run: dict) -> None:
    name = run.get("name", "?")
    if run["mode"] == "closed_loop":
        if not run.get("users"):
            raise ValueError(f"{name}: closed_loop needs users")
        if not run.get("duration_s") and not run.get("num_requests"):
            raise ValueError(f"{name}: closed_loop needs duration_s and/or num_requests")
    else:
        if run.get("request_rate") is None or not run.get("num_requests"):
            raise ValueError(f"{name}: open_loop needs request_rate and num_requests")
    if run["shared_prefix_tokens"] >= run["input_tokens"]:
        raise ValueError(f"{name}: shared_prefix_tokens must be < input_tokens")


def parse_cpu_list(spec: str) -> List[int]:
    cpus = []
    for part in str(spec).split(","):
        if "-" in part:
            lo, hi = part.split("-")
            cpus.extend(range(int(lo), int(hi) + 1))
        elif part.strip():
            cpus.append(int(part))
    return cpus


# ----------------------------------------------------------------------------
# Target discovery
# ----------------------------------------------------------------------------


def kubectl_json(args: List[str]) -> dict:
    out = subprocess.run(["kubectl", *args, "-o", "json"], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@dataclass
class Target:
    name: str
    base_url: str
    headers: Dict[str, str]
    model: str = ""
    engine: str = "auto"
    metrics_urls: List[str] = field(default_factory=list)
    error: Optional[str] = None
    instances: int = 0

    def describe(self) -> dict:
        return {
            "name": self.name, "base_url": self.base_url, "engine": self.engine, "model": self.model,
            "metrics_urls": self.metrics_urls, "instances": self.instances, "error": self.error,
        }


def resolve_target(tcfg: dict) -> Target:
    t = Target(name=tcfg["name"], base_url=tcfg.get("base_url", ""), headers=dict(tcfg.get("headers") or {}),
               model=tcfg.get("model", "auto"), engine=tcfg.get("engine", "auto"))
    try:
        return _resolve_target(t, tcfg)
    except subprocess.CalledProcessError as exc:
        t.error = f"kubectl: {(exc.stderr or '').strip() or exc}"
    except Exception as exc:  # noqa: BLE001 -- any failure just marks the target unusable
        t.error = f"{type(exc).__name__}: {exc}"
    return t


def _resolve_target(t: Target, tcfg: dict) -> Target:
    k8s = tcfg.get("kubernetes")
    if k8s:
        ns = k8s["namespace"]
        svc = kubectl_json(["get", "svc", k8s["service"], "-n", ns])
        port_spec = svc["spec"]["ports"][0]
        if not t.base_url:
            t.base_url = f"http://{svc['spec']['clusterIP']}:{port_spec['port']}"
        target_port = port_spec.get("targetPort", 8000)
        if not isinstance(target_port, int):
            target_port = 8000
        selector = ",".join(f"{k}={v}" for k, v in svc["spec"]["selector"].items())
        pods = kubectl_json(["get", "pods", "-n", ns, "-l", selector])["items"]
        ips = [p["status"]["podIP"] for p in pods
               if p["status"].get("podIP") and p["status"].get("phase") == "Running"]
        t.instances = len(ips)
        if not tcfg.get("metrics_urls"):
            t.metrics_urls = [f"http://{ip}:{target_port}/metrics" for ip in ips]
    if tcfg.get("metrics_urls"):
        t.metrics_urls = list(tcfg["metrics_urls"])
    t.base_url = t.base_url.rstrip("/")

    # Retry briefly: right after a deploy the pod can answer on localhost
    # before its readiness probe passes, and until it does the Service has
    # no endpoints and connections are refused.
    for attempt in range(6):
        try:
            status, body = http_get(t, "/v1/models", timeout=10)
            if status == 200:
                break
            last = f"/v1/models returned HTTP {status}"
        except OSError as exc:
            last = f"{type(exc).__name__}: {exc}"
        if attempt == 5:
            raise RuntimeError(f"{last} (after 6 attempts over ~50s)")
        time.sleep(10)
    models = json.loads(body)["data"]
    if t.model in ("", "auto"):
        t.model = models[0]["id"]
    if t.engine == "auto":
        owner = (models[0].get("owned_by") or "").lower()
        t.engine = "vllm" if "vllm" in owner else "llamacpp" if "llama" in owner else "unknown"
    if t.engine != "vllm":
        # Only vLLM exposes the Prometheus series this tool reads.
        t.metrics_urls = []
    return t


def _connection(target: Target, timeout: float):
    u = urlparse(target.base_url)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    return cls(u.hostname, u.port, timeout=timeout), u.path.rstrip("/")


def http_get(target: Target, path: str, timeout: float = 10):
    conn, prefix = _connection(target, timeout)
    try:
        conn.request("GET", prefix + path, headers=target.headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def http_post_json(target: Target, path: str, payload: dict, timeout: float = 60):
    conn, prefix = _connection(target, timeout)
    try:
        headers = {"Content-Type": "application/json", **target.headers}
        conn.request("POST", prefix + path, body=json.dumps(payload).encode(), headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, json.loads(body) if body else {}
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Prompt construction (exact token counts)
# ----------------------------------------------------------------------------


class PromptBuilder:
    """Builds chat prompts whose templated length is exactly `input_tokens`.

    Filler words are restricted to ones the server's tokenizer encodes as a
    single token with a leading space. Llama-3-style BPE pre-tokenizes on
    word boundaries, so N such words are N tokens; the fixed chat-template
    overhead is then measured once with a real max_tokens=1 request (which
    uses exactly the template the benchmark requests use) and every warmup
    request re-verifies the total. The real prompt_tokens of every measured
    request is also recorded from the server's usage report, so the report
    shows the true sizes, not these targets.
    """

    def __init__(self, target: Target, log):
        self.target = target
        self.log = log
        self.vocab = self._single_token_words()
        self.overhead = self._measure_overhead()

    def _count_tokens(self, text: str) -> int:
        if self.target.engine == "vllm":
            status, resp = http_post_json(self.target, "/tokenize",
                                          {"model": self.target.model, "prompt": text, "add_special_tokens": False})
            if status == 200:
                return int(resp["count"])
        status, resp = http_post_json(self.target, "/tokenize", {"content": text, "add_special": False})
        if status != 200:
            raise RuntimeError(f"/tokenize failed: HTTP {status} {resp}")
        return len(resp["tokens"])

    def _single_token_words(self) -> List[str]:
        vocab = sorted({w for w in WORDS if self._count_tokens(" " + w) == 1})
        if len(vocab) < 50:
            raise RuntimeError(f"only {len(vocab)} single-token filler words for this tokenizer")
        probe = " ".join(random.Random(7).choice(vocab) for _ in range(300))
        got = self._count_tokens(" " + probe)
        if got != 300:
            raise RuntimeError(f"tokenizer is not additive over filler words (300 words -> {got} tokens)")
        self.log(f"  prompt builder: {len(vocab)} single-token words, additivity verified")
        return vocab

    def _measure_overhead(self) -> int:
        words = 50
        msgs = self.messages([], self.random_words(random.Random(1), words))
        status, resp = http_post_json(self.target, "/v1/chat/completions",
                                      {"model": self.target.model, "messages": msgs, "max_tokens": 1,
                                       "temperature": 0.0, "stream": False}, timeout=300)
        if status != 200:
            raise RuntimeError(f"calibration request failed: HTTP {status} {resp}")
        overhead = int(resp["usage"]["prompt_tokens"]) - words
        self.log(f"  prompt builder: chat template + headers = {overhead} tokens")
        return overhead

    def random_words(self, rng: random.Random, n: int) -> List[str]:
        return [rng.choice(self.vocab) for _ in range(n)]

    @staticmethod
    def messages(prefix_words: List[str], suffix_words: List[str]) -> List[dict]:
        system = SYSTEM_HEADER + ("".join(" " + w for w in prefix_words))
        user = USER_HEADER + "".join(" " + w for w in suffix_words)
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def split(self, input_tokens: int, shared_prefix_tokens: int, correction: int = 0):
        suffix = input_tokens - self.overhead - shared_prefix_tokens + correction
        if suffix < 1:
            raise ValueError(f"input_tokens={input_tokens} too small for template overhead {self.overhead} "
                             f"+ shared_prefix_tokens={shared_prefix_tokens}")
        return shared_prefix_tokens, suffix


# ----------------------------------------------------------------------------
# Streaming request
# ----------------------------------------------------------------------------


def stream_chat(target: Target, messages: List[dict], run: dict, timeout: float) -> dict:
    payload = {
        "model": target.model,
        "messages": messages,
        "max_tokens": run["output_tokens"],
        "temperature": run["temperature"],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if run["ignore_eos"]:
        payload["ignore_eos"] = True
    rec = {"ok": False, "error": None, "status": None, "ttft_s": None, "e2e_s": None,
           "prompt_tokens": None, "completion_tokens": None, "cached_tokens": None,
           "token_chunks": 0, "itl_s": [], "finish_reason": None, "server": {}}
    conn, prefix = _connection(target, timeout)
    t_send = time.perf_counter()
    rec["t_send"] = t_send
    first = last = None
    usage = timings = None
    try:
        conn.request("POST", prefix + "/v1/chat/completions", body=json.dumps(payload).encode(),
                     headers={"Content-Type": "application/json", **target.headers})
        resp = conn.getresponse()
        rec["status"] = resp.status
        if resp.status != 200:
            rec["error"] = f"HTTP {resp.status}: {resp.read()[:300]!r}"
            return rec
        while True:
            line = resp.readline()
            if not line:
                break
            now = time.perf_counter()
            if now - t_send > timeout:
                raise TimeoutError(f"request exceeded {timeout}s")
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                break
            obj = json.loads(data)
            if obj.get("error"):
                raise RuntimeError(f"server error in stream: {obj['error']}")
            if obj.get("usage"):
                usage = obj["usage"]
            if obj.get("timings"):
                timings = obj["timings"]
            choices = obj.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            if choice.get("finish_reason"):
                rec["finish_reason"] = choice["finish_reason"]
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if text is None:
                text = delta.get("reasoning_content")
            # Every generated token gets its own chunk, but one that doesn't
            # complete a UTF-8 character yet (e.g. part of an emoji) arrives
            # with content "" -- confirmed on vLLM. Counting only non-empty
            # chunks merges those tokens into the next gap and inflates ITL,
            # so any chunk carrying a content field is a token, except the
            # bare role announcement.
            if text is not None and not ("role" in delta and text == ""):
                if first is None:
                    first = now
                else:
                    rec["itl_s"].append(now - last)
                last = now
                rec["token_chunks"] += 1
        t_end = time.perf_counter()
        rec["t_end"] = t_end
        rec["e2e_s"] = t_end - t_send
        if first is not None:
            rec["t_first"] = first
            rec["ttft_s"] = first - t_send
        if usage:
            rec["prompt_tokens"] = usage.get("prompt_tokens")
            rec["completion_tokens"] = usage.get("completion_tokens")
            details = usage.get("prompt_tokens_details") or {}
            rec["cached_tokens"] = details.get("cached_tokens")
        if timings:
            # llama.cpp per-request server timings: prompt_n excludes cached
            # tokens (cache_n), so prompt_n/prompt_ms is true prefill speed.
            rec["server"] = {k: timings.get(k) for k in
                             ("prompt_n", "prompt_ms", "predicted_n", "predicted_ms", "cache_n")}
            if rec["cached_tokens"] is None and timings.get("cache_n") is not None:
                rec["cached_tokens"] = timings["cache_n"]
        if rec["completion_tokens"] is None:
            rec["completion_tokens"] = rec["token_chunks"]
            rec["usage_missing"] = True
        rec["ok"] = first is not None
        if not rec["ok"]:
            rec["error"] = "stream ended without any content"
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"{type(exc).__name__}: {exc}"
        rec["t_end"] = time.perf_counter()
        rec["e2e_s"] = rec["t_end"] - t_send
    finally:
        conn.close()
    return rec


# ----------------------------------------------------------------------------
# Server metrics (vLLM Prometheus)
# ----------------------------------------------------------------------------


def scrape_prometheus(urls: List[str]) -> Dict[str, float]:
    totals: Dict[str, float] = {}
    for url in urls:
        u = urlparse(url)
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
        try:
            conn.request("GET", u.path or "/metrics")
            text = conn.getresponse().read().decode()
        finally:
            conn.close()
        for line in text.splitlines():
            if not line.startswith("vllm:"):
                continue
            name_end = line.find("{") if "{" in line.split(" ")[0] else line.find(" ")
            name = line[:name_end]
            try:
                value = float(line.rsplit(" ", 1)[1])
            except ValueError:
                continue
            if name.endswith("_created"):
                continue
            key = name
            if name == "vllm:kv_cache_usage_perc":
                totals[key] = max(totals.get(key, 0.0), value)
            else:
                totals[key] = totals.get(key, 0.0) + value
    return totals


class GaugeSampler(threading.Thread):
    def __init__(self, urls: List[str], interval: float = 2.0):
        super().__init__(daemon=True)
        self.urls, self.interval = urls, interval
        self.samples: List[dict] = []
        # Not `_stop`: that name is threading.Thread's own internal method.
        self._halt = threading.Event()

    def run(self):
        while not self._halt.is_set():
            try:
                m = scrape_prometheus(self.urls)
                self.samples.append({
                    "running": m.get("vllm:num_requests_running", 0.0),
                    "waiting": m.get("vllm:num_requests_waiting", 0.0),
                    "kv_cache_usage": m.get("vllm:kv_cache_usage_perc", 0.0),
                })
            except Exception:  # noqa: BLE001 -- best-effort sampling
                pass
            self._halt.wait(self.interval)

    def stop(self):
        self._halt.set()
        self.join(timeout=15)


def server_deltas(before: Dict[str, float], after: Dict[str, float]) -> dict:
    def d(name):
        return after.get(name, 0.0) - before.get(name, 0.0)

    def hist_mean(base):
        c = d(f"vllm:{base}_count")
        return d(f"vllm:{base}_sum") / c if c > 0 else None

    out = {
        "requests": d("vllm:request_success_total"),
        "prompt_tokens": d("vllm:prompt_tokens_total"),
        "generation_tokens": d("vllm:generation_tokens_total"),
        "cached_prompt_tokens": d("vllm:prompt_tokens_cached_total"),
        "prefix_cache_queries": d("vllm:prefix_cache_queries_total"),
        "prefix_cache_hits": d("vllm:prefix_cache_hits_total"),
        "preemptions": d("vllm:num_preemptions_total"),
        "mean_ttft_s": hist_mean("time_to_first_token_seconds"),
        "mean_queue_s": hist_mean("request_queue_time_seconds"),
        "mean_prefill_s": hist_mean("request_prefill_time_seconds"),
        "mean_decode_s": hist_mean("request_decode_time_seconds"),
        "mean_e2e_s": hist_mean("e2e_request_latency_seconds"),
        "mean_tpot_s": hist_mean("request_time_per_output_token_seconds"),
        "mean_itl_s": hist_mean("inter_token_latency_seconds"),
    }
    q = out["prefix_cache_queries"]
    out["prefix_cache_hit_rate"] = out["prefix_cache_hits"] / q if q > 0 else None
    prefill_time = d("vllm:request_prefill_time_seconds_sum")
    computed = d("vllm:request_prefill_kv_computed_tokens_sum")
    out["prefill_tokens_per_s"] = computed / prefill_time if prefill_time > 0 else None
    decode_time = d("vllm:request_decode_time_seconds_sum")
    decode_reqs = d("vllm:request_decode_time_seconds_count")
    out["decode_tokens_per_s"] = ((out["generation_tokens"] - decode_reqs) / decode_time
                                  if decode_time > 0 else None)
    return out


# ----------------------------------------------------------------------------
# Load generation
# ----------------------------------------------------------------------------


class Workload:
    """Thread-safe per-request prompt generator for one run."""

    def __init__(self, builder: PromptBuilder, run: dict, run_seed: int):
        self.builder, self.run = builder, run
        self.rng = random.Random(run_seed)
        self.lock = threading.Lock()
        self.correction = 0
        self.prefix_words = builder.random_words(self.rng, run["shared_prefix_tokens"])

    def next(self):
        with self.lock:
            prefix_n, suffix_n = self.builder.split(self.run["input_tokens"], self.run["shared_prefix_tokens"],
                                                    self.correction)
            hit_rate = self.run["cache_hit_rate"]
            if prefix_n == 0:
                kind = "none"
                prefix = []
            elif self.rng.random() < hit_rate:
                kind, prefix = "hit", self.prefix_words
            else:
                kind, prefix = "miss", self.builder.random_words(self.rng, prefix_n)
            suffix = self.builder.random_words(self.rng, suffix_n)
        return kind, PromptBuilder.messages(prefix, suffix)


def warmup(target: Target, workload: Workload, run: dict, timeout: float, log) -> None:
    """Runs warmup requests (excluded from results) until the prompt size is verified.

    Always at least one request: it confirms the prompt is exactly
    input_tokens long, and (when a shared prefix is configured) puts that
    prefix in the server's cache so every sweep point starts equally warm.
    """
    wanted = run["input_tokens"]
    sent, verified = 0, False
    while sent < max(int(run["warmup_requests"]), 1) or not verified:
        if sent >= int(run["warmup_requests"]) + 3:
            raise RuntimeError(f"could not size prompts to {wanted} tokens after {sent} attempts")
        with workload.lock:
            _, suffix_n = workload.builder.split(wanted, run["shared_prefix_tokens"], workload.correction)
            suffix = workload.builder.random_words(workload.rng, suffix_n)
            prefix = workload.prefix_words
        rec = stream_chat(target, PromptBuilder.messages(prefix, suffix), run, timeout)
        sent += 1
        if not rec["ok"]:
            raise RuntimeError(f"warmup request failed: {rec['error']}")
        got = rec.get("prompt_tokens")
        if got is None or got == wanted:
            verified = True
        else:
            workload.correction += wanted - got
            verified = False
            log(f"    warmup: prompt was {got} tokens, adjusting filler by {wanted - got}")


def run_closed_loop(target, workload, run, timeout, records, log):
    users = int(run["users"])
    duration = run.get("duration_s")
    max_requests = run.get("num_requests")
    think_lo, think_hi = run["think_time_s"]
    ramp = float(run["ramp_up_s"] or 0)
    t0 = time.perf_counter()
    deadline = t0 + float(duration) if duration else None
    counter = {"started": 0}
    lock = threading.Lock()
    think_rng = random.Random(run["seed"] + 1)

    def user(idx):
        time.sleep(ramp * idx / users if users > 1 else 0)
        while True:
            with lock:
                if deadline and time.perf_counter() >= deadline:
                    return
                if max_requests and counter["started"] >= max_requests:
                    return
                counter["started"] += 1
                think = think_rng.uniform(think_lo, think_hi)
            kind, msgs = workload.next()
            rec = stream_chat(target, msgs, run, timeout)
            rec.update(kind=kind, user=idx, t_scheduled=rec["t_send"])
            with lock:
                records.append(rec)
            if think > 0:
                time.sleep(think)

    threads = [threading.Thread(target=user, args=(i,), daemon=True) for i in range(users)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()


def run_open_loop(target, workload, run, timeout, records, log):
    n = int(run["num_requests"])
    rate = float(run["request_rate"])
    shape = float(run["burstiness"])
    rng = random.Random(run["seed"] + 2)
    sem = threading.Semaphore(int(run["max_concurrency"])) if run.get("max_concurrency") else None
    lock = threading.Lock()
    threads = []

    def fire(scheduled):
        if sem:
            sem.acquire()
        try:
            kind, msgs = workload.next()
            rec = stream_chat(target, msgs, run, timeout)
            rec.update(kind=kind, t_scheduled=scheduled)
            with lock:
                records.append(rec)
        finally:
            if sem:
                sem.release()

    t_next = time.perf_counter()
    for _ in range(n):
        now = time.perf_counter()
        if t_next > now:
            time.sleep(t_next - now)
        th = threading.Thread(target=fire, args=(t_next,), daemon=True)
        th.start()
        threads.append(th)
        if not math.isinf(rate):
            # Gamma inter-arrival times; shape 1 = Poisson, <1 burstier,
            # >1 more uniform -- same model as `vllm bench serve --burstiness`.
            t_next += rng.gammavariate(shape, 1.0 / (rate * shape))
    for th in threads:
        th.join()


# ----------------------------------------------------------------------------
# Aggregation and validation
# ----------------------------------------------------------------------------


def aggregate(run: dict, target: Target, records: List[dict], server: Optional[dict],
              gauges: List[dict], cpu_util: float, wall_s: float) -> dict:
    ok = [r for r in records if r["ok"]]
    failed = [r for r in records if not r["ok"]]
    summary = {
        "target": target.name, "engine": target.engine, "model": target.model, "instances": target.instances,
        "scenario": run["name"], "mode": run["mode"], "point": run["point"],
        "params": {k: v for k, v in run.items() if k not in ("sweep",)},
        "requests_total": len(records), "requests_ok": len(ok), "requests_failed": len(failed),
        "errors": sorted({r["error"] for r in failed})[:10],
    }
    if not ok:
        summary["checks"] = [("FAIL", "requests", "no successful requests")]
        return summary

    t_start = min(r["t_send"] for r in ok)
    t_end = max(r["t_end"] for r in ok)
    duration = t_end - t_start
    in_tok = sum(r["prompt_tokens"] or 0 for r in ok)
    out_tok = sum(r["completion_tokens"] or 0 for r in ok)
    summary["throughput"] = {
        "duration_s": duration,
        "requests_per_s": len(ok) / duration if duration > 0 else None,
        "input_tokens_per_s": in_tok / duration if duration > 0 else None,
        "output_tokens_per_s": out_tok / duration if duration > 0 else None,
        "total_tokens_per_s": (in_tok + out_tok) / duration if duration > 0 else None,
        "input_tokens_total": in_tok,
        "output_tokens_total": out_tok,
    }
    summary["concurrency"] = time_weighted_concurrency([(r["t_send"], r["t_end"]) for r in ok])

    def per_req(fn):
        vals = []
        for r in ok:
            try:
                vals.append(fn(r))
            except (TypeError, ZeroDivisionError):
                vals.append(None)
        return vals

    def decode_s(r):
        return r["t_end"] - r["t_first"]

    dist = {
        "ttft_ms": describe(per_req(lambda r: r["ttft_s"] * 1000), bootstrap=True),
        "tpot_ms": describe(per_req(lambda r: decode_s(r) / (r["completion_tokens"] - 1) * 1000
                                    if r["completion_tokens"] > 1 else None), bootstrap=True),
        "itl_ms": describe([g * 1000 for r in ok for g in r["itl_s"]]),
        "e2e_ms": describe(per_req(lambda r: r["e2e_s"] * 1000), bootstrap=True),
        "prompt_tokens": describe(per_req(lambda r: r["prompt_tokens"])),
        "completion_tokens": describe(per_req(lambda r: r["completion_tokens"])),
        "cached_tokens": describe(per_req(lambda r: r["cached_tokens"])),
        "decode_tokens_per_s": describe(per_req(lambda r: (r["completion_tokens"] - 1) / decode_s(r)
                                                if r["completion_tokens"] > 1 else None)),
        "effective_prefill_tokens_per_s": describe(per_req(lambda r: r["prompt_tokens"] / r["ttft_s"])),
    }
    if run["mode"] == "open_loop":
        dist["client_queue_ms"] = describe(per_req(lambda r: (r["t_send"] - r["t_scheduled"]) * 1000))
    if any(r["server"] for r in ok):
        dist["server_prefill_tokens_per_s"] = describe(per_req(
            lambda r: r["server"]["prompt_n"] / r["server"]["prompt_ms"] * 1000
            if r["server"].get("prompt_n") else None))
        dist["server_decode_tokens_per_s"] = describe(per_req(
            lambda r: r["server"]["predicted_n"] / r["server"]["predicted_ms"] * 1000
            if r["server"].get("predicted_n") else None))
        dist["server_prompt_ms"] = describe(per_req(lambda r: r["server"].get("prompt_ms")))
    summary["distributions"] = dist

    kinds = sorted({r["kind"] for r in ok if r["kind"] != "none"})
    if len(kinds) > 0 and run["shared_prefix_tokens"] > 0:
        summary["by_kind"] = {
            k: {
                "n": sum(1 for r in ok if r["kind"] == k),
                "ttft_ms": describe([r["ttft_s"] * 1000 for r in ok if r["kind"] == k]),
                "e2e_ms": describe([r["e2e_s"] * 1000 for r in ok if r["kind"] == k]),
                "cached_tokens": describe([r["cached_tokens"] for r in ok if r["kind"] == k]),
            } for k in kinds
        }

    if server:
        summary["server"] = server
    if gauges:
        summary["server_gauges"] = {
            "samples": len(gauges),
            "running_max": max(g["running"] for g in gauges),
            "running_mean": sum(g["running"] for g in gauges) / len(gauges),
            "waiting_max": max(g["waiting"] for g in gauges),
            "waiting_mean": sum(g["waiting"] for g in gauges) / len(gauges),
            "kv_cache_usage_max": max(g["kv_cache_usage"] for g in gauges),
        }
    summary["client"] = {"cpu_util_one_core": cpu_util, "wall_s": wall_s}
    summary["checks"] = validate(run, summary, ok, failed, server)
    return summary


def validate(run, summary, ok, failed, server) -> List[tuple]:
    checks = []
    total = len(ok) + len(failed)
    checks.append(("PASS" if not failed else "FAIL", "request errors",
                   f"{len(failed)}/{total} failed" + (f": {summary['errors'][0]}" if failed else "")))

    comp = [r["completion_tokens"] for r in ok]
    if run["ignore_eos"]:
        exact = sum(1 for c in comp if c == run["output_tokens"])
        checks.append(("PASS" if exact == len(ok) else "WARN", "output length",
                       f"{exact}/{len(ok)} generated exactly {run['output_tokens']} tokens "
                       f"(range {min(comp)}-{max(comp)})"))
    if any(r.get("usage_missing") for r in ok):
        checks.append(("WARN", "usage report", "server sent no usage; token counts are chunk counts"))

    prompts = [r["prompt_tokens"] for r in ok if r["prompt_tokens"] is not None]
    if prompts:
        exact = sum(1 for p in prompts if p == run["input_tokens"])
        checks.append(("PASS" if exact == len(prompts) else "WARN", "prompt length",
                       f"{exact}/{len(prompts)} prompts exactly {run['input_tokens']} tokens "
                       f"(range {min(prompts)}-{max(prompts)})"))

    aligned = sum(1 for r in ok if r["token_chunks"] == r["completion_tokens"])
    checks.append(("PASS" if aligned == len(ok) else "WARN", "one token per stream chunk",
                   f"{aligned}/{len(ok)} requests -- ITL is per token" if aligned == len(ok)
                   else f"only {aligned}/{len(ok)} -- server batches several tokens per chunk, "
                        f"so ITL is per chunk; use TPOT"))

    if server:
        for label, client_val, server_val in (
            ("server request count", len(ok), server["requests"]),
            ("server generated tokens", sum(comp), server["generation_tokens"]),
            ("server prompt tokens", sum(prompts), server["prompt_tokens"]),
        ):
            match = abs(client_val - server_val) < 0.5
            checks.append(("PASS" if match else "WARN", label,
                           f"client {client_val:.0f} vs server {server_val:.0f}"
                           + ("" if match else " -- other traffic on the server, or scrape missed an instance")))
        if server.get("mean_ttft_s") is not None:
            client_ttft = summary["distributions"]["ttft_ms"]["mean"] / 1000
            diff_ms = (client_ttft - server["mean_ttft_s"]) * 1000
            ok_diff = -50 <= diff_ms <= max(100.0, 0.10 * client_ttft * 1000)
            checks.append(("PASS" if ok_diff else "WARN", "client vs server TTFT",
                           f"client mean {client_ttft*1000:.0f} ms, server mean {server['mean_ttft_s']*1000:.0f} ms "
                           f"(client overhead {diff_ms:+.0f} ms)"))

    prompt_ms = (summary["distributions"].get("server_prompt_ms") or {}).get("mean")
    if prompt_ms is not None:
        client_ttft = summary["distributions"]["ttft_ms"]["mean"]
        checks.append(("PASS" if client_ttft >= prompt_ms else "WARN", "client vs server prefill time",
                       f"client mean TTFT {client_ttft:.0f} ms vs server prompt processing {prompt_ms:.0f} ms "
                       f"(queueing + network {client_ttft - prompt_ms:+.0f} ms)"))

    cpu = summary["client"]["cpu_util_one_core"]
    checks.append(("PASS" if cpu < 0.7 else "WARN", "client CPU headroom",
                   f"load generator used {cpu*100:.0f}% of one core"
                   + ("" if cpu < 0.7 else " -- client may be adding latency (GIL-bound)")))

    if run["mode"] == "closed_loop":
        conc = summary["concurrency"]["mean"]
        checks.append(("INFO", "achieved concurrency",
                       f"time-weighted mean {conc:.2f} in-flight (peak {summary['concurrency']['max']}) "
                       f"for {run['users']} users"))

    unreliable = []
    for metric in ("ttft_ms", "tpot_ms", "e2e_ms"):
        d = summary["distributions"][metric]
        for q in (90, 95, 99):
            if d.get("n") and not d.get(f"p{q}_reliable"):
                unreliable.append(f"p{q}")
    if unreliable:
        n = summary["distributions"]["e2e_ms"]["n"]
        worst = sorted(set(unreliable), key=lambda s: int(s[1:]))
        checks.append(("WARN", "sample size",
                       f"n={n}: {', '.join(worst)} not statistically meaningful "
                       f"(p90 needs >=10, p95 >=20, p99 >=100 samples); marked * in tables"))
    return checks


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------


def run_all(cfg: dict, config_path: Path, only_targets, only_scenarios, out_root: Path) -> Path:
    client_cfg = cfg.get("client") or {}
    timeout = float(client_cfg.get("request_timeout_s", 900))
    if client_cfg.get("cpu_affinity"):
        os.sched_setaffinity(0, parse_cpu_list(client_cfg["cpu_affinity"]))

    runs = expand_scenarios(cfg)
    if only_scenarios:
        runs = [r for r in runs if r["name"] in only_scenarios]
    tcfgs = [t for t in cfg.get("targets") or [] if not only_targets or t["name"] in only_targets]
    if not tcfgs or not runs:
        raise SystemExit("nothing to run (check --targets / --scenarios)")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = out_root / f"{stamp}-{config_path.stem}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "config.yaml").write_text(config_path.read_text())
    log_file = open(run_dir / "run.log", "a")

    def log(msg):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        log_file.write(line + "\n")
        log_file.flush()

    meta = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "config": str(config_path), "host": platform.node(),
        "client_cpu_affinity": client_cfg.get("cpu_affinity"), "python": platform.python_version(),
        "targets": [],
    }
    summaries = []
    raw = open(run_dir / "requests.jsonl", "w")
    try:
        for tcfg in tcfgs:
            target = resolve_target(tcfg)
            meta["targets"].append(target.describe())
            if target.error:
                log(f"target {target.name}: SKIPPED ({target.error})")
                continue
            log(f"target {target.name}: {target.engine} serving {target.model} at {target.base_url} "
                f"({target.instances or '?'} instance(s), {len(target.metrics_urls)} metrics endpoint(s))")
            builder = PromptBuilder(target, log)
            for idx, run in enumerate(runs):
                run = copy.deepcopy(run)
                log(f"  [{idx+1}/{len(runs)}] {run['name']} {run['point']} ({run['mode']})")
                workload = Workload(builder, run, run_seed=run["seed"] * 1000 + idx)
                warmup(target, workload, run, timeout, log)
                before = scrape_prometheus(target.metrics_urls) if target.metrics_urls else None
                sampler = GaugeSampler(target.metrics_urls) if target.metrics_urls else None
                if sampler:
                    sampler.start()
                records: List[dict] = []
                ru0 = resource.getrusage(resource.RUSAGE_SELF)
                w0 = time.perf_counter()
                (run_closed_loop if run["mode"] == "closed_loop" else run_open_loop)(
                    target, workload, run, timeout, records, log)
                wall = time.perf_counter() - w0
                ru1 = resource.getrusage(resource.RUSAGE_SELF)
                cpu_util = ((ru1.ru_utime - ru0.ru_utime) + (ru1.ru_stime - ru0.ru_stime)) / wall if wall else 0
                server = gauges = None
                if sampler:
                    sampler.stop()
                    gauges = sampler.samples
                    time.sleep(1.0)  # let the engine publish its final counter updates
                    server = server_deltas(before, scrape_prometheus(target.metrics_urls))
                summary = aggregate(run, target, records, server, gauges or [], cpu_util, wall)
                summaries.append(summary)
                for r in records:
                    raw.write(json.dumps({"target": target.name, "scenario": run["name"],
                                          "point": run["point"], **r}) + "\n")
                raw.flush()
                tp = summary.get("throughput") or {}
                ttft = (summary.get("distributions") or {}).get("ttft_ms", {})
                log(f"    done: {summary['requests_ok']}/{summary['requests_total']} ok, "
                    f"{tp.get('output_tokens_per_s') or 0:.1f} out tok/s, "
                    f"TTFT p50 {ttft.get('p50', float('nan')):.0f} ms")
                for status, name, detail in summary["checks"]:
                    if status in ("FAIL", "WARN"):
                        log(f"    {status} {name}: {detail}")
                write_outputs(run_dir, meta, summaries)
    except KeyboardInterrupt:
        log("interrupted -- writing report for completed runs")
    finally:
        raw.close()
        meta["finished_at"] = datetime.now().isoformat(timespec="seconds")
        write_outputs(run_dir, meta, summaries)
        log(f"report: {run_dir / 'report.md'}")
        log_file.close()
    return run_dir


def write_outputs(run_dir: Path, meta: dict, summaries: List[dict]) -> None:
    (run_dir / "summary.json").write_text(json.dumps({"meta": meta, "runs": summaries}, indent=2, default=str))
    rows = [bench_report.csv_row(s) for s in summaries]
    if rows:
        with open(run_dir / "summary.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    (run_dir / "report.md").write_text(bench_report.render([meta], summaries))


def load_run(path: Path):
    data = json.loads((path / "summary.json").read_text())
    return data["meta"], data["runs"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the benchmark described by a YAML config")
    r.add_argument("-c", "--config", default=str(SCRIPT_DIR / "bench_config.yaml"))
    r.add_argument("--targets", nargs="*", help="only these target names")
    r.add_argument("--scenarios", nargs="*", help="only these scenario names")
    r.add_argument("-o", "--output-dir", default=str(SCRIPT_DIR / "results"))
    r.add_argument("--dry-run", action="store_true", help="print the expanded run list and exit")
    rep = sub.add_parser("report", help="re-render report.md from a run directory")
    rep.add_argument("run_dir")
    cmp_ = sub.add_parser("compare", help="one report across several run directories")
    cmp_.add_argument("run_dirs", nargs="+")
    cmp_.add_argument("-o", "--output", default="compare.md")
    args = ap.parse_args()

    if args.cmd == "run":
        config_path = Path(args.config).resolve()
        cfg = yaml.safe_load(config_path.read_text())
        if args.dry_run:
            for run in expand_scenarios(cfg):
                if not args.scenarios or run["name"] in args.scenarios:
                    print(f"{run['name']:24s} {run['mode']:12s} {run['point']}")
            return
        run_dir = run_all(cfg, config_path, args.targets, args.scenarios, Path(args.output_dir))
        print()
        print((run_dir / "report.md").read_text())
    elif args.cmd == "report":
        meta, runs = load_run(Path(args.run_dir))
        out = Path(args.run_dir) / "report.md"
        out.write_text(bench_report.render([meta], runs))
        print(out.read_text())
    else:
        metas, runs = [], []
        for d in args.run_dirs:
            m, rs = load_run(Path(d))
            metas.append(m)
            runs.extend(rs)
        Path(args.output).write_text(bench_report.render(metas, runs))
        print(Path(args.output).read_text())


if __name__ == "__main__":
    sys.exit(main())
