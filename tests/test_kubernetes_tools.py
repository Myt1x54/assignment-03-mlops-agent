"""S12: Kubernetes tools against fake API objects (no cluster)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

import pytest
from urllib3.exceptions import MaxRetryError

import agent.tools.kubernetes_tools as kt
from agent.config import Settings, load_policy
from agent.errors import ServiceUnavailableError
from agent.schemas import ToolStatus
from agent.tools.base import ToolContext, execute_tool

T0 = datetime.now(UTC) - timedelta(hours=2)


def pod(name: str, ready: bool, restarts: int = 0, waiting: str | None = None):
    state = NS(waiting=NS(reason=waiting) if waiting else None)
    last = NS(terminated=NS(reason="Error") if restarts else None)
    return NS(
        metadata=NS(name=name, creation_timestamp=T0),
        status=NS(
            phase="Running",
            conditions=[NS(type="Ready", status="True" if ready else "False")],
            container_statuses=[NS(restart_count=restarts, state=state, last_state=last)],
        ),
        spec=NS(node_name="node-1", containers=[NS(image="mlops-api:0.1.0")]),
    )


def deployment(desired: int, ready: int):
    container = NS(image="mlops-api:0.1.0", env_from=[NS(config_map_ref=NS(name="churn-api-config"))])
    return NS(
        metadata=NS(name="churn-api", creation_timestamp=T0),
        spec=NS(
            replicas=desired,
            template=NS(
                metadata=NS(labels={"app.kubernetes.io/version": "0.1.0"}), spec=NS(containers=[container])
            ),
        ),
        status=NS(ready_replicas=ready, available_replicas=ready, updated_replicas=desired, conditions=[]),
    )


class FakeCore:
    def __init__(self, pods, ready_endpoints: int):
        self.pods, self.ready_endpoints = pods, ready_endpoints

    def list_namespaced_pod(self, ns, **kw):
        return NS(items=self.pods)

    def list_namespaced_service(self, ns, **kw):
        port = NS(port=80, target_port="http", protocol="TCP")
        spec = NS(type="ClusterIP", cluster_ip="10.0.0.1", ports=[port], selector={"app": "churn-api"})
        return NS(items=[NS(metadata=NS(name="churn-api"), spec=spec)])

    def list_namespaced_event(self, ns, **kw):
        e = NS(
            type="Warning",
            reason="Unhealthy",
            message="Readiness probe failed: HTTP probe failed with statuscode: 503",
            count=12,
            involved_object=NS(kind="Pod", name="churn-api-x"),
            last_timestamp=T0,
            event_time=None,
            metadata=NS(creation_timestamp=T0),
        )
        return NS(items=[e])

    def read_namespaced_pod_log(self, name, ns, **kw):
        return "INFO start\nERROR model load failed: [Errno 2] No such file: '/app/model/wrong.joblib'\n"

    def read_namespaced_config_map(self, name, ns, **kw):
        return NS(data={"APP_VERSION": "0.1.0", "MODEL_PATH": "/app/model/model.joblib"})

    def connect_get_namespaced_service_proxy_with_path(self, name, ns, path, **kw):
        return NS(data=json.dumps({"model_version": "7", "data_version": "data-v2.0"}).encode())


class FakeApps:
    def __init__(self, dep):
        self.dep, self.patches = dep, []

    def list_namespaced_deployment(self, ns, **kw):
        return NS(items=[self.dep])

    def read_namespaced_deployment(self, name, ns, **kw):
        return self.dep

    def patch_namespaced_deployment(self, name, ns, body, **kw):
        self.patches.append(("restart", name, ns, body))

    def patch_namespaced_deployment_scale(self, name, ns, body, **kw):
        self.patches.append(("scale", name, ns, body))


class FakeDiscovery:
    def __init__(self, ready: int):
        self.ready = ready

    def list_namespaced_endpoint_slice(self, ns, **kw):
        eps = [NS(conditions=NS(ready=True)) for _ in range(self.ready)]
        return NS(items=[NS(endpoints=eps)])


def install(monkeypatch, pods, dep, ready_endpoints):
    apis = NS(
        core=FakeCore(pods, ready_endpoints), apps=FakeApps(dep), discovery=FakeDiscovery(ready_endpoints)
    )
    monkeypatch.setattr(kt, "get_apis", lambda ctx: apis)
    return apis


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(request_id="req-k8s", settings=Settings(_env_file=None), policy=load_policy())


def test_list_deployments(monkeypatch, ctx):
    install(monkeypatch, [], deployment(2, 2), 2)
    r = execute_tool("k8s_list_deployments", {}, ctx)
    assert r.data["deployments"][0]["image"] == "mlops-api:0.1.0"
    assert (r.data["deployments"][0]["desired"], r.data["deployments"][0]["ready"]) == (2, 2)


def test_healthy_cluster(monkeypatch, ctx):
    install(monkeypatch, [pod("a", True), pod("b", True)], deployment(2, 2), 2)
    assert execute_tool("k8s_find_unhealthy", {}, ctx).data["healthy"] is True


def test_find_unhealthy_reports_not_ready_crashloop_and_missing_endpoints(monkeypatch, ctx):
    pods = [pod("a", False), pod("b", False, restarts=5, waiting="CrashLoopBackOff")]
    install(monkeypatch, pods, deployment(2, 0), 0)
    r = execute_tool("k8s_find_unhealthy", {}, ctx)
    assert r.data["healthy"] is False
    assert {p["name"] for p in r.data["unhealthy_pods"]} == {"a", "b"}
    assert "waiting: CrashLoopBackOff" in r.data["unhealthy_pods"][1]["why"]
    assert r.data["degraded_deployments"] == [{"name": "churn-api", "desired": 2, "ready": 0, "available": 0}]
    assert r.data["services_without_ready_endpoints"] == ["churn-api"]
    assert "Readiness probe failed" in r.data["recent_warning_events"][0]["message"]


def test_pod_logs_surface_error_lines(monkeypatch, ctx):
    install(monkeypatch, [], deployment(2, 2), 2)
    r = execute_tool("k8s_get_pod_logs", {"pod": "churn-api-x", "tail_lines": 800}, ctx)
    assert r.status is ToolStatus.OK
    assert any("model load failed" in line for line in r.data["error_lines"])


def test_deployed_version_includes_live_model_version(monkeypatch, ctx):
    install(monkeypatch, [], deployment(2, 2), 2)
    r = execute_tool("k8s_get_deployed_version", {}, ctx)
    assert r.data["image_tag"] == "0.1.0"
    assert r.data["live_version"] == {"model_version": "7", "data_version": "data-v2.0"}


def test_restart_outside_allowed_namespace_is_rejected(monkeypatch, ctx):
    apis = install(monkeypatch, [], deployment(2, 2), 2)
    r = execute_tool("k8s_restart_deployment", {"namespace": "default"}, ctx)
    assert r.status is ToolStatus.REJECTED
    assert apis.apps.patches == []


def test_restart_without_confirmation_does_nothing(monkeypatch, ctx):
    apis = install(monkeypatch, [], deployment(2, 2), 2)
    r = execute_tool("k8s_restart_deployment", {}, ctx)
    assert r.status is ToolStatus.CONFIRMATION_REQUIRED
    assert "churn-api" in r.data["summary"]
    assert apis.apps.patches == []


def test_restart_with_confirmation_patches_restarted_at(monkeypatch, ctx):
    apis = install(monkeypatch, [], deployment(2, 2), 2)
    first = execute_tool("k8s_restart_deployment", {}, ctx)
    ctx.confirmation_token = first.data["action"]["confirmation_token"]
    r = execute_tool("k8s_restart_deployment", {}, ctx)
    assert r.status is ToolStatus.OK
    kind, name, ns, body = apis.apps.patches[0]
    assert (kind, name, ns) == ("restart", "churn-api", "mlops-demo")
    assert "kubectl.kubernetes.io/restartedAt" in body["spec"]["template"]["metadata"]["annotations"]


def test_scale_limits_and_token_binding(monkeypatch, ctx):
    apis = install(monkeypatch, [], deployment(2, 2), 2)
    assert execute_tool("k8s_scale_deployment", {"replicas": 9}, ctx).status is ToolStatus.REJECTED
    first = execute_tool("k8s_scale_deployment", {"replicas": 3}, ctx)
    assert first.status is ToolStatus.CONFIRMATION_REQUIRED
    ctx.confirmation_token = first.data["action"]["confirmation_token"]
    # Approval for 3 replicas does not authorise 4.
    assert (
        execute_tool("k8s_scale_deployment", {"replicas": 4}, ctx).status is ToolStatus.CONFIRMATION_REQUIRED
    )
    assert execute_tool("k8s_scale_deployment", {"replicas": 3}, ctx).status is ToolStatus.OK
    assert apis.apps.patches == [("scale", "churn-api", "mlops-demo", {"spec": {"replicas": 3}})]


def test_cluster_unreachable_is_unavailable(monkeypatch, ctx):
    class Down:
        def list_namespaced_deployment(self, ns, **kw):
            raise MaxRetryError(pool=None, url="/apis/apps/v1")

    monkeypatch.setattr(kt, "get_apis", lambda c: NS(apps=Down()))
    assert execute_tool("k8s_list_deployments", {}, ctx).status is ToolStatus.UNAVAILABLE


def test_no_kubeconfig_is_unavailable(monkeypatch, ctx):
    def no_config(c):
        raise ServiceUnavailableError("no usable kubeconfig or in-cluster config")

    monkeypatch.setattr(kt, "get_apis", no_config)
    assert execute_tool("k8s_list_pods", {}, ctx).status is ToolStatus.UNAVAILABLE
