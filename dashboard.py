#!/usr/bin/env python3
"""Fleet dashboard from probe-advisor --json outputs: one self-contained HTML file (inline SVG, no deps).
Usage: dashboard.py OUT.html report1.json [report2.json ...]   |   dashboard.py --selftest"""
import html, json, sys
from collections import Counter

STACK = [("created→app start", "Before the app (scheduling, image, init containers)", "var(--c1)"),
         ("app start→app ready", "App startup", "var(--c2)"),
         ("app ready→Ready", "Probes until Ready (+ up to one scrape of lag)", "var(--c3)")]


def seg(r, k, q="p50"):
    return (r["segments"].get(k) or {}).get(q)


def fmt(x, unit=" s"):
    return "—" if x is None else ("<1" + unit if 0 <= x < 0.5 else f"{x:.0f}{unit}")


def saved(r):
    m = r.get("ready_model", {}).get("p50")
    return (m["current"] - m["recommended"]) if m else None


def bars(rows):
    scale = max((sum(seg(r, k) or 0 for k, _, _ in STACK) for r in rows), default=1) or 1
    h, lw, w = 26, 260, 620
    out = [f'<svg viewBox="0 0 {lw + w + 70} {h * len(rows) + 30}" role="img" aria-label="Startup by segment">']
    for i, r in enumerate(rows):
        y, x = i * h + 4, lw
        out.append(f'<text x="{lw - 8}" y="{y + 15}" text-anchor="end" class="lbl">{html.escape(r["namespace"])}</text>')
        for k, name, color in STACK:
            v = seg(r, k)
            if not v:
                continue
            bw = v / scale * w
            out.append(f'<rect x="{x:.1f}" y="{y}" width="{bw:.1f}" height="18" fill="{color}" rx="2">'
                       f'<title>{name}: {v:.0f} s (p50)</title></rect>')
            x += bw
        total = seg(r, "created→Ready")
        out.append(f'<text x="{x + 6:.1f}" y="{y + 14}" class="val">{fmt(total)}</text>')
        sv = saved(r)
        if sv:  # where Ready would land with the recommended probes
            mx = x - sv / scale * w
            out.append(f'<line x1="{mx:.1f}" x2="{mx:.1f}" y1="{y - 2}" y2="{y + 20}" class="rec"><title>Recommended probes: -{sv:.0f} s</title></line>')
    out.append("</svg>")
    return "".join(out)


def table(rows):
    head = ("Service", "Signals", "New pods", "Created→app p50/p90", "App ready p50/p99", "App→Ready p50", "Created→Ready p50/p90",
            "Saved/start", "Shutdown p99", "Unfinished stops", "Restarts", "Findings")
    out = ["<table><thead><tr>" + "".join(f"<th>{h}</th>" for h in head) + "</tr></thead><tbody>"]
    for r in rows:
        sev = Counter(s for s, _ in r["checks"])
        badges = " ".join(f'<span class="b {s}">{n} {s}</span>' for s, n in sorted(sev.items(), key=lambda x: "hml i".find(x[0][0])))
        cells = [html.escape(r["namespace"]), html.escape(r.get("signals") or "—"), r["pods"],
                 f'{fmt(seg(r, "created→app start"))} / {fmt(seg(r, "created→app start", "p90"))}',
                 f'{fmt(seg(r, "app start→app ready"))} / {fmt(seg(r, "app start→app ready", "p99"))}',
                 fmt(seg(r, "app ready→Ready")),
                 f'{fmt(seg(r, "created→Ready"))} / {fmt(seg(r, "created→Ready", "p90"))}',
                 fmt(saved(r)), fmt(seg(r, "graceful shutdown", "p99")),
                 f'{r.get("shutdown_unfinished", 0)}/{r.get("shutdown_seen", 0)}',
                 f'<span class="{"warn" if r.get("restarts", 0) >= 10 else ""}">{r.get("restarts", 0):.0f}</span>', badges]
        out.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
        out.append(f'<tr class="det"><td colspan="{len(head)}"><details><summary>Settings and findings</summary>{detail(r)}</details></td></tr>')
    out.append("</tbody></table>")
    return "".join(out)


def detail(r):
    cur, rec = r["current"], r.get("recommended")
    rows = []
    for probe in ("startup", "readiness", "liveness"):
        for k in ("delay", "period", "timeout", "failure", "success"):
            if k in cur["probes"][probe]:
                a, b = cur["probes"][probe][k], (rec["probes"][probe].get(k) if rec else None)
                rows.append((f"{probe}.{k}", a, b))
    for k in ("grace", "prestop_sleep", "app_timeout"):
        rows.append((k, cur["shutdown"][k], rec["shutdown"][k] if rec else None))
    t = "".join(f'<tr><td>{k}</td><td>{a}</td><td class="{"chg" if b not in (None, a) else ""}">{"=" if b in (None, a) else b}</td></tr>'
                for k, a, b in rows)
    f = "".join(f'<li><span class="b {s}">{s}</span> {html.escape(m)}</li>' for s, m in r["checks"])
    return f'<div class="dd"><table class="mini"><tr><th>Setting</th><th>Now</th><th>Recommended</th></tr>{t}</table><ul>{f}</ul></div>'


def render(reports, title="Pod startup and shutdown"):
    rows = sorted(reports, key=lambda r: -(seg(r, "created→Ready") or 0))
    pods = sum(r["pods"] for r in rows)
    readies = sorted(seg(r, "created→Ready") for r in rows if seg(r, "created→Ready"))
    pod_s = sum(r["pods"] * saved(r) * 24 / r.get("hours", 24) for r in rows if saved(r))
    kpi = [("Services", len(rows)), ("New pods / window", pods),
           ("Median created→Ready", fmt(readies[len(readies) // 2]) if readies else "—"),
           ("Pod-hours/day saved with recommended probes", f"{pod_s / 3600:.1f}")]
    common = Counter(m for r in rows for s, m in r["checks"] if s in ("high", "med"))
    legend = "".join(f'<span><i style="background:{c}"></i>{n}</span>' for _, n, c in STACK) + '<span><i class="recl"></i>Ready with recommended probes</span>'
    env = ", ".join(sorted({f'{r["env"]}/{r["source"]}' for r in rows}))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pod Startup Probes</title><style>
:root{{--bg:#fff;--fg:#1d1f23;--mut:#666b75;--line:#e3e5e8;--card:#f6f7f9;--c1:#9aa5b1;--c2:#3d7dd8;--c3:#e8a33d;--hi:#c62828;--me:#ef6c00;--lo:#5c6bc0}}
@media (prefers-color-scheme:dark){{:root:not([data-theme=light]){{--bg:#15171a;--fg:#e7e9ec;--mut:#9aa0a8;--line:#2b2f35;--card:#1d2024;--c1:#5d6670;--c2:#5b9bf0;--c3:#f0b25a;--hi:#ef5350;--me:#ffa040;--lo:#8c9eff}}}}
:root[data-theme=dark]{{--bg:#15171a;--fg:#e7e9ec;--mut:#9aa0a8;--line:#2b2f35;--card:#1d2024;--c1:#5d6670;--c2:#5b9bf0;--c3:#f0b25a;--hi:#ef5350;--me:#ffa040;--lo:#8c9eff}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:1280px;margin:0 auto;padding:24px 16px}} h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:28px 0 10px}}
.sub{{color:var(--mut)}} .kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:18px 0}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}} .kpi b{{display:block;font-size:24px}} .kpi span{{color:var(--mut);font-size:12px}}
svg{{width:100%;height:auto}} .lbl{{fill:var(--fg);font-size:12px}} .val{{fill:var(--mut);font-size:12px}} .rec{{stroke:var(--fg);stroke-width:2;stroke-dasharray:3 2}}
.legend{{display:flex;flex-wrap:wrap;gap:14px;color:var(--mut);font-size:12px}} .legend i{{display:inline-block;width:12px;height:12px;border-radius:2px;margin-right:5px;vertical-align:-2px}}
.recl{{border-left:2px dashed var(--fg);width:0!important}} .scroll{{overflow-x:auto}}
table{{border-collapse:collapse;width:100%;font-size:13px}} th,td{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}}
th{{color:var(--mut);font-weight:600}} tr.det td{{border-bottom:1px solid var(--line);padding:0 8px 6px;white-space:normal}} summary{{cursor:pointer;color:var(--mut);font-size:12px}}
.b{{display:inline-block;padding:1px 6px;border-radius:9px;font-size:11px;color:#fff}} .b.high{{background:var(--hi)}} .b.med{{background:var(--me)}} .b.low{{background:var(--lo)}} .b.info{{background:var(--c1)}}
.warn{{color:var(--hi);font-weight:700}} .dd{{display:flex;flex-wrap:wrap;gap:24px;padding:8px 0}} .mini{{width:auto}} .chg{{font-weight:700}} ul{{margin:0;padding-left:18px}}
</style></head><body><main>
<h1>{html.escape(title)}</h1><div class="sub">probe-advisor · {html.escape(env)} · p50 unless noted · Ready from kube-state-metrics can be up to one scrape late</div>
<div class="kpis">{"".join(f'<div class="kpi"><b>{v}</b><span>{k}</span></div>' for k, v in kpi)}</div>
<h2>Where the time goes until a new pod takes traffic</h2><div class="legend">{legend}</div>{bars(rows)}
<h2>Most common findings</h2><ul>{"".join(f"<li>{n}× {html.escape(m)}</li>" for m, n in common.most_common(6))}</ul>
<h2>Per service</h2><div class="scroll">{table(rows)}</div>
</main></body></html>"""


def selftest():
    r = {"namespace": "svc", "env": "prod", "source": "prometheus", "pods": 10, "signals": "spring-boot",
         "segments": {"created→app start": {"p50": 25, "p90": 30}, "app start→app ready": {"p50": 15, "p99": 28},
                      "app ready→Ready": {"p50": 60}, "created→Ready": {"p50": 100, "p90": 120}, "graceful shutdown": {"p99": 4}},
         "ready_model": {"p50": {"current": 30, "recommended": 16}}, "restarts": 12, "shutdown_seen": 3, "shutdown_unfinished": 1,
         "checks": [["high", "terminationGracePeriodSeconds 30 <= x"], ["med", "no preStop sleep: y"]],
         "current": {"probes": {"startup": {"delay": 10, "period": 5, "timeout": 2, "failure": 30},
                                "readiness": {"delay": 0, "period": 10, "timeout": 1, "success": 2},
                                "liveness": {"delay": 0, "period": 20, "timeout": 5, "failure": 6}},
                     "shutdown": {"grace": 30, "prestop_sleep": 0, "app_timeout": 30}},
         "recommended": {"probes": {"startup": {"delay": 0, "period": 2, "timeout": 2, "failure": 21},
                                    "readiness": {"delay": 0, "period": 5, "timeout": 1, "success": 1},
                                    "liveness": {"delay": 0, "period": 20, "timeout": 5, "failure": 6}},
                         "shutdown": {"grace": 25, "prestop_sleep": 10, "app_timeout": 10}}}
    out = render([r, {**r, "namespace": "other", "ready_model": {}, "recommended": None, "checks": []}])
    assert "<svg" in out and "svc" in out and "14 s" in out and 'class="warn">12' in out and "1/3" in out
    assert out.count("<details>") == 2 and "Pod-hours/day" in out
    print("selftest ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        selftest()
    elif len(sys.argv) >= 3:
        open(sys.argv[1], "w").write(render([json.load(open(p)) for p in sys.argv[2:]]))
        print(f"wrote {sys.argv[1]} ({len(sys.argv) - 2} services)")
    else:
        sys.exit(__doc__)
