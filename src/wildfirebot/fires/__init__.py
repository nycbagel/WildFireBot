"""Hotspot quality checks and FRP-weighted clusters."""

from wildfirebot.fires.cluster import Cluster, cluster_hotspots
from wildfirebot.fires.qc import filter_detections

__all__ = ["Cluster", "cluster_hotspots", "filter_detections"]
