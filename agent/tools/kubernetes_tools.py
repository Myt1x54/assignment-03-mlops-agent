"""Kubernetes tools (AGENT.md §3.4) — official Python client (DECISION D12, D13).

* Every tool is scoped to an allow-listed namespace (``K8S_ALLOWED_NAMESPACES``, default ``mlops-demo``);
  anything else is ``rejected`` before any API call.
* Write tools (restart, scale) check namespace + limits first, then require an orchestrator-held human
  confirmation token bound to the exact deployment/namespace/replicas.
* Every API call carries ``_request_timeout``; connection problems map to ``unavailable``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import Field

from agent.errors import InvalidRequestError, PermissionDeniedError, ServiceUnavailableError
from agent.safety import check_namespace, check_replicas, clamp_tail, require_confirmation
from agent.schemas import Permission, ToolInput
from agent.tools.base import ToolContext, tool

SYSTEM = "kubernetes"
_NAME = r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$"
DEFAULT_DEPLOYMENT = "churn-api"
SCAN_LINES = 2000  # upper bound of log lines fetched to search for errors


# --- client helpers -----------------------------------------------------------------------------------------


class K8s:
    """Thin holder of API objects so tests can substitute fakes via ``get_apis``."""

    def __init__(self, ctx: ToolContext) -> None:
        from kubernetes.config.config_exception import ConfigException

        from kubernetes import client, config

        try:
            config.load_kube_config(context=ctx.settings.k8s_context)
        except (ConfigException, FileNotFoundError):
            try:
                config.load_incluster_config()
            except ConfigException as exc:
                raise ServiceUnavailableError("no usable kubeconfig or in-cluster config") from exc
        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()
        self.discovery = client.DiscoveryV1Api()


def get_apis(ctx: ToolContext) -> Any:
    return K8s(ctx)


def kcall(ctx: ToolContext, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Invoke a client method with a timeout and typed error mapping."""
    from kubernetes.client.exceptions import ApiException
    from urllib3.exceptions import HTTPError as Urllib3Error

    try:
        return fn(*args, _request_timeout=min(ctx.settings.agent_tool_timeout_seconds, 15.0), **kwargs)
    except ApiException as exc:
        if exc.status == 404:
            raise InvalidRequestError(f"not found: {exc.reason}") from exc
        if exc.status in (401, 403):
            raise PermissionDeniedError(
                f"Kubernetes API denied the request ({exc.status} {exc.reason})"
            ) from exc
        if exc.status and exc.status >= 500:
            raise ServiceUnavailableError(f"Kubernetes API error {exc.status}") from exc
        raise InvalidRequestError(f"Kubernetes API error {exc.status}: {exc.reason}") from exc
    except Urllib3Error as exc:
        raise ServiceUnavailableError(
            f"Kubernetes API unreachable (context {ctx.settings.k8s_context})"
        ) from exc


def _age(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    seconds = int((datetime.now(UTC) - ts).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _pod_row(pod: Any) -> dict[str, Any]:
    statuses = pod.status.container_statuses or []
    waiting = [
        cs.state.waiting.reason
        for cs in statuses
        if cs.state and cs.state.waiting and cs.state.waiting.reason
    ]
    last_terminated = [
        cs.last_state.terminated.reason
        for cs in statuses
        if cs.last_state and cs.last_state.terminated and cs.last_state.terminated.reason
    ]
    conditions = {c.type: c.status for c in pod.status.conditions or []}
    return {
        "name": pod.metadata.name,
        "phase": pod.status.phase,
        "ready": conditions.get("Ready") == "True",
        "restarts": sum(cs.restart_count or 0 for cs in statuses),
        "waiting_reasons": waiting,
        "last_termination_reasons": last_terminated,
        "node": pod.spec.node_name,
        "age": _age(pod.metadata.creation_timestamp),
        "image": pod.spec.containers[0].image if pod.spec.containers else None,
    }


def _deployment_row(d: Any) -> dict[str, Any]:
    c = d.spec.template.spec.containers[0]
    labels = d.spec.template.metadata.labels or {}
    return {
        "name": d.metadata.name,
        "desired": d.spec.replicas,
        "ready": d.status.ready_replicas or 0,
        "available": d.status.available_replicas or 0,
        "updated": d.status.updated_replicas or 0,
        "image": c.image,
        "version_label": labels.get("app.kubernetes.io/version"),
        "age": _age(d.metadata.creation_timestamp),
        "conditions": {cond.type: cond.status for cond in d.status.conditions or []},
    }


# --- inputs -------------------------------------------------------------------------------------------------


class NamespaceIn(ToolInput):
    namespace: str | None = Field(default=None, pattern=_NAME, description="Defaults to mlops-demo")


class PodsIn(NamespaceIn):
    label_selector: str | None = Field(default=None, max_length=200, pattern=r"^[A-Za-z0-9_.\-/=,!() ]+$")


class PodLogsIn(NamespaceIn):
    pod: str = Field(pattern=_NAME)
    container: str | None = Field(default=None, pattern=_NAME)
    tail_lines: int = Field(default=50, ge=1, le=1000)
    previous: bool = Field(default=False, description="Logs of the previous (crashed) container instance")


class EventsIn(NamespaceIn):
    object_name: str | None = Field(default=None, pattern=_NAME, description="Only events for this object")
    warnings_only: bool = False
    limit: int = Field(default=20, ge=1, le=100)


class UnhealthyIn(NamespaceIn):
    restart_threshold: int = Field(default=3, ge=1, le=100)


class DeploymentIn(NamespaceIn):
    deployment: str = Field(default=DEFAULT_DEPLOYMENT, pattern=_NAME)


class ScaleIn(DeploymentIn):
    replicas: int = Field(ge=0, le=50, description="Target replica count (policy allows 1..5)")


def _ns(args: NamespaceIn, ctx: ToolContext) -> str:
    namespace = args.namespace or ctx.settings.k8s_namespace
    check_namespace(namespace, ctx.settings.allowed_namespaces)
    return namespace


# --- read tools ---------------------------------------------------------------------------------------------


@tool(
    name="k8s_list_deployments",
    system=SYSTEM,
    description="Deployments with desired/ready/available replicas, image, version label and conditions.",
    input_model=NamespaceIn,
)
def k8s_list_deployments(args: NamespaceIn, ctx: ToolContext) -> dict[str, Any]:
    ns = _ns(args, ctx)
    items = kcall(ctx, get_apis(ctx).apps.list_namespaced_deployment, ns).items
    return {"namespace": ns, "deployments": [_deployment_row(d) for d in items]}


@tool(
    name="k8s_list_pods",
    system=SYSTEM,
    description="Pods with phase, readiness, restart count, waiting/termination reasons, node and age.",
    input_model=PodsIn,
)
def k8s_list_pods(args: PodsIn, ctx: ToolContext) -> dict[str, Any]:
    ns = _ns(args, ctx)
    kwargs = {"label_selector": args.label_selector} if args.label_selector else {}
    items = kcall(ctx, get_apis(ctx).core.list_namespaced_pod, ns, **kwargs).items
    pods = [_pod_row(p) for p in items]
    return {"namespace": ns, "count": len(pods), "ready": sum(p["ready"] for p in pods), "pods": pods}


@tool(
    name="k8s_get_pod_logs",
    system=SYSTEM,
    description="Log tail of a pod (optionally the previous crashed container); secrets are redacted.",
    input_model=PodLogsIn,
)
def k8s_get_pod_logs(args: PodLogsIn, ctx: ToolContext) -> dict[str, Any]:
    ns = _ns(args, ctx)
    tail = clamp_tail(args.tail_lines, ctx.policy.safety.log_tail_max_lines)
    # Scan a bounded larger window for error lines (startup errors scroll out of a short tail behind probe
    # access logs), but return only the requested tail to the LLM.
    kwargs: dict[str, Any] = {"tail_lines": SCAN_LINES, "previous": args.previous}
    if args.container:
        kwargs["container"] = args.container
    text = kcall(ctx, get_apis(ctx).core.read_namespaced_pod_log, args.pod, ns, **kwargs) or ""
    lines = text.splitlines()
    errors = [
        ln for ln in lines if any(w in ln.lower() for w in ("error", "failed", "traceback", "exception"))
    ]
    return {
        "namespace": ns,
        "pod": args.pod,
        "previous": args.previous,
        "lines_scanned": len(lines),
        "error_lines": list(dict.fromkeys(errors))[-15:],
        "tail": lines[-tail:],
    }


@tool(
    name="k8s_get_events",
    system=SYSTEM,
    description="Recent namespace events (newest first), optionally only warnings or only for one object.",
    input_model=EventsIn,
)
def k8s_get_events(args: EventsIn, ctx: ToolContext) -> dict[str, Any]:
    ns = _ns(args, ctx)
    kwargs: dict[str, Any] = {}
    selectors = []
    if args.object_name:
        selectors.append(f"involvedObject.name={args.object_name}")
    if args.warnings_only:
        selectors.append("type=Warning")
    if selectors:
        kwargs["field_selector"] = ",".join(selectors)
    items = kcall(ctx, get_apis(ctx).core.list_namespaced_event, ns, **kwargs).items

    def when(e: Any) -> datetime:
        return (
            e.last_timestamp
            or e.event_time
            or e.metadata.creation_timestamp
            or datetime.min.replace(tzinfo=UTC)
        )

    events = [
        {
            "type": e.type,
            "reason": e.reason,
            "object": f"{e.involved_object.kind}/{e.involved_object.name}",
            "message": (e.message or "")[:300],
            "count": e.count,
            "last_seen": _age(when(e)),
        }
        for e in sorted(items, key=when, reverse=True)[: args.limit]
    ]
    return {"namespace": ns, "count": len(events), "events": events}


def _ready_endpoints(ctx: ToolContext, apis: Any, ns: str, service: str) -> int:
    slices = kcall(
        ctx,
        apis.discovery.list_namespaced_endpoint_slice,
        ns,
        label_selector=f"kubernetes.io/service-name={service}",
    ).items
    return sum(1 for s in slices for ep in s.endpoints or [] if ep.conditions is None or ep.conditions.ready)


@tool(
    name="k8s_get_services",
    system=SYSTEM,
    description="Services with type, cluster IP, ports, selector and number of READY endpoints.",
    input_model=NamespaceIn,
)
def k8s_get_services(args: NamespaceIn, ctx: ToolContext) -> dict[str, Any]:
    ns = _ns(args, ctx)
    apis = get_apis(ctx)
    services = []
    for s in kcall(ctx, apis.core.list_namespaced_service, ns).items:
        ports = [f"{p.port}->{p.target_port}/{p.protocol}" for p in s.spec.ports or []]
        services.append(
            {
                "name": s.metadata.name,
                "type": s.spec.type,
                "cluster_ip": s.spec.cluster_ip,
                "ports": ports,
                "selector": s.spec.selector or {},
                "ready_endpoints": _ready_endpoints(ctx, apis, ns, s.metadata.name),
            }
        )
    return {"namespace": ns, "services": services}


@tool(
    name="k8s_find_unhealthy",
    system=SYSTEM,
    description=(
        "Health sweep: pods not Ready / waiting (CrashLoopBackOff, ImagePullBackOff) / high restarts, "
        "deployments below desired replicas, services without ready endpoints, recent warning events."
    ),
    input_model=UnhealthyIn,
)
def k8s_find_unhealthy(args: UnhealthyIn, ctx: ToolContext) -> dict[str, Any]:
    ns = _ns(args, ctx)
    apis = get_apis(ctx)
    pods = [_pod_row(p) for p in kcall(ctx, apis.core.list_namespaced_pod, ns).items]
    bad_pods = [
        {**p, "why": _why(p, args.restart_threshold)}
        for p in pods
        if not p["ready"] or p["waiting_reasons"] or p["restarts"] >= args.restart_threshold
    ]
    deployments = [_deployment_row(d) for d in kcall(ctx, apis.apps.list_namespaced_deployment, ns).items]
    degraded = [d for d in deployments if (d["ready"] or 0) < (d["desired"] or 0)]
    no_endpoints = [
        s.metadata.name
        for s in kcall(ctx, apis.core.list_namespaced_service, ns).items
        if s.spec.selector and _ready_endpoints(ctx, apis, ns, s.metadata.name) == 0
    ]
    # A rollout whose new pods never become Ready: with maxUnavailable=0 the old pods keep serving, so
    # ready == desired, but updated < desired reveals the stuck rollout.
    stalled = [
        {"name": d["name"], "desired": d["desired"], "updated": d["updated"], "ready": d["ready"]}
        for d in deployments
        if (d["updated"] or 0) < (d["desired"] or 0) and any(not p["ready"] for p in pods)
    ]
    warnings = kcall(ctx, apis.core.list_namespaced_event, ns, field_selector="type=Warning").items

    def seen(e: Any) -> datetime:
        return (
            e.last_timestamp
            or e.event_time
            or e.metadata.creation_timestamp
            or datetime.min.replace(tzinfo=UTC)
        )

    recent_warnings = [
        {
            "reason": e.reason,
            "object": e.involved_object.name,
            "message": (e.message or "")[:200],
            "count": e.count,
            "last_seen": _age(seen(e)),
        }
        for e in sorted(warnings, key=seen, reverse=True)[:10]
    ]
    return {
        "namespace": ns,
        "healthy": not (bad_pods or degraded or no_endpoints or stalled),
        "pods_total": len(pods),
        "unhealthy_pods": bad_pods,
        "degraded_deployments": [
            {k: d[k] for k in ("name", "desired", "ready", "available")} for d in degraded
        ],
        "stalled_rollouts": stalled,
        "services_without_ready_endpoints": no_endpoints,
        "recent_warning_events": recent_warnings,
    }


def _why(pod: dict[str, Any], threshold: int) -> list[str]:
    reasons = []
    if not pod["ready"]:
        reasons.append("not Ready (readiness probe failing or container not running)")
    reasons += [f"waiting: {r}" for r in pod["waiting_reasons"]]
    if pod["restarts"] >= threshold:
        reasons.append(
            f"{pod['restarts']} restarts (last termination: {pod['last_termination_reasons'] or '?'})"
        )
    return reasons


@tool(
    name="k8s_get_deployed_version",
    system=SYSTEM,
    description=(
        "Which app/model version is deployed: image tag, version label, ConfigMap values, and the live "
        "/version response of the service (model version, MLflow run id, dataset version)."
    ),
    input_model=DeploymentIn,
)
def k8s_get_deployed_version(args: DeploymentIn, ctx: ToolContext) -> dict[str, Any]:
    ns = _ns(args, ctx)
    apis = get_apis(ctx)
    d = kcall(ctx, apis.apps.read_namespaced_deployment, args.deployment, ns)
    row = _deployment_row(d)
    config: dict[str, str] = {}
    for env_from in d.spec.template.spec.containers[0].env_from or []:
        if env_from.config_map_ref:
            cm = kcall(ctx, apis.core.read_namespaced_config_map, env_from.config_map_ref.name, ns)
            config.update(cm.data or {})
    live: dict[str, Any] | None
    live_error = None
    try:
        resp = kcall(
            ctx,
            apis.core.connect_get_namespaced_service_proxy_with_path,
            f"{args.deployment}:80",
            ns,
            "version",
            _preload_content=False,  # raw body: the client would otherwise str() the parsed JSON
        )
        raw = resp.data if hasattr(resp, "data") else resp
        live = json.loads(raw) if isinstance(raw, str | bytes) else raw
    except (InvalidRequestError, ServiceUnavailableError, PermissionDeniedError, ValueError) as exc:
        live, live_error = None, f"service /version not reachable: {exc}"
    return {
        "namespace": ns,
        "deployment": args.deployment,
        "image": row["image"],
        "image_tag": row["image"].rsplit(":", 1)[-1] if ":" in row["image"] else None,
        "version_label": row["version_label"],
        "configmap": {
            k: config[k] for k in ("APP_VERSION", "MODEL_PATH", "PREDICTION_THRESHOLD") if k in config
        },
        "replicas": {"desired": row["desired"], "ready": row["ready"]},
        "live_version": live,
        "live_version_error": live_error,
    }


# --- write tools (confirmation required) --------------------------------------------------------------------


@tool(
    name="k8s_restart_deployment",
    system=SYSTEM,
    description=(
        "Rollout-restart a deployment (namespace mlops-demo only). Use only when the user explicitly asks; "
        "requires human confirmation."
    ),
    input_model=DeploymentIn,
    permission=Permission.WRITE_CONFIRM,
)
def k8s_restart_deployment(args: DeploymentIn, ctx: ToolContext) -> dict[str, Any]:
    ns = _ns(args, ctx)
    require_confirmation(
        tool="k8s_restart_deployment",
        args={"deployment": args.deployment, "namespace": ns},
        token=ctx.confirmation_token,
        ttl_seconds=ctx.policy.safety.confirmation_ttl_seconds,
        summary=f"Rollout restart of deployment '{args.deployment}' in namespace '{ns}'",
    )
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    body = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": stamp}}}}}
    kcall(ctx, get_apis(ctx).apps.patch_namespaced_deployment, args.deployment, ns, body)
    return {
        "restarted": True,
        "deployment": args.deployment,
        "namespace": ns,
        "restartedAt": stamp,
        "note": "rollout initiated, not yet complete; verify later with k8s_find_unhealthy",
    }


@tool(
    name="k8s_scale_deployment",
    system=SYSTEM,
    description=(
        "Scale a deployment to N replicas (policy range 1..5, namespace mlops-demo only). Use only when the "
        "user explicitly asks; requires human confirmation."
    ),
    input_model=ScaleIn,
    permission=Permission.WRITE_CONFIRM,
)
def k8s_scale_deployment(args: ScaleIn, ctx: ToolContext) -> dict[str, Any]:
    ns = _ns(args, ctx)
    limits = ctx.policy.safety
    check_replicas(args.replicas, limits.scale_min_replicas, limits.scale_max_replicas)
    apis = get_apis(ctx)
    current = kcall(ctx, apis.apps.read_namespaced_deployment, args.deployment, ns).spec.replicas
    require_confirmation(
        tool="k8s_scale_deployment",
        args={"deployment": args.deployment, "namespace": ns, "replicas": args.replicas},
        token=ctx.confirmation_token,
        ttl_seconds=limits.confirmation_ttl_seconds,
        summary=f"Scale deployment '{args.deployment}' in '{ns}' from {current} to {args.replicas} replicas",
    )
    kcall(
        ctx,
        apis.apps.patch_namespaced_deployment_scale,
        args.deployment,
        ns,
        {"spec": {"replicas": args.replicas}},
    )
    return {
        "scaled": True,
        "deployment": args.deployment,
        "namespace": ns,
        "from": current,
        "to": args.replicas,
        "note": "scale requested; new pods may still be starting - verify with k8s_list_deployments",
    }
