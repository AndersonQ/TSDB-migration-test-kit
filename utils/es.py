"""
All functions related to the ES client are placed here.
"""

from elasticsearch import Elasticsearch
from elasticsearch.client import IngestClient
from elasticsearch.helpers import scan

import json
import os.path
import sys

from utils.tsdb import *


def get_client(elasticsearch_host, elasticsearch_ca_path, elasticsearch_user, elasticsearch_pwd, cloud_id, cloud_pwd):
    """
    Create ES client.
    If cloud values are provided, they will take priority over the local deployment.
    :param elasticsearch_host: ES host.
    :param elasticsearch_ca_path: Path to ES certificate.
    :param elasticsearch_user: Name of the ES user.
    :param elasticsearch_pwd: Password for ES.
    :param cloud_id: Cloud ID. Default is empty.
    :param cloud_pwd: Password for the elastic cloud. Default is empty.
    :return: ES client.
    """
    if cloud_id != "" and cloud_pwd != "":
        print("Client will connect to the cloud.")
        return Elasticsearch(
            cloud_id=cloud_id,
            basic_auth=("elastic", cloud_pwd)
        )
    kwargs = {
        "hosts": elasticsearch_host,
        "basic_auth": (elasticsearch_user, elasticsearch_pwd),
    }
    if elasticsearch_ca_path:
        kwargs["ca_certs"] = elasticsearch_ca_path
    else:
        # Use default system/certifi CA bundle when no explicit path is provided
        import ssl
        kwargs["ssl_context"] = ssl.create_default_context()
    return Elasticsearch(**kwargs)


def add_doc_from_file(client: Elasticsearch, index_name: str, doc_path: str):
    """
    Given a JSON file, add the document to the index.
    This function does not check if the file exists, since that requirement was already
    checked before it was called.
    :param client: ES client.
    :param index_name: name of the index to place the document.
    :param doc_path: path to the document to add.
    """
    file = open(doc_path)
    content = json.load(file)
    file.close()
    client.index(index=index_name, document=content)


def place_documents(client: Elasticsearch, index_name: str, folder_docs: str):
    """
    Place all documents from folder to an index.
    :param client: ES client.
    :param index_name: name of the index to add the documents.
    :param folder_docs: path to the folder with the documents to add.
    """
    print("Placing documents on the index {name}...".format(name=index_name))
    if not client.indices.exists(index=index_name):
        print("Index {name} does not exist. Program will end.".format(name=index_name))
        sys.exit(1)

    if not os.path.isdir(folder_docs):
        print("Folder {} does not exist. Documents cannot be placed. Program will end.".format(folder_docs))
        sys.exit(1)

    for doc in os.listdir(folder_docs):
        doc_path = os.path.join(folder_docs, doc)
        if os.path.isfile(doc_path):
            add_doc_from_file(client, index_name, doc_path)

    # From Elastic docs: Use the refresh API to explicitly make all operations performed on one or more indices since
    # the last refresh available for search. If the request targets a data stream, it refreshes the stream’s backing
    # indices.
    client.indices.refresh(index=index_name)
    resp = client.search(index=index_name, query={"match_all": {}})
    n_docs = resp['hits']['total']['value']
    print("Successfully placed {} documents on the index {name}.\n".format(n_docs, name=index_name))


def create_index(client: Elasticsearch, index_name: str, mappings: {} = {}, settings: {} = {}):
    """
    Create new ES index. If the index already exists, it will be deleted and a new one created.
    :param client: ES client.
    :param index_name: name of the index.
    :param mappings: mappings to be used for the new index. If not specified, default ones will be used.
    :param settings: settings to be used for the new index. If not specified, default ones will be used.
    """
    if client.indices.exists(index=index_name):
        client.indices.delete(index=index_name)
    client.indices.create(index=index_name, mappings=mappings, settings=settings)
    print("Index {name} successfully created.\n".format(name=index_name))


def create_index_missing_for_docs(client: Elasticsearch, tsdb_index_name: str, overwritten_index_name: str):
    """
    Create an index to place all the documents that were updated at least one time.
    :param client: ES client.
    :param tsdb_index_name: name of the TSDB index that holds the indexed documents.
    :param overwritten_index_name: name of the index to store overwritten documents.
    """
    create_index(client, overwritten_index_name)
    pipelines = IngestClient(client)
    pipeline_name = 'get-missing-docs'
    pipelines.put_pipeline(id=pipeline_name, body={
        'description': "Drop all documents that were not overwritten.",
        "processors": [
            {
                "drop": {
                    "if": "ctx._version == 1"
                }
            }
        ]
    })
    dest = {
        "index": overwritten_index_name,
        "version_type": "external",
        "pipeline": pipeline_name
    }
    client.reindex(source={"index": tsdb_index_name}, dest=dest, refresh=True)


def build_query(dimensions_exist: {}, dimensions_missing: []):
    """
    Build query to retrieve document based on the dimensions.
    :param dimensions_exist: Dictionary containing field and field value of the dimensions that exist in the document.
    :param dimensions_missing: Dictionary of the dimension fields missing in the document.
    :return: query.
    """
    query = {
        "bool": {
            "must": [],
            "must_not": []
        }
    }
    for field in dimensions_exist:
        term = {
            "term": {
                field: dimensions_exist[field]
            }
        }
        query["bool"]["must"].append(term)

    for field in dimensions_missing:
        exists = {
            "exists": {
                "field": field
            }
        }
        query["bool"]["must_not"].append(exists)

    return query


def get_and_place_documents(client: Elasticsearch, data_stream: str, dir_name: str, dimensions_values: {},
                            dimensions_missing: [], n: int, number_of_docs):
    """
    Given the dimensions, place the documents in the directory.
    :param client: ES client.
    :param data_stream: Name of the data stream.
    :param dir_name: Name of the parent directory.
    :param dimensions_values: Values for the dimension fields that exist in the document.
    :param dimensions_missing: List of dimension fields missing in the document.
    :param n: Number of the directory inside the parent directory. Example: 1 would create dir_name/1.
    :param number_of_docs: Number of documents to get with that set of dimensions.
    :param index_name: Index name in which documents are stored.
    """
    query = build_query(dimensions_values, dimensions_missing)

    res = client.search(index=data_stream, query=query, sort={"@timestamp": "asc"}, size=number_of_docs)

    dir_for_docs = os.path.join(dir_name, str(n))
    os.mkdir(dir_for_docs)

    for doc in res["hits"]["hits"]:
        name = doc["_id"] + ".json"
        with open(os.path.join(dir_for_docs, name), 'w') as file:
            json.dump(doc, file, indent=4)


def get_missing_docs_info(client: Elasticsearch, data_stream: str, display_docs: int, dir,
                          get_overlapping_files: bool, copy_docs_per_dimension: int,
                          overwritten_index_name: str):
    """
    Display the dimensions of the first @display_docs documents.
    If @get_overlapping_files is set to True, then @copy_docs_per_dimension documents will be placed in a directory
    (if the directory does not exist!).
    :param client: ES client.
    :param display_docs: number of documents to display.
    :param dir: name of the directory.
    :param get_overlapping_files: true if you want to place fields in the directory, false otherwise.
    :param copy_docs_per_dimension: number of documents to get for a set of dimensions.
    :param overwritten_index_name: name of the index holding overwritten documents.
    """
    if get_overlapping_files:
        if os.path.exists(dir):
            print("WARNING: The directory {} exists. Please delete it. Documents will not be placed.\n".format(dir))
            get_overlapping_files = False
        else:
            os.mkdir(dir)
            n = 1

    body = {'size': display_docs, 'query': {'match_all': {}}}
    res = client.search(index=overwritten_index_name, body=body)
    dimensions = time_series_fields["dimension"]

    print("The timestamp and dimensions of the first {} overwritten documents are:".format(display_docs))
    for doc in res["hits"]["hits"]:
        if get_overlapping_files:
            dimensions_values = {"@timestamp": doc["_source"]["@timestamp"]}
            dimensions_missing = []

        print("- Timestamp {}:".format(doc["_source"]["@timestamp"]))
        for dimension in dimensions:
            el = doc["_source"]
            keys = dimension.split(".")
            for key in keys:
                if key not in el:
                    el = "(Missing value)"
                    break
                el = el[key]
            print("\t{}: {}".format(dimension, el))
            if get_overlapping_files:
                if el != "(Missing value)":
                    dimensions_values[dimension] = el
                else:
                    dimensions_missing.append(dimension)

        if get_overlapping_files:
            get_and_place_documents(client, data_stream, dir, dimensions_values, dimensions_missing, n, copy_docs_per_dimension)
            n += 1


def _process_bulk_batch(client: Elasticsearch, operations: [], batch_sources: []):
    """
    Send a single bulk request and categorize the per-document results.
    :param client: ES client.
    :param operations: list of alternating action / source dicts for client.bulk().
    :param batch_sources: list of original _source dicts (one per document, aligned with operations).
    :return: (created, duplicates, duplicate_docs, error_count, failed_docs) for this batch.
    """
    resp = client.bulk(operations=operations)
    created = 0
    duplicates = 0
    duplicate_docs = []
    error_count = 0
    failed = []

    for i, item in enumerate(resp["items"]):
        action_result = item["create"]
        if "error" in action_result:
            if action_result.get("status") == 409:
                duplicates += 1
                duplicate_docs.append({
                    "status": 409,
                    "document": batch_sources[i]
                })
            else:
                error_count += 1
                failed.append({
                    "error": action_result["error"],
                    "status": action_result.get("status"),
                    "document": batch_sources[i]
                })
        elif action_result.get("result") == "created":
            created += 1
        else:
            error_count += 1
            failed.append({
                "error": {"type": "unexpected_result", "reason": "result was: {}".format(action_result.get("result"))},
                "status": action_result.get("status"),
                "document": batch_sources[i]
            })

    return created, duplicates, duplicate_docs, error_count, failed


def save_failed_docs(failed_docs: [], filepath: str):
    """
    Save documents that failed during bulk indexing to a single NDJSON file for inspection.
    Each line contains a JSON object with the error details and the original document source.
    :param failed_docs: list of dicts with 'error', 'status', and 'document' keys.
    :param filepath: path to the NDJSON file where failed documents will be saved.
    """
    if not failed_docs:
        return

    if os.path.exists(filepath):
        print("WARNING: The file {} already exists. It will be replaced.\n".format(filepath))

    with open(filepath, 'w') as f:
        for failed in failed_docs:
            f.write(json.dumps(failed) + "\n")

    print("Saved {} failed documents to {}\n".format(len(failed_docs), filepath))


def copy_docs_from_to(client: Elasticsearch, source_index: str, dest_index: str, max_docs: int,
                       batch_size: int = 1000):
    """
    Copy documents from one index to the other using scan + bulk API.
    This mimics how Elastic Agent integrations send data via the _bulk API,
    rather than using the server-side reindex API.
    :param client: ES client.
    :param source_index: source index with the documents to be copied to a new index.
    :param dest_index: destination index for the documents.
    :param max_docs: max number of documents to copy. -1 means all.
    :param batch_size: number of documents per bulk request.
    :return: (total, created, duplicates, duplicate_docs, error_count, failed_docs).
    """
    print("Copying documents from {} to {}...".format(source_index, dest_index))
    if not client.indices.exists(index=source_index):
        print("Source index {name} does not exist. Program will end.".format(name=source_index))
        sys.exit(1)

    total = 0
    created = 0
    duplicates = 0
    duplicate_docs = []
    error_count = 0
    failed_docs = []

    operations = []
    batch_sources = []

    for doc in scan(client, index=source_index):
        if max_docs != -1 and total >= max_docs:
            break

        operations.append({"create": {"_index": dest_index}})
        operations.append(doc["_source"])
        batch_sources.append(doc["_source"])
        total += 1

        if len(batch_sources) >= batch_size:
            c, d, dd, e, f = _process_bulk_batch(client, operations, batch_sources)
            created += c
            duplicates += d
            duplicate_docs.extend(dd)
            error_count += e
            failed_docs.extend(f)
            operations = []
            batch_sources = []

    # Send remaining documents
    if operations:
        c, d, dd, e, f = _process_bulk_batch(client, operations, batch_sources)
        created += c
        duplicates += d
        duplicate_docs.extend(dd)
        error_count += e
        failed_docs.extend(f)

    client.indices.refresh(index=dest_index)

    # Print summary
    print("\nBulk indexing summary for {} -> {}:".format(source_index, dest_index))
    print("\tTotal documents sent: {}".format(total))
    print("\tCreated: {}".format(created))
    print("\tDuplicates (409): {}".format(duplicates))
    print("\tFailed: {}".format(error_count))
    print()

    return total, created, duplicates, duplicate_docs, error_count, failed_docs


def get_tsdb_config(client: Elasticsearch, data_stream_name: str, docs_index: int, settings_mappings_index: int):
    """
    Get the index name where documents are placed, and mappings and settings for the new TSDB index.
    :param client: ES client.
    :param data_stream_name:
    :param docs_index: number of the index in the data stream with the documents to be moved to the TSDB index.
    :param settings_mappings_index: number of the index for the settings and mappings for the TSDB index.
    :return: documents index name, settings and mappings for the TSDB index.
    """
    data_stream = client.indices.get_data_stream(name=data_stream_name)
    n_indexes = len(data_stream["data_streams"][0]["indices"])

    # Get the index to use for document retrieval
    if docs_index == -1:
        docs_index = 0
    elif docs_index >= n_indexes:
        print("ERROR: Data stream {} has {} indexes. Documents index number {} is not valid.".format(data_stream_name,
                                                                                                     n_indexes,
                                                                                                     docs_index))
        sys.exit(1)

    # Get index to use for settings/mappings
    if settings_mappings_index == -1:
        settings_mappings_index = n_indexes - 1
    elif settings_mappings_index >= n_indexes:
        print("ERROR: Data stream {} has {} indexes. Settings/mappings index number {} is not valid.".format(
            data_stream_name, n_indexes, settings_mappings_index))
        sys.exit(1)

    docs_index_name = data_stream["data_streams"][0]["indices"][docs_index]["index_name"]
    settings_mappings_index_name = data_stream["data_streams"][0]["indices"][settings_mappings_index]["index_name"]

    print("Index being used for the documents is {}.".format(docs_index_name))
    print("Index being used for the settings and mappings is {}.".format(settings_mappings_index_name))
    print()

    mappings = client.indices.get_mapping(index=settings_mappings_index_name)[settings_mappings_index_name]["mappings"]
    settings = client.indices.get_settings(index=settings_mappings_index_name)[settings_mappings_index_name]["settings"]

    settings = get_tsdb_settings(mappings, settings)

    return docs_index_name, mappings, settings


def copy_from_data_stream(client: Elasticsearch, data_stream_name: str, docs_index: int, settings_mappings_index: int,
                          max_docs: int, tsdb_index_name: str):
    """
    Given a data stream, it copies the documents retrieved from the given index and places them in a new
    index with TSDB enabled, using the bulk API to simulate how Elastic Agent sends data.
    :param client: ES client.
    :param data_stream_name: name of the data stream.
    :param docs_index: number of the index to use to retrieve the documents.
    :param settings_mappings_index: number of the index to use to get the mappings and settings for the TSDB index.
    :param max_docs: maximum documents to be indexed.
    :param tsdb_index_name: name of the destination TSDB index.
    :return: (total, created, duplicates, duplicate_docs, error_count, failed_docs).
    """
    print("Testing data stream {}.".format(data_stream_name))

    if not client.indices.exists(index=data_stream_name):
        print("\tData stream {} does not exist. Program will end.".format(data_stream_name))
        sys.exit(1)

    source_index, mappings, settings = get_tsdb_config(client, data_stream_name, docs_index, settings_mappings_index)

    create_index(client, tsdb_index_name, mappings, settings)

    return copy_docs_from_to(client, source_index, tsdb_index_name, max_docs)
