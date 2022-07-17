import datetime
import inspect
import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import List, Dict, Optional

import requests
from dataclasses_json import dataclass_json, LetterCase
from kubernetes import config, client
from kubernetes.client import ApiException, V1ConfigMap, V1ObjectMeta
from kubernetes.config import ConfigException
# noinspection PyPackageRequirements
# false positive
from telegram import Bot


def create_logger(name: str, level: int = logging.DEBUG) -> logging.Logger:
    logger = logging.Logger(name)
    ch = logging.StreamHandler(sys.stdout)

    formatting = "[{}] %(asctime)s\t%(levelname)s\t%(module)s.%(funcName)s#%(lineno)d | %(message)s".format(name)
    formatter = logging.Formatter(formatting)
    ch.setFormatter(formatter)

    logger.addHandler(ch)
    logger.setLevel(level)

    return logger


class ApiError(Exception):
    def __init__(self, url: str, code: int, body: str, message: str):
        self.url = url
        self.code = code
        self.body = body
        self.message = message
        super().__init__(self.message)

    def __str__(self):
        return f"[{self.code}] ({self.url}) {self.message}:\n{self.body}"


@dataclass_json(letter_case=LetterCase.KEBAB)
@dataclass
class Movie:
    title: str
    year: int
    added_at: str

    def __str__(self):
        return f"{self.title} ({self.year})"


@dataclass_json
@dataclass
class MovieResponse:
    name: str
    movies: List[Movie]
    error: str


def get_kubernetes_api():
    try:
        config.load_incluster_config()
    except ConfigException:
        config.load_kube_config()

    return client.CoreV1Api()


def get_or_create_configmap(api, namespace: str, configmap_name: str, key_name: str, tries: int = 0):
    try:
        return api.read_namespaced_config_map(configmap_name, namespace)
    except ApiException:
        api.create_namespaced_config_map(namespace, body=V1ConfigMap(
            metadata=V1ObjectMeta(
                name=configmap_name,
            ),
            data={key_name: "0"}
        ))

        if tries < 5:
            return get_or_create_configmap(api, namespace, configmap_name, key_name, tries + 1)
        else:
            raise e


def read_last_timestamp(namespace: str, configmap_name: str, key_name: str) -> Optional[int]:
    api = get_kubernetes_api()
    configmap = get_or_create_configmap(api, namespace, configmap_name, key_name)
    if key_name in configmap.data:
        return int(configmap.data[key_name])

    return None


def update_last_timestamp(namespace: str, configmap_name: str, key_name: str, timestamp: int):
    api = get_kubernetes_api()
    api.patch_namespaced_config_map(configmap_name, namespace, {
        "data": {
            key_name: str(timestamp)
        }
    })


def get_plex_content_since(since: int) -> List[MovieResponse]:
    base_url = os.getenv("API_URL")
    if not base_url:
        raise LookupError("couldn't find `API_URL` in environemnt")

    url = os.path.sep.join([base_url, "movies", str(since)])
    response = requests.get(url)

    if not response.ok:
        raise ApiError(url, response.status_code, str(response.content), "failed to retrieve content from plex servers")

    return [MovieResponse.from_json(json.dumps(js)) for js in response.json()["data"]]


def get_new_movies_from_responses(responses: List[MovieResponse]) -> Dict[str, List[Movie]]:
    movies = dict()
    for server in responses:
        movies[server.name] = server.movies

    return movies


def _split_messages(lines):
    message_length = 4096
    messages = []
    current_length = 0
    current_message = 0
    for line in lines:
        if len(messages) <= current_message:
            messages.append([])

        line_length = len(line)
        if current_length + line_length < message_length:
            current_length += line_length
            messages[current_message].append(line)
        else:
            current_length = 0
            current_message += 1

    return messages


# noinspection PyShadowingNames
def send_update(new_movies: Dict[str, List[Movie]], token: str, chatlist: List[str]):
    logger = create_logger(inspect.currentframe().f_code.co_name)
    if not token:
        logger.error("`BOT_TOKEN` not defined in environment, skip sending telegram message")
        return

    if not chatlist:
        logger.error("chatlist is empty (env var: TELEGRAM_CHATLIST)")

    messages = []
    for server_name, movies in new_movies.items():
        messages.append(server_name)
        for movie in movies:
            messages.append(f"\n    {str(movie)}")

    for user in chatlist:
        for message in _split_messages(messages):
            Bot(token=token).send_message(chat_id=user, text="".join(message))


# noinspection PyShadowingNames
def main(token: str, chatlist: List[str]):
    configmap_name = os.getenv("LAST_SEEN_CONFIGMAP_NAME") or "plex-library-update-notifier-last-seen"
    configmap_key_name = os.getenv("LAST_SEEN_KEY_NAME") or "LAST_SEEN"

    namespace = os.getenv("NAMESPACE")
    if not namespace:
        raise LookupError("`NAMESPACE` has to be set as an environment variable")

    timestamp: int = read_last_timestamp(namespace, configmap_name, configmap_key_name)
    if timestamp is None:
        raise Exception("couldn't read timestamp from configmap")

    responses = get_plex_content_since(timestamp)
    timestamp: float = datetime.datetime.now().timestamp()
    new_movies = get_new_movies_from_responses(responses)

    if new_movies:
        send_update(new_movies, token, chatlist)
        update_last_timestamp(namespace, configmap_name, configmap_key_name, int(timestamp))
    else:
        print("no new titles")


if __name__ == "__main__":
    logger = create_logger("__main__")
    try:
        token = os.getenv("BOT_TOKEN")
        chatlist = os.getenv("CHATLIST")
        error_chat_id = [os.getenv("ERROR_CHAT_ID")] or chatlist

        if not token or not chatlist:
            raise LookupError("both `BOT_TOKEN` and `CHATLIST` must be set")
        chatlist = chatlist.split(",")
        main(token, chatlist)
    except Exception as e:
        # noinspection PyTypeChecker
        # this is a correct type check failure but... it's fine
        send_update({"error": ["plex-library-update-notifier", "failed to complete", str(e)]}, token, error_chat_id)
        logger.exception("caught Exception", exc_info=True)
        sys.exit(1)

    logger.debug("success")
