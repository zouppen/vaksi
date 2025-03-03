from typing import Type

import asyncio
from collections import deque
from mautrix.errors.request import MNotFound, MatrixStandardRequestError
from mautrix.types import Format, MessageType, TextMessageEventContent
from mautrix.util import markdown
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper
from maubot.handlers import command, web
from maubot.matrix import MaubotHTMLParser
from maubot import Plugin, MessageEvent
from aiohttp.web import Request, Response, json_response
import json

BOT_HELLO_STATE = 'fi.hacklab.vaksi.hello'

class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("bridges.slack")
        helper.copy("bridge_timeout")
        helper.copy("link_previews")
        helper.copy("hello.plain")
        helper.copy("hello.html")
        helper.copy("tokens")

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
        self.gc_preventer = {"slack": None}

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
        self.gc_preventer[queue] = asyncio.create_task(act)

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
        # The chat with the bot is sequential, so NO await here!
        act = self.client.send_message(appserv, content)

        try:
            # Await response first and then return both 1st and 2nd event
            async with asyncio.timeout(self.config["bridge_timeout"]):
                return await self.sequential("slack", act)
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

    async def clear_hello(self, room_id: str) -> bool:
        try:
            ans = await self.client.get_account_data(BOT_HELLO_STATE, room_id)
            hello = bool(ans)
        except MNotFound:
            hello = False
        if hello:
            # Remove the flag
            await self.client.set_account_data(BOT_HELLO_STATE, None, room_id)
        return hello

    async def set_hello(self, room_id: str) -> None:
        await self.client.set_account_data(BOT_HELLO_STATE, {"hello": True}, room_id)

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

    def auth(self, req: Request) -> None:
        key = req.headers.get("authorization")
        valids = self.config["tokens"]
        if key is None:
            raise BotException("Authorization header missing")
        if not valids:
            raise BotException("No authentication tokens configured")
        if key not in valids:
            raise BotException("Unauthorized")

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

    @command.passive("")
    async def process_incoming(self, evt: MessageEvent, match) -> None:
        need_hello = await self.clear_hello(evt.room_id)
        if need_hello:
            content = TextMessageEventContent(MessageType.TEXT, format=Format.HTML)
            content.body = self.config["hello.plain"]
            content.formatted_body = self.config["hello.html"]
            if not self.config["link_previews"]:
                content["com.beeper.linkpreviews"] = []
            event_id = await self.client.send_message(evt.room_id, content)
            self.log.debug("Bot note sent to %s, event_id %s", evt.room_id, event_id)

    @web.get("/directs")
    async def web_directs(self, req: Request) -> Response:
        try:
            self.auth(req)
            dms = await self.client.get_account_data('m.direct')
            return json_response(dms)
        except BotException as e:
            return json_response({"error": str(e), "source": "bot"})

    @web.get("/direct/slack/{id}")
    async def web_slack_find_pm(self, req: Request) -> Response:
        try:
            self.auth(req)
            room_id = await self.open_slack_pm(req.match_info["id"])
            return json_response({"room": room_id})
        except MatrixStandardRequestError as e:
            return json_response({"error": e.message, "source": "matrix"})
        except BotException as e:
            return json_response({"error": str(e), "source": "bot"})
        except SlackException as e:
            return json_response({"error": str(e), "source": "slack"})

    @web.post("/direct/slack/{id}")
    async def web_slack_pm(self, req: Request) -> Response:
        try:
            self.auth(req)

            # Preparing message contents
            try:
                data = await req.json()
            except json.JSONDecodeError as e:
                raise BotException("Invalid JSON given")
            plain = data.get('plain')
            md = data.get('md')
            html = data.get('html')

            if md is not None:
                if html is not None:
                    raise BotException("Do not provide both md and html")
                html = markdown.render(md)
            elif html is None and plain is None:
                raise BotException("Invalid combination of inputs")

            if plain is None:
                plain = (await MaubotHTMLParser().parse(html)).text

            if html is None:
                content = TextMessageEventContent(MessageType.TEXT)
                content.body = plain
            else:
                content = TextMessageEventContent(MessageType.TEXT, format=Format.HTML)
                content.formatted_body = html
                content.body = plain

            # Link previews
            if not self.config["link_previews"]:
                content["com.beeper.linkpreviews"] = []

            # Finding the room and posting the message
            room_id = await self.open_slack_pm(req.match_info["id"])
            await self.set_hello(room_id) # Bot replies next time
            event_id = await self.client.send_message(room_id, content)
            return json_response({"room": room_id, "event": event_id})
        except MatrixStandardRequestError as e:
            return json_response({"error": e.message, "source": "matrix"})
        except BotException as e:
            return json_response({"error": str(e), "source": "bot"})
        except SlackException as e:
            return json_response({"error": str(e), "source": "slack"})
