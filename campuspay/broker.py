import pika

EXCHANGE = "campuspay.events"
QUEUE = "campuspay.notifications"
DEAD_EXCHANGE = "campuspay.dead"
DEAD_QUEUE = "campuspay.notifications.dead"


def connect(url: str):
    params = pika.URLParameters(url)
    params.heartbeat = 30
    params.blocked_connection_timeout = 3
    params.socket_timeout = 3
    params.stack_timeout = 5
    params.connection_attempts = 1
    return pika.BlockingConnection(params)


def topology(channel):
    channel.exchange_declare(EXCHANGE, exchange_type="fanout", durable=True)
    channel.exchange_declare(DEAD_EXCHANGE, exchange_type="fanout", durable=True)
    channel.queue_declare(DEAD_QUEUE, durable=True)
    channel.queue_bind(DEAD_QUEUE, DEAD_EXCHANGE)
    channel.queue_declare(QUEUE, durable=True, arguments={"x-dead-letter-exchange": DEAD_EXCHANGE})
    channel.queue_bind(QUEUE, EXCHANGE)


def publish(url: str, event_id: str, payload: str):
    connection = connect(url)
    try:
        channel = connection.channel()
        topology(channel)
        channel.confirm_delivery()
        # Blocking publisher confirmations + mandatory routing: failures leave outbox pending.
        channel.basic_publish(
            EXCHANGE,
            "",
            payload.encode(),
            mandatory=True,
            properties=pika.BasicProperties(
                content_type="application/json", delivery_mode=2, message_id=str(event_id)
            ),
        )
    finally:
        connection.close()
