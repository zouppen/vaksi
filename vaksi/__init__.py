from typing import Type

import asyncio
from mautrix.errors.request import MatrixStandardRequestError
from mautrix.types import MessageType, TextMessageEventContent
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper
from maubot.handlers import command, web
from maubot import Plugin, MessageEvent
from aiohttp.web import Request, Response, json_response

class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("bridges.slack")

class BotException(Exception):
    pass

class SlackException(Exception):
    pass

class Vaksi(Plugin):
    async def start(self) -> None:
        self.config.load_and_update()
        self.log.debug("Slack-bridge on %s", self.config["bridges.slack"])
        self.log.debug("Webbiappis on %s", self.webapp_url)
        self.slack_query = None

    async def stop(self) -> None:
        pass

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    async def find_matrix_pm(self, mxid: str) -> str:
        all_dms = await self.client.get_account_data('m.direct')
        dm_list = all_dms.get(mxid)
        if dm_list is None or len(dm_list) < 1:
            return None
        else:
            # Of many, pick the last (most likely newest)
            return dm_list[-1]

    async def open_slack_pm(self, slack_id: str) -> str:
        appserv = await self.find_matrix_pm(self.config["bridges.slack"])
        if appserv is None:
            raise BotException("No PM open with the Slack bot")

        # Make the appservice to do all the heavy lifting
        content = TextMessageEventContent(MessageType.TEXT)
        content.body = f"!slack start-chat {slack_id}"
        event_id = await self.client.send_message(appserv, content)
        return event_id

    def validate_bot(self, bridge: str, mxid: str) -> bool:
        correct = self.config["bridges"][bridge]
        if mxid != correct:
            self.log.debug("Incorrect user %s sent bot-like message, ignoring", mxid)
            return False
        else:
            return True

    @command.passive("^Failed.*: (.*)$", msgtypes=[MessageType.NOTICE])
    async def collect_error(self, evt: MessageEvent, match: tuple[str]) -> None:
        if not self.validate_bot("slack", evt.sender):
            return

        self.slack_query.set_exception(SlackException(match[1]))
        self.slack_query = None

    @command.passive("chat with .*(!.*)\)$", msgtypes=[MessageType.NOTICE])
    async def collect_room_id(self, evt: MessageEvent, match: tuple[str]) -> None:
        if not self.validate_bot("slack", evt.sender):
            return

        self.slack_query.set_result(match[1])
        self.slack_query = None

    @web.get("/directs")
    async def post_data(self, req: Request) -> Response:
        dms = await self.client.get_account_data('m.direct')
        return json_response(dms)

    @web.get("/direct/slack/{id}")
    async def post_data(self, req: Request) -> Response:
        try:
            self.slack_query = asyncio.Future()
            await self.open_slack_pm(req.match_info["id"])
            room_id = await self.slack_query
            return json_response({"room": room_id})
        except MatrixStandardRequestError as e:
            return json_response({"error": e.message, "source": "matrix"})
        except BotException as e:
            return json_response({"error": str(e), "source": "bot"})
        except SlackException as e:
            return json_response({"error": str(e), "source": "slack"})
