"""
This file has everything related to TSDB automation.
Some of these functions might need to be updated in the future.
For example: discard_unknown_settings discard settings that are not currently accepted
in the ES Python client. If the situation changes, the function will no longer be accurate.
"""

import sys

# This is a dictionary for all the time series fields accepted as of today (29.June.2023).
# routing_path is also part of the dictionary since it is mandatory to have it for a time series index.
time_series_fields = {
    "dimension": [],
    "counter": [],
    "gauge": [],
    "routing_path": []
}

# We need to set the routing path to create a TSDB index.
# As of today (29.June.2023), only keyword fields are accepted.
accepted_fields_for_routing = ["keyword"]


def make_tsdb_index_name(data_stream):
    """Return the TSDB-enabled index name for a given data stream."""
    return "tsdb-" + data_stream


def make_overwritten_index_name(data_stream):
    """Return the overwritten-docs index name for a given data stream."""
    return "tsdb-overwritten-" + data_stream


# Some settings cause an error as they are not known to ElasticSearch Python client.
# This function discards the ones that were causing me error (there might be more!).
def discard_unknown_settings(settings: []):
    settings["index"].pop("provided_name", None)
    settings["index"].pop("uuid", None)
    settings["index"].pop("creation_date", None)
    if "version" in settings["index"]:
        settings["index"]["version"].pop("created", None)
    return settings


def get_time_series_fields(mappings: {}):
    """
    Place all fields in the time time_series_fields dictionary.
    :param mappings: Mappings dictionary.
    """
    for key in time_series_fields:
        time_series_fields[key].clear()

    fields = mappings["properties"]

    # A function to flatten the name of the fields
    def get_all_fields(fields: {}, common: str, result: {}):
        def join_strings(str1: str, str2: str):
            if str1 == "":
                return str2
            return str1 + "." + str2

        for key in fields:
            if "properties" in fields[key]:
                get_all_fields(fields[key]["properties"], join_strings(common, key), result)
            else:
                new_key = join_strings(common, key)
                result[new_key] = fields[key]

    result = {}
    get_all_fields(fields, "", result)

    # Split the time series fields according to metric / dimension
    def cluster_fields_by_type(fields: {}):
        for field in fields:
            if "time_series_dimension" in fields[field] and fields[field]["time_series_dimension"]:
                time_series_fields["dimension"].append(field)
                if fields[field]["type"] in accepted_fields_for_routing:
                    time_series_fields["routing_path"].append(field)
            if "time_series_metric" in fields[field]:
                metric = fields[field]["time_series_metric"]
                time_series_fields[metric].append(field)

    cluster_fields_by_type(result)

    if len(time_series_fields["routing_path"]) == 0:
        print("Routing path is empty. Program will end.")
        sys.exit(1)

    print("The time series fields for the TSDB index are: ")
    for key in time_series_fields:
        if len(time_series_fields[key]) > 0:
            print("\t- {} ({} fields):".format(key, len(time_series_fields[key])))
            for value in time_series_fields[key]:
                print("\t\t- {}".format(value))
    print()


def get_tsdb_settings(mappings: {}, settings: {}):
    """
    Modify the settings, so they fit the TSDB enabled mode.
    Get all time series metrics using the mappings.
    :param mappings: mappings.
    :param settings: settings.
    :return: modified settings for the TSDB index.
    """
    # Some settings cause an error on the ES client. This function removes them.
    discard_unknown_settings(settings)
    # Add the time_series mode
    settings["index"] |= {"mode": "time_series"}

    # Get all time series fields
    get_time_series_fields(mappings)

    # Set a new window to avoid time series end / start time errors
    time_series = {
        "time_series": {
            "end_time": "2100-06-08T14:41:54.000Z",
            "start_time": "1900-06-08T09:54:18.000Z"
        }
    }
    settings["index"] |= time_series
    settings["index"] |= {"routing_path": time_series_fields["routing_path"]}

    return settings
