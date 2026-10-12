from __future__ import annotations

import argparse
import sys

from transport_portal import (
    TransportPortalClient,
    TransportPortalError,
    filter_arrivals_for_station,
    format_arrivals,
)


def configure_output() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Показать остановку по id.")
    parser.add_argument("station_id", type=int, help="ID остановки")
    return parser


def main() -> int:
    configure_output()
    args = build_parser().parse_args()
    client = TransportPortalClient()

    station = client.get_station_by_id(args.station_id, include_archived=True)
    if station is None:
        print(f"Остановка с id={args.station_id} не найдена.", file=sys.stderr)
        return 1

    print(f"id: {station.id}")
    print(f"name: {station.public_name}")
    if station.display_name != station.name:
        print(f"portal_name: {station.name}")
    print(f"direction: {station.direction}")
    print(f"archived: {'yes' if station.is_archived else 'no'}")
    print(f"lat,lng: {station.lat}, {station.lng}")
    print(
        "osm: "
        f"https://www.openstreetmap.org/?mlat={station.lat}&mlon={station.lng}"
        f"#map=18/{station.lat}/{station.lng}"
    )

    try:
        arrivals = client.get_station_forecasts(station.id)
        arrivals = filter_arrivals_for_station(station, arrivals)
    except TransportPortalError as exc:
        print(f"\nНе удалось получить прибытия: {exc}")
        return 0

    print("\nБлижайшие прибытия:")
    print(format_arrivals(arrivals))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
