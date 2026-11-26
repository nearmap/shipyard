---
type: llm
---
The change adds `chunked()` in batch/chunks.py. Its range stops at `len(items) - size + 1`, so a final batch shorter than `size` is never produced, and `upload()` silently drops up to BATCH_SIZE - 1 trailing records.
PASS if the final report identifies this defect: the last partial batch (tail records) is dropped, located in batch/chunks.py.
FAIL if it reports no defect, only reports other issues, or places the defect anywhere else.
