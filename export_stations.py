from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from transport_portal import TransportPortalClient


def configure_output() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Экспорт всех остановок в файлы.")
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Принудительно заново скачать список остановок.",
    )
    return parser


def main() -> int:
    configure_output()
    args = build_parser().parse_args()
    client = TransportPortalClient()
    stations = sorted(
        client.get_all_stations(force_refresh=args.refresh_cache),
        key=lambda station: station.id,
    )
    json_path = Path("all_stations.json")

    json_payload = [
        {
            "id": station.id,
            "name": station.name,
            "direction": station.direction,
            "lat": station.lat,
            "lng": station.lng,
            "osm_url": (
                f"https://www.openstreetmap.org/?mlat={station.lat}&mlon={station.lng}"
                f"#map=18/{station.lat}/{station.lng}"
            ),
        }
        for station in stations
    ]
    json_path.write_text(
        json.dumps(json_payload, ensure_ascii=False, indent=2),
        encoding="utf-8-sig",
    )

    print(f"Сохранено {len(stations)} остановок в отсортированный JSON:")
    print(json_path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
