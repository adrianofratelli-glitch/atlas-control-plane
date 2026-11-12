"""Which Atlas cluster does MONGODB_URI point to?

Atlas SRV hosts look like ``<cluster-label>.<project-hash>.mongodb.net``. The
``<project-hash>`` is shared by EVERY cluster of the same project, so comparing
only that label would treat all clusters of a project as the same one, and an
explain/index/data tool aimed at cluster B would silently run on cluster A.

Identity is therefore decided by host names: the hosts in MONGODB_URI must
intersect the hosts Atlas publishes for the cluster (``connectionStrings``:
standard, SRV, private and private-endpoint variants). Pure functions, no I/O.
"""
from __future__ import annotations

from urllib.parse import urlsplit

_SCHEMES = ("mongodb://", "mongodb+srv://")


def _hosts_of(connection_string: str) -> set[str]:
    value = str(connection_string or "").strip()
    if not value.lower().startswith(_SCHEMES):
        return set()
    try:
        netloc = urlsplit(value).netloc
    except ValueError:
        return set()
    hostlist = netloc.rsplit("@", 1)[-1]
    hosts = set()
    for part in hostlist.split(","):
        part = part.strip().lower()
        if part.startswith("["):  # IPv6 literal: keep as-is, without the port
            host = part.split("]", 1)[0] + "]"
        else:
            host = part.split(":", 1)[0]
        if host:
            hosts.add(host.rstrip("."))
    return hosts


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def uri_hosts(uri: str) -> frozenset[str]:
    """Host names (lower-case, no port) of a MongoDB connection string."""
    return frozenset(_hosts_of(uri))


def cluster_hosts(cluster: dict) -> frozenset[str]:
    """Every host name Atlas publishes for a cluster (Admin API v2 cluster document)."""
    cluster = cluster or {}
    candidates = _strings(cluster.get("connectionStrings") or {})
    candidates += [cluster.get("srvAddress") or "", cluster.get("mongoURI") or ""]
    hosts: set[str] = set()
    for candidate in candidates:
        hosts |= _hosts_of(candidate)
    return frozenset(hosts)


def uri_targets_cluster(uri: str, cluster: dict) -> bool:
    """True only when MONGODB_URI shares at least one host with this exact cluster."""
    mine = uri_hosts(uri)
    return bool(mine) and bool(mine & cluster_hosts(cluster))
