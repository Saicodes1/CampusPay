import uvicorn

from campuspay.observability import configure_logging

if __name__ == "__main__":
    configure_logging()
    # Host-run defaults to loopback; Compose explicitly opts into the container interface.
    import os

    uvicorn.run(
        "campuspay.api:app",
        host=os.environ.get("CAMPUSPAY_HOST", "127.0.0.1"),
        port=8080,
        log_config=None,
        access_log=False,
        proxy_headers=False,
    )
