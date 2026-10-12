from __future__ import annotations

import argparse
import sys

from transport_portal import (
    TransportPortalClient,
    TransportPortalError,
    filter_arrivals_for_station,
    format_arrivals,
    sort_station_matches_for_display,
)


DISPLAY_MATCH_LIMIT = 12


def format_station_choice(station) -> str:
    if station.display_name != station.name:
        if station.transport_icon:
            return f"{station.transport_icon} {station.name} • {station.display_name}"
        return f"{station.name} • {station.display_name}"
    return station.public_name


def configure_output() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Минимальный CLI для проверки API транспорта Екатеринбурга."
    )
    parser.add_argument("query", help="Название остановки, например: УрФУ")
    parser.add_argument(
        "--station-id",
        type=int,
        help="Явно указать ID остановки, если название неоднозначно.",
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Принудительно заново скачать список остановок.",
    )
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="Показывать и скрытые из публичного каталога остановки.",
    )
    return parser


def main() -> int:
    configure_output()
    parser = build_parser()
    args = parser.parse_args()
    client = TransportPortalClient()

    try:
        if args.refresh_cache:
            client.get_all_stations(force_refresh=True)

        station = None
        matches = []

        if args.station_id is not None:
            matches = client.find_stations(
                args.query,
                include_archived=args.include_archived,
            )
            station = next((item[0] for item in matches if item[0].id == args.station_id), None)
            if station is None:
                station = client.get_station_by_id(
                    args.station_id,
                    include_archived=True,
                )
            if station is None:
                print(f"Остановка с id={args.station_id} не найдена.", file=sys.stderr)
                return 1
        else:
            archived_exact = client.find_exact_station(
                args.query,
                include_archived=True,
            )
            if archived_exact is not None and archived_exact.is_archived and not args.include_archived:
                print(
                    "Эта остановка скрыта из публичного каталога, потому что у неё нет маршрутов.",
                    file=sys.stderr,
                )
                return 3

            station, matches = client.choose_station(
                args.query,
                include_archived=args.include_archived,
            )
            if station is None:
                if not matches:
                    archived_station, archived_matches = client.choose_station(
                        args.query,
                        include_archived=True,
                    )
                    if not args.include_archived and (archived_station is not None or archived_matches):
                        print(
                            "Эта остановка скрыта из публичного каталога, потому что у неё нет маршрутов.",
                            file=sys.stderr,
                        )
                        return 3
                    print("Ничего не найдено. Попробуй более короткий запрос.", file=sys.stderr)
                    return 1

                print("Найдено несколько похожих остановок. Укажи одну из них через --station-id:")
                for candidate, score in sort_station_matches_for_display(matches)[:DISPLAY_MATCH_LIMIT]:
                    print(
                        f"- id={candidate.id} | {format_station_choice(candidate)} | "
                        f"direction={candidate.direction} | score={score:.2f}"
                    )
                return 2

        arrivals = client.get_station_forecasts(station.id)
        arrivals = filter_arrivals_for_station(station, arrivals)

        print(f"Остановка: {station.public_name}")
        print(f"id: {station.id}")
        print(f"direction: {station.direction}")
        if station.display_name != station.name:
            print(f"Портальное название: {station.name}")
        if station.is_archived:
            print("Статус: скрыта из публичного каталога")
        print()
        print(format_arrivals(arrivals))
        return 0
    except TransportPortalError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
