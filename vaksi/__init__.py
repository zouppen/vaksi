from typing import Type

import asyncio
from collections import deque
from functools import partial
from mautrix.errors.request import MatrixStandardRequestError
from mautrix.types import MessageType, TextMessageEventContent
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper
from maubot.handlers import command, web
from maubot import Plugin, MessageEvent
from aiohttp.web import Request, Response, json_response

class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("bridges.slack")
        helper.copy("bridge_timeout")

class BotException(Exception):
    pass

class SlackException(Exception):
    pass

class Vaksi(Plugin):
    async def start(self) -> None:
        self.config.load_and_update()
        self.log.debug("Slack-bridge on %s", self.config["bridges.slack"])
        self.log.debug("Webbiappis on %s", self.webapp_url)
        self.queues = {"slack": deque()}
        self.sinks = {"slack": None}

    async def stop(self) -> None:
        pass

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    def try_fire(self, queue) -> None:
        if self.sinks[queue] is not None:
            # Still processing previous
            return
        if not self.queues[queue]:
            return

        act, sink = self.queues[queue].popleft()
        self.sinks[queue] = sink
        asyncio.create_task(act())

    def sequential(self, queue: str, act) -> None:
        response = asyncio.Future()
        self.queues[queue].append((act, response))
        self.try_fire(queue)
        return response

    async def find_matrix_pm(self, mxid: str) -> str:
        all_dms = await self.client.get_account_data('m.direct')
        dm_list = all_dms.get(mxid)
        if not dm_list:
            return None
        else:
            # Of many, pick the last (most likely newest)
            return dm_list[-1]

    async def open_slack_pm(self, slack_id: str):
        appserv = await self.find_matrix_pm(self.config["bridges.slack"])
        if appserv is None:
            raise BotException("No PM open with the Slack bot")

        # Make the appservice to do all the heavy lifting
        content = TextMessageEventContent(MessageType.TEXT)
        content.body = f"!slack start-chat {slack_id}"
        act = partial(self.client.send_message, appserv, content)

        # The chat with the bot is sequential
        try:
            return await asyncio.wait_for(self.sequential("slack", act), self.config["bridge_timeout"])
        except TimeoutError:
            self.log.debug("Bot response timeout while querying %s. Flushing queues", slack_id)
            self.panic_flush("slack")
            raise BotException("Timeout while communicating with the bot")

    def panic_flush(self, bridge: str) -> None:
        # Exception to here is handlded by caller, so just setting it null
        self.sinks[bridge] = None
        while self.queues[bridge]:
            _, sink = self.queues[bridge].popleft()
            sink.set_exception(BotException("Panic flush due to previous timeout"))

    def match_request(self, bridge: str, evt: MessageEvent):
        correct = self.config["bridges"][bridge]
        if evt.sender != correct:
            self.log.debug("Incorrect user %s sent bot-like message, ignoring. Event ID: %s", evt.sender, evt.event_id)
            return None
        sink = self.sinks[bridge]
        if sink is None:
            self.log.debug("Unexpected bot response ignored. Event ID: %s", evt.event_id)
        else:
            # Run the next in queue
            self.sinks[bridge] = None
            self.try_fire(bridge)
        return sink

    @command.passive("^Failed.*: (.*)$", msgtypes=[MessageType.NOTICE])
    async def collect_error(self, evt: MessageEvent, match: tuple[str]) -> None:
        req = self.match_request("slack", evt)
        if req is not None:
            req.set_exception(SlackException(match[1]))

    @command.passive("chat with .*(!.*)\)$", msgtypes=[MessageType.NOTICE])
    async def collect_room_id(self, evt: MessageEvent, match: tuple[str]) -> None:
        req = self.match_request("slack", evt)
        if req is not None:
            req.set_result(match[1])

    @web.get("/directs")
    async def post_data(self, req: Request) -> Response:
        dms = await self.client.get_account_data('m.direct')
        return json_response(dms)

    @web.get("/direct/slack/{id}")
    async def post_data(self, req: Request) -> Response:
        try:
            room_id = await self.open_slack_pm(req.match_info["id"])
            return json_response({"room": room_id})
        except MatrixStandardRequestError as e:
            return json_response({"error": e.message, "source": "matrix"})
        except BotException as e:
            return json_response({"error": str(e), "source": "bot"})
        except SlackException as e:
            return json_response({"error": str(e), "source": "slack"})
