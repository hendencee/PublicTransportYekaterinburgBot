from __future__ import annotations

import argparse
import heapq
import math
import re
from collections import defaultdict
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from typing import Iterable

from transport_portal import (
    Arrival,
    DEFAULT_ROUTE_DETAILS_WORKERS,
    RouteDetails,
    RouteStation,
    Station,
    TransportPortalClient,
    TransportPortalError,
    normalize_text,
    sort_station_matches_for_display,
)


AVERAGE_SPEED_MPS_BY_ROUTE_TYPE = {
    1: 5.2,
    2: 4.7,
    4: 5.8,
}
WALKING_SPEED_MPS = 1.35
TRANSFER_WALK_RADIUS_METERS = 300
ACCESS_WALK_RADIUS_METERS = 700
BOARDING_PENALTY_SECONDS = 60
WALKING_PENALTY_MULTIPLIER = 1.1
MIN_TRANSIT_SEGMENT_SECONDS = 45
DWELL_SECONDS_PER_STOP = 18
MAX_ACCESS_STATIONS = 14
MAX_REALTIME_FORECAST_STATIONS = 36
REALTIME_FORECAST_HORIZON_SECONDS = 45 * 60
NO_MATCHING_FORECAST_WAIT_SECONDS = 10 * 60
SHORT_FEEDER_MAX_STOPS = 3
SHORT_FEEDER_MAX_IN_VEHICLE_SECONDS = 8 * 60
FEWER_TRANSFERS_PREFERENCE_SECONDS = 90
SELECTED_STATION_ACCESS_RADIUS_METERS = 120
SOURCE_BOARDING_SAME_ROUTE_PREFERENCE_SECONDS = 6 * 60
COORDINATE_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[,; ]\s*(-?\d+(?:\.\d+)?)\s*$")
ROUTE_TYPE_ICONS = {
    1: "🚌",
    2: "🚎",
    4: "🚊",
}
ROUTE_NUMBER_RE = re.compile(r"[^0-9A-Za-zА-Яа-я]+")


@dataclass(slots=True)
class ResolvedLocation:
    label: str
    lat: float
    lng: float
    station: Station | None


@dataclass(slots=True)
class PlannerEdge:
    kind: str
    from_station_id: int | None
    to_station_id: int | None
    duration_seconds: float
    distance_meters: float
    route_id: int | None = None
    route_number: str | None = None
    route_type_id: int | None = None
    subroute_id: int | None = None
    subroute_name: str | None = None
    stop_count: int = 0
    in_vehicle_seconds: float = 0.0
    wait_seconds: float = 0.0
    wait_is_realtime: bool = False


@dataclass(slots=True)
class RoutePlan:
    source: ResolvedLocation
    destination: ResolvedLocation
    total_seconds: float
    edges: list[PlannerEdge]

    @property
    def transfers(self) -> int:
        ride_count = sum(1 for edge in self.edges if edge.kind == "ride")
        return max(0, ride_count - 1)


class StaticRoutePlanner:
    def __init__(
        self,
        client: TransportPortalClient,
        transfer_walk_radius_meters: int = TRANSFER_WALK_RADIUS_METERS,
        access_walk_radius_meters: int = ACCESS_WALK_RADIUS_METERS,
    ) -> None:
        self.client = client
        self.transfer_walk_radius_meters = transfer_walk_radius_meters
        self.access_walk_radius_meters = access_walk_radius_meters
        self.stations = client.get_all_stations(include_archived=False)
        self.station_by_id = {station.id: station for station in self.stations}
        self.graph: dict[int, list[PlannerEdge]] | None = None
        self._forecast_cache: dict[int, list[Arrival]] = {}
        self._forecast_cache_failures: set[int] = set()
        self._forecast_fetch_count = 0

    def ensure_graph(
        self,
        force_refresh_routes: bool = False,
        max_workers: int = DEFAULT_ROUTE_DETAILS_WORKERS,
    ) -> None:
        if self.graph is not None and not force_refresh_routes:
            return

        route_catalog = self.client.get_route_catalog(
            force_refresh=force_refresh_routes,
            max_workers=max_workers,
        )
        graph: dict[int, list[PlannerEdge]] = defaultdict(list)

        self._add_transit_edges(graph, route_catalog)
        self._add_walking_edges(graph, self.stations)

        self.graph = dict(graph)

    def resolve_location(self, raw_value: str) -> ResolvedLocation:
        resolved, matches = self.resolve_location_candidates(raw_value)
        if resolved is not None:
            return resolved

        if matches:
            options = "\n".join(f"- {station_header(station)}" for station in matches[:8])
            raise TransportPortalError(
                "Запрос неоднозначен. Уточни одну из остановок:\n"
                f"{options}"
            )

        raise TransportPortalError(f"Не удалось найти точку или остановку: {raw_value}")

    def resolve_location_candidates(self, raw_value: str) -> tuple[ResolvedLocation | None, list[Station]]:
        coordinate_match = COORDINATE_RE.match(raw_value)
        if coordinate_match:
            lat = float(coordinate_match.group(1))
            lng = float(coordinate_match.group(2))
            return (
                ResolvedLocation(
                    label=f"{lat:.6f}, {lng:.6f}",
                    lat=lat,
                    lng=lng,
                    station=None,
                ),
                [],
            )

        station, matches = self.client.choose_station(raw_value, include_archived=False)
        if station is not None:
            return (
                ResolvedLocation(
                    label=station_header(station),
                    lat=station.lat,
                    lng=station.lng,
                    station=station,
                ),
                [],
            )

        if matches:
            display_matches = sort_station_matches_for_display(matches)
            return None, [station for station, _score in display_matches]

        return None, []

    def plan(
        self,
        source_value: str,
        destination_value: str,
        force_refresh_routes: bool = False,
        max_workers: int = DEFAULT_ROUTE_DETAILS_WORKERS,
    ) -> RoutePlan:
        self.ensure_graph(
            force_refresh_routes=force_refresh_routes,
            max_workers=max_workers,
        )
        if self.graph is None:
            raise TransportPortalError("Не удалось собрать граф маршрутов")

        source = self.resolve_location(source_value)
        destination = self.resolve_location(destination_value)
        return self.plan_between_resolved(source, destination)

    def plan_between_resolved(
        self,
        source: ResolvedLocation,
        destination: ResolvedLocation,
    ) -> RoutePlan:
        self._forecast_cache = {}
        self._forecast_cache_failures = set()
        self._forecast_fetch_count = 0
        self.ensure_graph()
        if self.graph is None:
            raise TransportPortalError("Не удалось собрать граф маршрутов")

        direct_walk_distance = haversine_distance_meters(
            source.lat,
            source.lng,
            destination.lat,
            destination.lng,
        )
        direct_walk_seconds = walking_seconds_for_distance(direct_walk_distance)

        source_candidates = self._get_access_candidates(source)
        target_candidates = self._get_access_candidates(destination)

        distances, previous_edges = self._run_dijkstra(source_candidates)

        best_total_seconds = direct_walk_seconds
        best_station_id: int | None = None
        best_final_walk_seconds = direct_walk_seconds
        best_final_walk_distance = direct_walk_distance

        for station_id, walk_seconds, walk_distance in target_candidates:
            transit_seconds = distances.get(station_id)
            if transit_seconds is None:
                continue

            total_seconds = transit_seconds + walk_seconds
            if total_seconds < best_total_seconds:
                best_total_seconds = total_seconds
                best_station_id = station_id
                best_final_walk_seconds = walk_seconds
                best_final_walk_distance = walk_distance

        if best_station_id is None:
            return RoutePlan(
                source=source,
                destination=destination,
                total_seconds=best_total_seconds,
                edges=[
                    PlannerEdge(
                        kind="walk",
                        from_station_id=None,
                        to_station_id=None,
                        duration_seconds=direct_walk_seconds,
                        distance_meters=direct_walk_distance,
                    )
                ],
            )

        edges = self._reconstruct_edges(previous_edges, best_station_id)
        edges = self._optimize_redundant_feeders(edges)
        edges = self._prefer_better_direct_alternatives(edges)
        edges = self._prefer_source_boarding_for_later_route(source, edges)
        edges = self._merge_consecutive_walks(edges)
        if best_final_walk_distance > 15:
            final_walk_edge = PlannerEdge(
                kind="walk",
                from_station_id=best_station_id,
                to_station_id=None,
                duration_seconds=best_final_walk_seconds,
                distance_meters=best_final_walk_distance,
            )
            edges.append(final_walk_edge)
            edges = self._merge_consecutive_walks(edges)

        total_seconds = sum(edge.duration_seconds for edge in edges)

        return RoutePlan(
            source=source,
            destination=destination,
            total_seconds=total_seconds,
            edges=edges,
        )

    def _add_transit_edges(
        self,
        graph: dict[int, list[PlannerEdge]],
        route_catalog: Iterable[RouteDetails],
    ) -> None:
        seen_signatures: set[tuple[int, tuple[int, ...]]] = set()

        for route in route_catalog:
            route_icon = ROUTE_TYPE_ICONS.get(route.route_type_id, "🚌")
            for subroute in route.subroutes:
                if len(subroute.stations) < 2:
                    continue

                station_sequence = tuple(station.id for station in subroute.stations)
                signature = (route.id, station_sequence)
                if signature in seen_signatures:
                    continue
                seen_signatures.add(signature)

                cumulative_seconds = [0.0]
                for previous_station, next_station in zip(subroute.stations, subroute.stations[1:]):
                    segment_distance = haversine_distance_meters(
                        previous_station.lat,
                        previous_station.lng,
                        next_station.lat,
                        next_station.lng,
                    )
                    segment_seconds = estimate_transit_seconds(
                        segment_distance,
                        route.route_type_id,
                    )
                    cumulative_seconds.append(cumulative_seconds[-1] + segment_seconds)

                for from_index, from_station in enumerate(subroute.stations[:-1]):
                    for to_index in range(from_index + 1, len(subroute.stations)):
                        to_station = subroute.stations[to_index]
                        in_vehicle_seconds = cumulative_seconds[to_index] - cumulative_seconds[from_index]
                        graph[from_station.id].append(
                            PlannerEdge(
                                kind="ride",
                                from_station_id=from_station.id,
                                to_station_id=to_station.id,
                                duration_seconds=in_vehicle_seconds,
                                distance_meters=0.0,
                                route_id=route.id,
                                route_number=f"{route_icon}{route.number}",
                                route_type_id=route.route_type_id,
                                subroute_id=subroute.id,
                                subroute_name=subroute.name,
                                stop_count=to_index - from_index,
                                in_vehicle_seconds=in_vehicle_seconds,
                                wait_seconds=0.0,
                            )
                        )

    def _add_walking_edges(
        self,
        graph: dict[int, list[PlannerEdge]],
        stations: list[Station],
    ) -> None:
        for station in stations:
            graph.setdefault(station.id, [])

        for index, left_station in enumerate(stations):
            for right_station in stations[index + 1:]:
                if abs(left_station.lat - right_station.lat) * 111_320 > self.transfer_walk_radius_meters:
                    continue

                average_lat_cos = math.cos(math.radians((left_station.lat + right_station.lat) / 2))
                lng_distance = abs(left_station.lng - right_station.lng) * 111_320 * average_lat_cos
                if lng_distance > self.transfer_walk_radius_meters:
                    continue

                walking_distance = haversine_distance_meters(
                    left_station.lat,
                    left_station.lng,
                    right_station.lat,
                    right_station.lng,
                )
                if walking_distance <= 0 or walking_distance > self.transfer_walk_radius_meters:
                    continue

                walking_seconds = walking_seconds_for_distance(walking_distance)
                graph[left_station.id].append(
                    PlannerEdge(
                        kind="walk",
                        from_station_id=left_station.id,
                        to_station_id=right_station.id,
                        duration_seconds=walking_seconds,
                        distance_meters=walking_distance,
                    )
                )
                graph[right_station.id].append(
                    PlannerEdge(
                        kind="walk",
                        from_station_id=right_station.id,
                        to_station_id=left_station.id,
                        duration_seconds=walking_seconds,
                        distance_meters=walking_distance,
                    )
                )

    def _get_access_candidates(self, location: ResolvedLocation) -> list[tuple[int, float, float]]:
        candidates: list[tuple[int, float, float]] = []
        if location.station is not None:
            nearby_stations = [
                station
                for station in self.stations
                if haversine_distance_meters(
                    location.station.lat,
                    location.station.lng,
                    station.lat,
                    station.lng,
                )
                <= SELECTED_STATION_ACCESS_RADIUS_METERS
            ]
        else:
            nearby_stations = self.stations
        for station in nearby_stations:
            distance_meters = haversine_distance_meters(
                location.lat,
                location.lng,
                station.lat,
                station.lng,
            )
            if distance_meters > self.access_walk_radius_meters:
                continue
            candidates.append(
                (
                    station.id,
                    walking_seconds_for_distance(distance_meters),
                    distance_meters,
                )
            )

        candidates.sort(key=lambda item: (item[1], item[0]))
        return candidates[:MAX_ACCESS_STATIONS]

    def _run_dijkstra(
        self,
        source_candidates: list[tuple[int, float, float]],
    ) -> tuple[dict[int, float], dict[int, tuple[int | None, PlannerEdge]]]:
        if self.graph is None:
            raise TransportPortalError("Граф маршрутов ещё не собран")

        distances: dict[int, float] = {}
        previous_edges: dict[int, tuple[int | None, PlannerEdge]] = {}
        queue: list[tuple[float, int]] = []

        for station_id, walk_seconds, walk_distance in source_candidates:
            current_best = distances.get(station_id)
            if current_best is not None and current_best <= walk_seconds:
                continue

            distances[station_id] = walk_seconds
            previous_edges[station_id] = (
                None,
                PlannerEdge(
                    kind="walk",
                    from_station_id=None,
                    to_station_id=station_id,
                    duration_seconds=walk_seconds,
                    distance_meters=walk_distance,
                ),
            )
            heapq.heappush(queue, (walk_seconds, station_id))

        while queue:
            current_seconds, station_id = heapq.heappop(queue)
            if current_seconds > distances.get(station_id, float("inf")):
                continue

            for edge in self.graph.get(station_id, []):
                next_station_id = edge.to_station_id
                if next_station_id is None:
                    continue

                selected_edge = edge
                if edge.kind == "ride":
                    wait_seconds, wait_is_realtime = self._estimate_wait_seconds(
                        station_id=station_id,
                        edge=edge,
                        boarding_time_offset_seconds=current_seconds,
                    )
                    selected_edge = replace(
                        edge,
                        wait_seconds=wait_seconds,
                        wait_is_realtime=wait_is_realtime,
                        duration_seconds=(edge.in_vehicle_seconds or edge.duration_seconds) + wait_seconds,
                    )

                candidate_seconds = current_seconds + selected_edge.duration_seconds
                if candidate_seconds >= distances.get(next_station_id, float("inf")):
                    continue

                distances[next_station_id] = candidate_seconds
                previous_edges[next_station_id] = (station_id, selected_edge)
                heapq.heappush(queue, (candidate_seconds, next_station_id))

        return distances, previous_edges

    def _estimate_wait_seconds(
        self,
        station_id: int,
        edge: PlannerEdge,
        boarding_time_offset_seconds: float,
        force_fetch: bool = False,
    ) -> tuple[float, bool]:
        if boarding_time_offset_seconds > REALTIME_FORECAST_HORIZON_SECONDS:
            return BOARDING_PENALTY_SECONDS, False

        arrivals = self._get_station_forecasts_cached(station_id, force_fetch=force_fetch)
        if not arrivals:
            if station_id not in self._forecast_cache_failures:
                return NO_MATCHING_FORECAST_WAIT_SECONDS, False
            return BOARDING_PENALTY_SECONDS, False

        matching_arrivals = self._filter_matching_arrivals(arrivals, edge)
        if not matching_arrivals:
            return NO_MATCHING_FORECAST_WAIT_SECONDS, False

        for arrival in matching_arrivals:
            wait_seconds = arrival.arrival_time_in_sec - boarding_time_offset_seconds
            if wait_seconds >= -15:
                return max(0.0, wait_seconds), True

        return NO_MATCHING_FORECAST_WAIT_SECONDS, False

    def _get_station_forecasts_cached(self, station_id: int, force_fetch: bool = False) -> list[Arrival]:
        cached = self._forecast_cache.get(station_id)
        if cached is not None:
            return cached

        if station_id in self._forecast_cache_failures:
            return []

        if not force_fetch and self._forecast_fetch_count >= MAX_REALTIME_FORECAST_STATIONS:
            return []

        try:
            arrivals = self.client.get_station_forecasts(station_id)
        except TransportPortalError:
            self._forecast_cache_failures.add(station_id)
            return []

        self._forecast_cache[station_id] = arrivals
        self._forecast_fetch_count += 1
        return arrivals

    def _filter_matching_arrivals(self, arrivals: list[Arrival], edge: PlannerEdge) -> list[Arrival]:
        route_key = normalize_route_key(edge.route_number)

        base_matches = [
            arrival
            for arrival in arrivals
            if normalize_route_key(arrival.route_number) == route_key
            and (
                edge.route_type_id is None
                or arrival.route_type_id is None
                or arrival.route_type_id == edge.route_type_id
            )
        ]
        if not base_matches:
            return []

        direction_matches = [
            arrival
            for arrival in base_matches
            if is_direction_match(arrival.where_go, edge.subroute_name)
        ]
        return direction_matches or base_matches

    def _optimize_redundant_feeders(self, edges: list[PlannerEdge]) -> list[PlannerEdge]:
        current_edges = self._recalculate_edge_timing(edges)

        for _ in range(3):
            simplified_edges, changed = self._collapse_redundant_feeder_once(current_edges)
            if not changed:
                return current_edges
            current_edges = self._recalculate_edge_timing(simplified_edges)

        return current_edges

    def _prefer_better_direct_alternatives(self, edges: list[PlannerEdge]) -> list[PlannerEdge]:
        current_edges = self._recalculate_edge_timing(edges, force_fetch=True)
        updated_edges: list[PlannerEdge] = []
        current_seconds = 0.0
        changed = False

        for edge in current_edges:
            if edge.kind != "ride" or edge.from_station_id is None or edge.to_station_id is None:
                updated_edges.append(edge)
                current_seconds += edge.duration_seconds
                continue

            better_edge = self._find_best_direct_ride_option(
                from_station_id=edge.from_station_id,
                to_station_id=edge.to_station_id,
                boarding_time_offset_seconds=current_seconds,
            )
            if better_edge is not None and better_edge.duration_seconds + 15 < edge.duration_seconds:
                updated_edges.append(better_edge)
                current_seconds += better_edge.duration_seconds
                changed = True
                continue

            updated_edges.append(edge)
            current_seconds += edge.duration_seconds

        return updated_edges if changed else current_edges

    def _recalculate_edge_timing(self, edges: list[PlannerEdge], force_fetch: bool = False) -> list[PlannerEdge]:
        updated_edges: list[PlannerEdge] = []
        current_seconds = 0.0

        for edge in edges:
            if edge.kind != "ride" or edge.from_station_id is None:
                updated_edges.append(edge)
                current_seconds += edge.duration_seconds
                continue

            in_vehicle_seconds = edge.in_vehicle_seconds or edge.duration_seconds
            wait_seconds, wait_is_realtime = self._estimate_wait_seconds(
                station_id=edge.from_station_id,
                edge=edge,
                boarding_time_offset_seconds=current_seconds,
                force_fetch=force_fetch,
            )
            updated_edge = replace(
                edge,
                wait_seconds=wait_seconds,
                wait_is_realtime=wait_is_realtime,
                duration_seconds=in_vehicle_seconds + wait_seconds,
            )
            updated_edges.append(updated_edge)
            current_seconds += updated_edge.duration_seconds

        return updated_edges

    def _collapse_redundant_feeder_once(self, edges: list[PlannerEdge]) -> tuple[list[PlannerEdge], bool]:
        current_seconds = 0.0

        for index in range(len(edges) - 1):
            feeder_edge = edges[index]
            next_edge = edges[index + 1]

            if feeder_edge.kind != "ride" or next_edge.kind != "ride":
                current_seconds += feeder_edge.duration_seconds
                continue

            if (
                feeder_edge.from_station_id is None
                or feeder_edge.to_station_id is None
                or next_edge.from_station_id is None
                or next_edge.to_station_id is None
                or feeder_edge.to_station_id != next_edge.from_station_id
            ):
                current_seconds += feeder_edge.duration_seconds
                continue

            if feeder_edge.stop_count > SHORT_FEEDER_MAX_STOPS:
                current_seconds += feeder_edge.duration_seconds
                continue

            if feeder_edge.in_vehicle_seconds > SHORT_FEEDER_MAX_IN_VEHICLE_SECONDS:
                current_seconds += feeder_edge.duration_seconds
                continue

            direct_edge = self._find_direct_ride_edge(
                from_station_id=feeder_edge.from_station_id,
                target_edge=next_edge,
            )
            if direct_edge is None:
                current_seconds += feeder_edge.duration_seconds
                continue

            direct_wait_seconds, direct_wait_is_realtime = self._estimate_wait_seconds(
                station_id=feeder_edge.from_station_id,
                edge=direct_edge,
                boarding_time_offset_seconds=current_seconds,
                force_fetch=True,
            )
            direct_total_seconds = (direct_edge.in_vehicle_seconds or direct_edge.duration_seconds) + direct_wait_seconds
            feeder_combo_seconds = feeder_edge.duration_seconds + next_edge.duration_seconds

            if direct_total_seconds > feeder_combo_seconds + FEWER_TRANSFERS_PREFERENCE_SECONDS:
                current_seconds += feeder_edge.duration_seconds
                continue

            replacement_edge = replace(
                direct_edge,
                wait_seconds=direct_wait_seconds,
                wait_is_realtime=direct_wait_is_realtime,
                duration_seconds=direct_total_seconds,
            )
            updated_edges = edges[:index] + [replacement_edge] + edges[index + 2 :]
            return updated_edges, True

        return edges, False

    def _find_direct_ride_edge(self, from_station_id: int, target_edge: PlannerEdge) -> PlannerEdge | None:
        if self.graph is None:
            return None

        candidates = [
            edge
            for edge in self.graph.get(from_station_id, [])
            if edge.kind == "ride"
            and edge.to_station_id == target_edge.to_station_id
            and edge.route_id == target_edge.route_id
            and edge.subroute_id == target_edge.subroute_id
        ]
        if not candidates:
            return None

        return min(candidates, key=lambda edge: (edge.in_vehicle_seconds or edge.duration_seconds, edge.stop_count))

    def _prefer_source_boarding_for_later_route(
        self,
        source: ResolvedLocation,
        edges: list[PlannerEdge],
    ) -> list[PlannerEdge]:
        source_candidates = self._get_access_candidates(source)
        current_edges = self._recalculate_edge_timing(edges, force_fetch=True)

        for index, edge in enumerate(current_edges):
            if edge.kind != "ride":
                continue

            prefix_edges = current_edges[:index]
            if not any(prefix_edge.kind == "ride" for prefix_edge in prefix_edges):
                continue

            current_prefix_total = sum(item.duration_seconds for item in prefix_edges) + edge.duration_seconds
            best_replacement_edges: list[PlannerEdge] | None = None
            best_replacement_total: float | None = None

            for station_id, walk_seconds, walk_distance in source_candidates:
                direct_edge = self._find_direct_ride_edge(station_id, edge)
                if direct_edge is None:
                    continue

                wait_seconds, wait_is_realtime = self._estimate_wait_seconds(
                    station_id=station_id,
                    edge=direct_edge,
                    boarding_time_offset_seconds=walk_seconds,
                    force_fetch=True,
                )
                direct_total = walk_seconds + wait_seconds + (direct_edge.in_vehicle_seconds or direct_edge.duration_seconds)
                if direct_total > current_prefix_total + SOURCE_BOARDING_SAME_ROUTE_PREFERENCE_SECONDS:
                    continue

                replacement_edges: list[PlannerEdge] = []
                if walk_distance > 15:
                    replacement_edges.append(
                        PlannerEdge(
                            kind="walk",
                            from_station_id=None,
                            to_station_id=station_id,
                            duration_seconds=walk_seconds,
                            distance_meters=walk_distance,
                        )
                    )

                replacement_edges.append(
                    replace(
                        direct_edge,
                        wait_seconds=wait_seconds,
                        wait_is_realtime=wait_is_realtime,
                        duration_seconds=(direct_edge.in_vehicle_seconds or direct_edge.duration_seconds) + wait_seconds,
                    )
                )

                if best_replacement_total is None or direct_total < best_replacement_total:
                    best_replacement_total = direct_total
                    best_replacement_edges = replacement_edges

            if best_replacement_edges is None:
                continue

            updated_edges = best_replacement_edges + current_edges[index + 1 :]
            return self._recalculate_edge_timing(updated_edges, force_fetch=True)

        return current_edges

    def _find_best_direct_ride_option(
        self,
        from_station_id: int,
        to_station_id: int,
        boarding_time_offset_seconds: float,
    ) -> PlannerEdge | None:
        if self.graph is None:
            return None

        candidates = [
            edge
            for edge in self.graph.get(from_station_id, [])
            if edge.kind == "ride" and edge.to_station_id == to_station_id
        ]
        if not candidates:
            return None

        best_edge: PlannerEdge | None = None
        best_total_seconds: float | None = None
        for edge in candidates:
            wait_seconds, wait_is_realtime = self._estimate_wait_seconds(
                station_id=from_station_id,
                edge=edge,
                boarding_time_offset_seconds=boarding_time_offset_seconds,
                force_fetch=True,
            )
            total_seconds = (edge.in_vehicle_seconds or edge.duration_seconds) + wait_seconds
            updated_edge = replace(
                edge,
                wait_seconds=wait_seconds,
                wait_is_realtime=wait_is_realtime,
                duration_seconds=total_seconds,
            )
            if (
                best_total_seconds is None
                or total_seconds < best_total_seconds
                or (
                    abs(total_seconds - best_total_seconds) < 1
                    and updated_edge.wait_seconds < (best_edge.wait_seconds if best_edge is not None else float("inf"))
                )
            ):
                best_total_seconds = total_seconds
                best_edge = updated_edge

        return best_edge

    def _merge_consecutive_walks(self, edges: list[PlannerEdge]) -> list[PlannerEdge]:
        merged_edges: list[PlannerEdge] = []

        for edge in edges:
            if (
                merged_edges
                and merged_edges[-1].kind == "walk"
                and edge.kind == "walk"
                and merged_edges[-1].to_station_id == edge.from_station_id
            ):
                previous_edge = merged_edges[-1]
                merged_edges[-1] = PlannerEdge(
                    kind="walk",
                    from_station_id=previous_edge.from_station_id,
                    to_station_id=edge.to_station_id,
                    duration_seconds=previous_edge.duration_seconds + edge.duration_seconds,
                    distance_meters=previous_edge.distance_meters + edge.distance_meters,
                )
                continue

            merged_edges.append(edge)

        return merged_edges

    def _reconstruct_edges(
        self,
        previous_edges: dict[int, tuple[int | None, PlannerEdge]],
        target_station_id: int,
    ) -> list[PlannerEdge]:
        edges: list[PlannerEdge] = []
        current_station_id: int | None = target_station_id

        while current_station_id is not None:
            previous = previous_edges.get(current_station_id)
            if previous is None:
                break
            previous_station_id, edge = previous
            edges.append(edge)
            current_station_id = previous_station_id

        edges.reverse()
        return edges


def normalize_route_key(route_number: str | None) -> str:
    if not route_number:
        return ""
    return ROUTE_NUMBER_RE.sub("", route_number).upper()


def is_direction_match(actual_direction: str | None, expected_direction: str | None) -> bool:
    if not actual_direction or not expected_direction:
        return True

    normalized_actual = normalize_text(actual_direction)
    normalized_expected = normalize_text(expected_direction)
    if not normalized_actual or not normalized_expected:
        return True

    if normalized_actual == normalized_expected:
        return True
    if normalized_actual in normalized_expected or normalized_expected in normalized_actual:
        return True

    return SequenceMatcher(None, normalized_actual, normalized_expected).ratio() >= 0.72


def estimate_transit_seconds(distance_meters: float, route_type_id: int | None) -> float:
    average_speed = AVERAGE_SPEED_MPS_BY_ROUTE_TYPE.get(route_type_id, 5.0)
    scaled_distance = max(120.0, distance_meters * 1.18)
    return max(MIN_TRANSIT_SEGMENT_SECONDS, scaled_distance / average_speed + DWELL_SECONDS_PER_STOP)


def walking_seconds_for_distance(distance_meters: float) -> float:
    return distance_meters / WALKING_SPEED_MPS * WALKING_PENALTY_MULTIPLIER


def haversine_distance_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius_meters = 6_371_000
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lng = math.radians(lng2 - lng1)

    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lng / 2) ** 2
    )
    return 2 * radius_meters * math.asin(math.sqrt(haversine))


def station_header(station: Station | RouteStation) -> str:
    description = getattr(station, "custom_title", "").strip()
    base_name = station.name.strip()
    icon = getattr(station, "transport_icon", "").strip()
    title = base_name
    if description and description.lower() != base_name.lower():
        title = f"{base_name} ({description})"
    if icon:
        return f"{icon}{title}"
    return title


def format_duration(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    hours, remaining_minutes = divmod(minutes, 60)
    if hours == 0:
        return f"{remaining_minutes} мин."
    return f"{hours} ч. {remaining_minutes} мин."


def format_plan(plan: RoutePlan, planner: StaticRoutePlanner) -> str:
    has_unconfirmed_wait = any(
        edge.kind == "ride" and edge.wait_seconds > 0 and not edge.wait_is_realtime
        for edge in plan.edges
    )
    total_duration_text = format_duration(plan.total_seconds)
    if has_unconfirmed_wait:
        total_duration_text = f"~{total_duration_text}"

    header_lines = [
        f"От: {plan.source.label}",
        f"До: {plan.destination.label}",
        "",
        f"Итого: {total_duration_text}, пересадок: {plan.transfers}",
    ]

    if not plan.edges:
        header_lines.append("")
        header_lines.append("Маршрут не найден.")
        return "\n".join(header_lines)

    step_lines: list[str] = []
    step_index = 1

    for edge in plan.edges:
        if edge.kind == "walk":
            if (
                edge.from_station_id is None
                and edge.to_station_id is not None
                and plan.source.station is not None
                and edge.to_station_id == plan.source.station.id
                and edge.distance_meters <= 25
            ):
                continue

            if edge.from_station_id is None and edge.to_station_id is None:
                step_lines.append(
                    f"{step_index}. Пешком до точки назначения: {format_duration(edge.duration_seconds)}"
                )
                step_index += 1
                continue

            if edge.from_station_id is None and edge.to_station_id is not None:
                target_station = planner.station_by_id.get(edge.to_station_id)
                target_label = station_header(target_station) if target_station is not None else "остановки"
                step_lines.append(
                    f"{step_index}. Пешком до {target_label}: {format_duration(edge.duration_seconds)}"
                )
                step_index += 1
                continue

            if edge.from_station_id is not None and edge.to_station_id is None:
                source_station = planner.station_by_id.get(edge.from_station_id)
                source_label = station_header(source_station) if source_station is not None else "остановки"
                step_lines.append(
                    f"{step_index}. Пешком от {source_label} до точки назначения: {format_duration(edge.duration_seconds)}"
                )
                step_index += 1
                continue

            source_station = planner.station_by_id.get(edge.from_station_id)
            target_station = planner.station_by_id.get(edge.to_station_id)
            source_label = station_header(source_station) if source_station is not None else "остановки"
            target_label = station_header(target_station) if target_station is not None else "остановки"
            step_lines.append(
                f"{step_index}. Перейти пешком от {source_label} до {target_label}: {format_duration(edge.duration_seconds)}"
            )
            step_index += 1
            continue

        if edge.from_station_id is None or edge.to_station_id is None:
            continue

        source_station = planner.station_by_id.get(edge.from_station_id)
        target_station = planner.station_by_id.get(edge.to_station_id)
        source_label = station_header(source_station) if source_station is not None else "остановки"
        target_label = station_header(target_station) if target_station is not None else "остановки"
        route_direction = edge.subroute_name or "конечной"
        in_vehicle_duration = format_duration(edge.in_vehicle_seconds or edge.duration_seconds)
        ride_duration_text = f"~{in_vehicle_duration}"
        if edge.wait_seconds > 0:
            if edge.wait_is_realtime:
                wait_duration = format_duration(edge.wait_seconds)
                ride_duration_text = f"~{in_vehicle_duration} + {wait_duration} ожидание"
            else:
                ride_duration_text = f"~{in_vehicle_duration} + ожидание неизвестно"
        step_lines.append(
            f"{step_index}. {edge.route_number} до {route_direction}: от {source_label} до {target_label}, "
            f"{edge.stop_count} ост., {ride_duration_text}"
        )
        step_index += 1

    return "\n".join(header_lines + [""] + [("\n\n".join(step_lines))])


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Экспериментальный статический планировщик маршрутов Екатеринбурга.",
    )
    parser.add_argument("source", help='Начальная точка: остановка или координаты "lat,lng"')
    parser.add_argument("destination", help='Конечная точка: остановка или координаты "lat,lng"')
    parser.add_argument(
        "--refresh-routes",
        action="store_true",
        help="Принудительно обновить локальный кэш маршрутов перед построением маршрута.",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=DEFAULT_ROUTE_DETAILS_WORKERS,
        help="Сколько параллельных запросов использовать при обновлении route-каталога.",
    )
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    client = TransportPortalClient()
    planner = StaticRoutePlanner(client)

    try:
        plan = planner.plan(
            source_value=args.source,
            destination_value=args.destination,
            force_refresh_routes=args.refresh_routes,
            max_workers=max(1, args.max_workers),
        )
    except TransportPortalError as exc:
        print(f"Ошибка: {exc}")
        return 1

    print(format_plan(plan, planner))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
