"""
Download new data from an MDS provider and load it to db.
"""
from datetime import datetime
import pytz
import logging
import pdb

import _setpath
import argutil
from mds_provider_client import *
import requests
from pypgrest import Postgrest

from config import secrets
from config import config


def build_client_params(
    cfg,
    keys=[
        "auth_type",
        "delay",
        "headers",
        "url",
        "timeout",
        "token",
        "user",
        "password",
    ],
):
    # package config elems that need to be passed to mds_provider_client
    return {key: cfg[key] for key in keys if key in cfg}


def get_token(url, data):
    res = requests.post(url, data=data)
    res.raise_for_status()
    return res.json()


def most_recent(client, provider_id, key="end_time"):
    """
    Return the most recent trip record for the given provider
    """
    results = client.select(
        {
            "select": f"{key}",
            "provider_id": f"eq.{provider_id}",
            "limit": 1,
            "order": f"{key}.desc",
        }
    )

    if results:
        return results[0].get(key)
    else:
        return "2018-12-01T00:00:00"


def cli_args():
    parser = argutil.get_parser(
        "mds.py",
        "Extract data from MDS endpoint and load to staging database.",
        "provider_name",
        "--start",
        "--end",
        "--replace",
    )

    args = parser.parse_args()

    return args


def get_data(client, end_time, interval, paging):

    data = client.get_trips(
        start_time=end_time - interval, end_time=end_time, paging=paging
    )

    return data


def get_coords(feature):
    if feature["geometry"]["coordinates"]:
        return feature["geometry"]["coordinates"]
    else:
        # some provider data has an empty coordinates element
        return [None, None]


def parse_routes(trips):
    for trip in trips:
        if not trip.get("route"):
            # some provider data is missing a route element
            trip["start_longitude"], trip["start_latitude"] = [0, 0]
            trip["end_longitude"], trip["end_latitude"] = [0, 0]
            continue

        if not trip["route"].get("features"):
            # some provider data has an empty route element
            trip["start_longitude"], trip["start_latitude"] = [0, 0]
            trip["end_longitude"], trip["end_latitude"] = [0, 0]
            continue

        trip["start_longitude"], trip["start_latitude"] = get_coords(
            trip["route"]["features"][0]
        )

        trip["end_longitude"], trip["end_latitude"] = get_coords(
            trip["route"]["features"][-1]
        )

        trip.pop("route")
    return trips


def to_unix(iso_string, tz_string="+00:00"):
    # handle format YYYY-MM-DDTHH:MM:SS+00:00 (timezone ignored)
    iso_string = iso_string.replace(tz_string, "")
    dt = datetime.strptime(iso_string, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=pytz.UTC)
    return int(dt.timestamp())


def to_iso(unix):
    if unix < 4_100_264_520:
        return datetime.utcfromtimestamp(int(unix)).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        # try milliseconds instead
        return datetime.utcfromtimestamp(int(unix / 1000)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )


def floats_to_iso(data, keys):
    for row in data:
        for key in keys:
            row[key] = to_iso(row[key])
    return data


def drop_dupes(trips, key="trip_id"):
    ids = []
    new_trips = []

    for trip in trips:
        if trip[key] not in ids:
            ids.append(trip[key])
            new_trips.append(trip)
        else:
            print(trip[key])

    return new_trips


def post_data(client, data):
    print("Post {} trips...".format(len(data)))

    client.upsert(data)

    return data


def main():

    args = cli_args()

    provider_name = args.provider_name

    cfg = secrets.PROVIDERS[provider_name]

    pgrest = Postgrest(secrets.PG["url"], auth=secrets.PG["token"])

    start = args.start
    end = args.end
    offset = cfg["time_offset_seconds"]

    if not start:
        # use the most recent record as the start date (minus the offset)
        start = most_recent(pgrest, cfg["provider_id"])
        start = to_unix(start)
        start = start - offset

    if not end:
        # use current time as the end date
        end = int(datetime.today().timestamp())

    interval = cfg["interval"]

    if cfg.get("time_format") == "mills":
        # mills to unix
        start, end, interval = int(start * 1000), int(end * 1000), int(interval * 1000)

    auth_type = cfg.get("auth_type")

    if not cfg.get("token") and auth_type.lower() != "httpbasicauth":
        token_res = get_token(cfg["auth_url"], cfg["auth_data"])
        cfg["token"] = token_res[cfg["auth_token_res_key"]]

    client_params = build_client_params(cfg)

    client = ProviderClient(**client_params)

    total = 0

    for i in range(start, end, interval):

        data = get_data(client, i, interval, cfg["paging"])

        print(start)

        if data:

            data = parse_routes(data)

            data = drop_dupes(data)

            data = [
                {
                    field["name"]: row[field["name"]]
                    for field in config.FIELDS
                    if field.get("upload_mds")
                }
                for row in data
            ]

            data = floats_to_iso(
                data,
                [field["name"] for field in config.FIELDS if field.get("datetime")],
            )

            post_data(pgrest, data)

            total += len(data)

        else:
            continue

    return total


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.DEBUG)

    main()
