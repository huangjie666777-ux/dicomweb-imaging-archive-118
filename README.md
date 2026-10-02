# 纯后端 DICOMweb 影像联调服务

基于 FastAPI 0.115、pydicom 3.0、SQLite 和本地磁盘实现的最小可联调 DICOMweb 服务。所有命令均使用仓库中的 `.venv/bin/python`。

## 实现范围

- **STOW-RS**：`POST /studies`，接收 `multipart/related; type="application/dicom"`。
- **QIDO-RS**：查询研究、序列、实例，返回 `application/dicom+json`。
- **WADO-RS**：按研究、序列或实例层级以 `multipart/related` 取回 DICOM Part 10 原字节。
- **metadata**：提供完整 DICOM JSON 元数据，`PixelData` 以可访问的 `BulkDataURI` 表示。
- **持久化**：原字节按 SHA-256 内容寻址存入磁盘，层级索引和元数据存入 SQLite。
- **恢复**：重启时从 SQLite 恢复，删除未提交的 `tmp/*.part` 临时文件和未索引对象。

仅接受 **Explicit VR Little Endian**（`1.2.840.10008.1.2.1`）DICOM Part 10 文件。服务会检查：

- 128 字节 preamble 后的 `DICM`；
- 文件元信息传输语法；
- `StudyInstanceUID`、`SeriesInstanceUID`、`SOPInstanceUID`、`SOPClassUID`；
- `MediaStorageSOPClassUID == SOPClassUID`；
- `MediaStorageSOPInstanceUID == SOPInstanceUID`；
- 文件结尾截断、基础 VR 解码失败和明显的 `PixelData` 长度不足。

同一请求中的实例逐个解析、逐实例独立事务提交。坏实例进入失败 SOP 引用列表，不阻断其他实例。相同 SOP UID 且原字节 SHA-256 相同为幂等；相同 SOP UID 但字节不同或层级不一致返回冲突，且不会迁移研究/序列。失败实例不会留下索引、临时文件或未引用对象。

## 目录结构

- `app/config.py`：环境变量和限制配置。
- `app/multipart.py`：流式 multipart/related 解析，part 落临时文件。
- `app/dicom.py`：DICOM Part 10 校验、层级身份提取、DICOM JSON 编码。
- `app/storage.py`：内容寻址对象存储、临时文件和重启清理。
- `app/db.py`：SQLite schema、事务和并发设置。
- `app/archive.py`：逐实例归档、幂等/冲突、QIDO/WADO 数据访问。
- `app/http.py`：FastAPI 路由和 HTTP 表示。
- `scripts/generate_samples.py`：生成带 8×8 16-bit `PixelData` 的 CT 示例。
- `tests/`：STOW/QIDO/WADO、异常、幂等、冲突、并发唯一、重启恢复和限制测试。

## 启动

```bash
export DICOMWEB_DATA_DIR=./data
export DICOMWEB_MAX_REQUEST_BYTES=209715200
export DICOMWEB_MAX_INSTANCES=1000
.venv/bin/uvicorn app.http:app --host 127.0.0.1 --port 8000
```

健康检查：

```bash
curl -s http://127.0.0.1:8000/health
```

## 生成并上传示例

```bash
.venv/bin/python scripts/generate_samples.py --out samples

curl -v -X POST http://127.0.0.1:8000/studies \
  -H 'Content-Type: multipart/related; type="application/dicom"; boundary=STOWBOUNDARY' \
  --data-binary @-

# 如果要直接用 curl 的 @文件，先生成一个 multipart 载荷文件：
.venv/bin/python - <<'PY'
from pathlib import Path
parts = []
for path in sorted(Path("samples").glob("*.dcm")):
    parts.append(b"--STOWBOUNDARY\r\nContent-Type: application/dicom\r\n\r\n")
    parts.append(path.read_bytes())
    parts.append(b"\r\n")
parts.append(b"--STOWBOUNDARY--\r\n")
Path("stow.payload").write_bytes(b"".join(parts))
PY

curl -sS -X POST http://127.0.0.1:8000/studies \
  -H 'Content-Type: multipart/related; type="application/dicom"; boundary=STOWBOUNDARY' \
  --data-binary @stow.payload | .venv/bin/python -m json.tool
```

成功引用位于 DICOM 序列 `0008,1199 ReferencedSOPSequence`，失败引用位于 `0008,1198 FailedSOPSequence`，失败项包含 DICOM 失败原因码 `0008,1197` 和短原因。

## QIDO-RS

```bash
curl -s 'http://127.0.0.1:8000/studies?PatientID=PATIENT-001&StudyDate=20261003&limit=100&offset=0'
curl -s 'http://127.0.0.1:8000/studies?StudyInstanceUID=<STUDY_UID>'
curl -s 'http://127.0.0.1:8000/studies/<STUDY_UID>/series'
curl -s 'http://127.0.0.1:8000/studies/<STUDY_UID>/instances'
curl -s 'http://127.0.0.1:8000/studies/<STUDY_UID>/series/<SERIES_UID>/instances'
```

也提供根层级 `GET /series` 与 `GET /instances`。支持的精确匹配参数：

- `StudyInstanceUID`
- `SeriesInstanceUID`
- `SOPInstanceUID`
- `PatientID`
- `StudyDate`
- `limit`（1–1000，默认 100）和 `offset`（默认 0）

结果按对应 UID 字典序稳定排序。路径中的研究/系列 UID 会约束子层级；未知或不匹配层级返回 404/403。

## WADO-RS、metadata 与 BulkData

```bash
curl -sS http://127.0.0.1:8000/studies/<STUDY_UID> -o study.multipart
curl -sS http://127.0.0.1:8000/studies/<STUDY_UID>/series/<SERIES_UID> -o series.multipart
curl -sS http://127.0.0.1:8000/studies/<STUDY_UID>/series/<SERIES_UID>/instances/<SOP_UID> -o instance.multipart

curl -sS http://127.0.0.1:8000/studies/<STUDY_UID>/metadata
curl -sS http://127.0.0.1:8000/studies/<STUDY_UID>/series/<SERIES_UID>/metadata
curl -sS http://127.0.0.1:8000/studies/<STUDY_UID>/series/<SERIES_UID>/instances/<SOP_UID>/metadata
```

当实例包含 `PixelData` 时，metadata 中：

```json
{"7FE00010": {"BulkDataURI": "/studies/<STUDY_UID>/series/<SERIES_UID>/instances/<SOP_UID>/bulkdata/7FE00010"}}
```

用以下 URL 可直接取回像素字节：

```bash
curl -sS http://127.0.0.1:8000/studies/<STUDY_UID>/series/<SERIES_UID>/instances/<SOP_UID>/bulkdata/7FE00010 -o pixeldata.bin
```

## 测试

```bash
.venv/bin/pytest -q
```

测试覆盖：合法双实例 STOW/QIDO；截断、Implicit VR、文件元信息身份不一致；坏实例不阻断好实例；同字节幂等和异内容冲突；完整层级 WADO、metadata 和 BulkData；重启清理；8 并发同 SOP UID 唯一；请求字节限制。

## 明确限制

- 不做用户认证、TLS、压缩传输、Range 请求或跨节点复制。
- 不接受除 Explicit VR Little Endian 外的传输语法，不做转码。
- QIDO 仅实现本联调要求的精确匹配、分页和 UID 排序，不实现模糊匹配、IncludeField、时间范围等完整 PS3.18 能力。
- BulkData 仅暴露 `PixelData (7FE0,0010)`。
- 上传 multipart part 先落临时盘，字节限制按完整 HTTP 请求体统计。
