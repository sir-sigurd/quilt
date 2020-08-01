"""
send documents representing object data to elasticsearch for supported file extensions.
note: we truncate outbound documents to DOC_SIZE_LIMIT characters
(to bound memory pressure and request size to elastic)

a little knowledge on deletes and delete markers:
if bucket versioning is on:
    - `aws s3api delete-object (no --version-id)` or `aws s3 rm`
        - push a new delete marker onto the stack with a version-id
        - generate ObjectRemoved:DeleteMarkerCreated

if bucket versioning was on and is then turned off:
    - `aws s3 rm` or `aws s3api delete-object (no --version-id)`
        - replace event at top of stack
            - if a versioned delete marker, push a new one on top of it
            - if an un-versioned delete marker, replace that marker with new marker
            with version "null" (ObjectCreate will similarly replace the same with an object
            of version "null")
            - if object, destroy object
        - generate ObjectRemoved:DeleteMarkerCreated
            - problem: no way of knowing if DeleteMarkerCreated destroyed bytes
            or just created a DeleteMarker; this is usually given by the return
            value of `delete-object` but the S3 event has no knowledge of the same
    - `aws s3api delete-object --version-id VERSION`
        - destroy corresponding delete marker or object; v may be null in which
        case it will destroy the object with version null (occurs when adding
        new objects to a bucket that aws versioned but is no longer)
        - generate ObjectRemoved:Deleted

if bucket version is off and has always been off:
    - `aws s3 rm` or `aws s3api delete-object`
        - destroy object
        - generate a single ObjectRemoved:Deleted

counterintuitive things:
    - turning off versioning doesn't mean version stack can't get deeper (by at
    least 1) as indicated above in the case where a new marker is pushed onto
    the version stack
"""
import datetime
import json
from typing import Optional
import pathlib
import re
from os.path import split
import traceback
from urllib.parse import unquote, unquote_plus

import boto3
import botocore
import nbformat
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from t4_lambda_shared.preview import (
    ELASTIC_LIMIT_BYTES,
    ELASTIC_LIMIT_LINES,
    extract_parquet,
    get_bytes,
    get_preview_lines,
    trim_to_bytes
)
from t4_lambda_shared.utils import (
    get_available_memory,
    MANIFEST_PREFIX_V1,
    POINTER_PREFIX_V1,
    query_manifest_content,
    separated_env_to_iter
)

from document_queue import (
    DocTypes,
    DocumentQueue,
    CONTENT_INDEX_EXTS,
    EVENT_PREFIX,
    MAX_RETRY
)


# 10 MB, see https://amzn.to/2xJpngN
NB_VERSION = 4  # default notebook version for nbformat
# currently only affects .parquet, TODO: extend to other extensions
SKIP_ROWS_EXTS = separated_env_to_iter('SKIP_ROWS_EXTS')
SELECT_PACKAGE_META = "SELECT * from S3Object o WHERE o.version IS NOT MISSING LIMIT 1"
TEST_EVENT = "s3:TestEvent"
# we need to filter out GetObject and HeadObject calls generated by the present
#  lambda in order to display accurate analytics in the Quilt catalog
#  a custom user agent enables said filtration
USER_AGENT_EXTRA = " quilt3-lambdas-es-indexer"


def now_like_boto3():
    """ensure timezone UTC for consistency with boto3:
    Example of what boto3 returns on head_object:
        'LastModified': datetime.datetime(2019, 11, 6, 3, 1, 16, tzinfo=tzutc()),
    """
    return datetime.datetime.now(tz=datetime.timezone.utc)


def should_retry_exception(exception):
    """don't retry certain 40X errors"""
    if hasattr(exception, 'response'):
        error_code = exception.response.get('Error', {}).get('Code', 218)
        return error_code not in ["402", "403", "404"]
    return False


def infer_extensions(key, ext):
    """guess extensions if possible"""
    # Handle special case of hive partitions
    # see https://www.qubole.com/blog/direct-writes-to-increase-spark-performance/
    if (
            re.fullmatch(r".c\d{3,5}", ext) or re.fullmatch(r".*-c\d{3,5}$", key)
            or key.endswith("_0")
            or ext == ".pq"
    ):
        return ".parquet"

    return ext


@retry(
    stop=stop_after_attempt(MAX_RETRY),
    wait=wait_exponential(multiplier=2, min=4, max=30),
    retry=(retry_if_exception(should_retry_exception))
)
def select_manifest_meta(s3_client, bucket: str, key: str):
    """
    wrapper for retry and returning a string
    """
    try:
        raw = query_manifest_content(
            s3_client,
            bucket=bucket,
            key=key,
            sql_stmt=SELECT_PACKAGE_META
        )
        return raw.read()
    except botocore.exceptions.ClientError as cle:
        print(f"Unable to S3 select manifest: {cle}")

    return None


def index_if_manifest(
        s3_client,
        doc_queue: DocumentQueue,
        event_type: str,
        *,
        bucket: str,
        etag: str,
        ext: str,
        key: str,
        last_modified: str,
        version_id: Optional[str],
        size: int
) -> bool:
    """index manifest files as package documents in ES
        Returns:
            - True if manifest (and passes to doc_queue for indexing)
            - False if not a manifest (no attempt at indexing)
    """
    pointer_prefix, pointer_file = split(key)
    if not pointer_prefix.startswith(POINTER_PREFIX_V1):
        return False
    err_msg = None
    try:
        manifest_timestamp = int(pointer_file)
    except ValueError as err:
        err_msg = f"Unexpected manifest pointer file: s3://{bucket}/{key}: {err}"
    else:
        if not 1451631600 <= manifest_timestamp <= 1767250800:
            err_msg = f"Invalid manifest pointer s3://{bucket}{key}"
    if err_msg:
        print(err_msg)
        return False

    package_hash = get_plain_text(
        bucket,
        key,
        size,
        None,
        etag=etag,
        s3_client=s3_client,
        version_id=version_id,
    ).strip()

    manifest_key = f"{MANIFEST_PREFIX_V1}{package_hash}"
    first = select_manifest_meta(s3_client, bucket, manifest_key)
    if not first:
        return False
    try:
        first_dict = json.loads(first)
        doc_queue.append(
            event_type,
            DocTypes.PACKAGE,
            bucket=bucket,
            etag=etag,
            ext=ext,
            handle=pointer_prefix[len(POINTER_PREFIX_V1):],
            key=manifest_key,
            last_modified=last_modified,
            package_hash=package_hash,
            comment=str(first_dict.get("message", "")),
            metadata=json.dumps(first_dict.get("user_meta", {}))
        )
        return True
    except (json.JSONDecodeError, botocore.exceptions.ClientError) as exc:
        print(
            f"{exc}\n"
            f"\tFailed to select first line of manifest s3://{bucket}/{key}."
            f"\tGot {first}."
        )
        return False


def maybe_get_contents(bucket, key, ext, *, etag, version_id, s3_client, size):
    """get the byte contents of a file if it's a target for deep indexing"""
    if ext.endswith('.gz'):
        compression = 'gz'
        ext = ext[:-len('.gz')]
    else:
        compression = None

    content = ""
    inferred_ext = infer_extensions(key, ext)
    if inferred_ext in CONTENT_INDEX_EXTS:
        if inferred_ext == ".ipynb":
            content = trim_to_bytes(
                # we have no choice but to fetch the entire notebook, because we
                # are going to parse it
                # warning: huge notebooks could spike memory here
                get_notebook_cells(
                    bucket,
                    key,
                    size,
                    compression,
                    etag=etag,
                    s3_client=s3_client,
                    version_id=version_id
                ),
                ELASTIC_LIMIT_BYTES
            )
        elif inferred_ext == ".parquet":
            if size >= get_available_memory():
                print(f"{bucket}/{key} too large to deserialize; skipping contents")
                # at least index the key and other stats, but don't overrun memory
                # and fail indexing altogether
                return ""
            obj = retry_s3(
                "get",
                bucket,
                key,
                size,
                etag=etag,
                s3_client=s3_client,
                version_id=version_id
            )
            body, info = extract_parquet(
                get_bytes(obj["Body"], compression),
                as_html=False,
                skip_rows=(inferred_ext in SKIP_ROWS_EXTS)
            )
            # be smart and just send column names to ES (instead of bloated full schema)
            # if this is not an HTML/catalog preview
            columns = ','.join(list(info['schema']['names']))
            content = trim_to_bytes(f"{columns}\n{body}", ELASTIC_LIMIT_BYTES)
        else:
            content = get_plain_text(
                bucket,
                key,
                size,
                compression,
                etag=etag,
                s3_client=s3_client,
                version_id=version_id
            )

    return content


def extract_text(notebook_str):
    """ Extract code and markdown
    Args:
        * nb - notebook as a string
    Returns:
        * str - select code and markdown source (and outputs)
    Pre:
        * notebook is well-formed per notebook version 4
        * "cell_type" is defined for all cells
        * "source" defined for all "code" and "markdown" cells
    Throws:
        * Anything nbformat.reads() can throw :( which is diverse and poorly
        documented, hence the `except Exception` in handler()
    Notes:
        * Deliberately decided not to index output streams and display strings
        because they were noisy and low value
        * Tested this code against ~6400 Jupyter notebooks in
        s3://alpha-quilt-storage/tree/notebook-search/
        * Might be useful to index "cell_type" : "raw" in the future
    See also:
        * Format reference https://nbformat.readthedocs.io/en/latest/format_description.html
    """
    formatted = nbformat.reads(notebook_str, as_version=NB_VERSION)
    text = []
    for cell in formatted.get("cells", []):
        if "source" in cell and cell.get("cell_type") in ("code", "markdown"):
            text.append(cell["source"])

    return "\n".join(text)


def get_notebook_cells(bucket, key, size, compression, *, etag, s3_client, version_id):
    """extract cells for ipynb notebooks for indexing"""
    text = ""
    try:
        obj = retry_s3(
            "get",
            bucket,
            key,
            size,
            etag=etag,
            s3_client=s3_client,
            version_id=version_id
        )
        data = get_bytes(obj["Body"], compression)
        notebook = data.getvalue().decode("utf-8")
        try:
            text = extract_text(notebook)
        except (json.JSONDecodeError, nbformat.reader.NotJSONError):
            print(f"Invalid JSON in {key}.")
        except (KeyError, AttributeError) as err:
            print(f"Missing key in {key}: {err}")
        # there might be more errors than covered by test_read_notebook
        # better not to fail altogether
        except Exception as exc:  # pylint: disable=broad-except
            print(f"Exception in file {key}: {exc}")
    except UnicodeDecodeError as uni:
        print(f"Unicode decode error in {key}: {uni}")

    return text


def get_plain_text(
        bucket,
        key,
        size,
        compression,
        *,
        etag,
        s3_client,
        version_id
) -> str:
    """get plain text object contents"""
    text = ""
    try:
        obj = retry_s3(
            "get",
            bucket,
            key,
            size,
            etag=etag,
            s3_client=s3_client,
            limit=ELASTIC_LIMIT_BYTES,
            version_id=version_id
        )
        lines = get_preview_lines(
            obj["Body"],
            compression,
            ELASTIC_LIMIT_LINES,
            ELASTIC_LIMIT_BYTES
        )
        text = '\n'.join(lines)
    except UnicodeDecodeError as ex:
        print(f"Unicode decode error in {key}", ex)

    return text


def make_s3_client():
    """make a client with a custom user agent string so that we can
    filter the present lambda's requests to S3 from object analytics"""
    configuration = botocore.config.Config(user_agent_extra=USER_AGENT_EXTRA)
    return boto3.client("s3", config=configuration)


def handler(event, context):
    """enumerate S3 keys in event, extract relevant data, queue events, send to
    elastic via bulk() API
    """
    # message is a proper SQS message, which either contains a single event
    # (from the bucket notification system) or batch-many events as determined
    # by enterprise/**/bulk_loader.py
    # An exception that we'll want to re-raise after the batch sends
    content_exception = None
    for message in event["Records"]:
        body = json.loads(message["body"])
        body_message = json.loads(body["Message"])
        if "Records" not in body_message:
            if body_message.get("Event") == TEST_EVENT:
                # Consume and ignore this event, which is an initial message from
                # SQS; see https://forums.aws.amazon.com/thread.jspa?threadID=84331
                continue
            print("Unexpected message['body']. No 'Records' key.", message)
            raise Exception("Unexpected message['body']. No 'Records' key.")
        batch_processor = DocumentQueue(context)
        events = body_message.get("Records", [])
        s3_client = make_s3_client()
        # event is a single S3 event
        for event_ in events:
            try:
                event_name = event_["eventName"]
                # Process all Create:* and Remove:* events
                if not any(event_name.startswith(n) for n in EVENT_PREFIX.values()):
                    continue
                bucket = unquote(event_["s3"]["bucket"]["name"])
                # In the grand tradition of IE6, S3 events turn spaces into '+'
                key = unquote_plus(event_["s3"]["object"]["key"])
                version_id = event_["s3"]["object"].get("versionId")
                version_id = unquote(version_id) if version_id else None
                # Skip delete markers when versioning is on
                if version_id and event_name == "ObjectRemoved:DeleteMarkerCreated":
                    continue
                # ObjectRemoved:Delete does not include "eTag"
                etag = unquote(event_["s3"]["object"].get("eTag", ""))
                # Get two levels of extensions to handle files like .csv.gz
                path = pathlib.PurePosixPath(key)
                ext1 = path.suffix
                ext2 = path.with_suffix('').suffix
                ext = (ext2 + ext1).lower()

                # Handle delete first and then continue so that
                # head_object and get_object (below) don't fail
                if event_name.startswith(EVENT_PREFIX["Removed"]):
                    batch_processor.append(
                        event_name,
                        DocTypes.OBJECT,
                        bucket=bucket,
                        ext=ext,
                        etag=etag,
                        key=key,
                        last_modified=now_like_boto3(),
                        text="",
                        version_id=version_id
                    )
                    continue

                try:
                    head = retry_s3(
                        "head",
                        bucket,
                        key,
                        s3_client=s3_client,
                        version_id=version_id,
                        etag=etag
                    )
                except botocore.exceptions.ClientError as exception:
                    # "null" version sometimes results in 403s for buckets
                    # that have changed versioning, retry without it
                    if (exception.response.get('Error', {}).get('Code') == "403"
                            and version_id == "null"):
                        head = retry_s3(
                            "head",
                            bucket,
                            key,
                            s3_client=s3_client,
                            version_id=None,
                            etag=etag
                        )
                    else:
                        raise exception

                size = head["ContentLength"]
                last_modified = head["LastModified"]

                index_if_manifest(
                    s3_client,
                    batch_processor,
                    event_name,
                    bucket=bucket,
                    etag=etag,
                    ext=ext,
                    key=key,
                    last_modified=last_modified,
                    size=size,
                    version_id=version_id
                )

                try:
                    text = maybe_get_contents(
                        bucket,
                        key,
                        ext,
                        etag=etag,
                        version_id=version_id,
                        s3_client=s3_client,
                        size=size
                    )
                # we still want an entry for this document in elastic so that, e.g.,
                # the file counts from elastic are correct. re-raise below.
                except Exception as exc:  # pylint: disable=broad-except
                    text = ""
                    content_exception = exc
                    print("Content extraction failed", exc, bucket, key, etag, version_id)

                batch_processor.append(
                    event_name,
                    DocTypes.OBJECT,
                    bucket=bucket,
                    key=key,
                    ext=ext,
                    etag=etag,
                    version_id=version_id,
                    last_modified=last_modified,
                    size=size,
                    text=text
                )

            except botocore.exceptions.ClientError as boto_exc:
                if not should_retry_exception(boto_exc):
                    continue
                print("Fatal exception for record", event_, boto_exc)
                traceback.print_tb(boto_exc.__traceback__)
                raise boto_exc
        # flush the queue
        batch_processor.send_all()
        # note: if there are multiple content exceptions in the batch, this will
        # only raise the most recent one;
        # re-raise so that get_contents() failures end up in the DLQ
        if content_exception:
            raise content_exception


def retry_s3(
        operation,
        bucket,
        key,
        size=None,
        limit=None,
        *,
        etag,
        version_id,
        s3_client
):
    """retry head or get operation to S3 with; stop before we run out of time.
    retry is necessary since, due to eventual consistency, we may not
    always get the required version of the object.
    """
    if operation == "head":
        function_ = s3_client.head_object
    elif operation == "get":
        function_ = s3_client.get_object
    else:
        raise ValueError(f"unexpected operation: {operation}")
    # Keyword arguments to function_
    arguments = {
        "Bucket": bucket,
        "Key": key
    }
    if operation == 'get' and size and limit:
        # can only request range if file is not empty
        arguments['Range'] = f"bytes=0-{min(size, limit)}"
    if version_id:
        arguments['VersionId'] = version_id
    else:
        arguments['IfMatch'] = etag

    @retry(
        # debug
        reraise=True,
        stop=stop_after_attempt(MAX_RETRY),
        wait=wait_exponential(multiplier=2, min=4, max=30),
        retry=(retry_if_exception(should_retry_exception))
    )
    def call():
        """local function so we can set stop_after_delay dynamically"""
        # TODO: remove all this, stop_after_delay is not dynamically loaded anymore
        return function_(**arguments)

    return call()
