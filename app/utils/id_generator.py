"""ID 生成收口：所有由中台生成的 ID 都从这里出。

    tenant_id / user_id 是调用方传入的；kb_id / document_id / chunk_id /
    retrieval_log_id 由中台生成（UUID v4）。

不要在各 service 里手写 uuid4()。将来要给 ID 加前缀、或换成雪花算法，
只改这个文件一处。
"""

import uuid


def new_id() -> str:
    """生成一个 UUID v4 字符串。"""

    return str(uuid.uuid4())


def new_kb_id() -> str:
    return new_id()


def new_document_id() -> str:
    return new_id()


def new_chunk_id() -> str:
    return new_id()


def new_retrieval_log_id() -> str:
    return new_id()
