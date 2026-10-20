"""uv run python -m campuspay.replay_dlq --max 10; preserve malformed events for inspection."""

import argparse

from campuspay import broker
from campuspay.config import Settings
from campuspay.schemas import PaymentEvent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=10)
    args = parser.parse_args()
    settings = Settings()
    connection = broker.connect(settings.rabbit_url)
    try:
        channel = connection.channel()
        broker.topology(channel)
        for _ in range(max(0, min(args.max, 100))):
            method, _, body = channel.basic_get(broker.DEAD_QUEUE, auto_ack=False)
            if method is None:
                break
            try:
                event = PaymentEvent.model_validate_json(body)
            except ValueError:
                channel.basic_nack(method.delivery_tag, requeue=True)
                print("Malformed event retained in DLQ; inspect it in RabbitMQ management.")
                break
            broker.publish(settings.rabbit_url, str(event.eventId), body.decode())
            channel.basic_ack(method.delivery_tag)
            print(f"Replayed {event.eventId}")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
