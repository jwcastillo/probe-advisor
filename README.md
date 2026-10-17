# probe-advisor

How long your Kubernetes pods really take to start and to stop, where the time goes, and which probe and shutdown settings would stop wasting it or killing pods.

Probes are copied from template to template and rarely revisited. A typical result:
- the app is up in 15 s, but the pod is Ready at 30 s because of `initialDelaySeconds` and `successThreshold`;
- `terminationGracePeriodSeconds` equals the app's own shutdown timeout, so SIGKILL can land mid-shutdown;
- there is no `preStop`, so scale-in drops requests.

probe-advisor measures it per namespace and tells you what to change.

```bash
MONITORING_TOKEN=... python3 probe_advisor.py --profile profiles/my.toml --env prod -n my-service
python3 probe_advisor.py ... --served          # also the first request each pod served (Loki)
python3 probe_advisor.py ... --source kubectl  # exact timestamps + the real probe spec + Unhealthy events
python3 probe_advisor.py ... --json            # for dashboards / CI
python3 probe_advisor.py --selftest
```

Python 3.11+, standard library only.

## What it measures

| Phase | Segments | Settings it checks |
|---|---|---|
| Start | created → scheduled → container → app start → app ready → Ready → first served request | `startupProbe` (delay, period, failureThreshold vs p99 start), `readinessProbe` (period, successThreshold, timeout) |
| Running | restarts, probe failures | `livenessProbe` (kill budget vs GC pauses / throttling, timeout, same endpoint as readiness) |
| Stop | graceful shutdown duration, shutdowns that never finished | `terminationGracePeriodSeconds` vs `preStop` + app shutdown timeout, missing `preStop` |

## Signals for other stacks

When the main "started" regex matches nothing in a namespace, the tool tries each `[[signal_sets]]` of the profile in order. A set can use two lines instead of one:
- `app_start`: the line that marks process start;
- `started`: the line that says the app is listening.

```toml
[[signal_sets]]
name = "node-npm"
started = 'listening on port [0-9]+'
app_start = '> [^ ]+@[0-9][^ ]* start'
```

## Sources

All of them are optional. You only need the one that gives pod creation times.

- **Prometheus with kube-state-metrics:** `kube_pod_created`, `kube_pod_status_ready`, `kube_pod_container_status_restarts_total`. Ready is only as precise as the scrape interval.
- **Loki:** the app's "I'm up" line (Spring Boot by default), first served request, graceful-shutdown lines. The pod filter is applied as `| pod="x"`, which works whether `pod` is an indexed label or structured metadata.
- **kubectl:** pod conditions and container `startedAt` (1 s resolution), the real probe and lifecycle spec, `Unhealthy` events.

## Recommendations

- **startup:** no delay, period 2 s, `failureThreshold = p99(app start) × 1.5 / period`.
- **readiness:** period 5 s, `successThreshold: 1`.
- **liveness:** same endpoint and timeout, enough failures to cover 60 s of trouble.
- **stop:** `preStop` sleep 10 s, app shutdown timeout = p99 × 1.5 (at least 10 s), grace = preStop + timeout + 5 s.

The tool estimates the Ready time with current and recommended probes using a kubelet model.

What it cannot know: how long the probe calls themselves take. A readiness check that runs a warmup request with a 1 s timeout fails silently. Measure that before lowering timeouts.

## License

Apache-2.0
