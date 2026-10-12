from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib import error, request

from bot_config import get_required_env, load_env_file
from route_planner import ResolvedLocation, StaticRoutePlanner, format_plan
from transport_portal import (
    Station,
    TransportPortalClient,
    TransportPortalError,
    filter_arrivals_for_station,
    format_arrivals,
    sort_station_matches_for_display,
)


class TelegramApiError(RuntimeError):
    pass


DISPLAY_MATCH_LIMIT = 12
NUMBER_BUTTONS_PER_ROW = 4
SEARCH_SESSION_TTL_SECONDS = 6 * 60 * 60
MAX_SEARCH_SESSIONS = 200
ROUTE_SESSION_TTL_SECONDS = 30 * 60
ROUTE_PROGRESS_TEXT = "Строю маршрут, это может занять до полуминуты при первом запросе."
ROUTE_QUERY_RE = re.compile(r"^\s*от\s+(.+?)\s+до\s+(.+?)\s*$", re.IGNORECASE)
BACK_BUTTON_TEXT = "Назад"


@dataclass(slots=True)
class SearchSession:
    created_at: float
    station_ids: list[int]


@dataclass(slots=True)
class RouteDialogSession:
    created_at: float
    source_location: ResolvedLocation | None = None
    pending_field: str | None = None
    pending_station_ids: list[int] | None = None


class TelegramBotApi:
    def __init__(self, token: str, timeout: float = 35.0) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.timeout = timeout

    def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        data = json.dumps(payload or {}).encode("utf-8")
        req = request.Request(
            url=f"{self.base_url}/{method}",
            data=data,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
                "User-Agent": "ekb-transport-bot/1.0",
            },
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            description = f"HTTP Error {exc.code}"
            try:
                raw_error = exc.read().decode("utf-8")
                payload = json.loads(raw_error)
                description = payload.get("description", description)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
            raise TelegramApiError(f"{method}: {description}") from exc
        except error.URLError as exc:
            raise TelegramApiError(f"Ошибка запроса к Telegram API: {exc}") from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TelegramApiError("Telegram API вернул невалидный JSON") from exc

        if not payload.get("ok"):
            description = payload.get("description", "Неизвестная ошибка Telegram API")
            raise TelegramApiError(description)

        return payload.get("result")

    def get_me(self) -> dict[str, Any]:
        result = self.call("getMe")
        if not isinstance(result, dict):
            raise TelegramApiError("Неожиданный ответ getMe")
        return result

    def delete_webhook(self, drop_pending_updates: bool = False) -> None:
        self.call("deleteWebhook", {"drop_pending_updates": drop_pending_updates})

    def set_my_commands(self, commands: list[dict[str, str]]) -> None:
        self.call("setMyCommands", {"commands": commands})

    def set_chat_menu_button_to_commands(self) -> None:
        self.call("setChatMenuButton", {"menu_button": {"type": "commands"}})

    def get_updates(self, offset: int | None = None, timeout: int = 25) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        result = self.call("getUpdates", payload)
        if not isinstance(result, list):
            raise TelegramApiError("Неожиданный ответ getUpdates")
        return result

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = self.call("sendMessage", payload)
        if isinstance(result, dict):
            return result
        return None

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self.call("editMessageText", payload)

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        self.call("answerCallbackQuery", payload)


class EkbTransportTelegramBot:
    def __init__(self, token: str) -> None:
        self.telegram = TelegramBotApi(token)
        self.transport = TransportPortalClient()
        self.route_planner: StaticRoutePlanner | None = None
        self.search_sessions: dict[str, SearchSession] = {}
        self.route_sessions: dict[int, RouteDialogSession] = {}
        self._search_session_counter = 0

    def run(self, drop_pending_updates: bool = False) -> None:
        me = self.telegram.get_me()
        print(f"Бот запущен: @{me.get('username', 'unknown')}")

        self.telegram.delete_webhook(drop_pending_updates=drop_pending_updates)
        self._configure_telegram_ui()

        offset: int | None = None
        while True:
            try:
                updates = self.telegram.get_updates(offset=offset, timeout=25)
                for update in updates:
                    offset = int(update["update_id"]) + 1
                    self.handle_update(update)
            except KeyboardInterrupt:
                print("Остановка бота по Ctrl+C")
                return
            except Exception as exc:
                print(f"Ошибка в polling loop: {exc}", file=sys.stderr)
                time.sleep(3)

    def _configure_telegram_ui(self) -> None:
        try:
            self.telegram.set_my_commands(build_bot_commands())
            self.telegram.set_chat_menu_button_to_commands()
        except TelegramApiError as exc:
            print(f"Не удалось обновить список команд в Telegram: {exc}", file=sys.stderr)

    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self._handle_callback_query(update["callback_query"])
            return
        if "message" in update:
            self._handle_message(update["message"])

    def _handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()
        if not chat_id or not text:
            return

        command = text.split(maxsplit=1)[0]

        if command == "/about" or command.startswith("/about@"):
            self.telegram.send_message(chat_id, self._about_text())
            return

        if command == "/route" or command.startswith("/route@"):
            self._start_route_dialog(chat_id)
            return

        if command.startswith("/"):
            self.telegram.send_message(
                chat_id,
                "Для поиска просто отправь название остановки.\n\n"
                "Для пошагового построения маршрута используй /route.\n\n"
                "Доступны команды: /about и /route.",
            )
            return

        if self._get_route_session(chat_id) is not None:
            self._handle_route_dialog_input(chat_id, text)
            return

        route_query = parse_route_query(text)
        if route_query is not None:
            source_query, destination_query = route_query
            self._handle_route_query(chat_id, source_query, destination_query)
            return

        self._handle_station_query(chat_id, text)

    def _handle_callback_query(self, callback_query: dict[str, Any]) -> None:
        query_id = callback_query.get("id")
        data = (callback_query.get("data") or "").strip()
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")

        if query_id:
            try:
                self.telegram.answer_callback_query(query_id)
            except TelegramApiError as exc:
                print(f"Не удалось подтвердить callback query: {exc}", file=sys.stderr)

        if not chat_id or not message_id:
            return

        if data.startswith("routepick:"):
            self._handle_route_pick_callback(chat_id, message_id, data)
            return

        if data.startswith("back:"):
            self._handle_back_callback(chat_id, message_id, data)
            return

        if data.startswith("pick:"):
            self._handle_pick_callback(chat_id, message_id, data)
            return

        if data.startswith("station:"):
            self._handle_legacy_station_callback(chat_id, message_id, data)

    def _handle_back_callback(self, chat_id: int, message_id: int, data: str) -> None:
        session_id = data.split(":", 1)[1]
        stations = self._get_search_session_stations(session_id)
        if not stations:
            self.telegram.edit_message_text(
                chat_id,
                message_id,
                "Этот список результатов уже устарел. Напиши остановку заново.",
            )
            return

        keyboard = {
            "inline_keyboard": build_numbered_station_keyboard(stations, session_id),
        }
        self.telegram.edit_message_text(
            chat_id,
            message_id,
            build_station_selection_message(stations),
            reply_markup=keyboard,
        )

    def _handle_pick_callback(self, chat_id: int, message_id: int, data: str) -> None:
        try:
            _prefix, session_id, station_id_raw = data.split(":", 2)
            station_id = int(station_id_raw)
        except ValueError:
            self.telegram.send_message(chat_id, "Не удалось разобрать выбранную остановку.")
            return

        try:
            station = self.transport.get_station_by_id(station_id, include_archived=True)
            if station is None:
                self.telegram.send_message(chat_id, "Остановка не найдена.")
                return
            if station.is_archived:
                self.telegram.send_message(chat_id, archived_station_text())
                return

            text = build_station_response(self.transport, station)
            reply_markup = None
            if self._get_search_session(session_id) is not None:
                reply_markup = {
                    "inline_keyboard": build_back_keyboard(session_id),
                }
            self.telegram.edit_message_text(
                chat_id,
                message_id,
                text,
                reply_markup=reply_markup,
            )
        except (TransportPortalError, TelegramApiError) as exc:
            self.telegram.send_message(chat_id, f"Ошибка: {exc}")

    def _handle_legacy_station_callback(self, chat_id: int, message_id: int, data: str) -> None:
        try:
            station_id = int(data.split(":", 1)[1])
        except ValueError:
            self.telegram.send_message(chat_id, "Не удалось разобрать выбранную остановку.")
            return

        try:
            station = self.transport.get_station_by_id(station_id, include_archived=True)
            if station is None:
                self.telegram.send_message(chat_id, "Остановка не найдена.")
                return
            if station.is_archived:
                self.telegram.send_message(chat_id, archived_station_text())
                return

            text = build_station_response(self.transport, station)
            self.telegram.edit_message_text(chat_id, message_id, text)
        except (TransportPortalError, TelegramApiError) as exc:
            self.telegram.send_message(chat_id, f"Ошибка: {exc}")

    def _handle_station_query(self, chat_id: int, query: str) -> None:
        try:
            archived_exact = self.transport.find_exact_station(query, include_archived=True)
            if archived_exact is not None and archived_exact.is_archived:
                self.telegram.send_message(chat_id, archived_station_text())
                return

            station, matches = self.transport.choose_station(query)
            if station is not None:
                text = build_station_response(self.transport, station)
                self.telegram.send_message(chat_id, text)
                return

            if not matches:
                archived_station, archived_matches = self.transport.choose_station(
                    query,
                    include_archived=True,
                )
                if archived_station is not None or archived_matches:
                    self.telegram.send_message(chat_id, archived_station_text())
                    return
                self.telegram.send_message(
                    chat_id,
                    "Не нашёл такую остановку. Попробуй более короткий запрос, например: УрФУ",
                )
                return

            public_matches = self.transport.merge_public_station_matches(matches)
            display_matches = sort_station_matches_for_display(public_matches)[:DISPLAY_MATCH_LIMIT]
            display_stations = [station for station, _score in display_matches]
            session_id = self._create_search_session(display_stations)
            keyboard = {
                "inline_keyboard": build_numbered_station_keyboard(display_stations, session_id),
            }
            self.telegram.send_message(
                chat_id,
                build_station_selection_message(display_stations),
                reply_markup=keyboard,
            )
        except TransportPortalError as exc:
            self.telegram.send_message(chat_id, f"Ошибка: {exc}")

    def _handle_route_query(self, chat_id: int, source_query: str, destination_query: str) -> None:
        progress_message = self.telegram.send_message(chat_id, ROUTE_PROGRESS_TEXT)

        try:
            planner = self._get_route_planner()
            plan = planner.plan(source_query, destination_query)
            response_text = format_plan(plan, planner)
        except TransportPortalError as exc:
            response_text = f"Ошибка: {exc}"

        progress_message_id = None
        if progress_message is not None:
            progress_message_id = progress_message.get("message_id")

        if progress_message_id is None:
            self.telegram.send_message(chat_id, response_text)
            return

        try:
            self.telegram.edit_message_text(chat_id, progress_message_id, response_text)
        except TelegramApiError:
            self.telegram.send_message(chat_id, response_text)

    def _start_route_dialog(self, chat_id: int) -> None:
        self._prune_route_sessions()
        self.route_sessions[chat_id] = RouteDialogSession(created_at=time.time())
        self.telegram.send_message(
            chat_id,
            "Режим построения маршрута включён.\n\n"
            "Отправь точку A.\n"
            "Пока можно использовать остановку или координаты `lat,lng`.",
        )

    def _handle_route_dialog_input(self, chat_id: int, text: str) -> None:
        session = self._get_route_session(chat_id)
        if session is None:
            self.telegram.send_message(chat_id, "Сессия построения маршрута уже устарела. Напиши /route заново.")
            return

        planner = self._get_route_planner()
        field = "source" if session.source_location is None else "destination"
        session.pending_field = None
        session.pending_station_ids = None

        try:
            resolved_location, station_matches = planner.resolve_location_candidates(text)
        except TransportPortalError as exc:
            point_name = "A" if field == "source" else "B"
            self.telegram.send_message(
                chat_id,
                f"Не удалось распознать точку {point_name}.\n\n"
                f"{exc}\n\n"
                f"Отправь точку {point_name} ещё раз.",
            )
            return

        if station_matches:
            public_matches = self.transport.merge_public_station_matches(
                [(station, 1.0) for station in station_matches]
            )
            display_stations = [station for station, _score in public_matches[:DISPLAY_MATCH_LIMIT]]
            session.pending_field = field
            session.pending_station_ids = [station.id for station in display_stations]
            session.created_at = time.time()
            keyboard = {
                "inline_keyboard": build_numbered_route_keyboard(display_stations, field),
            }
            point_name = "A" if field == "source" else "B"
            self.telegram.send_message(
                chat_id,
                f"Точка {point_name}: нашёл несколько вариантов.\n\n"
                f"{build_station_selection_message(display_stations)}",
                reply_markup=keyboard,
            )
            return

        if resolved_location is None:
            point_name = "A" if field == "source" else "B"
            self.telegram.send_message(
                chat_id,
                f"Не удалось распознать точку {point_name}. Отправь её ещё раз.",
            )
            return

        session.created_at = time.time()
        if field == "source":
            session.source_location = resolved_location
            self.telegram.send_message(
                chat_id,
                f"Точка A: {resolved_location.label}\n\n"
                "Теперь отправь точку B.",
            )
            return

        source_location = session.source_location
        self.route_sessions.pop(chat_id, None)
        if source_location is None:
            self.telegram.send_message(chat_id, "Сессия маршрута сбилась. Напиши /route заново.")
            return

        self.telegram.send_message(
            chat_id,
            f"Точка A: {source_location.label}\n"
            f"Точка B: {resolved_location.label}",
        )
        self._handle_resolved_route(chat_id, source_location, resolved_location)

    def _handle_route_pick_callback(self, chat_id: int, message_id: int, data: str) -> None:
        try:
            _prefix, field, station_id_raw = data.split(":", 2)
            station_id = int(station_id_raw)
        except ValueError:
            self.telegram.send_message(chat_id, "Не удалось разобрать выбранную точку маршрута.")
            return

        session = self._get_route_session(chat_id)
        if (
            session is None
            or session.pending_field != field
            or not session.pending_station_ids
            or station_id not in session.pending_station_ids
        ):
            self.telegram.edit_message_text(
                chat_id,
                message_id,
                "Этот список вариантов для маршрута уже устарел. Напиши /route заново.",
            )
            return

        station = self.transport.get_station_by_id(station_id, include_archived=False)
        if station is None:
            self.telegram.send_message(chat_id, "Не удалось найти выбранную остановку.")
            return

        public_station = self.transport.get_public_station(station)
        resolved_location = ResolvedLocation(
            label=build_station_response_header(public_station),
            lat=station.lat,
            lng=station.lng,
            station=station,
        )
        session.pending_field = None
        session.pending_station_ids = None
        session.created_at = time.time()

        try:
            self.telegram.edit_message_text(
                chat_id,
                message_id,
                f"Выбрано: {resolved_location.label}",
            )
        except TelegramApiError:
            pass

        if field == "source":
            session.source_location = resolved_location
            self.telegram.send_message(
                chat_id,
                f"Точка A: {resolved_location.label}\n\n"
                "Теперь отправь точку B.",
            )
            return

        source_location = session.source_location
        self.route_sessions.pop(chat_id, None)
        if source_location is None:
            self.telegram.send_message(chat_id, "Сессия маршрута сбилась. Напиши /route заново.")
            return

        self.telegram.send_message(
            chat_id,
            f"Точка A: {source_location.label}\n"
            f"Точка B: {resolved_location.label}",
        )
        self._handle_resolved_route(chat_id, source_location, resolved_location)

    def _handle_resolved_route(
        self,
        chat_id: int,
        source_location: ResolvedLocation,
        destination_location: ResolvedLocation,
    ) -> None:
        progress_message = self.telegram.send_message(chat_id, ROUTE_PROGRESS_TEXT)

        try:
            planner = self._get_route_planner()
            plan = planner.plan_between_resolved(source_location, destination_location)
            response_text = format_plan(plan, planner)
        except TransportPortalError as exc:
            response_text = f"Ошибка: {exc}"

        progress_message_id = None
        if progress_message is not None:
            progress_message_id = progress_message.get("message_id")

        if progress_message_id is None:
            self.telegram.send_message(chat_id, response_text)
            return

        try:
            self.telegram.edit_message_text(chat_id, progress_message_id, response_text)
        except TelegramApiError:
            self.telegram.send_message(chat_id, response_text)

    def _get_route_planner(self) -> StaticRoutePlanner:
        if self.route_planner is None:
            self.route_planner = StaticRoutePlanner(self.transport)
        return self.route_planner

    @staticmethod
    def _about_text() -> str:
        return (
            "Неофициальный бот на основе данных маршрут.екатеринбург.рф.\n\n"
            "По вопросам багов, проблем и предложений:\n"
            "@hendencee\n"
            "hhj859271@gmail.com"
        )

    def _create_search_session(self, stations: list[Station]) -> str:
        self._prune_search_sessions()
        self._search_session_counter += 1
        session_id = f"{int(time.time()):x}{self._search_session_counter:x}"
        self.search_sessions[session_id] = SearchSession(
            created_at=time.time(),
            station_ids=[station.id for station in stations],
        )
        self._prune_search_sessions()
        return session_id

    def _get_search_session(self, session_id: str) -> SearchSession | None:
        self._prune_search_sessions()
        session = self.search_sessions.get(session_id)
        if session is None:
            return None
        return session

    def _get_route_session(self, chat_id: int) -> RouteDialogSession | None:
        self._prune_route_sessions()
        session = self.route_sessions.get(chat_id)
        if session is None:
            return None
        return session

    def _get_search_session_stations(self, session_id: str) -> list[Station] | None:
        session = self._get_search_session(session_id)
        if session is None:
            return None

        stations: list[Station] = []
        for station_id in session.station_ids:
            station = self.transport.get_station_by_id(station_id, include_archived=True)
            if station is None:
                continue
            stations.append(station)

        return stations or None

    def _prune_search_sessions(self) -> None:
        now = time.time()
        expired_session_ids = [
            session_id
            for session_id, session in self.search_sessions.items()
            if now - session.created_at > SEARCH_SESSION_TTL_SECONDS
        ]
        for session_id in expired_session_ids:
            self.search_sessions.pop(session_id, None)

        if len(self.search_sessions) <= MAX_SEARCH_SESSIONS:
            return

        overflow = len(self.search_sessions) - MAX_SEARCH_SESSIONS
        oldest_session_ids = sorted(
            self.search_sessions,
            key=lambda session_id: self.search_sessions[session_id].created_at,
        )[:overflow]
        for session_id in oldest_session_ids:
            self.search_sessions.pop(session_id, None)

    def _prune_route_sessions(self) -> None:
        now = time.time()
        expired_chat_ids = [
            chat_id
            for chat_id, session in self.route_sessions.items()
            if now - session.created_at > ROUTE_SESSION_TTL_SECONDS
        ]
        for chat_id in expired_chat_ids:
            self.route_sessions.pop(chat_id, None)


def build_station_response(
    transport: TransportPortalClient,
    station: Station,
) -> str:
    public_station = transport.get_public_station(station)
    try:
        arrivals = transport.get_public_station_forecasts(public_station)
        arrivals = filter_arrivals_for_station(public_station, arrivals)
        arrivals_text = format_arrivals(arrivals)
    except TransportPortalError as exc:
        if is_forecast_temporarily_unavailable_error(exc):
            arrivals_text = "Сейчас сервис прогноза временно недоступен. Попробуй ещё раз через минуту."
        else:
            raise
    return f"{build_station_response_header(public_station)}\n\n{arrivals_text}"


def archived_station_text() -> str:
    return (
        "Эта остановка скрыта из публичного каталога, "
        "потому что у неё сейчас нет закреплённых маршрутов."
    )


def build_station_response_header(station: Station) -> str:
    icon_prefix = station.transport_icon or ""
    if station.display_name != station.name:
        return f"{icon_prefix}{station.name} ({station.display_name})"
    return f"{icon_prefix}{station.name}"


def build_station_selection_message(stations: list[Station]) -> str:
    lines: list[str] = []
    for index, station in enumerate(stations, start=1):
        lines.append(f"{index}. {build_station_response_header(station)}")
    return "\n\n".join(lines)


def build_numbered_station_keyboard(stations: list[Station], session_id: str) -> list[list[dict[str, str]]]:
    buttons = [
        {
            "text": str(index),
            "callback_data": f"pick:{session_id}:{station.id}",
        }
        for index, station in enumerate(stations, start=1)
    ]
    return [
        buttons[row_start : row_start + NUMBER_BUTTONS_PER_ROW]
        for row_start in range(0, len(buttons), NUMBER_BUTTONS_PER_ROW)
    ]


def build_numbered_route_keyboard(stations: list[Station], field: str) -> list[list[dict[str, str]]]:
    buttons = [
        {
            "text": str(index),
            "callback_data": f"routepick:{field}:{station.id}",
        }
        for index, station in enumerate(stations, start=1)
    ]
    return [
        buttons[row_start : row_start + NUMBER_BUTTONS_PER_ROW]
        for row_start in range(0, len(buttons), NUMBER_BUTTONS_PER_ROW)
    ]


def build_back_keyboard(session_id: str) -> list[list[dict[str, str]]]:
    return [[{"text": BACK_BUTTON_TEXT, "callback_data": f"back:{session_id}"}]]


def build_bot_commands() -> list[dict[str, str]]:
    return [
        {"command": "route", "description": "Пошагово построить маршрут"},
        {"command": "about", "description": "О боте и контакты"},
    ]


def parse_route_query(text: str) -> tuple[str, str] | None:
    match = ROUTE_QUERY_RE.match(text)
    if match is not None:
        source_query = match.group(1).strip()
        destination_query = match.group(2).strip()
        if source_query and destination_query:
            return source_query, destination_query

    for separator in ("->", "→", "=>"):
        if separator not in text:
            continue
        source_query, destination_query = text.split(separator, 1)
        source_query = source_query.strip()
        destination_query = destination_query.strip()
        if source_query and destination_query:
            return source_query, destination_query

    return None


def is_forecast_temporarily_unavailable_error(exc: TransportPortalError) -> bool:
    message = str(exc).lower()
    return (
        "сервис прогноза временно недоступен" in message
        or "service unavailable" in message
        or "http error 503" in message
        or "http error 502" in message
        or "http error 504" in message
    )


def build_station_choice_label(station: Station) -> str:
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
    parser = argparse.ArgumentParser(description="Telegram-бот для остановок Екатеринбурга.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Только проверить токен через getMe и завершиться.",
    )
    parser.add_argument(
        "--drop-pending",
        action="store_true",
        help="Сбросить накопившиеся апдейты перед стартом polling.",
    )
    return parser


def main() -> int:
    configure_output()
    load_env_file()
    args = build_parser().parse_args()
    token = get_required_env("TELEGRAM_BOT_TOKEN")

    bot = EkbTransportTelegramBot(token)
    if args.check:
        me = bot.telegram.get_me()
        print(f"Токен валиден. Бот: @{me.get('username', 'unknown')}")
        return 0

    bot.run(drop_pending_updates=args.drop_pending)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
