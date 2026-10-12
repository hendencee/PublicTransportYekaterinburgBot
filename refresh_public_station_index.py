from __future__ import annotations

import argparse
import sys

from transport_portal import (
    DEFAULT_ROUTE_INDEX_WORKERS,
    TransportPortalClient,
    TransportPortalError,
)


def configure_output() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Обновить индекс публичных остановок и скрыть остановки без маршрутов."
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Сначала заново скачать список остановок с портала.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_ROUTE_INDEX_WORKERS,
        help="Сколько параллельных запросов использовать при проверке маршрутов.",
    )
    return parser


def main() -> int:
    configure_output()
    args = build_parser().parse_args()
    client = TransportPortalClient()

    try:
        route_status = client.refresh_route_status_cache(
            force_station_refresh=args.refresh_cache,
            max_workers=args.workers,
        )
    except TransportPortalError as exc:
        print(f"Ошибка обновления индекса: {exc}", file=sys.stderr)
        return 1

    total = len(route_status)
    archived = sum(1 for route_count in route_status.values() if route_count == 0)
    public = total - archived

    print(f"Проверено остановок: {total}")
    print(f"Публичных остановок: {public}")
    print(f"Скрыто остановок без маршрутов: {archived}")
    print(f"Кэш сохранён: {client.route_status_cache_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
