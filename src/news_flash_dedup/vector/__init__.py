"""P19 包：ES/Milvus 双写与孔洞修复单元层（fake_store）+ B5/E2 真向量轨。"""

from . import coordinator
from . import embedding_client  # noqa: F401  （B5/E2 S1 模型网关）
from . import milvus_store  # noqa: F401  （主窗口落位：T3 真 Milvus 接入层）
from . import pipeline  # noqa: F401  （B5/E2 S4 写入真链编排）
from . import qwen_bpe  # noqa: F401  （B5/E2 S2 案 A vendored BPE）
from .coordinator import (
    P19VectorError,
    mark_vector_state,
    reconcile_pending_failures,
    upsert_vectors,
)
from .fake_store import FakeMilvusStore, FakeVectorRow

__all__ = [
    "FakeMilvusStore",
    "FakeVectorRow",
    "P19VectorError",
    "coordinator",
    "embedding_client",
    "mark_vector_state",
    "pipeline",
    "qwen_bpe",
    "reconcile_pending_failures",
    "upsert_vectors",
]