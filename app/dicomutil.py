"""DICOM Part 10 parsing, validation and DICOM JSON metadata helpers."""
from __future__ import annotations

import re
from io import BytesIO

from pydicom import dcmread
from pydicom.dataset import Dataset
from pydicom.uid import ExplicitVRLittleEndian, UID

_UID_RE = re.compile(r"^[0-9]+(\.[0-9]+)*$")


class InstanceRejection(Exception):
    """Raised when an instance cannot be accepted; carries a DICOM-style reason."""


def _require_uid(ds: Dataset, keyword: str) -> str:
    value = getattr(ds, keyword, None)
    if value is None or not _UID_RE.match(str(value)):
        raise InstanceRejection(f"missing or invalid {keyword}")
    return str(value)


def parse_instance(data: bytes) -> Dataset:
    """Parse and validate one Part 10 Explicit VR Little Endian instance."""
    if len(data) < 132 or data[128:132] != b"DICM":
        raise InstanceRejection("not a DICOM Part 10 file")
    try:
        ds = dcmread(BytesIO(data))
        # Force full decode so truncated datasets surface here.
        for elem in ds:
            elem.VR
    except InstanceRejection:
        raise
    except Exception as exc:  # EOFError, ValueError, struct.error, ...
        raise InstanceRejection(f"cannot parse dataset (truncated or corrupt): {exc}")

    meta = getattr(ds, "file_meta", None)
    if meta is None or "TransferSyntaxUID" not in meta:
        raise InstanceRejection("missing File Meta Information (not Part 10)")
    if UID(meta.TransferSyntaxUID) != ExplicitVRLittleEndian:
        raise InstanceRejection(
            f"unsupported Transfer Syntax {meta.TransferSyntaxUID}; "
            "only Explicit VR Little Endian is accepted"
        )

    study_uid = _require_uid(ds, "StudyInstanceUID")
    _require_uid(ds, "SeriesInstanceUID")
    sop_uid = _require_uid(ds, "SOPInstanceUID")
    sop_class = getattr(ds, "SOPClassUID", None)
    if sop_class is None:
        raise InstanceRejection("missing SOPClassUID")
    if "MediaStorageSOPClassUID" not in meta or "MediaStorageSOPInstanceUID" not in meta:
        raise InstanceRejection("file meta missing Media Storage UIDs")
    if str(meta.MediaStorageSOPClassUID) != str(sop_class):
        raise InstanceRejection("SOPClassUID does not match file meta MediaStorageSOPClassUID")
    if str(meta.MediaStorageSOPInstanceUID) != sop_uid:
        raise InstanceRejection("SOPInstanceUID does not match file meta MediaStorageSOPInstanceUID")
    if study_uid != str(ds.StudyInstanceUID):
        raise InstanceRejection("inconsistent StudyInstanceUID")
    return ds


def metadata_json(ds: Dataset, bulk_uri: str) -> dict:
    """DICOM JSON of the dataset; Pixel Data and other bulk VRs become BulkDataURI."""
    def handler(elem):
        return bulk_uri

    return ds.to_json_dict(bulk_data_threshold=0, bulk_data_element_handler=handler)


STUDY_KEYS = [
    "StudyDate", "StudyTime", "AccessionNumber", "PatientName", "PatientID",
    "StudyInstanceUID", "StudyID",
]
SERIES_KEYS = [
    "Modality", "SeriesInstanceUID", "SeriesNumber", "SeriesDescription",
    "BodyPartExamined",
]
INSTANCE_KEYS = [
    "SOPClassUID", "SOPInstanceUID", "InstanceNumber", "Rows", "Columns",
    "BitsAllocated", "NumberOfFrames", "ContentDate", "ContentTime",
]
