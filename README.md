# DICOMweb 纯后端（STOW-RS / QIDO-RS / WADO-RS）

基于 FastAPI 0.115 + pydicom 3.0 的影像联调后端：DICOM 原字节存磁盘、索引存 SQLite，
无任何外部服务依赖。

## 运行

```bash
.venv/bin/python make_sample.py                 # 生成 samples/ 下两个 MR 示例实例
.venv/bin/python -m uvicorn app.main:app --port 8111
```

环境变量：`DICOMWEB_DATA_DIR`（默认 `data/`）、`DICOMWEB_MAX_UPLOAD_BYTES`（默认 200MB）、
`DICOMWEB_MAX_INSTANCES`（单次请求最大实例数，默认 200）。

## 接口

- `POST /studies`（STOW-RS）：`multipart/related`，每个 part 为 `application/dicom`。
  仅接受 Explicit VR Little Endian 的 Part 10 实例；校验 Study/Series/SOP Instance UID、
  SOP Class UID 与 File Meta 一致性；截断、非 Part 10、其他传输语法均被拒。
  逐实例独立提交，响应为 DICOM JSON：`00081199`（ReferencedSOPSequence，成功）与
  `00081198`（FailedSOPSequence，含 `00081197` 原因）。全部成功返回 200，否则 409。
  相同 SOP Instance UID + 相同字节为幂等成功；同 UID 不同内容返回冲突，绝不迁移研究/序列。
- `GET /studies`、`/series`、`/instances` 及层级路径（QIDO-RS）：支持
  `StudyInstanceUID` / `SeriesInstanceUID` / `SOPInstanceUID` / `PatientID` / `StudyDate`
  精确匹配与 `limit` / `offset`，结果按 UID 排序，输出 DICOM JSON。
- `GET /studies/{uid}[/series/{uid}[/instances/{uid}]]`（WADO-RS）：
  `multipart/related; type="application/dicom"` 返回原字节；`/metadata` 返回 DICOM JSON，
  Pixel Data 以 `BulkDataURI` 关联。未知或不匹配层级返回 404。

## curl 示例

```bash
B=bnd; { for f in samples/sample-1.dcm samples/sample-2.dcm; do
  printf -- "--%s\r\nContent-Type: application/dicom\r\n\r\n" $B; cat $f; printf "\r\n";
done; printf -- "--%s--\r\n" $B; } > /tmp/stow.bin
curl -X POST localhost:8111/studies \
  -H "Content-Type: multipart/related; type=\"application/dicom\"; boundary=$B" \
  --data-binary @/tmp/stow.bin
curl "localhost:8111/studies?PatientID=PAT-001"
curl "localhost:8111/studies/<study>/series/<series>/instances/<sop>" -o inst.bin
curl "localhost:8111/studies/<study>/series/<series>/instances/<sop>/metadata"
```

## 设计与范围

- `app/dicomutil.py` 解析校验，`app/store.py` 归档+索引（先写临时文件、DB 事务内提交，
  失败无悬空记录；启动时清理 `data/tmp/` 未发布临时文件），`app/stow.py` / `app/qido.py` /
  `app/wado.py` 为 HTTP 层，`app/config.py` 集中限额。
- 同 SOP UID 并发提交由 SQLite 主键 + 进程锁保证唯一。
- 范围：仅 Explicit VR Little Endian；QIDO 为精确匹配（无模糊/范围匹配）；不含
  WADO-URI、frames、渲染与删除接口。

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
```
