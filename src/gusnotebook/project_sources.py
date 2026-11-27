"""References to approved input files, without copying or hashing their contents."""

import codecs
import csv
import io
import os
from pathlib import Path
import stat

from .project_files import ProjectError, parent_fd


def file_revision(info):
    return dict(size=info.st_size, mtime_ns=info.st_mtime_ns, ctime_ns=info.st_ctime_ns,
                device=info.st_dev, inode=info.st_ino)


def inspect_source(path, *, schema=True):
    """Read at most a CSV header or Parquet metadata, regardless of file size."""
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise ProjectError("Approved inputs must be absolute file paths on the workspace host")
    path = Path(os.path.abspath(path))
    with parent_fd(path) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ProjectError("Approved input must be a regular file")
            columns = []
            suffix = path.suffix.lower()
            if schema and suffix == ".csv":
                prefix = stream.read(1024 * 1024)
                text = codecs.getincrementaldecoder("utf-8-sig")().decode(prefix)
                try:
                    header = next(csv.reader(io.StringIO(text), strict=True), [])
                except csv.Error as exc:
                    raise ProjectError("CSV header could not be read within 1 MB") from exc
                # Do not mistake a truncated first line for the entire header.
                if before.st_size > len(prefix) and "\n" not in text and "\r" not in text:
                    raise ProjectError("CSV header exceeds 1 MB")
                columns = [{"name": column, "type": "CSV (inferred during execution)"} for column in header]
            elif schema and suffix == ".parquet":
                import pyarrow.parquet as pq
                columns = [{"name": field.name, "type": str(field.type)} for field in pq.read_schema(stream)]
            if schema and suffix in {".csv", ".parquet"} and (not columns or len(columns) > 1000):
                raise ProjectError("Input must have 1 to 1,000 columns")
            after = os.fstat(stream.fileno())
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if file_revision(before) != file_revision(after) or file_revision(after) != file_revision(current):
                raise ProjectError("Input changed while checking its metadata; try again")
    return dict(path=str(path), storage="reference", size=before.st_size, suffix=suffix,
                schema=columns, file_revision=file_revision(before))


def revision(source):
    if "file_revision" in source:
        return ("reference", source["path"], source["file_revision"])
    return ("legacy_snapshot", source.get("sha256"))


def verify_reference(source):
    if "file_revision" in source:
        if inspect_source(source["path"], schema=False)["file_revision"] != source["file_revision"]:
            raise ProjectError("Approved input changed: " + source["path"] +
                               ". Register the original path again before running this analysis.")


def public_source(source, root):
    result = {key: value for key, value in source.items() if key != "snapshot"}
    if "path" not in result:
        # Older registrations discarded the original path. Keep them usable;
        # never guess a source path or delete data needed by old notebooks.
        result.update(storage="legacy_snapshot", path=str(Path(root) / "data/inputs" /
                      source["id"] / (source["sha256"] + source["suffix"])))
    return result
