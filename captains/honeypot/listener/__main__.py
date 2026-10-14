import asyncio
import logging
import os
import signal

from captains.honeypot.listener.server import AlertSink, Listener, task_identity


async def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    listener = Listener(
        AlertSink(os.environ["ALERT_QUEUE_URL"]),
        os.environ.get("TOKEN_ID", ""),
        os.environ.get("WORKSPACE_ID", ""),
        task_identity(),
    )
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    await listener.serve(os.environ.get("LISTEN_HOST", "0.0.0.0"), int(os.environ.get("LISTEN_PORT", "5432")), stop)


if __name__ == "__main__":
    asyncio.run(main())
