import threading
import typing
from datetime import datetime
from typing import Literal, Optional

Level = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LEVELS: list[Level] = list(typing.get_args(Level))

LEVEl_TO_NUMBER: dict[Level, int] = {
    "DEBUG": 0,
    "INFO": 10,
    "WARNING": 20,
    "ERROR": 30,
    "CRITICAL": 40,
}


class Logger:
    _instances = {}
    _lock = threading.Lock()

    def __new__(cls, name: str, *args, **kwargs):
        with cls._lock:
            current_params_key = f"{args}_{kwargs}"

            instance, params_key = cls._instances.get(name, (None, None))
            if instance is None or (params_key != current_params_key):
                instance = super().__new__(cls)
                cls._instances[name] = (instance, current_params_key)

                instance._initialized = False

        return cls._instances[name][0]

    def __init__(
        self,
        name: str,
        level: Optional[Level] = "INFO",
        msg_format: str = "{asctime} {name}:{levelname} {message}",
        datefmt: str = "%Y-%m-%d %H:%M:%S",
    ):
        if self._initialized:
            return
        if level is None:
            raise RuntimeError("Level should be specified")

        self.name = name
        self.level = level
        self.verbose = level == "INFO"
        self.msg_format = msg_format
        self.datefmt = datefmt

        self._access_level = LEVEl_TO_NUMBER[level]

        self._initialized = True

    def _console_log(self, message: str, level: Level):
        if LEVEl_TO_NUMBER[level] >= self._access_level:
            print(
                self.msg_format.format(
                    asctime=datetime.now().strftime(self.datefmt),
                    levelname=level,
                    name=self.name,
                    message=message,
                )
            )

    def debug(self, message: str):
        self._console_log(message, "DEBUG")

    def info(self, message: str):
        self._console_log(message, "INFO")

    def warning(self, message: str):
        self._console_log(message, "WARNING")

    def error(self, message: str):
        self._console_log(message, "ERROR")

    def critical(self, message: str):
        self._console_log(message, "CRITICAL")
