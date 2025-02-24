from typing import Type

from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

from maubot import Plugin


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("example_1")
        helper.copy("example_2.list")
        helper.copy("example_2.value")


class Vaksi(Plugin):
    async def start(self) -> None:
        self.config.load_and_update()
        self.log.debug("Loaded %s from config example 2", self.config["example_2.value"])

    async def stop(self) -> None:
        pass

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config
