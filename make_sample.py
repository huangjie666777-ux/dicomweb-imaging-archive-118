"""Generate sample DICOM instances (Explicit VR Little Endian) under samples/."""
from __future__ import annotations

import datetime
from pathlib import Path

import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage, generate_uid

SAMPLES = Path(__file__).parent / "samples"


def build_instance(study_uid, series_uid, sop_uid, patient_id, study_date,
                   series_number, instance_number, pattern: int) -> Dataset:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = MRImageStorage
    meta.MediaStorageSOPInstanceUID = sop_uid
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.ImplementationClassUID = generate_uid("2.25.")

    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = MRImageStorage
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.Modality = "MR"
    ds.PatientID = patient_id
    ds.PatientName = "Demo^Dicomweb"
    ds.StudyDate = study_date
    ds.StudyTime = "120000"
    ds.StudyID = "DEMO-STUDY"
    ds.AccessionNumber = "ACC123"
    ds.SeriesNumber = series_number
    ds.SeriesDescription = "Demo series"
    ds.InstanceNumber = instance_number
    ds.Rows = 16
    ds.Columns = 16
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    pixels = bytes(
        value
        for i in range(16 * 16)
        for value in divmod((i * pattern) % 4096, 256)[::-1]
    )
    ds.PixelData = pixels
    return ds


def main():
    SAMPLES.mkdir(exist_ok=True)
    study_uid = generate_uid()
    series_uid = generate_uid()
    study_date = datetime.date.today().strftime("%Y%m%d")
    paths = []
    for instance_number in (1, 2):
        ds = build_instance(
            study_uid, series_uid, generate_uid(), "PAT-001", study_date,
            series_number=1, instance_number=instance_number,
            pattern=instance_number,
        )
        path = SAMPLES / f"sample-{instance_number}.dcm"
        ds.save_as(path, enforce_file_format=True)
        paths.append(path)
    print("study:", study_uid)
    print("series:", series_uid)
    for path in paths:
        print("wrote", path)


if __name__ == "__main__":
    main()
