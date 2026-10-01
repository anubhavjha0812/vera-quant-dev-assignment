import logging

from vera_quant.logging import configure_logging


def test_configure_logging_sets_json_handler() -> None:
    configure_logging()
    root = logging.getLogger()
    assert len(root.handlers) ==1
    root.handlers[0].format(logging.LogRecord("x", logging.INFO, "", 0, "hi", (), None))