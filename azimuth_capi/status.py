import json
import logging

from .models.v1alpha1 import (
    ClusterPhase,
    NetworkingPhase,
    ControlPlanePhase,
    NodePhase,
    AddonPhase,
    NodeRole,
    NodeStatus,
    AddonStatus,
    ServiceStatus
)


logger = logging.getLogger(__name__)


def _any_node_has_phase(cluster, *phases):
    """
    Returns true if any node has one of the given phases.
    """
    return any(node.phase in phases for node in cluster.status.nodes.values())


def _multiple_kubelet_versions(cluster, role):
    """
    Returns true if nodes with the given role have different kubelet versions,
    false otherwise.
    """
    versions = set(
        node.kubelet_version
        for node in cluster.status.nodes.values()
        if node.role is role
    )
    return len(versions) > 1


def _any_addon_has_phase(cluster, *phases):
    """
    Returns true if any addon has one of the given phases.
    """
    return any(addon.phase in phases for addon in cluster.status.addons.values())


def _reconcile_cluster_phase(cluster):
    """
    Sets the overall cluster phase based on the component phases.
    """
    if cluster.status.networking_phase in {
        NetworkingPhase.PENDING,
        NetworkingPhase.PROVISIONING
    }:
        cluster.status.phase = ClusterPhase.RECONCILING
    elif cluster.status.networking_phase is NetworkingPhase.DELETING:
        cluster.status.phase = ClusterPhase.DELETING
    elif cluster.status.networking_phase is NetworkingPhase.FAILED:
        cluster.status.phase = ClusterPhase.FAILED
    elif cluster.status.networking_phase is NetworkingPhase.UNKNOWN:
        cluster.status.phase = ClusterPhase.UNKNOWN
    # The networking phase is Provisioned
    elif cluster.status.control_plane_phase in {
        ControlPlanePhase.PENDING,
        ControlPlanePhase.SCALING_UP,
        ControlPlanePhase.SCALING_DOWN
    }:
        # If the control plane is scaling but there are control plane nodes with
        # different versions, that is still part of an upgrade
        if _multiple_kubelet_versions(cluster, NodeRole.CONTROL_PLANE):
            cluster.status.phase = ClusterPhase.UPGRADING
        else:
            cluster.status.phase = ClusterPhase.RECONCILING
    elif cluster.status.control_plane_phase is ControlPlanePhase.UPGRADING:
        cluster.status.phase = ClusterPhase.UPGRADING
    elif cluster.status.control_plane_phase is ControlPlanePhase.DELETING:
        cluster.status.phase = ClusterPhase.DELETING
    elif cluster.status.control_plane_phase is ControlPlanePhase.FAILED:
        cluster.status.phase = ClusterPhase.FAILED
    elif cluster.status.control_plane_phase is ControlPlanePhase.UNKNOWN:
        cluster.status.phase = ClusterPhase.UNKNOWN
    # The control plane phase is Ready or Unhealthy
    # If there are workers with different versions, assume an upgrade is in progress
    elif _multiple_kubelet_versions(cluster, NodeRole.WORKER):
        cluster.status.phase = ClusterPhase.UPGRADING
    elif _any_node_has_phase(
        cluster,
        NodePhase.PENDING,
        NodePhase.PROVISIONING,
        NodePhase.DELETING,
        NodePhase.DELETED
    ):
        cluster.status.phase = ClusterPhase.RECONCILING
    # All nodes are either Ready, Unhealthy, Failed or Unknown
    elif _any_addon_has_phase(
        cluster,
        AddonPhase.PENDING,
        AddonPhase.RECONCILING,
        AddonPhase.UNINSTALLING
    ):
        cluster.status.phase = ClusterPhase.RECONCILING
    # All addons are either Ready, Failed or Unknown
    # Now we know that there is no reconciliation happening, consider cluster health
    elif (
        cluster.status.control_plane_phase is ControlPlanePhase.UNHEALTHY or
        _any_node_has_phase(
            cluster,
            NodePhase.UNHEALTHY,
            NodePhase.FAILED,
            NodePhase.UNKNOWN
        ) or
        _any_addon_has_phase(
            cluster,
            AddonPhase.UNHEALTHY,
            AddonPhase.FAILED,
            AddonPhase.UNKNOWN
        )
    ):
        cluster.status.phase = ClusterPhase.UNHEALTHY
    else:
        cluster.status.phase = ClusterPhase.READY


def cluster_updated(cluster, obj):
    """
    Updates the status when a CAPI cluster is updated.
    """
    # Just set the networking phase
    phase = obj.get("status", {}).get("phase", "Unknown")
    cluster.status.networking_phase = NetworkingPhase(phase)


def cluster_deleted(cluster, obj):
    """
    Updates the status when a CAPI cluster is deleted.
    """
    cluster.status.networking_phase = NetworkingPhase.UNKNOWN


def cluster_absent(cluster):
    """
    Called when the CAPI cluster is missing on resume.
    """
    cluster.status.networking_phase = NetworkingPhase.UNKNOWN


def control_plane_updated(cluster, obj):
    """
    Updates the status when a CAPI control plane is updated.
    """
    status = obj.get("status", {})
    # The control plane object has no phase property
    # Instead, we must derive it from the conditions
    conditions = status.get("conditions", [])
    ready = next((c for c in conditions if c["type"] == "Ready"), None)
    components_healthy = next(
        (c for c in conditions if c["type"] == "ControlPlaneComponentsHealthy"),
        None
    )
    if ready:
        if ready["status"] == "True":
            if components_healthy and components_healthy["status"] == "True":
                next_phase = ControlPlanePhase.READY
            else:
                next_phase = ControlPlanePhase.UNHEALTHY
        elif ready["reason"] == "ScalingUp":
            next_phase = ControlPlanePhase.SCALING_UP
        elif ready["reason"] == "ScalingDown":
            next_phase = ControlPlanePhase.SCALING_DOWN
        elif ready["reason"].startswith("Waiting"):
            next_phase = ControlPlanePhase.SCALING_UP
        elif ready["reason"] == "RollingUpdateInProgress":
            next_phase = ControlPlanePhase.UPGRADING
        elif ready["reason"] == "Deleting":
            next_phase = ControlPlanePhase.DELETING
        else:
            logger.warn("UNKNOWN CONTROL PLANE REASON: %s", ready["reason"])
            next_phase = ControlPlanePhase.UNHEALTHY
    else:
        next_phase = ControlPlanePhase.PENDING
    cluster.status.control_plane_phase = next_phase
    # The Kubernetes version in the control plane object has a leading v that we don't want
    # The accurate version is in the status, but we use the spec if that is not set yet
    cluster.status.kubernetes_version = status.get("version", obj["spec"]["version"]).lstrip("v")


def control_plane_deleted(cluster, obj):
    """
    Updates the status when a CAPI control plane is deleted.
    """
    cluster.status.control_plane_phase = ControlPlanePhase.UNKNOWN
    # Also reset the Kubernetes version of the cluster, as it can no longer be determined
    cluster.status.kubernetes_version = None


def control_plane_absent(cluster):
    """
    Called when the control plane is missing on resume.
    """
    cluster.status.control_plane_phase = ControlPlanePhase.UNKNOWN
    # Also reset the Kubernetes version of the cluster, as it can no longer be determined
    cluster.status.kubernetes_version = None


def machine_updated(cluster, obj, infra_machine):
    """
    Updates the status when a CAPI machine is updated.
    """
    labels = obj["metadata"]["labels"]
    status = obj.get("status", {})
    phase = status.get("phase", "Unknown")
    # We want to break the running phase down depending on node health
    # We also want to remove the Provisioned phase as a transition between Provisioning
    # and Ready, and just leave those nodes in the Provisioning phase
    # All other CAPI machine phases correspond to the node phases
    if phase == "Running":
        conditions = status.get("conditions", [])
        healthy = next((c for c in conditions if c["type"] == "NodeHealthy"), None)
        if healthy and healthy["status"] == "True":
            node_phase = NodePhase.READY
        else:
            node_phase = NodePhase.UNHEALTHY
    elif phase == "Provisioned":
        node_phase = NodePhase.PROVISIONING
    else:
        node_phase = NodePhase(phase)
    # Replace the node object in the node set
    cluster.status.nodes[obj["metadata"]["name"]] = NodeStatus(
        # The node role should be in the labels
        role = NodeRole(labels["capi.stackhpc.com/component"]),
        phase = node_phase,
        # This assumes an OpenStackMachine for now
        size = infra_machine.spec.flavor,
        ip = next(
            (
                a["address"]
                for a in status.get("addresses", [])
                if a["type"] == "InternalIP"
            ),
            None
        ),
        ips = [
            a["address"]
            for a in status.get("addresses", [])
            if a["type"] == "InternalIP"
        ],
        # Take the version from the spec, which should always be set
        kubelet_version = obj["spec"]["version"].lstrip("v"),
        # The node group will be in a label if applicable
        node_group = labels.get("capi.stackhpc.com/node-group"),
        # Use the timestamp from the metadata for the created time
        created = obj["metadata"]["creationTimestamp"]
    )


def machine_deleted(cluster, obj):
    """
    Updates the status when a CAPI machine is deleted.
    """
    # Just remove the node from the node set
    cluster.status.nodes.pop(obj["metadata"]["name"], None)


def remove_unknown_nodes(cluster, machines):
    """
    Given the current set of machines, remove any unknown nodes from the status.
    """
    current = set(m.metadata.name for m in machines)
    known = set(cluster.status.nodes.keys())
    for name in known - current:
        cluster.status.nodes.pop(name)


def kubeconfig_secret_updated(cluster, obj):
    """
    Updates the status when a kubeconfig secret is updated.
    """
    cluster.status.kubeconfig_secret_name = obj["metadata"]["name"]


def addon_updated(cluster, obj):
    """
    Updates the status when an addon is updated.
    """
    component = obj["metadata"]["labels"]["capi.stackhpc.com/component"]
    status = obj.get("status", {})
    cluster.status.addons[component] = AddonStatus(
        phase = status.get("phase", AddonPhase.UNKNOWN.value),
    )
    # Get the provided by reference for this addon
    provided_by_ref = f"{obj['kind'].lower()}/{obj['metadata']['name']}"
    # Extract the services for the addon
    annotations = obj.get("metadata", {}).get("annotations", {})
    services = json.loads(annotations.get("azimuth.stackhpc.com/services", "{}"))
    # Get the names of the services that are owner by this addon
    known_services = {
        k
        for k, v in cluster.status.services.items()
        if v.provided_by == provided_by_ref
    }
    # Update the current services
    for name, service in services.items():
        cluster.status.services[name] = ServiceStatus(
            provided_by = provided_by_ref,
            **service
        )
    # Remove any known services that are not present
    for name in known_services.difference(services.keys()):
        cluster.status.services.pop(name, None)


def addon_deleted(cluster, obj):
    """
    Updates the status when an addon is deleted.
    """
    component = obj["metadata"]["labels"]["capi.stackhpc.com/component"]
    cluster.status.addons.pop(component, None)
    # When an addon is deleted, remove all the services that are provided by it
    provided_by_ref = f"{obj['kind'].lower()}/{obj['metadata']['name']}"
    cluster.status.services = {
        k: v
        for k, v in cluster.status.services.items()
        if v.provided_by == provided_by_ref
    }


def remove_unknown_addons(cluster, addons):
    """
    Given the current set of addons, remove any unknown addons from the status.
    """
    current = set(a["metadata"]["labels"]["capi.stackhpc.com/component"] for a in addons)
    known = set(cluster.status.addons.keys())
    for component in known - current:
        cluster.status.addons.pop(component)
    # Also remove services that don't belong to the known addons
    refs = { f"{a['kind'].lower()}/{a['metadata']['name']}" for a in addons }
    cluster.status.services = {
        k: v
        for k, v in cluster.status.services.items()
        if v.provided_by in refs
    }


def finalise(cluster):
    """
    Apply final derived elements to the status.
    """
    _reconcile_cluster_phase(cluster)
    cluster.status.node_count = len(cluster.status.nodes)
    cluster.status.addon_count = len(cluster.status.addons)
