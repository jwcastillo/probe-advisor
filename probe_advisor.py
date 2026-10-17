#!/usr/bin/env python3
"""probe-advisor: how long Kubernetes pods really take to start and to stop, where the time goes, and
which probe / shutdown settings would stop wasting it or killing pods. Stdlib only.

Covers the whole pod lifecycle:
  start     startupProbe + readinessProbe: created -> app start -> app ready -> Ready -> first served request
  running   livenessProbe: restarts, probe failures, kill budget vs pauses
  stop      terminationGracePeriodSeconds, preStop, app graceful shutdown: how long it takes, whether it finishes

Sources (all optional except the one giving pod creation times):
  prometheus  kube-state-metrics: kube_pod_created, kube_pod_status_ready, kube_pod_container_status_restarts_total
  loki        app log lines: "started" (default Spring Boot), first served request (--served), graceful shutdown
  kubectl     pod conditions and container startedAt (1 s), the real probe/lifecycle spec, Unhealthy events

Usage:
  probe_advisor.py --profile my.toml --env prod -n my-namespace [--hours 24] [--source prometheus|kubectl] [--served] [--json]
  probe_advisor.py --selftest
"""
import argparse, concurrent.futures as cf, datetime as dt, json, math, os, re, subprocess, time, tomllib
import urllib.parse, urllib.request

DEFAULTS = {
    # Kubernetes defaults; a profile overrides them (e.g. with the values of your deploy template), kubectl replaces them.
    "probes": {"startup": {"delay": 0, "period": 10, "timeout": 1, "failure": 3, "path": None},
               "readiness": {"delay": 0, "period": 10, "timeout": 1, "success": 1, "failure": 3, "path": None},
               "liveness": {"delay": 0, "period": 10, "timeout": 1, "failure": 3, "path": None}},
    "shutdown": {"grace": 30, "prestop_sleep": 0, "app_timeout": 30},
    "signals": {"started": r"Started \S+ in ([0-9.]+) seconds \((?:process|JVM) running for ([0-9.]+)\)",
                "served": None, "served_contains": None,
                "shutdown_start": r"Commencing graceful shutdown", "shutdown_done": r"Graceful shutdown complete",
                "shutdown_aborted": r"Graceful shutdown aborted",
                "app_start": None,  # only for "started" regexes without group 2: a line marking process start
                "skip_containers": ["istio-proxy", "linkerd-proxy"]},
    # Tried in order when the main "started" regex matches nothing in a namespace (e.g. another stack).
    "signal_sets": [],
    "recommend": {"startup_period": 2, "readiness_period": 5, "margin": 1.5, "liveness_budget": 60, "prestop_sleep": 10},
}


# ---------- model ----------

def ready_at(app_ready, p):
    """Seconds from container start until kubelet marks Ready, if the app answers from app_ready on.
    Startup probe ticks at delay, delay+period...; readiness then needs `success` consecutive passes."""
    s, r = p["startup"], p["readiness"]
    tick = s["delay"] + max(0, math.ceil((app_ready - s["delay"]) / s["period"])) * s["period"]
    return tick + (r["success"] - 1) * r["period"]


def recommend(app_p99, stop_p99, cur, sd, rec):
    period = rec["startup_period"]
    lv = cur["liveness"]
    # Liveness: same endpoint/timeouts, enough failures to survive a long GC pause or CPU throttling.
    lv_fail = max(lv["failure"], math.ceil(rec["liveness_budget"] / lv["period"]))
    prestop = max(sd["prestop_sleep"], rec["prestop_sleep"])
    app_timeout = sd["app_timeout"] if stop_p99 is None else min(sd["app_timeout"], max(10, math.ceil(stop_p99 * rec["margin"])))
    return {"probes": {"startup": {**cur["startup"], "delay": 0, "period": period,
                                   "failure": max(3, math.ceil(app_p99 * rec["margin"] / period))},
                       "readiness": {**cur["readiness"], "delay": 0, "period": rec["readiness_period"], "success": 1},
                       "liveness": {**lv, "delay": 0, "failure": lv_fail}},
            "shutdown": {"prestop_sleep": prestop, "app_timeout": app_timeout, "grace": prestop + app_timeout + 5}}


def checks(probes, sd, app_p99, stop_p99, restarts, fails):
    """Misconfigurations, worst first. Each: (severity, message)."""
    s, r, lv = probes["startup"], probes["readiness"], probes["liveness"]
    out = []
    if app_p99 and s["delay"] + s["failure"] * s["period"] < app_p99 * 1.2:
        out.append(("high", f"startup budget {s['delay'] + s['failure'] * s['period']} s < p99 app start {app_p99:.0f} s × 1.2: slow starts get killed (crashloop)"))
    if lv.get("path") and lv["path"] == r.get("path"):
        out.append(("high", "liveness and readiness use the same endpoint: a dependency outage restarts every pod"))
    if lv["failure"] * lv["period"] < 30:
        out.append(("high", f"liveness kills after {lv['failure'] * lv['period']} s of failures: a GC pause or CPU throttling restarts the pod"))
    if lv["timeout"] <= 1:
        out.append(("med", "liveness timeout 1 s: one slow answer under load counts as a failure"))
    if sd["grace"] <= sd["prestop_sleep"] + sd["app_timeout"]:
        out.append(("high", f"terminationGracePeriodSeconds {sd['grace']} <= preStop {sd['prestop_sleep']} + app shutdown timeout {sd['app_timeout']}: "
                            "SIGKILL can land in the middle of the graceful shutdown"))
    if sd["prestop_sleep"] == 0:
        out.append(("med", "no preStop sleep: the pod gets SIGTERM while endpoints/sidecars still send it traffic (errors on scale-in and deploys)"))
    if stop_p99 and stop_p99 > sd["grace"] - sd["prestop_sleep"]:
        out.append(("high", f"p99 graceful shutdown {stop_p99:.0f} s exceeds what the grace period leaves"))
    if r["timeout"] <= 1:
        out.append(("med", "readiness timeout 1 s: if the readiness check does real work (warmup, dependencies) measure its latency"))
    if r["success"] > 1:
        out.append(("low", f"readiness successThreshold {r['success']}: adds {(r['success'] - 1) * r['period']} s to every start"))
    if s["delay"] > 0:
        out.append(("low", f"startup initialDelaySeconds {s['delay']}: nothing can be Ready before it"))
    if restarts:
        out.append(("info", f"{restarts:.0f} container restarts in the window" + (f"; probe failures: {fails}" if fails else "")))
    return out


def pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    return xs[min(len(xs) - 1, round(p * (len(xs) - 1)))] if xs else None


# ---------- sources ----------

def http_json(url, params, token):
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params))
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def prom_first(env, token, query, start, end, step, want=None):
    """{pod: first timestamp where the series == want} or {pod: first value} when want is None."""
    d = http_json(env["prometheus"] + "/api/v1/query_range", {"query": query, "start": start, "end": end, "step": step}, token)
    out = {}
    for r in d["data"]["result"]:
        for t, v in r["values"]:
            if want is None or float(v) == want:
                out[r["metric"]["pod"]] = float(v) if want is None else float(t)
                break
    return out


def prom_value(env, token, query, at):
    d = http_json(env["prometheus"] + "/api/v1/query", {"query": query, "time": at}, token)
    return sum(float(r["value"][1]) for r in d["data"]["result"])


def loki_lines(env, token, query, start, end, limit=5000, chunk=7200):
    """(pod, epoch_s, line) for every line of a LogQL query, oldest first, chunked in time."""
    pod_label = env.get("loki_labels", {}).get("pod", "pod")
    out, t = [], start
    while t < end:
        d = http_json(env["loki"] + "/loki/api/v1/query_range",
                      {"query": query, "start": f"{int(t)}000000000", "end": f"{int(min(end, t + chunk))}000000000",
                       "limit": limit, "direction": "forward"}, token)
        for s in d["data"]["result"]:
            for ts_, line in s["values"]:
                out.append((s["stream"].get(pod_label), int(ts_) / 1e9, line))
        t += chunk
    return out


def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None


def kubectl_pods(ns, context, skip):
    ctx = ["--context", context] if context else []
    pods = json.loads(subprocess.check_output(["kubectl", *ctx, "-n", ns, "get", "pods", "-o", "json"]))["items"]
    ev = json.loads(subprocess.check_output(["kubectl", *ctx, "-n", ns, "get", "events", "-o", "json",
                                             "--field-selector", "reason=Unhealthy"]))["items"]
    return parse_kubectl(pods, ev, skip)


def parse_kubectl(pods, events, skip):
    out, spec, fails = {}, None, {}
    for e in events:
        kind = e.get("message", "").split(" probe")[0]
        fails.setdefault(e["involvedObject"]["name"], {}).setdefault(kind, 0)
        fails[e["involvedObject"]["name"]][kind] += e.get("count", 1)
    for p in pods:
        cond = {c["type"]: ts(c.get("lastTransitionTime")) for c in p["status"].get("conditions", [])}
        app = next((c for c in p["spec"]["containers"] if c["name"] not in skip), None)
        st = next((c for c in p["status"].get("containerStatuses", []) if app and c["name"] == app["name"]), {})
        name = p["metadata"]["name"]
        out[name] = {"created": ts(p["metadata"]["creationTimestamp"]), "scheduled": cond.get("PodScheduled"),
                     "container_started": ts(st.get("state", {}).get("running", {}).get("startedAt")),
                     "ready": cond.get("Ready") if p["status"].get("phase") == "Running" else None,
                     "restarts": st.get("restartCount", 0), "probe_failures": fails.get(name, {})}
        if spec is None and app:
            spec = (probe_spec(app), {"grace": p["spec"].get("terminationGracePeriodSeconds", 30),
                                      "prestop_sleep": prestop_sleep(app)})
    return out, spec


def probe_spec(c):
    def one(p, extra):
        p = p or {}
        path = (p.get("httpGet") or {}).get("path")
        return {"delay": p.get("initialDelaySeconds", 0), "period": p.get("periodSeconds", 10),
                "timeout": p.get("timeoutSeconds", 1), "failure": p.get("failureThreshold", 3), "path": path, **extra(p)}
    rp = c.get("readinessProbe")
    return {"startup": one(c.get("startupProbe") or rp, lambda p: {}),
            "readiness": one(rp, lambda p: {"success": p.get("successThreshold", 1)}),
            "liveness": one(c.get("livenessProbe"), lambda p: {})}


def prestop_sleep(c):
    h = ((c.get("lifecycle") or {}).get("preStop") or {})
    if "sleep" in h:
        return h["sleep"].get("seconds", 0)
    cmd = " ".join((h.get("exec") or {}).get("command", []))
    m = re.search(r"sleep\s+([0-9]+)", cmd)
    return int(m[1]) if m else 0


# ---------- analysis ----------

def collect(args, cfg):
    env = cfg["envs"][args.env]
    token = os.environ.get(env.get("token_env", ""), "")
    sig, probes, sd = cfg["signals"], cfg["probes"], dict(cfg["shutdown"])
    end = time.time(); start = end - args.hours * 3600
    lab = env.get("loki_labels", {})
    ns_l, pod_l, ct_l = lab.get("namespace", "namespace"), lab.get("pod", "pod"), lab.get("container", "container")
    skip = sig["skip_containers"]
    extra = {"restarts": 0}

    if args.source == "kubectl":
        pods, spec = kubectl_pods(args.namespace, env.get("kube_context"), skip)
        if spec:
            probes = spec[0]; sd.update(spec[1])
        start = min([p["created"] for p in pods.values()] + [end]) - 60
        extra["restarts"] = sum(p["restarts"] for p in pods.values())
    else:
        q = lambda m, more="": f'max by (pod)({m}{{namespace="{args.namespace}"{more}}})'
        created = prom_first(env, token, q("kube_pod_created"), start, end, 300)
        ready = prom_first(env, token, q("kube_pod_status_ready", ',condition="true"'), start, end,
                           env.get("ready_step", 30), want=1.0)
        pods = {p: {"created": c, "ready": ready.get(p)} for p, c in created.items() if c >= start + 300}
        notskip = "".join(f',container!="{c}"' for c in skip)
        extra["restarts"] = prom_value(env, token, f'sum(increase(kube_pod_container_status_restarts_total'
                                                   f'{{namespace="{args.namespace}"{notskip}}}[{int(args.hours * 3600)}s]))', end)

    if env.get("loki"):
        sel = f'{{{ns_l}="{args.namespace}"' + "".join(f',{ct_l}!="{c}"' for c in skip) + "}"
        for cand in [sig] + [{**sig, **alt} for alt in cfg.get("signal_sets", [])]:
            if cand.get("started") and started_lines(env, token, sel, cand, pods, start, end):
                sig = cand
                extra["signals"] = cand.get("name", "default")
                break
        if args.served and sig.get("served"):
            first_served(env, token, sel, pod_l, sig, pods)
        extra["stops"] = shutdowns(env, token, sel, sig, start, end)
    return pods, probes, sd, extra


def started_lines(env, token, sel, sig, pods, start, end):
    """Fills started_log / app_s / jvm_start. Seconds since process start come from group 2 of `started`
    (Spring: "process running for N") or, without it, from the first `app_start` line of the pod."""
    rx = re.compile(sig["started"])
    first_start = {}
    if rx.groups < 2 and sig.get("app_start"):
        for pod, t, _ in loki_lines(env, token, f"{sel} |~ `{sig['app_start']}`", start, end):
            first_start.setdefault(pod, t)
    hits = 0
    for pod, t, line in loki_lines(env, token, f"{sel} |~ `{sig['started']}`", start, end):
        m = rx.search(line)
        if not m or pod not in pods or "started_log" in pods[pod]:
            continue
        if rx.groups >= 2:
            app_s = float(m[2])
        elif pod in first_start and first_start[pod] <= t:
            app_s = t - first_start[pod]
        else:
            continue
        pods[pod].update(started_log=t, app_s=app_s, jvm_start=t - app_s)
        hits += 1
    return hits


def first_served(env, token, sel, pod_l, sig, pods):
    rx = re.compile(sig["served"])
    contains = f' |= `{sig["served_contains"]}`' if sig.get("served_contains") else ""
    def one(item):
        pod, p = item
        # `| pod="x"` instead of a stream selector: works whether the pod is an indexed label or structured metadata.
        for _, t, line in loki_lines(env, token, f'{sel}{contains} | {pod_l}="{pod}"', p["started_log"] - 5,
                                     p["started_log"] + 900, limit=200, chunk=905):
            if rx.search(line):
                return pod, t
        return pod, None
    with cf.ThreadPoolExecutor(8) as ex:
        for pod, t in ex.map(one, [(k, v) for k, v in pods.items() if v.get("started_log")]):
            pods[pod]["served"] = t


def shutdowns(env, token, sel, sig, start, end):
    """{pod: {start, done, aborted}} from the app's graceful-shutdown log lines."""
    keys = [k for k in ("shutdown_start", "shutdown_done", "shutdown_aborted") if sig.get(k)]
    if "shutdown_start" not in keys:
        return {}
    pat = "|".join(f"({sig[k]})" for k in keys)
    out = {}
    for pod, t, line in loki_lines(env, token, f"{sel} |~ `{pat}`", start, end):
        for k in keys:
            if re.search(sig[k], line):
                out.setdefault(pod, {}).setdefault(k.split("_")[1], t)
    return out


def segments(pods, stops):
    seg = {k: [] for k in ("created→scheduled", "scheduled→container", "created→app start", "app start→app ready",
                           "app ready→Ready", "app ready→1st served", "created→Ready", "graceful shutdown")}
    for p in pods.values():
        d = lambda a, b: (p[b] - p[a]) if p.get(a) and p.get(b) else None
        start = p.get("container_started") or p.get("jvm_start")
        seg["created→scheduled"].append(d("created", "scheduled"))
        seg["scheduled→container"].append(d("scheduled", "container_started"))
        seg["created→app start"].append((start - p["created"]) if start and p.get("created") else None)
        seg["app start→app ready"].append(p.get("app_s"))
        seg["app ready→Ready"].append(d("started_log", "ready"))
        seg["app ready→1st served"].append(d("started_log", "served"))
        seg["created→Ready"].append(d("created", "ready"))
    for s in stops.values():
        if s.get("start") and s.get("done"):
            seg["graceful shutdown"].append(s["done"] - s["start"])
    return {k: [x for x in v if x is not None and x >= 0] for k, v in seg.items()}


def report(args, cfg, pods, probes, sd, extra):
    stops = extra.get("stops", {})
    seg = segments(pods, stops)
    app, stop = seg["app start→app ready"], seg["graceful shutdown"]
    app_p99, stop_p99 = pct(app, .99), pct(stop, .99)
    fails = {}
    for p in pods.values():
        for k, v in p.get("probe_failures", {}).items():
            fails[k] = fails.get(k, 0) + v
    res = {"namespace": args.namespace, "env": args.env, "source": args.source, "pods": len(pods),
           "signals": extra.get("signals"),
           "segments": {k: {"n": len(v), "p50": pct(v, .5), "p90": pct(v, .9), "p99": pct(v, .99)} for k, v in seg.items()},
           "current": {"probes": probes, "shutdown": sd}, "restarts": extra.get("restarts", 0), "probe_failures": fails,
           "shutdown_seen": len(stops), "shutdown_unfinished": sum(1 for s in stops.values() if s.get("start") and not s.get("done")),
           "shutdown_aborted": sum(1 for s in stops.values() if s.get("aborted"))}
    res["checks"] = checks(probes, sd, app_p99, stop_p99, res["restarts"], fails)
    if app:
        rec = recommend(app_p99, stop_p99, probes, sd, cfg["recommend"])
        res["recommended"] = rec
        res["ready_model"] = {q: {"app_ready": pct(app, v), "current": ready_at(pct(app, v), probes),
                                  "recommended": ready_at(pct(app, v), rec["probes"])} for q, v in (("p50", .5), ("p90", .9))}
    return res


def markdown(r, scrape):
    f = lambda x: "—" if x is None else f"{x:.1f}"
    out = [f"## {r['namespace']} ({r['env']}, {r['source']}, signals: {r.get('signals') or 'none matched'}): {r['pods']} new pods", "",
           "| Segment | n | p50 (s) | p90 (s) | p99 (s) |", "|---|---|---|---|---|"]
    out += [f"| {k} | {v['n']} | {f(v['p50'])} | {f(v['p90'])} | {f(v['p99'])} |" for k, v in r["segments"].items() if v["n"]]
    if r["shutdown_seen"]:
        out += ["", f"Graceful shutdowns seen: {r['shutdown_seen']} · never logged 'complete' (likely SIGKILL): "
                    f"{r['shutdown_unfinished']} · aborted with requests active: {r['shutdown_aborted']}"]
    rows = [("startup", ("delay", "period", "timeout", "failure")), ("readiness", ("delay", "period", "timeout", "success")),
            ("liveness", ("delay", "period", "timeout", "failure"))]
    rec = r.get("recommended")
    out += ["", "| Setting | Now | Recommended |", "|---|---|---|"]
    for probe, keys in rows:
        cur = r["current"]["probes"][probe]
        nw = rec["probes"][probe] if rec else {}
        for k in keys:
            out.append(f"| {probe}.{k} | {cur.get(k)} | {nw.get(k, '—') if nw.get(k) != cur.get(k) else '='} |")
    for k, name in (("grace", "terminationGracePeriodSeconds"), ("prestop_sleep", "preStop sleep"), ("app_timeout", "app shutdown timeout")):
        cur, nw = r["current"]["shutdown"][k], (rec["shutdown"][k] if rec else None)
        out.append(f"| {name} | {cur} | {'=' if nw in (None, cur) else nw} |")
    if "ready_model" in r:
        out += ["", "| App ready at | Ready with current probes | Ready with recommended | Saved |", "|---|---|---|---|"]
        out += [f"| {q} {f(m['app_ready'])} s | {f(m['current'])} s | {f(m['recommended'])} s | {f(m['current'] - m['recommended'])} s |"
                for q, m in r["ready_model"].items()]
    if r["checks"]:
        out += ["", "Findings:"] + [f"- **{sev}** {msg}" for sev, msg in r["checks"]]
    out += ["", "Caveats:",
            "- Ready is gated by the probes: it shows when kubelet let traffic in, not when the app could take it.",
            "- Readiness/liveness timeouts are kept as they are: no source gives the latency of the probe calls. Measure them.",
            "- Pods restored from a CRaC snapshot log the `Started` line of the training run, not their own."]
    if r["source"] != "kubectl":
        out.append(f"- Ready comes from kube-state-metrics: up to one scrape interval (~{scrape} s) late. "
                   "Probe paths, preStop and grace come from the profile, not the cluster: run --source kubectl to read them.")
    return "\n".join(out)


def load_profile(path):
    cfg = json.loads(json.dumps(DEFAULTS))
    if path:
        with open(path, "rb") as fh:
            user = tomllib.load(fh)
        for k, v in user.items():
            if k == "probes":
                for probe, vals in v.items():
                    cfg["probes"][probe] = {**cfg["probes"][probe], **vals}
            elif isinstance(v, dict) and k != "envs":
                cfg[k] = {**cfg.get(k, {}), **v}
            else:
                cfg[k] = v
    return cfg


def selftest():
    cfg = load_profile(None)
    p = json.loads(json.dumps(cfg["probes"]))
    p["startup"].update(delay=10, period=5, timeout=2, failure=30)
    p["readiness"].update(period=10, success=2)
    assert ready_at(5.3, p) == 20 and ready_at(0.2, p) == 20 and ready_at(12, p) == 25
    sd = {"grace": 30, "prestop_sleep": 0, "app_timeout": 30}
    rec = recommend(6.0, 4.0, p, sd, cfg["recommend"])
    assert rec["probes"]["startup"]["failure"] == 5 and ready_at(5.3, rec["probes"]) == 6 and ready_at(0.2, rec["probes"]) == 2
    assert rec["probes"]["liveness"]["failure"] == 6                     # 60 s budget / 10 s period
    assert rec["shutdown"] == {"prestop_sleep": 10, "app_timeout": 10, "grace": 25}, rec["shutdown"]
    msgs = " ".join(m for _, m in checks(p, sd, 6.0, 4.0, 0, {}))
    assert "SIGKILL" in msgs and "no preStop" in msgs and "successThreshold 2" in msgs
    p2 = json.loads(json.dumps(p)); p2["liveness"]["path"] = p2["readiness"]["path"] = "/health"
    assert "same endpoint" in " ".join(m for _, m in checks(p2, sd, 6, None, 0, {}))
    assert re.search(cfg["signals"]["started"], "Started Application in 9.346 seconds (process running for 13.505)")[2] == "13.505"
    assert re.search(cfg["signals"]["started"], "Started FooApp in 3.1 seconds (JVM running for 4.0)")
    pod = {"metadata": {"name": "a", "creationTimestamp": "2026-01-01T00:00:00Z"},
           "spec": {"terminationGracePeriodSeconds": 45,
                    "containers": [{"name": "istio-proxy"},
                                   {"name": "app", "startupProbe": {"initialDelaySeconds": 10, "periodSeconds": 5, "httpGet": {"path": "/live"}},
                                    "readinessProbe": {"periodSeconds": 10, "successThreshold": 2, "httpGet": {"path": "/ready"}},
                                    "livenessProbe": {"periodSeconds": 20, "failureThreshold": 6, "timeoutSeconds": 5, "httpGet": {"path": "/live"}},
                                    "lifecycle": {"preStop": {"exec": {"command": ["sh", "-c", "sleep 10"]}}}}]},
           "status": {"phase": "Running", "conditions": [{"type": "PodScheduled", "lastTransitionTime": "2026-01-01T00:00:02Z"},
                                                         {"type": "Ready", "lastTransitionTime": "2026-01-01T00:00:40Z"}],
                      "containerStatuses": [{"name": "app", "restartCount": 1, "state": {"running": {"startedAt": "2026-01-01T00:00:15Z"}}}]}}
    ev = [{"message": "Readiness probe failed: timeout", "involvedObject": {"name": "a"}, "count": 3}]
    out, (spec, shut) = parse_kubectl([pod], ev, cfg["signals"]["skip_containers"])
    assert out["a"]["container_started"] - out["a"]["created"] == 15 and out["a"]["probe_failures"] == {"Readiness": 3}
    assert spec["startup"]["delay"] == 10 and spec["readiness"]["success"] == 2 and spec["liveness"]["failure"] == 6
    assert shut == {"grace": 45, "prestop_sleep": 10}
    seg = segments({"a": {**out["a"], "started_log": out["a"]["container_started"] + 6, "app_s": 6}},
                   {"a": {"start": 100.0, "done": 104.5}, "b": {"start": 100.0}})
    assert seg["created→scheduled"] == [2] and seg["app ready→Ready"] == [19] and seg["created→Ready"] == [40]
    assert seg["graceful shutdown"] == [4.5]
    pods2 = {"p": {"created": 0.0}}
    fake = {"app_start": [("p", 30.0, "> app@1.0 start"), ("p", 33.0, "> app@1.0 start")],
            "started": [("p", 42.4, "ready at http://localhost:8080/x")]}
    global loki_lines
    real = loki_lines
    loki_lines = lambda env, token, q, s, e, **k: fake["app_start" if "start`" in q and "ready" not in q else "started"]
    try:
        n = started_lines({}, "", "{}", {"started": r"ready at http", "app_start": r"> \S+@\S+ start"}, pods2, 0, 100)
    finally:
        loki_lines = real
    assert n == 1 and abs(pods2["p"]["app_s"] - 12.4) < 1e-9 and pods2["p"]["jvm_start"] == 30.0, pods2
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile"); ap.add_argument("--env"); ap.add_argument("-n", "--namespace")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--source", choices=["prometheus", "kubectl"], default="prometheus")
    ap.add_argument("--served", action="store_true", help="first served request per pod from Loki (1 query per pod)")
    ap.add_argument("--started-regex", help="override the 'app is up' log regex (group 2 = seconds since process start)")
    ap.add_argument("--json", action="store_true"); ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not (a.env and a.namespace):
        ap.error("--env and --namespace are required")
    cfg = load_profile(a.profile)
    if a.started_regex:
        cfg["signals"]["started"] = a.started_regex
    pods, probes, sd, extra = collect(a, cfg)
    r = report(a, cfg, pods, probes, sd, extra)
    print(json.dumps(r, indent=2) if a.json else markdown(r, cfg["envs"][a.env].get("scrape_interval", 60)))


if __name__ == "__main__":
    main()
