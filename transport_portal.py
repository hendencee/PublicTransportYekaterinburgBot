from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import random
import re
import time
import unicodedata
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib import error, request


BASE_URL = "https://маршрут.екатеринбург.рф"
ASCII_BASE_URL = "https://xn--80axnakf7a.xn--80acgfbsl1azdqr.xn--p1ai"
API_URL = f"{ASCII_BASE_URL}/php/apiRequest.php"
API_TOKEN = "11111111-50b3-4fec-b922-8a50a1d38366"
DEFAULT_REGION_ID = 66002
CACHE_TTL_SECONDS = 24 * 60 * 60
DEFAULT_ROUTE_INDEX_WORKERS = 6
DEFAULT_ROUTE_DETAILS_WORKERS = 4
STATION_LABEL_LINE_RE = re.compile(r"^(?P<icon>\D*?)(?P<station_id>\d+)=(?P<label>.*)$")
TRANSPORT_ICON_SORT_PRIORITY = {
    "🚌": 0,
    "🚊": 1,
}
ROUTE_TYPE_IDS_BY_STATION_ICON = {
    "🚌": {1, 2},
    "🚊": {4},
}
ROUTE_TYPE_IDS_BY_SYMBOL = {
    "А": 1,
    "Т": 2,
    "Тб": 2,
    "Тр": 4,
    "Тм": 4,
}
PUBLIC_STATION_GROUPS = {
    1736: {
        "member_ids": (1735, 1736),
        "custom_title": "ул. Мира, в ст. ул. Первомайской",
    },
}
PUBLIC_STATION_GROUP_BY_MEMBER_ID = {
    member_id: canonical_id
    for canonical_id, group in PUBLIC_STATION_GROUPS.items()
    for member_id in group["member_ids"]
}
REQUEST_RETRY_ATTEMPTS = 3
REQUEST_RETRY_DELAY_SECONDS = 0.8
RETRYABLE_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}
SEARCH_STOP_WORDS = {
    "в",
    "во",
    "на",
    "по",
    "при",
    "к",
    "ко",
    "у",
    "от",
    "до",
    "из",
    "для",
}


class TransportPortalError(RuntimeError):
    pass


def repair_mojibake(text: str) -> str:
    if not text:
        return text

    # Some station names in the bulk list come back as UTF-8 decoded as latin-1.
    if not any(marker in text for marker in ("Ð", "Ñ", "Â")):
        return text

    try:
        repaired = text.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return text

    return repaired


@dataclass(slots=True)
class Station:
    id: int
    name: str
    description: str
    address: str
    direction: int
    lat: float
    lng: float
    transport_icon: str = ""
    custom_title: str = ""
    route_count: int | None = None

    @property
    def display_name(self) -> str:
        return self.custom_title or self.name

    @property
    def public_name(self) -> str:
        if self.transport_icon:
            return f"{self.transport_icon} {self.display_name}"
        return self.display_name

    @property
    def search_text(self) -> str:
        parts = [self.name]
        if self.custom_title:
            parts.append(self.custom_title)
        if self.description:
            parts.append(self.description)
        if self.address:
            parts.append(self.address)
        return " ".join(part for part in parts if part)

    @property
    def is_archived(self) -> bool:
        return self.route_count == 0


@dataclass(slots=True)
class StationLabel:
    transport_icon: str
    custom_title: str


@dataclass(slots=True)
class Arrival:
    station_id: int
    route_id: int | None
    route_number: str | None
    route_name: str | None
    route_type_id: int | None
    where_go: str | None
    last_station: str | None
    arrival_time_in_sec: int

    @property
    def arrival_minutes(self) -> int:
        return max(1, round(self.arrival_time_in_sec / 60))


@dataclass(slots=True)
class RouteSummary:
    id: int
    name: str
    number: str
    short_name: str
    route_type_id: int | None
    from_station: str
    to_station: str
    company_name: str | None
    price: Any | None


@dataclass(slots=True)
class RouteStation:
    id: int
    name: str
    description: str
    lat: float
    lng: float
    direction: int
    transport_icon: str = ""
    custom_title: str = ""

    @property
    def display_name(self) -> str:
        return self.custom_title or self.name

    @property
    def public_name(self) -> str:
        if self.transport_icon:
            return f"{self.transport_icon} {self.display_name}"
        return self.display_name


@dataclass(slots=True)
class Subroute:
    id: int
    name: str
    is_main: bool
    forward_direction: bool
    stations: list[RouteStation]
    points: list[tuple[float, float]]


@dataclass(slots=True)
class RouteDetails:
    id: int
    name: str
    number: str
    short_name: str
    from_station: str
    to_station: str
    route_type_id: int | None
    route_type_symbol: str | None
    company_name: str | None
    price: Any | None
    subroutes: list[Subroute]

    @property
    def display_label(self) -> str:
        return self.short_name or self.number or str(self.id)


class TransportPortalClient:
    def __init__(
        self,
        region_id: int = DEFAULT_REGION_ID,
        cache_path: str | Path = ".stations_cache.json",
        station_labels_path: str | Path = "stationsdesc.txt",
        route_status_cache_path: str | Path = ".station_route_status.json",
        route_catalog_cache_path: str | Path = ".route_catalog_cache.json",
        timeout: float = 20.0,
    ) -> None:
        self.region_id = region_id
        self.cache_path = Path(cache_path)
        self.station_labels_path = Path(station_labels_path)
        self.route_status_cache_path = Path(route_status_cache_path)
        self.route_catalog_cache_path = Path(route_catalog_cache_path)
        self.timeout = timeout
        self._station_labels: dict[int, StationLabel] | None = None
        self._station_labels_mtime: float | None = None
        self._route_status: dict[int, int] | None = None
        self._route_status_mtime: float | None = None
        self._route_catalog: list[RouteDetails] | None = None
        self._route_catalog_mtime: float | None = None

    def get_all_stations(
        self,
        force_refresh: bool = False,
        include_archived: bool = True,
    ) -> list[Station]:
        if not force_refresh:
            cached = self._load_station_cache()
            if cached is not None:
                return self._filter_archived(cached, include_archived=include_archived)

        payload = self._post("getStations.php", {"q": ""})
        stations = [self._station_from_dict(item) for item in payload]
        self._save_station_cache(payload)
        return self._filter_archived(stations, include_archived=include_archived)

    def get_station_routes(self, station_id: int) -> list[dict[str, Any]]:
        return self._post("getStationRoutes.php", {"sid": station_id})

    def get_station_forecasts(self, station_id: int) -> list[Arrival]:
        payload = self._post("getStationForecasts.php", {"sid": station_id})
        arrivals = [
            Arrival(
                station_id=int(item["stationId"]),
                route_id=self._optional_int(item.get("routeId")),
                route_number=item.get("routeNumber"),
                route_name=item.get("routeName"),
                route_type_id=self._optional_int(item.get("routeTypeId")),
                where_go=item.get("whereGo"),
                last_station=item.get("lastStation"),
                arrival_time_in_sec=int(item["arrivalTimeInSec"]),
            )
            for item in payload
            if item.get("arrivalTimeInSec") is not None
        ]
        return sorted(arrivals, key=lambda item: item.arrival_time_in_sec)

    def get_station_by_id(self, station_id: int, include_archived: bool = True) -> Station | None:
        stations = self.get_all_stations(include_archived=include_archived)
        return next((station for station in stations if station.id == station_id), None)

    def get_public_station(self, station: Station) -> Station:
        canonical_id = PUBLIC_STATION_GROUP_BY_MEMBER_ID.get(station.id)
        if canonical_id is None:
            return station

        canonical_station = self.get_station_by_id(canonical_id, include_archived=True) or station
        group = PUBLIC_STATION_GROUPS[canonical_id]
        return replace(
            canonical_station,
            custom_title=group.get("custom_title", canonical_station.custom_title),
        )

    def get_public_station_group_member_ids(self, station_id: int) -> tuple[int, ...]:
        canonical_id = PUBLIC_STATION_GROUP_BY_MEMBER_ID.get(station_id)
        if canonical_id is None:
            return (station_id,)
        return tuple(PUBLIC_STATION_GROUPS[canonical_id]["member_ids"])

    def get_public_station_forecasts(self, station: Station) -> list[Arrival]:
        arrivals: list[Arrival] = []
        for member_id in self.get_public_station_group_member_ids(station.id):
            arrivals.extend(self.get_station_forecasts(member_id))
        return sorted(arrivals, key=lambda item: item.arrival_time_in_sec)

    def merge_public_station_matches(self, matches: list[tuple[Station, float]]) -> list[tuple[Station, float]]:
        merged_matches: list[tuple[Station, float]] = []
        indexes_by_station_id: dict[int, int] = {}

        for station, score in matches:
            public_station = self.get_public_station(station)
            existing_index = indexes_by_station_id.get(public_station.id)
            if existing_index is None:
                indexes_by_station_id[public_station.id] = len(merged_matches)
                merged_matches.append((public_station, score))
                continue

            existing_station, existing_score = merged_matches[existing_index]
            if score > existing_score:
                merged_matches[existing_index] = (public_station, score)
            else:
                merged_matches[existing_index] = (existing_station, existing_score)

        return merged_matches

    def find_stations(
        self,
        query: str,
        limit: int = 10,
        include_archived: bool = False,
    ) -> list[tuple[Station, float]]:
        stations = self.get_all_stations(include_archived=include_archived)
        normalized_query = normalize_text(query)
        query_words = normalized_query.split()
        query_tokens = set(query_words)
        significant_query_tokens = extract_significant_query_tokens(query_words)
        if not normalized_query:
            return []

        name_only_candidates = [
            station
            for station in stations
            if all(token in normalize_text(station.name) for token in significant_query_tokens)
        ]
        if name_only_candidates:
            return self._rank_stations(
                stations=name_only_candidates,
                normalized_query=normalized_query,
                query_tokens=significant_query_tokens,
                query_words=query_words,
                limit=limit,
                text_getter=lambda station: station.name,
                minimum_token_hits=minimum_required_token_hits(significant_query_tokens),
            )

        return self._rank_stations(
            stations=stations,
            normalized_query=normalized_query,
            query_tokens=significant_query_tokens,
            query_words=query_words,
            limit=limit,
            text_getter=lambda station: station.search_text,
            minimum_token_hits=minimum_required_token_hits(significant_query_tokens),
        )

    def choose_station(
        self,
        query: str,
        include_archived: bool = False,
    ) -> tuple[Station | None, list[tuple[Station, float]]]:
        matches = self.find_stations(query, include_archived=include_archived)
        if not matches:
            return None, []

        if len(matches) == 1:
            return matches[0][0], matches

        top_station, top_score = matches[0]
        second_score = matches[1][1]
        if top_score >= 0.92 and top_score - second_score >= 0.08:
            return top_station, matches

        exact_name_matches = [
            item
            for item in matches
            if normalize_text(item[0].name) == normalize_text(query)
            or normalize_text(item[0].display_name) == normalize_text(query)
        ]
        if len(exact_name_matches) == 1:
            return exact_name_matches[0][0], matches

        return None, matches

    def find_exact_station(self, query: str, include_archived: bool = True) -> Station | None:
        normalized_query = normalize_text(query)
        if not normalized_query:
            return None

        exact_matches = [
            station
            for station in self.get_all_stations(include_archived=include_archived)
            if normalize_text(station.name) == normalized_query
            or normalize_text(station.display_name) == normalized_query
        ]
        if len(exact_matches) == 1:
            return exact_matches[0]
        return None

    def get_routes(self) -> list[RouteSummary]:
        payload = self._post("getRoutes.php", {})
        return [self._route_summary_from_dict(item) for item in payload]

    def get_route_details(self, route_id: int, route_type_id: int | None = None) -> RouteDetails:
        payload = self._post_any("getRouteById.php", {"id": route_id})
        if not isinstance(payload, dict):
            raise TransportPortalError("getRouteById.php: неожиданный формат ответа")
        return self._route_details_from_dict(payload, route_type_id=route_type_id)

    def get_routes_with_main_subroutes(self, route_ids: list[int]) -> list[RouteDetails]:
        if not route_ids:
            return []
        payload = self._post("getRoutesWithMainSubroutes.php", {"rids": route_ids})
        return [self._route_details_from_dict(item) for item in payload]

    def get_route_catalog(
        self,
        force_refresh: bool = False,
        max_workers: int = DEFAULT_ROUTE_DETAILS_WORKERS,
    ) -> list[RouteDetails]:
        if not force_refresh:
            cached = self._load_route_catalog_cache()
            if cached is not None:
                return cached

        route_summaries = self.get_routes()
        fetched_details: dict[int, RouteDetails] = {}

        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
            future_to_summary = {
                executor.submit(self.get_route_details, summary.id, summary.route_type_id): summary
                for summary in route_summaries
            }
            for future in as_completed(future_to_summary):
                summary = future_to_summary[future]
                try:
                    fetched_details[summary.id] = future.result()
                except TransportPortalError:
                    continue

        catalog = [fetched_details[summary.id] for summary in route_summaries if summary.id in fetched_details]
        if not catalog:
            raise TransportPortalError("Не удалось загрузить ни одного маршрута для каталога")

        self._save_route_catalog_cache(catalog)
        return catalog

    def get_route_count(self, station_id: int) -> int | None:
        route_status = self._load_route_status_cache()
        if route_status is None:
            return None
        return route_status.get(station_id)

    def is_public_station(self, station_id: int) -> bool:
        route_count = self.get_route_count(station_id)
        return route_count != 0

    def refresh_route_status_cache(
        self,
        force_station_refresh: bool = False,
        max_workers: int = DEFAULT_ROUTE_INDEX_WORKERS,
    ) -> dict[int, int]:
        stations = self.get_all_stations(
            force_refresh=force_station_refresh,
            include_archived=True,
        )
        route_status = self._build_route_status_cache(
            station_ids=[station.id for station in stations],
            max_workers=max_workers,
        )
        self._save_route_status_cache(route_status)
        return route_status

    def _post(self, command: str, data: dict[str, Any]) -> list[dict[str, Any]]:
        payload = self._post_any(command, data)
        if not isinstance(payload, list):
            raise TransportPortalError(f"{command}: неожиданный формат ответа")
        return payload

    def _post_data(self, command: str, data: dict[str, Any]) -> Any:
        payload = {
            "t": API_TOKEN,
            "ct": 26,
            "cd": command,
            "reg": self.region_id,
            "w": -1,
            "data": {
                "wuid": self._generate_wuid(),
                **data,
            },
        }
        encoded_payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(
            url=f"{API_URL}?{command}",
            data=encoded_payload,
            headers={
                "Content-Type": "text/plain;charset=UTF-8",
                "Accept": "application/json, text/plain, */*",
                "Origin": ASCII_BASE_URL,
                "Referer": f"{ASCII_BASE_URL}/#/search_station",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/136.0.0.0 Safari/537.36"
                ),
            },
            method="POST",
        )

        for attempt in range(1, REQUEST_RETRY_ATTEMPTS + 1):
            try:
                with request.urlopen(req, timeout=self.timeout) as response:
                    raw_data = response.read().decode("utf-8")
                break
            except error.HTTPError as exc:
                if self._should_retry_http_error(exc.code, attempt):
                    time.sleep(REQUEST_RETRY_DELAY_SECONDS * attempt)
                    continue
                raise self._build_http_error(command, exc) from exc
            except error.URLError as exc:
                if attempt < REQUEST_RETRY_ATTEMPTS:
                    time.sleep(REQUEST_RETRY_DELAY_SECONDS * attempt)
                    continue
                raise TransportPortalError(f"Не удалось выполнить запрос {command}: {exc}") from exc
        else:
            raise TransportPortalError(f"Не удалось выполнить запрос {command}: превышено число попыток")

        try:
            payload = json.loads(raw_data)
        except json.JSONDecodeError as exc:
            raise TransportPortalError(f"Сервер вернул невалидный JSON для {command}") from exc

        if payload.get("r") != "ok":
            message = payload.get("message") or "Неизвестная ошибка API"
            raise TransportPortalError(f"{command}: {message}")

        data = payload.get("data")
        if not isinstance(data, list):
            raise TransportPortalError(f"{command}: неожиданный формат ответа")
        return data

    def _post_any(self, command: str, data: dict[str, Any]) -> Any:
        payload = {
            "t": API_TOKEN,
            "ct": 26,
            "cd": command,
            "reg": self.region_id,
            "w": -1,
            "data": {
                "wuid": self._generate_wuid(),
                **data,
            },
        }
        encoded_payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(
            url=f"{API_URL}?{command}",
            data=encoded_payload,
            headers={
                "Content-Type": "text/plain;charset=UTF-8",
                "Accept": "application/json, text/plain, */*",
                "Origin": ASCII_BASE_URL,
                "Referer": f"{ASCII_BASE_URL}/#/search_station",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/136.0.0.0 Safari/537.36"
                ),
            },
            method="POST",
        )

        for attempt in range(1, REQUEST_RETRY_ATTEMPTS + 1):
            try:
                with request.urlopen(req, timeout=self.timeout) as response:
                    raw_data = response.read().decode("utf-8")
                break
            except error.HTTPError as exc:
                if self._should_retry_http_error(exc.code, attempt):
                    time.sleep(REQUEST_RETRY_DELAY_SECONDS * attempt)
                    continue
                raise self._build_http_error(command, exc) from exc
            except error.URLError as exc:
                if attempt < REQUEST_RETRY_ATTEMPTS:
                    time.sleep(REQUEST_RETRY_DELAY_SECONDS * attempt)
                    continue
                raise TransportPortalError(f"Не удалось выполнить запрос {command}: {exc}") from exc
        else:
            raise TransportPortalError(f"Не удалось выполнить запрос {command}: превышено число попыток")

        try:
            response_payload = json.loads(raw_data)
        except json.JSONDecodeError as exc:
            raise TransportPortalError(f"Сервер вернул невалидный JSON для {command}") from exc

        if response_payload.get("r") != "ok":
            message = response_payload.get("message") or "Неизвестная ошибка API"
            raise TransportPortalError(f"{command}: {message}")

        return response_payload.get("data")

    def _load_station_cache(self) -> list[Station] | None:
        if not self.cache_path.exists():
            return None

        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

        created_at = payload.get("created_at")
        stations = payload.get("stations")
        if not isinstance(created_at, (int, float)) or not isinstance(stations, list):
            return None
        if time.time() - float(created_at) > CACHE_TTL_SECONDS:
            return None

        return [self._station_from_dict(item) for item in stations]

    def _save_station_cache(self, stations: list[dict[str, Any]]) -> None:
        payload = {
            "created_at": time.time(),
            "stations": stations,
        }
        self.cache_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _station_from_dict(self, item: dict[str, Any]) -> Station:
        station_id = int(item["id"])
        station_label = self._load_station_labels().get(station_id)
        return Station(
            id=station_id,
            name=repair_mojibake(item.get("name", "")),
            description=repair_mojibake(item.get("description", "")),
            address=repair_mojibake(item.get("adress", "")),
            direction=int(item.get("direction", 0)),
            lat=float(item["lat"]),
            lng=float(item["lng"]),
            transport_icon=station_label.transport_icon if station_label is not None else "",
            custom_title=station_label.custom_title if station_label is not None else "",
            route_count=self.get_route_count(station_id),
        )

    def _route_summary_from_dict(self, item: dict[str, Any]) -> RouteSummary:
        return RouteSummary(
            id=int(item["id"]),
            name=repair_mojibake(item.get("name", "")),
            number=str(item.get("number", "")),
            short_name=repair_mojibake(item.get("shortName", "")),
            route_type_id=self._optional_int(item.get("routeTypeId")),
            from_station=repair_mojibake(item.get("fromStation", "")),
            to_station=repair_mojibake(item.get("toStation", "")),
            company_name=repair_mojibake(item.get("companyName", "")) or None,
            price=item.get("price"),
        )

    def _route_details_from_dict(
        self,
        item: dict[str, Any],
        route_type_id: int | None = None,
    ) -> RouteDetails:
        route_type_symbol = repair_mojibake(item.get("type", "")) or None
        if route_type_id is None:
            route_type_id = self._optional_int(item.get("routeTypeId"))
        if route_type_id is None and route_type_symbol is not None:
            route_type_id = ROUTE_TYPE_IDS_BY_SYMBOL.get(route_type_symbol)

        subroutes_payload = item.get("subroutes")
        if subroutes_payload is None:
            subroutes_payload = item.get("mainSubroutes", [])

        return RouteDetails(
            id=int(item["id"]),
            name=repair_mojibake(item.get("name", "")),
            number=str(item.get("number", "")),
            short_name=repair_mojibake(item.get("shortName", "")),
            from_station=repair_mojibake(item.get("fromStation", "")),
            to_station=repair_mojibake(item.get("toStation", "")),
            route_type_id=route_type_id,
            route_type_symbol=route_type_symbol,
            company_name=repair_mojibake(item.get("companyName", "")) or None,
            price=item.get("price"),
            subroutes=[self._subroute_from_dict(subroute) for subroute in subroutes_payload if isinstance(subroute, dict)],
        )

    def _subroute_from_dict(self, item: dict[str, Any]) -> Subroute:
        return Subroute(
            id=int(item["id"]),
            name=repair_mojibake(item.get("name", "")),
            is_main=bool(item.get("isMain")),
            forward_direction=bool(item.get("forwardDirection")),
            stations=[self._route_station_from_dict(station) for station in item.get("stations", []) if isinstance(station, dict)],
            points=[
                (float(point["lat"]), float(point["lng"]))
                for point in item.get("points", [])
                if isinstance(point, dict) and point.get("lat") is not None and point.get("lng") is not None
            ],
        )

    def _route_station_from_dict(self, item: dict[str, Any]) -> RouteStation:
        station_id = int(item["id"])
        station_label = self._load_station_labels().get(station_id)
        return RouteStation(
            id=station_id,
            name=repair_mojibake(item.get("name", "")),
            description=repair_mojibake(item.get("description", "")),
            lat=float(item["lat"]),
            lng=float(item["lng"]),
            direction=int(item.get("direction", 0)),
            transport_icon=station_label.transport_icon if station_label is not None else "",
            custom_title=station_label.custom_title if station_label is not None else "",
        )

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if value is None:
            return None
        return int(value)

    @staticmethod
    def _generate_wuid() -> int:
        return random.randint(100_000_000_000, 999_999_999_999)

    def _load_station_labels(self) -> dict[int, StationLabel]:
        if not self.station_labels_path.exists():
            self._station_labels = {}
            self._station_labels_mtime = None
            return self._station_labels

        mtime = self.station_labels_path.stat().st_mtime
        if self._station_labels is not None and self._station_labels_mtime == mtime:
            return self._station_labels

        labels: dict[int, StationLabel] = {}
        for raw_line in self.station_labels_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            match = STATION_LABEL_LINE_RE.match(line)
            if match is None:
                continue

            station_id = int(match.group("station_id"))
            transport_icon = match.group("icon").strip()
            custom_title = match.group("label").strip()
            if custom_title.lower() == "none":
                custom_title = ""

            labels[station_id] = StationLabel(
                transport_icon=transport_icon,
                custom_title=custom_title,
            )

        self._station_labels = labels
        self._station_labels_mtime = mtime
        return labels

    def _load_route_status_cache(self) -> dict[int, int] | None:
        if not self.route_status_cache_path.exists():
            self._route_status = None
            self._route_status_mtime = None
            return None

        mtime = self.route_status_cache_path.stat().st_mtime
        if self._route_status is not None and self._route_status_mtime == mtime:
            return self._route_status

        try:
            payload = json.loads(self.route_status_cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

        route_counts = payload.get("route_counts")
        if not isinstance(route_counts, dict):
            return None

        parsed: dict[int, int] = {}
        for station_id, route_count in route_counts.items():
            try:
                parsed[int(station_id)] = int(route_count)
            except (TypeError, ValueError):
                continue

        self._route_status = parsed
        self._route_status_mtime = mtime
        return parsed

    def _save_route_status_cache(self, route_status: dict[int, int]) -> None:
        payload = {
            "created_at": time.time(),
            "route_counts": {str(station_id): route_count for station_id, route_count in route_status.items()},
        }
        self.route_status_cache_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._route_status = route_status
        self._route_status_mtime = self.route_status_cache_path.stat().st_mtime

    def _load_route_catalog_cache(self) -> list[RouteDetails] | None:
        if not self.route_catalog_cache_path.exists():
            self._route_catalog = None
            self._route_catalog_mtime = None
            return None

        mtime = self.route_catalog_cache_path.stat().st_mtime
        if self._route_catalog is not None and self._route_catalog_mtime == mtime:
            return self._route_catalog

        try:
            payload = json.loads(self.route_catalog_cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

        created_at = payload.get("created_at")
        routes = payload.get("routes")
        if not isinstance(created_at, (int, float)) or not isinstance(routes, list):
            return None
        if time.time() - float(created_at) > CACHE_TTL_SECONDS:
            return None

        catalog = [self._route_details_from_dict(route) for route in routes if isinstance(route, dict)]
        self._route_catalog = catalog
        self._route_catalog_mtime = mtime
        return catalog

    def _save_route_catalog_cache(self, routes: list[RouteDetails]) -> None:
        payload = {
            "created_at": time.time(),
            "routes": [self._route_details_to_dict(route) for route in routes],
        }
        self.route_catalog_cache_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._route_catalog = routes
        self._route_catalog_mtime = self.route_catalog_cache_path.stat().st_mtime

    @staticmethod
    def _route_details_to_dict(route: RouteDetails) -> dict[str, Any]:
        return {
            "id": route.id,
            "name": route.name,
            "number": route.number,
            "shortName": route.short_name,
            "fromStation": route.from_station,
            "toStation": route.to_station,
            "routeTypeId": route.route_type_id,
            "type": route.route_type_symbol,
            "companyName": route.company_name,
            "price": route.price,
            "subroutes": [
                {
                    "id": subroute.id,
                    "name": subroute.name,
                    "isMain": subroute.is_main,
                    "forwardDirection": subroute.forward_direction,
                    "stations": [
                        {
                            "id": station.id,
                            "name": station.name,
                            "description": station.description,
                            "lat": station.lat,
                            "lng": station.lng,
                            "direction": station.direction,
                        }
                        for station in subroute.stations
                    ],
                    "points": [
                        {
                            "lat": lat,
                            "lng": lng,
                        }
                        for lat, lng in subroute.points
                    ],
                }
                for subroute in route.subroutes
            ],
        }

    def _build_route_status_cache(
        self,
        station_ids: list[int],
        max_workers: int,
    ) -> dict[int, int]:
        route_status: dict[int, int] = {}
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
            future_to_station_id = {
                executor.submit(self._fetch_route_count, station_id): station_id
                for station_id in station_ids
            }
            for future in as_completed(future_to_station_id):
                station_id = future_to_station_id[future]
                try:
                    route_status[station_id] = future.result()
                except TransportPortalError:
                    continue
        return route_status

    def _fetch_route_count(self, station_id: int) -> int:
        return len(self.get_station_routes(station_id))

    @staticmethod
    def _filter_archived(stations: list[Station], include_archived: bool) -> list[Station]:
        if include_archived:
            return stations
        return [station for station in stations if not station.is_archived]

    @staticmethod
    def _rank_stations(
        stations: list[Station],
        normalized_query: str,
        query_tokens: set[str],
        query_words: list[str],
        limit: int,
        text_getter: Any,
        minimum_token_hits: int,
    ) -> list[tuple[Station, float]]:
        ranked: list[tuple[Station, float]] = []

        for station in stations:
            normalized_station = normalize_text(text_getter(station))
            score = score_station_match(
                query=normalized_query,
                query_tokens=query_tokens,
                query_words=query_words,
                station_text=normalized_station,
                minimum_token_hits=minimum_token_hits,
            )
            if score <= 0:
                continue
            ranked.append((station, score))

        ranked.sort(
            key=lambda item: (
                -item[1],
                item[0].display_name.lower(),
                abs(item[0].direction),
                item[0].id,
            )
        )
        return ranked[:limit]

    @staticmethod
    def _should_retry_http_error(status_code: int, attempt: int) -> bool:
        return status_code in RETRYABLE_HTTP_STATUS_CODES and attempt < REQUEST_RETRY_ATTEMPTS

    @staticmethod
    def _build_http_error(command: str, exc: error.HTTPError) -> TransportPortalError:
        if exc.code in RETRYABLE_HTTP_STATUS_CODES and command == "getStationForecasts.php":
            return TransportPortalError(
                "Сервис прогноза временно недоступен. Попробуй ещё раз через минуту."
            )
        return TransportPortalError(f"Не удалось выполнить запрос {command}: HTTP Error {exc.code}")


def normalize_text(text: str) -> str:
    text = text.lower().replace("ё", "е")
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\bрф\b", "россии", text)
    text = re.sub(r"\bроссийской федерации\b", "россии", text)
    text = re.sub(r"[\"'`«»]", " ", text)
    text = re.sub(r"\b(ул|улица|проспект|пр-т|пр|переулок|пер|пл|площадь)\b", " ", text)
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_significant_query_tokens(query_words: list[str]) -> set[str]:
    significant_tokens = {token for token in query_words if token and token not in SEARCH_STOP_WORDS}
    return significant_tokens or set(query_words)


def minimum_required_token_hits(query_tokens: set[str]) -> int:
    token_count = len(query_tokens)
    if token_count <= 1:
        return 0
    if token_count == 2:
        return 1
    return 2


def station_transport_sort_priority(station: Station) -> int:
    return TRANSPORT_ICON_SORT_PRIORITY.get(station.transport_icon, 2)


def sort_station_matches_for_display(matches: list[tuple[Station, float]]) -> list[tuple[Station, float]]:
    indexed_matches = list(enumerate(matches))
    indexed_matches.sort(
        key=lambda item: (
            station_transport_sort_priority(item[1][0]),
            item[0],
        )
    )
    return [match for _, match in indexed_matches]


def filter_arrivals_for_station(station: Station, arrivals: list[Arrival]) -> list[Arrival]:
    allowed_route_type_ids = ROUTE_TYPE_IDS_BY_STATION_ICON.get(station.transport_icon)
    if not allowed_route_type_ids:
        return arrivals

    filtered_arrivals = [
        arrival
        for arrival in arrivals
        if arrival.route_type_id in allowed_route_type_ids
    ]
    return filtered_arrivals or arrivals


def score_station_match(
    query: str,
    query_tokens: set[str],
    query_words: list[str],
    station_text: str,
    minimum_token_hits: int = 0,
) -> float:
    if not query or not station_text:
        return 0.0

    ratio = SequenceMatcher(None, query, station_text).ratio()

    token_hits = 0
    for token in query_tokens:
        if token and token in station_text:
            token_hits += 1
    if token_hits < minimum_token_hits:
        return 0.0
    token_score = token_hits / max(1, len(query_tokens))

    prefix_bonus = 0.15 if station_text.startswith(query) else 0.0
    contains_bonus = 0.10 if query in station_text else 0.0
    first_word_bonus = 0.12 if query_words and station_text.startswith(query_words[0]) else 0.0

    return ratio * 0.50 + token_score * 0.33 + prefix_bonus + contains_bonus + first_word_bonus


def format_arrivals(arrivals: list[Arrival], limit: int | None = None) -> str:
    if not arrivals:
        return "Сейчас для этой остановки нет данных о ближайших прибытиях."

    grouped_arrivals: dict[tuple[str, str], list[Arrival]] = {}
    for arrival in arrivals:
        route_key = arrival.route_number or f"route:{arrival.route_id}"
        direction = arrival.where_go or "без направления"
        grouped_arrivals.setdefault((route_key, direction), []).append(arrival)

    visible_arrival_groups = list(grouped_arrivals.values())
    if limit is not None:
        visible_arrival_groups = visible_arrival_groups[:limit]

    lines = []
    for arrival_group in visible_arrival_groups:
        first_arrival = arrival_group[0]
        route_number = first_arrival.route_number or "?"
        direction = first_arrival.where_go or "без направления"
        arrival_minutes = "/".join(str(arrival.arrival_minutes) for arrival in arrival_group[:3])
        lines.append(
            f"{route_number} через {arrival_minutes} мин. в ст. {direction}"
        )
    return "\n".join(lines)
