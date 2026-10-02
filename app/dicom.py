"""DICOM Part 10 parsing, strict validation and DICOM JSON helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydicom import Dataset, dcmread
from pydicom.errors import InvalidDicomError
from pydicom.tag import BaseTag, Tag
from pydicom.uid import ExplicitVRLittleEndian


DICM_MAGIC = b"DICM"


class InstanceError(Exception):
    """Raised when a candidate instance cannot be accepted."""


@dataclass(frozen=True)
class InstanceIdentity:
    study_uid: str
    series_uid: str
    sop_uid: str
    sop_class_uid: str
    patient_id: str | None
    patient_name: str | None
    study_date: str | None
    study_id: str | None
    modality: str | None
    series_number: int | None
    instance_number: int | None


STUDY_TAGS = [
    Tag(0x00100010),  # PatientName
    Tag(0x00100020),  # PatientID
    Tag(0x00080020),  # StudyDate
    Tag(0x00080030),  # StudyTime
    Tag(0x00200010),  # StudyID
    Tag(0x00080050),  # AccessionNumber
    Tag(0x00081030),  # StudyDescription
    Tag(0x0020000D),  # StudyInstanceUID
]

SERIES_TAGS = [
    Tag(0x00080060),  # Modality
    Tag(0x0008103E),  # SeriesDescription
    Tag(0x00200011),  # SeriesNumber
    Tag(0x0020000E),  # SeriesInstanceUID
    Tag(0x0020000D),  # StudyInstanceUID
]

INSTANCE_TAGS = [
    Tag(0x00080016),  # SOPClassUID
    Tag(0x00080018),  # SOPInstanceUID
    Tag(0x00200013),  # InstanceNumber
    Tag(0x00280010),  # Rows
    Tag(0x00280011),  # Columns
    Tag(0x00280100),  # BitsAllocated
    Tag(0x00280008),  # NumberOfFrames
    Tag(0x0020000D),
    Tag(0x0020000E),
]


def _require_uid(value: Any, label: str) -> str:
    if value is None or str(value).strip() == "":
        raise InstanceError(f"missing {label}")
    uid = str(value).strip()
    if not _valid_uid(uid):
        raise InstanceError(f"invalid {label}: {uid}")
    return uid


def _valid_uid(uid: str) -> bool:
    if not 1 <= len(uid) <= 64:
        return False
    # UID characters are digits and separating dots; no leading/trailing dot.
    if not all(ch.isdigit() or ch == "." for ch in uid):
        return False
    if uid.startswith(".") or uid.endswith(".") or ".." in uid:
        return False
    return all(part != "" and not part.startswith("0") or part == "0" for part in uid.split("."))


def _optional_str(ds: Dataset, tag: BaseTag) -> str | None:
    if tag not in ds or ds[tag].is_empty:
        return None
    return str(ds[tag].value)


def _optional_int(ds: Dataset, tag: BaseTag) -> int | None:
    if tag not in ds or ds[tag].is_empty:
        return None
    value = ds[tag].value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _force_value_decoding(ds: Dataset) -> None:
    # iterall walks sequence members; accessing .value decodes deferred data and
    # can expose truncation or VR decoding errors.
    for element in ds.iterall():
        _ = element.value
    for element in ds.file_meta:
        _ = element.value


def _check_pixel_data_length(ds: Dataset) -> None:
    if Tag(0x7FE00010) not in ds:
        return
    required = [Tag(0x00280010), Tag(0x00280011), Tag(0x00280100), Tag(0x00280002)]
    if any(tag not in ds for tag in required):
        return
    try:
        rows = int(ds.Rows)
        columns = int(ds.Columns)
        bits = int(ds.BitsAllocated)
        samples = int(ds.SamplesPerPixel)
        frames = int(getattr(ds, "NumberOfFrames", 1) or 1)
    except (TypeError, ValueError):
        return
    expected = rows * columns * (bits // 8) * samples * frames
    pixel_data = bytes(ds.PixelData)
    if len(pixel_data) < expected:
        raise InstanceError(
            f"truncated PixelData: have {len(pixel_data)} bytes, need {expected}"
        )


def load_instance(path: Path) -> tuple[Dataset, InstanceIdentity]:
    try:
        with path.open("rb") as handle:
            preamble = handle.read(132)
        if len(preamble) < 132 or preamble[128:132] != DICM_MAGIC:
            raise InstanceError("not a DICOM Part 10 file (missing DICM preamble)")
        ds = dcmread(str(path), force=False)
    except (InvalidDicomError, OSError, EOFError, ValueError, TypeError) as exc:
        raise InstanceError(f"unreadable or truncated DICOM Part 10 instance: {exc}") from exc

    if not hasattr(ds, "file_meta") or ds.file_meta is None:
        raise InstanceError("missing DICOM file meta information")
    transfer_syntax = getattr(ds.file_meta, "TransferSyntaxUID", None)
    if str(transfer_syntax) != str(ExplicitVRLittleEndian):
        raise InstanceError(
            f"unsupported TransferSyntaxUID {transfer_syntax}; only "
            f"{ExplicitVRLittleEndian} (Explicit VR Little Endian) is accepted"
        )

    try:
        _force_value_decoding(ds)
        _check_pixel_data_length(ds)
    except (InvalidDicomError, OSError, EOFError, ValueError, TypeError) as exc:
        raise InstanceError(f"truncated or malformed instance: {exc}") from exc

    meta_class = _require_uid(
        getattr(ds.file_meta, "MediaStorageSOPClassUID", None),
        "MediaStorageSOPClassUID",
    )
    meta_instance = _require_uid(
        getattr(ds.file_meta, "MediaStorageSOPInstanceUID", None),
        "MediaStorageSOPInstanceUID",
    )
    sop_class = _require_uid(getattr(ds, "SOPClassUID", None), "SOPClassUID")
    sop_uid = _require_uid(getattr(ds, "SOPInstanceUID", None), "SOPInstanceUID")
    study_uid = _require_uid(getattr(ds, "StudyInstanceUID", None), "StudyInstanceUID")
    series_uid = _require_uid(getattr(ds, "SeriesInstanceUID", None), "SeriesInstanceUID")

    if meta_class != sop_class:
        raise InstanceError(
            "SOP class mismatch between file meta and dataset: "
            f"{meta_class} != {sop_class}"
        )
    if meta_instance != sop_uid:
        raise InstanceError(
            "SOP instance mismatch between file meta and dataset: "
            f"{meta_instance} != {sop_uid}"
        )

    identity = InstanceIdentity(
        study_uid=study_uid,
        series_uid=series_uid,
        sop_uid=sop_uid,
        sop_class_uid=sop_class,
        patient_id=_optional_str(ds, Tag(0x00100020)),
        patient_name=_optional_str(ds, Tag(0x00100010)),
        study_date=_optional_str(ds, Tag(0x00080020)),
        study_id=_optional_str(ds, Tag(0x00200010)),
        modality=_optional_str(ds, Tag(0x00080060)),
        series_number=_optional_int(ds, Tag(0x00200011)),
        instance_number=_optional_int(ds, Tag(0x00200013)),
    )
    return ds, identity


def _dataset_subset(ds: Dataset, tags: list[BaseTag]) -> Dataset:
    subset = Dataset()
    for tag in tags:
        if tag in ds and not ds[tag].is_empty:
            subset[tag] = ds[tag]
    return subset


def _json_with_tag(ds: Dataset, tag: BaseTag, value_json: dict[str, Any]) -> dict[str, Any]:
    result = ds.to_json_dict(bulk_data_threshold=1024 * 1024)
    result[f"{tag.group:04X}{tag.element:04X}"] = value_json
    return result


def study_json(ds: Dataset) -> dict[str, Any]:
    return _dataset_subset(ds, STUDY_TAGS).to_json_dict()


def series_json(ds: Dataset) -> dict[str, Any]:
    return _dataset_subset(ds, SERIES_TAGS).to_json_dict()


def instance_json(ds: Dataset) -> dict[str, Any]:
    return _dataset_subset(ds, INSTANCE_TAGS).to_json_dict()


def full_metadata_json(ds: Dataset, bulk_data_uri: str) -> dict[str, Any]:
    result = Dataset(ds)
    has_pixel_data = Tag(0x7FE00010) in result
    if has_pixel_data:
        del result[Tag(0x7FE00010)]
    encoded = result.to_json_dict(bulk_data_threshold=1024 * 1024)
    if has_pixel_data:
        encoded["7FE00010"] = {"BulkDataURI": bulk_data_uri}
    return encoded


def uid_ref_json(sop_class_uid: str, sop_uid: str, reason: int | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "00081150": {"vr": "UI", "Value": [sop_class_uid]},
        "00081155": {"vr": "UI", "Value": [sop_uid]},
    }
    if reason is not None:
        item["00081197"] = {"vr": "US", "Value": [reason]}
    return item
