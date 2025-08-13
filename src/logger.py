import threading
from datetime import datetime
from typing import Literal, Optional, get_args

Level = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LEVELS = list(get_args(Level))
LEVEL_TO_NUMBER = dict(zip(LEVELS, range(0, 50, 10)))  # {"DEBUG":0, "INFO":10, ...}


class Logger:
    _instances: dict[str, tuple["Logger", str]] = {}
    _lock = threading.Lock()

    def __new__(cls, name: str, *args, **kwargs):
        key = f"{args}_{kwargs}"
        with cls._lock:
            inst, old_key = cls._instances.get(name, (None, None))
            if not inst or old_key != key:
                inst = super().__new__(cls)
                cls._instances[name] = (inst, key)
                inst._initialized = False
        return cls._instances[name][0]

    def __init__(
        self,
        name: str,
        level: Optional[Level] = "INFO",
        msg_format: str = "{asctime} {name}:{levelname} {message}",
        datefmt: str = "%Y-%m-%d %H:%M:%S",
    ):
        if getattr(self, "_initialized", False):
            return
        if level is None:
            raise ValueError("Level must be specified")
        self.name = name
        self.level: Level = level
        self.verbose = level == "INFO"
        self.msg_format, self.datefmt = msg_format, datefmt
        self._access_level = LEVEL_TO_NUMBER[level]
        self._initialized = True

    def get_child(self, name: str) -> "Logger":
        return Logger(name, self.level, self.msg_format, self.datefmt)

    def _console_log(self, msg: str, lvl: Level):
        if LEVEL_TO_NUMBER[lvl] >= self._access_level:
            print(
                self.msg_format.format(
                    asctime=datetime.now().strftime(self.datefmt),
                    levelname=lvl,
                    name=self.name,
                    message=msg,
                )
            )

    def log(self, level: Level, msg: str):
        self._console_log(msg, level)

    def debug(self, msg):
        self.log("DEBUG", msg)

    def info(self, msg):
        self.log("INFO", msg)

    def warning(self, msg):
        self.log("WARNING", msg)

    def error(self, msg):
        self.log("ERROR", msg)

    def critical(self, msg):
        self.log("CRITICAL", msg)
