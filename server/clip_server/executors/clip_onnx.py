import os
from multiprocessing.pool import ThreadPool, Pool
from typing import List, Tuple, Optional

import onnxruntime as ort

from jina import Executor, requests, DocumentArray

from clip_server.model import clip
from clip_server.model.clip_onnx import CLIPOnnxModel

_SIZE = {
    'RN50': 224,
    'RN101': 224,
    'RN50x4': 288,
    'RN50x16': 384,
    'RN50x64': 448,
    'ViT-B/32': 224,
    'ViT-B/16': 224,
    'ViT-L/14': 224,
}


class CLIPEncoder(Executor):
    def __init__(
        self,
        name: str = 'ViT-B/32',
        device: Optional[str] = None,
        num_worker_preprocess: int = 4,
        minibatch_size: int = 64,
        pool_backend: str = 'thread',
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._preprocess_blob = clip._transform_blob(_SIZE[name])
        self._preprocess_tensor = clip._transform_ndarray(_SIZE[name])
        self._model = CLIPOnnxModel(name)
        if pool_backend == 'thread':
            self._pool = ThreadPool(processes=num_worker_preprocess)
        else:
            self._pool = Pool(processes=num_worker_preprocess)
        self._minibatch_size = minibatch_size

        import torch

        if not device:
            self._device = 'cuda' if torch.cuda.is_available() else 'cpu'
        else:
            self._device = device

        # define the priority order for the execution providers
        providers = ['CPUExecutionProvider']

        # prefer CUDA Execution Provider over CPU Execution Provider
        if device == 'cuda':
            providers.insert(0, 'CUDAExecutionProvider')
            providers.insert(0, 'TensorrtExecutionProvider')

        sess_options = ort.SessionOptions()

        # Enables all available optimizations including layout optimizations
        sess_options.graph_optimization_level = (
            ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        )

        # Run the operators in the graph in parallel
        sess_options.execution_mode = ort.ExecutionMode.ORT_PARALLEL

        if self._device != 'cuda' and (not os.environ.get('OMP_NUM_THREADS')):
            num_threads = torch.get_num_threads() // self.runtime_args.replicas
            if num_threads < 2:
                self.logger.warning(
                    f'Too many encoder replicas ({self.runtime_args.replicas})'
                )

            # The number of threads used to parallelize the execution of the graph (across nodes)
            sess_options.inter_op_num_threads = 1
            sess_options.intra_op_num_threads = max(num_threads, 1)

        self._model.start_sessions(sess_options=sess_options, providers=providers)

    def _preproc_image(self, da: 'DocumentArray') -> 'DocumentArray':
        for d in da:
            if d.tensor is not None:
                d.tensor = self._preprocess_tensor(d.tensor)
            else:
                if not d.blob and d.uri:
                    # in case user uses HTTP protocol and send data via curl not using .blob (base64), but in .uri
                    d.load_uri_to_blob()
                d.tensor = self._preprocess_blob(d.blob)
        da.tensors = da.tensors.cpu().numpy()
        return da

    def _preproc_text(self, da: 'DocumentArray') -> Tuple['DocumentArray', List[str]]:
        texts = da.texts
        da.tensors = clip.tokenize(texts).cpu().numpy()
        da[:, 'mime_type'] = 'text'
        return da, texts

    @requests
    async def encode(self, docs: 'DocumentArray', **kwargs):
        _img_da = docs.find(
            {'$or': [{'blob': {'$exists': True}}, {'tensor': {'$exists': True}}]}
        )
        _txt_da = docs.find({'text': {'$exists': True}})

        # for image
        if _img_da:
            for minibatch in _img_da.map_batch(
                self._preproc_image, batch_size=self._minibatch_size, pool=self._pool
            ):
                minibatch.embeddings = self._model.encode_image(minibatch.tensors)

        # for text
        if _txt_da:
            for minibatch, _texts in _txt_da.map_batch(
                self._preproc_text, batch_size=self._minibatch_size, pool=self._pool
            ):
                minibatch.embeddings = self._model.encode_text(minibatch.tensors)
                minibatch.texts = _texts

        # drop tensors
        docs.tensors = None
