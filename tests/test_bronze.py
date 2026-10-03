import io

import pandas as pd
import pytest

from bianque.pipeline.bronze import KEY_RE, ingest_one
from bianque.pipeline.manifest import find_pending, load_manifest, save_manifest


@pytest.mark.parametrize(
    ("key", "dataset", "partition", "filename"),
    [
        (
            "data/transactions/year=2026/month=06/day=17/part-0.csv",
            "transactions",
            "year=2026/month=06/day=17/",
            "part-0",
        ),
        ("data/customers.csv", None, "", "customers"),
    ],
)
def test_key_re_matches_facts_and_root_tables(key, dataset, partition, filename):
    m = KEY_RE.match(key)
    assert m is not None
    assert (m["dataset"], m["partition"], m["filename"]) == (dataset, partition, filename)


@pytest.mark.parametrize("key", ["data/transactions/", "data/readme.pdf", "other/x.csv"])
def test_key_re_ignores_non_csv_and_other_prefixes(key):
    assert KEY_RE.match(key) is None


def test_find_pending_returns_new_and_modified_only():
    source = pd.DataFrame({"key": ["a", "b", "c"], "etag": ["1", "2", "3"]})
    manifest = pd.DataFrame({"key": ["a", "b"], "etag": ["1", "old"]})
    assert find_pending(source, manifest)["key"].tolist() == ["b", "c"]


def test_save_manifest_upserts_by_key(tmp_path):
    path = tmp_path / "manifest.parquet"
    save_manifest(path, load_manifest(path), pd.DataFrame({"key": ["a", "b"], "etag": ["1", "2"]}))
    save_manifest(path, load_manifest(path), pd.DataFrame({"key": ["b"], "etag": ["3"]}))
    result = load_manifest(path).sort_values("key")
    assert result[["key", "etag"]].values.tolist() == [["a", "1"], ["b", "3"]]


class FakeS3:
    def __init__(self, body: bytes):
        self.body = body

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.body)}


def test_ingest_one_writes_text_parquet_with_lineage(tmp_path):
    item = {
        "key": "data/transactions/year=2026/month=06/day=17/part-0.csv",
        "etag": "abc",
        "dataset": "transactions",
        "partition": "year=2026/month=06/day=17/",
        "filename": "part-0",
    }
    result = ingest_one(FakeS3(b"id,amount\n1,10.50\n2,\n"), "bucket", tmp_path, item)

    df = pd.read_parquet(result["bronze_path"])
    assert result["rows"] == 2
    assert df["amount"].tolist()[0] == "10.50"  # kept as text, not parsed
    assert pd.isna(df["amount"].tolist()[1])  # empty cell -> null
    assert set(df["_source_key"]) == {item["key"]}
    assert set(df["_source_etag"]) == {"abc"}
    assert "_ingested_at" in df.columns
