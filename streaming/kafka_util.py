import time

from confluent_kafka import KafkaError, KafkaException
from confluent_kafka.admin import AdminClient, NewTopic


def ensure_topic(bootstrap, topic, retries=15):
    """Create the topic (1 partition keeps one match strictly in order). Safe to call repeatedly."""
    admin = AdminClient({"bootstrap.servers": bootstrap})
    for attempt in range(1, retries + 1):
        try:
            admin.create_topics([NewTopic(topic, num_partitions=1, replication_factor=1)])[topic].result()
            return
        except KafkaException as e:
            if e.args[0].code() == KafkaError.TOPIC_ALREADY_EXISTS:
                return
            if attempt == retries:
                raise
            time.sleep(2)
