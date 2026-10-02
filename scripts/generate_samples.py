"""Generate tiny valid Explicit VR Little Endian DICOM Part 10 samples."""

from __future__ import annotations

import argparse
from pathlib import Path

from pydicom.dataset import Dataset, FileDataset
from pydicom.uid import (
    CTImageStorage,
    ExplicitVRLittleEndian,
    generate_uid,
)


def make_instance(
    out_path: Path,
    study_uid: str,
    series_uid: str,
    instance_uid: str,
    patient_id: str = "PATIENT-001",
    study_date: str = "20261003",
    rows: int = 8,
    columns: int = 8,
) -> None:
    file_meta = Dataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = instance_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = generate_uid()

    ds = FileDataset(str(out_path), Dataset(), file_meta=file_meta, preamble=b"\0" * 128)

    ds.PatientName = "Test^Patient"
    ds.PatientID = patient_id
    ds.StudyDate = study_date
    ds.StudyTime = "120000"
    ds.StudyID = "STUDY-1"
    ds.AccessionNumber = "ACC-1"
    ds.StudyDescription = "DICOMweb integration study"
    ds.StudyInstanceUID = study_uid
    ds.Modality = "CT"
    ds.SeriesDescription = "Synthetic axial slices"
    ds.SeriesNumber = 1
    ds.SeriesInstanceUID = series_uid
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = instance_uid
    ds.InstanceNumber = 1

    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows = rows
    ds.Columns = columns
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    pixels = b"".join((index % 65536).to_bytes(2, "little") for index in range(rows * columns))
    ds.PixelData = pixels
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ds.save_as(str(out_path), enforce_file_format=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="samples")
    args = parser.parse_args()
    out_dir = Path(args.out)
    study_uid = generate_uid()
    series_uid = generate_uid()
    for index in range(2):
        make_instance(
            out_dir / f"instance_{index + 1}.dcm",
            study_uid=study_uid,
            series_uid=series_uid,
            instance_uid=generate_uid(),
        )
    (out_dir / "uids.txt").write_text(
        f"STUDY={study_uid}\nSERIES={series_uid}\n", encoding="utf-8"
    )
    print(f"wrote 2 samples under {out_dir} for study {study_uid}")


if __name__ == "__main__":
    main()
