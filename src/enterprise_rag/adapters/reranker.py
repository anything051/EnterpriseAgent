"""Optional CPU CrossEncoder with pinned BGE ONNX artifacts and a bounded deadline."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from functools import cached_property
from hashlib import file_digest
import logging
from pathlib import Path
from threading import BoundedSemaphore

from enterprise_rag.core.ranking import RerankError

logger = logging.getLogger(__name__)
ARTIFACTS = {
    "tokenizer.json": "48564c5c7d3fa64d85d95e65414a542385f88b0f128fd8d4163fd7a57f2be05c",
    "model_int8.onnx": "2059d8ef0b6e935b4845e11b38c9af9e9e2e7b91f69fc99efe03254e0a7da8d3",
}


class OnnxReranker:
    name = "bge-reranker-base-int8@280bcc27"

    def __init__(self, directory: Path, timeout: float):
        self.directory = directory
        self.timeout = timeout
        self._slot = BoundedSemaphore(1)
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rerank")

    def close(self) -> None:
        self._worker.shutdown(wait=False, cancel_futures=True)

    @cached_property
    def _components(self):
        from onnxruntime import InferenceSession, SessionOptions, disable_telemetry_events
        from tokenizers import Tokenizer

        for filename, expected in ARTIFACTS.items():
            with (self.directory / filename).open("rb") as stream:
                if file_digest(stream, "sha256").hexdigest() != expected:
                    raise ValueError(f"Rerank artifact checksum mismatch: {filename}")

        tokenizer = Tokenizer.from_file(str(self.directory / "tokenizer.json"))
        tokenizer.enable_truncation(max_length=512)
        tokenizer.enable_padding(pad_id=tokenizer.token_to_id("<pad>"), pad_token="<pad>")
        options = SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        disable_telemetry_events()
        session = InferenceSession(str(self.directory / "model_int8.onnx"), sess_options=options,
                                   providers=["CPUExecutionProvider"])
        return tokenizer, session

    def _score(self, question: str, passages: list[str], options) -> list[float]:
        import numpy as np

        tokenizer, session = self._components
        batch = tokenizer.encode_batch([(question, passage) for passage in passages])
        tensors = {
            "input_ids": np.asarray([row.ids for row in batch], dtype=np.int64),
            "attention_mask": np.asarray([row.attention_mask for row in batch], dtype=np.int64),
            "token_type_ids": np.asarray([row.type_ids for row in batch], dtype=np.int64),
        }
        feeds = {spec.name: tensors[spec.name] for spec in session.get_inputs()}
        logits, = session.run(None, feeds, run_options=options)
        return logits.reshape(-1).tolist()

    def score(self, question: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        try:
            from onnxruntime import RunOptions
        except ImportError as error:
            raise RerankError("unavailable") from error

        if not self._slot.acquire(blocking=False):
            raise RerankError("timeout")
        options = RunOptions()
        try:
            pending = self._worker.submit(self._score, question, passages, options)
        except RuntimeError as error:
            self._slot.release()
            raise RerankError("unavailable") from error
        pending.add_done_callback(lambda _: self._slot.release())
        try:
            return pending.result(timeout=self.timeout)
        except TimeoutError as error:
            options.terminate = True
            raise RerankError("timeout") from error
        except Exception as error:
            logger.warning("reranker_unavailable model=%s error_type=%s", self.name, type(error).__name__)
            raise RerankError("unavailable") from error
